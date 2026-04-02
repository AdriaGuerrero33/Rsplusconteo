"""
Verificador de reseñas Google Maps — sin Playwright, solo httpx.

Estrategia de detección (en orden):
  1. Fetch directo con bypass del consent GDPR (POST real al formulario)
  2. Si falla, Jina.ai Reader (renderiza JS, evita consent desde sus servidores)
  3. Clasificación: ACTIVA | ELIMINADA | INCIERTA
"""

import asyncio
import json as _json
import re
from typing import AsyncGenerator

import httpx

# ─── Constantes ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


# Frases que aparecen cuando una reseña fue eliminada
DELETED_PHRASES: list[tuple[str, str]] = [
    ("no longer available",        "EN: 'no longer available'"),
    ("review is no longer",        "EN: 'review is no longer'"),
    ("ya no está disponible",      "ES: 'ya no está disponible'"),
    ("esta reseña ya no",          "ES: 'esta reseña ya no'"),
    ("reseña no disponible",       "ES: 'reseña no disponible'"),
    ("cette avis n'est plus",      "FR: n'est plus disponible"),
    ("diese rezension ist nicht",  "DE: rezension nicht"),
    ("\\u003e\\u003c",             ""),   # ignorar falsos positivos de markup
]

# Patrón de puntuación de estrellas en el texto de la página
STAR_RE = re.compile(r"\b\d[,.]?\d?\s*(estrellas?|stars?)\b", re.IGNORECASE)


# ─── Extracción de texto de reseña desde HTML ─────────────────────────────────

