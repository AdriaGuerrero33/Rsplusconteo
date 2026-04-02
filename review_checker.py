"""
Verificador de reseñas Google Maps — sin Playwright, solo httpx.

Playwright no puede conectarse desde Railway (timeout de red del contenedor).
httpx sí funciona: la respuesta HTML inicial de Google Maps contiene señales
suficientes para clasificar cada reseña.

Clasificación:
  ACTIVA    — se encontró texto real de reseña en el HTML
  ELIMINADA — se encontró frase de eliminación en el HTML
  INCIERTA  — no hay señal suficiente en la respuesta
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
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Cookies para saltar la página de consentimiento GDPR de Google
# (Railway está en la UE, consent.google.com intercepta todas las peticiones sin cookie)
CONSENT_COOKIES = {
    "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MjktMF9SQzEaAmRlIAEaBgiA_LynBg",
    "CONSENT": "YES+cb.20210328-17-p0.en+FX+111",
    "NID": "511=placeholder",
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
    Sigue redirects manualmente. Si aparece consent.google.com, extrae el
    parámetro 'continue' y salta directamente a la URL real con cookies forzadas.
    httpx elimina las cookies en redirects cross-domain (RFC 7231), así que
    tenemos que inyectarlas en cada petición manualmente.
    """
    from urllib.parse import urlparse, parse_qs, unquote, urljoin

    cookie_header = (
        "SOCS=CAISHAgBEhJnd3NfMjAyMzA4MjktMF9SQzEaAmRlIAEaBgiA_LynBg; "
        "CONSENT=YES+cb.20210328-17-p0.en+FX+111"
    )

    current = url
    for _ in range(12):
        r = await client.get(
            current,
            follow_redirects=False,
            headers={**HEADERS, "Cookie": cookie_header},
        )
        if r.status_code not in (301, 302, 303, 307, 308):
            return r.text, str(r.url)

        location = r.headers.get("location", "")
        if not location:
            return r.text, str(r.url)

        # Resolver URL relativa
        if location.startswith("/"):
            location = urljoin(current, location)

        # Saltar consent.google.com extrayendo el destino real
        if "consent.google.com" in location:
            qs = parse_qs(urlparse(location).query)
            cont = qs.get("continue", [None])[0]
            if cont:
                location = unquote(cont)
                print(f"[consent] saltando → {location[:80]}", flush=True)

        current = location

    # Último intento
    r = await client.get(
        current,
        follow_redirects=False,
        headers={**HEADERS, "Cookie": cookie_header},
    )
    return r.text, str(r.url)


# ─── Verificación de una URL ──────────────────────────────────────────────────

async def _check_single_review(client: httpx.AsyncClient, url: str) -> dict:
    """
    Clasifica una URL de reseña mediante análisis del HTML inicial.
    No usa navegador — solo httpx.
    """
    final_url = url
    try:
        html, final_url = await _fetch_bypassing_consent(client, url)
        html_lower = html.lower()
        print(f"[check] {url[:60]} | {len(html)} chars | final={final_url[:80]}", flush=True)

        # ── A. Señal de ELIMINACIÓN ───────────────────────────────────────────
        for phrase, label in DELETED_PHRASES:
            if not label:
                continue  # ignorar entradas vacías
            if phrase in html_lower:
                print(f"[check] ELIMINADA — frase: {label!r}", flush=True)
                return {
                    "status": "ELIMINADA",
                    "detail": label,
                    "evidence": [f"HTML contiene: '{phrase}'"],
                    "review_text": "",
                    "final_url": final_url,
                }

        # ── B. Extracción de texto de reseña ──────────────────────────────────
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

        # ── C. Señal de estrellas en el HTML ──────────────────────────────────
        star_m = STAR_RE.search(html_lower)
        if star_m:
            # Estrella en el contexto de puntuación es señal activa
            return {
                "status": "ACTIVA",
                "detail": f"Puntuación en HTML: '{star_m.group()}'",
                "evidence": [f"Estrellas en HTML: '{star_m.group()}'"],
                "review_text": "",
                "final_url": final_url,
            }

        # ── D. Sin señal ──────────────────────────────────────────────────────
        return {
            "status": "INCIERTA",
            "detail": "Sin señales en el HTML inicial",
            "evidence": [f"URL: {final_url[:100]}", f"HTML: {len(html)} chars"],
            "review_text": "",
            "final_url": final_url,
        }

    except httpx.TimeoutException:
        return {"status": "INCIERTA", "detail": "Timeout HTTP (>15s)",
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

    # Un único cliente httpx compartido para todas las peticiones
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=15,
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