def _ld_review_body(obj) -> str:
    """Busca reviewBody/description en JSON-LD recursivamente."""
    if isinstance(obj, dict):
        for k in ("reviewBody", "description", "text"):
            v = obj.get(k, "")
            if isinstance(v, str) and len(v) > 15:
                return v
        for v in obj.values():
            r = _ld_review_body(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _ld_review_body(item)
            if r:
                return r
    return ""


def _extract_review_text(html: str) -> str:
    """
    Intenta extraer el texto de la reseña del HTML inicial.
    Orden de intento:
      1. JSON-LD (<script type="application/ld+json">)
      2. Meta description / og:description
      3. Strings largos embebidos en datos JS de Google
    """
    # 1. JSON-LD
    for ld_raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            text = _ld_review_body(_json.loads(ld_raw.strip()))
            if text:
                return text[:350]
        except Exception:
            pass

    # 2. Meta description
    for pattern in [
        r'<meta\s+name=["\']description["\'][^>]+content=["\']([^"\']{30,})["\']',
        r'<meta\s+content=["\']([^"\']{30,})["\'][^>]+name=["\']description["\']',
        r'<meta\s+property=["\']og:description["\'][^>]+content=["\']([^"\']{30,})["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            desc = m.group(1).strip()
            # Excluir descripciones genéricas de Maps o del lugar (no de reseña)
            if any(p in desc.lower() for p in [
                "google maps", "cómo llegar", "how to get", "street view",
                "reviews for", "opiniones de", "find local businesses",
                "maps.google", "ver el mapa", "google maps"
            ]):
                continue
            # Debe tener ratio de letras alto (texto real, no coordenadas/JS)
            letters = sum(1 for c in desc if c.isalpha())
            if letters / max(len(desc), 1) < 0.60:
                continue
            return desc[:350]

    # 3. Datos embebidos en JS de Google Maps
    # Google codifica los datos de la página en estructuras JSON dentro de scripts.
    # Buscamos strings que parezcan texto de reseña: ≥8 palabras, ≤400 chars,
    # alto ratio de letras (texto humano, no código/SVG/coordenadas).
    seen = set()
    for raw in re.findall(r'"([^"\\]{50,400})"', html):
        text = raw.replace("\\n", " ").replace("\\t", " ").strip()
        if text in seen:
            continue
        seen.add(text)
        # Filtrar URLs, HTML, código, rutas de archivo
        if text.startswith(("http", "/", "{", "M", "m")):
            continue
        if any(c in text for c in ("<", ">", "\\", "=", ";", "{", "}", "%", "@")):
            continue
        # Ratio de letras > 65%: descarta SVG paths, coordenadas, hashes, base64
        letters = sum(1 for c in text if c.isalpha())
        if letters / max(len(text), 1) < 0.65:
            continue
        words = text.split()
        # ≥8 palabras reales, ninguna excesivamente larga (no código)
        if len(words) >= 8 and all(len(w) <= 20 for w in words):
            return text[:350]

    return ""


# ─── Bypass GDPR consent ─────────────────────────────────────────────────────

async def _fetch_bypassing_consent(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str]:
    """
    Sigue todos los redirects. Si acaba en consent.google.com, acepta el
    formulario mediante POST para obtener una cookie SOCS real de Google.
    httpx lleva esa cookie automáticamente en los redirects posteriores.
    """
    from urllib.parse import urljoin

    # Paso 1: seguir todos los redirects (puede aterrizar en consent.google.com)
    r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)

    # Paso 2: si acabamos en consent, aceptar el formulario vía POST
    if "consent.google.com" in str(r.url):
        print(f"[consent] Detectada página de consentimiento: {str(r.url)[:80]}", flush=True)
        html_consent = r.text

        action_m = re.search(
            r'<form[^>]+action=["\']([^"\']+)["\']', html_consent, re.IGNORECASE
        )
        if action_m:
            action = urljoin(str(r.url), action_m.group(1))

            # Extraer todos los campos ocultos del formulario
            form_data = {}
            for m in re.finditer(
                r'<input[^>]+name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                html_consent, re.IGNORECASE,
            ):
                form_data[m.group(1)] = m.group(2)
            for m in re.finditer(
                r'<input[^>]+value=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\']',
                html_consent, re.IGNORECASE,
            ):
                if m.group(2) not in form_data:
                    form_data[m.group(2)] = m.group(1)

            # set_eom=false → "Aceptar todo" (no solo esenciales)
            form_data["set_eom"] = "false"

            print(
                f"[consent] POST {action} | campos: {list(form_data.keys())}",
                flush=True,
            )

            # POST — httpx guarda la cookie SOCS en su jar y la usa en los redirects
            r = await client.post(
                action,
                data=form_data,
                headers={**HEADERS, "Referer": str(r.url)},
                follow_redirects=True,
                timeout=15,
            )
            print(
                f"[consent] POST resultado: {r.status_code} | {str(r.url)[:80]}",
                flush=True,
            )
        else:
            print("[consent] No se encontró el formulario en la página de consentimiento", flush=True)

    return r.text, str(r.url)


# ─── Jina.ai Reader (fallback) ───────────────────────────────────────────────

async def _fetch_via_jina(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """
    Usa Jina AI Reader (r.jina.ai) como proxy.
    Renderiza JavaScript desde sus propios servidores (fuera EU), devuelve
    texto limpio en markdown. Evita el consent GDPR de Google completamente.
    """
    jina_url = f"https://r.jina.ai/{url}"
    print(f"[jina] Fetching {jina_url[:80]}", flush=True)
    r = await client.get(
        jina_url,
        headers={
            "Accept": "text/plain",
            "User-Agent": "Mozilla/5.0 (compatible)",
            "X-No-Cache": "true",
        },
        follow_redirects=True,
        timeout=30,
    )
    text = r.text
    print(f"[jina] {r.status_code} | {len(text)} chars", flush=True)
    return text, str(r.url)


def _classify_from_text(text: str, source: str = "") -> dict | None:
    """
    Clasifica una reseña a partir de texto plano (p.ej. respuesta de Jina).
    Retorna dict de resultado o None si no hay señal.
    """
    text_lower = text.lower()

    # Señales de eliminación
    for phrase, label in DELETED_PHRASES:
        if not label:
            continue
        if phrase in text_lower:
            return {
                "status": "ELIMINADA",
                "detail": f"{label} [{source}]",
                "evidence": [f"Texto contiene: '{phrase}'"],
                "review_text": "",
            }

    # Señal de estrellas
    star_m = STAR_RE.search(text_lower)

    # Extraer texto de reseña (para texto plano, relajamos el filtro)
    review_text = ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Saltar líneas de navegación / metadata típicas de Jina
        if any(skip in line.lower() for skip in [
            "google maps", "sign in", "directions", "open in", "share",
            "saved", "nearby", "photos", "reviews", "overview", "menu",
            "r.jina.ai", "jina", "http", "©", "terms", "privacy",
        ]):
            continue
        words = line.split()
        letters = sum(1 for c in line if c.isalpha())
        if (len(words) >= 5
                and letters / max(len(line), 1) > 0.55
                and all(len(w) <= 25 for w in words)):
            review_text = line[:350]
            break

    if review_text:
        return {
            "status": "ACTIVA",
            "detail": f"Texto de reseña encontrado [{source}]",
            "evidence": [f"Texto: \"{review_text[:100]}\""],
            "review_text": review_text,
        }

    if star_m:
        return {
            "status": "ACTIVA",
            "detail": f"Puntuación encontrada [{source}]: '{star_m.group()}'",
            "evidence": [f"Estrellas: '{star_m.group()}'"],
            "review_text": "",
        }

    return None


# ─── Verificación de una URL ──────────────────────────────────────────────────

async def _check_single_review(client: httpx.AsyncClient, url: str) -> dict:
    """
    Clasifica una URL de reseña.
    Intento 1: fetch directo con bypass consent GDPR (POST al formulario).
    Intento 2: Jina.ai Reader si el primero da INCIERTA.
    """
    final_url = url
    try:
        # ── Intento 1: fetch directo con bypass consent ───────────────────────
        html, final_url = await _fetch_bypassing_consent(client, url)
        html_lower = html.lower()
        print(f"[check] {url[:60]} | {len(html)} chars | final={final_url[:80]}", flush=True)

        still_on_consent = "consent.google.com" in final_url

        if not still_on_consent:
            for phrase, label in DELETED_PHRASES:
                if not label:
                    continue
                if phrase in html_lower:
                    print(f"[check] ELIMINADA — frase: {label!r}", flush=True)
                    return {
                        "status": "ELIMINADA",
                        "detail": label,
                        "evidence": [f"HTML contiene: '{phrase}'"],
                        "review_text": "",
                        "final_url": final_url,
                    }

            review_text = _extract_review_text(html)
            print(f"[check] review_text='{review_text[:80]}'", flush=True)
            if review_text:
                return {
                    "status": "ACTIVA",
                    "detail": "Texto de reseña encontrado en HTML",
                    "evidence": [f"Texto: \"{review_text[:100]}\""],
                    "review_text": review_text,
                    "final_url": final_url,
                }

            star_m = STAR_RE.search(html_lower)
            if star_m:
                return {
                    "status": "ACTIVA",
                    "detail": f"Puntuación en HTML: '{star_m.group()}'",
                    "evidence": [f"Estrellas en HTML: '{star_m.group()}'"],
                    "review_text": "",
                    "final_url": final_url,
                }

        # ── Intento 2: Jina.ai Reader ─────────────────────────────────────────
        print(f"[check] Sin señal directa {'(consent)' if still_on_consent else ''} → probando Jina.ai", flush=True)
        try:
            jina_text, jina_url = await _fetch_via_jina(client, url)
            result = _classify_from_text(jina_text, source="Jina")
            if result:
                result["final_url"] = jina_url
                return result
            print(f"[jina] Sin señal en respuesta Jina ({len(jina_text)} chars)", flush=True)
        except Exception as je:
            print(f"[jina] Error: {je}", flush=True)

        # ── Sin señal en ningún intento ───────────────────────────────────────
        return {
            "status": "INCIERTA",
            "detail": "Sin señales tras fetch directo y Jina.ai",
            "evidence": [f"URL: {final_url[:100]}", f"HTML: {len(html)} chars"],
            "review_text": "",
            "final_url": final_url,
        }

    except httpx.TimeoutException:
        return {"status": "INCIERTA", "detail": "Timeout HTTP (>20s)",
                "evidence": ["TimeoutException"], "review_text": "", "final_url": final_url}
    except Exception as exc:
        return {"status": "INCIERTA", "detail": f"Error: {str(exc)[:120]}",
                "evidence": [repr(exc)[:200]], "review_text": "", "final_url": final_url}


# ─── Stream de verificación ───────────────────────────────────────────────────

async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 5,
    delay_seconds: float = 0.5,
) -> AsyncGenerator[dict, None]:
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    # Un único cliente httpx compartido — el cookie jar acumula la SOCS de Google
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20,
    ) as client:

        async def check_one(idx: int, item: dict):
            async with semaphore:
                if idx > 0:
                    await asyncio.sleep(delay_seconds)
                result = await _check_single_review(client, item["url"])
                result = {**item, **result, "index": idx}
            await result_queue.put(result)

        tasks = [asyncio.create_task(check_one(i, item)) for i, item in enumerate(url_items)]

        received = 0
        while received < total:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=60.0)
                yield result
                received += 1
            except asyncio.TimeoutError:
                break

        await asyncio.gather(*tasks, return_exceptions=True)


# ─── Compatibilidad CLI ───────────────────────────────────────────────────────

async def check_reviews(url_items, max_concurrent=5, delay_seconds=0.5, progress_callback=None):
    results = {}
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), len(url_items), result)
    return [results.get(i) for i in range(len(url_items))]
