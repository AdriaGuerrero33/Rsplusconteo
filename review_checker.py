"""
Verificador de reseñas Google Maps — 3 capas de detección:

  1. Fetch directo  — POST al formulario GDPR para obtener cookie real
  2. Jina.ai        — renderiza JS desde servidores fuera EU
  3. Claude Vision  — captura de pantalla (thum.io) + Claude lee la imagen

Clasificación final: ACTIVA | ELIMINADA | INCIERTA
"""

import asyncio
import base64
import json as _json
import os
import re
from typing import AsyncGenerator

import httpx

# ─── Constantes ───────────────────────────────────────────────────────────────

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

DELETED_PHRASES: list[tuple[str, str]] = [
    ("no longer available",       "EN: 'no longer available'"),
    ("review is no longer",       "EN: 'review is no longer'"),
    ("ya no está disponible",     "ES: 'ya no está disponible'"),
    ("esta reseña ya no",         "ES: 'esta reseña ya no'"),
    ("reseña no disponible",      "ES: 'reseña no disponible'"),
    ("cette avis n'est plus",     "FR: n'est plus disponible"),
    ("diese rezension ist nicht", "DE: rezension nicht"),
]

STAR_RE = re.compile(r"\b[1-5][,.]?\d?\s*(estrellas?|stars?)\b", re.IGNORECASE)
# Puntuación numérica: "4/5", "5.0", "rating":5  (señal fuerte de reseña activa)
RATING_RE = re.compile(r'(?:"ratingValue"|"reviewRating"|starRating)[^0-9]*([1-5](?:[.,]\d)?)', re.IGNORECASE)

# ─── Extracción de texto desde HTML ───────────────────────────────────────────

def _ld_review_body(obj) -> str:
    if isinstance(obj, dict):
        for k in ("reviewBody", "description", "text"):
            v = obj.get(k, "")
            if isinstance(v, str) and len(v) > 1:   # reseñas de 1 sola palabra
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
    # 1. JSON-LD
    for ld_raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            text = _ld_review_body(_json.loads(ld_raw.strip()))
            if text:
                return text[:350]
        except Exception:
            pass

    # 2. Meta description — Google a veces pone el texto de reseña aquí
    for pattern in [
        r'<meta\s+name=["\']description["\'][^>]+content=["\']([^"\']{5,})["\']',
        r'<meta\s+content=["\']([^"\']{5,})["\'][^>]+name=["\']description["\']',
        r'<meta\s+property=["\']og:description["\'][^>]+content=["\']([^"\']{5,})["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            desc = m.group(1).strip()
            # Descartar descripciones genéricas del sitio, no de reseña
            if any(p in desc.lower() for p in [
                "google maps", "cómo llegar", "how to get", "street view",
                "find local businesses", "maps.google", "ver el mapa",
            ]):
                continue
            # Limpiar prefijo de puntuación: "4/5 estrellas · " o "5 stars - "
            desc = re.sub(
                r'^[\d/,.]+ ?(estrellas?|stars?|de \d)[^·\-–—]*[·\-–—]\s*',
                '', desc, flags=re.IGNORECASE
            ).strip()
            if not desc:
                continue
            letters = sum(1 for c in desc if c.isalpha())
            if letters / max(len(desc), 1) >= 0.50:
                return desc[:350]

    # 3. Strings JS embebidos (umbrales relajados para reseñas cortas)
    seen: set[str] = set()
    for raw in re.findall(r'"([^"\\]{10,400})"', html):
        text = raw.replace("\\n", " ").replace("\\t", " ").strip()
        if text in seen:
            continue
        seen.add(text)
        if text.startswith(("http", "/", "{", "M", "m", "data:", "function")):
            continue
        if any(c in text for c in ("<", ">", "\\", "=", ";", "{", "}", "%", "@", ".")):
            continue
        letters = sum(1 for c in text if c.isalpha())
        if letters / max(len(text), 1) < 0.70:
            continue
        words = text.split()
        # Mínimo 2 palabras (cubre reseñas muy cortas tipo "Muy bueno")
        if len(words) >= 2 and all(len(w) <= 25 for w in words):
            return text[:350]

    return ""


# ─── Capa 1: Fetch directo con bypass consent GDPR ────────────────────────────

async def _fetch_bypassing_consent(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """
    Sigue todos los redirects. Si acaba en consent.google.com,
    acepta el formulario mediante POST para obtener una cookie SOCS real.
    """
    from urllib.parse import urljoin

    r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)

    if "consent.google.com" in str(r.url):
        print(f"[consent] Formulario detectado: {str(r.url)[:80]}", flush=True)
        html_consent = r.text

        action_m = re.search(
            r'<form[^>]+action=["\']([^"\']+)["\']', html_consent, re.IGNORECASE
        )
        if action_m:
            action = urljoin(str(r.url), action_m.group(1))
            form_data: dict[str, str] = {}
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
            form_data["set_eom"] = "false"

            print(f"[consent] POST {action} | campos: {list(form_data.keys())}", flush=True)
            r = await client.post(
                action,
                data=form_data,
                headers={**HEADERS, "Referer": str(r.url)},
                follow_redirects=True,
                timeout=15,
            )
            print(f"[consent] POST → {r.status_code} | {str(r.url)[:80]}", flush=True)
        else:
            print("[consent] No se encontró formulario", flush=True)

    return r.text, str(r.url)


# ─── Capa 2: Jina.ai Reader ───────────────────────────────────────────────────

async def _fetch_via_jina(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Jina.ai renderiza JS desde servidores propios (fuera EU), devuelve texto limpio."""
    jina_url = f"https://r.jina.ai/{url}"
    print(f"[jina] GET {jina_url[:80]}", flush=True)
    r = await client.get(
        jina_url,
        headers={"Accept": "text/plain", "User-Agent": "Mozilla/5.0 (compatible)", "X-No-Cache": "true"},
        follow_redirects=True,
        timeout=30,
    )
    print(f"[jina] {r.status_code} | {len(r.text)} chars", flush=True)
    return r.text, str(r.url)


def _classify_text(text: str, source: str) -> dict | None:
    """Clasifica desde texto plano (Jina o Vision)."""
    text_lower = text.lower()
    for phrase, label in DELETED_PHRASES:
        if phrase in text_lower:
            return {"status": "ELIMINADA", "detail": f"{label} [{source}]",
                    "evidence": [f"'{phrase}'"], "review_text": ""}

    star_m = STAR_RE.search(text_lower)
    review_text = ""
    skip = {"google maps","sign in","directions","open in","share","saved","nearby",
            "photos","reviews","overview","menu","jina","http","©","terms","privacy"}
    for line in text.split("\n"):
        line = line.strip()
        if not line or any(s in line.lower() for s in skip):
            continue
        words = line.split()
        letters = sum(1 for c in line if c.isalpha())
        if len(words) >= 5 and letters / max(len(line), 1) > 0.55 and all(len(w) <= 25 for w in words):
            review_text = line[:350]
            break

    if review_text:
        return {"status": "ACTIVA", "detail": f"Texto encontrado [{source}]",
                "evidence": [f'"{review_text[:100]}"'], "review_text": review_text}
    if star_m:
        return {"status": "ACTIVA", "detail": f"Estrellas [{source}]: '{star_m.group()}'",
                "evidence": [f"'{star_m.group()}'"], "review_text": ""}
    return None


# ─── Capa 3: Claude Vision (screenshot de thum.io) ───────────────────────────

async def _check_via_vision(url: str) -> dict | None:
    """
    Obtiene una captura de pantalla con thum.io (gratuito, sin API key)
    y usa Claude Vision para leer el contenido y clasificar la reseña.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[vision] Sin ANTHROPIC_API_KEY — saltando capa Vision", flush=True)
        return None

    try:
        from anthropic import AsyncAnthropic

        # thum.io captura desde servidores propios (sin consent GDPR)
        screenshot_url = f"https://image.thum.io/get/width/1280/fullpage/{url}"
        print(f"[vision] Capturando screenshot: {screenshot_url[:80]}", flush=True)

        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as sc:
            r = await sc.get(screenshot_url)

        if r.status_code != 200 or len(r.content) < 5000:
            print(f"[vision] Screenshot fallido: {r.status_code} / {len(r.content)} bytes", flush=True)
            return None

        img_b64 = base64.standard_b64encode(r.content).decode()
        content_type = r.headers.get("content-type", "image/jpeg").split(";")[0]
        print(f"[vision] Screenshot OK: {len(r.content)} bytes ({content_type})", flush=True)

        ai = AsyncAnthropic(api_key=api_key)
        resp = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": content_type, "data": img_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a screenshot of a Google Maps review URL.\n"
                            "Determine the status and extract review text if visible.\n\n"
                            "Rules:\n"
                            "- ACTIVA: visible review text written by a user (the actual review content)\n"
                            "- ELIMINADA: page explicitly says 'no longer available', "
                            "'review not found', or similar deletion message\n"
                            "- INCIERTA: consent/cookie page, generic Google Maps page, "
                            "error page, or cannot clearly determine\n\n"
                            "IMPORTANT: If you see a cookie consent form or GDPR page, "
                            "classify as INCIERTA (not ELIMINADA).\n\n"
                            "Respond ONLY with valid JSON:\n"
                            '{"status":"ACTIVA|ELIMINADA|INCIERTA","review_text":"...","reason":"..."}'
                        ),
                    },
                ],
            }],
        )

        raw = resp.content[0].text.strip()
        print(f"[vision] Claude respuesta: {raw[:200]}", flush=True)

        # Extraer JSON aunque venga con texto alrededor
        json_m = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_m:
            data = _json.loads(json_m.group())
            status = data.get("status", "INCIERTA")
            if status not in ("ACTIVA", "ELIMINADA", "INCIERTA"):
                status = "INCIERTA"
            return {
                "status": status,
                "detail": f"Claude Vision: {data.get('reason', '')}",
                "review_text": data.get("review_text", ""),
                "evidence": [f"Screenshot analizado por Claude Vision"],
            }

    except Exception as e:
        print(f"[vision] Error: {e}", flush=True)

    return None


# ─── Verificación de una URL (3 capas) ────────────────────────────────────────

async def _check_single_review(client: httpx.AsyncClient, url: str) -> dict:
    final_url = url
    try:
        # ── Capa 1: fetch directo ──────────────────────────────────────────────
        html, final_url = await _fetch_bypassing_consent(client, url)
        html_lower = html.lower()
        print(f"[check] {url[:55]} | {len(html)} chars | {final_url[:70]}", flush=True)

        on_consent = "consent.google.com" in final_url

        if not on_consent:
            for phrase, label in DELETED_PHRASES:
                if phrase in html_lower:
                    return {"status": "ELIMINADA", "detail": label,
                            "evidence": [f"'{phrase}'"], "review_text": "", "final_url": final_url}
            rev = _extract_review_text(html)
            if rev:
                return {"status": "ACTIVA", "detail": "Texto en HTML",
                        "evidence": [f'"{rev[:100]}"'], "review_text": rev, "final_url": final_url}
            # Puntuación numérica en JSON-LD (señal fuerte: la reseña existe aunque sea sin texto)
            rating_m = RATING_RE.search(html)
            if rating_m:
                return {"status": "ACTIVA", "detail": f"Puntuación {rating_m.group(1)}/5 en HTML",
                        "evidence": [f"rating={rating_m.group(1)}"], "review_text": "", "final_url": final_url}
            star_m = STAR_RE.search(html_lower)
            if star_m:
                return {"status": "ACTIVA", "detail": f"Estrellas: '{star_m.group()}'",
                        "evidence": [f"'{star_m.group()}'"], "review_text": "", "final_url": final_url}

        # ── Capa 2: Jina.ai ───────────────────────────────────────────────────
        print(f"[check] Capa 1 sin señal → Jina.ai", flush=True)
        try:
            jina_text, jina_url = await _fetch_via_jina(client, url)
            res = _classify_text(jina_text, "Jina")
            if res:
                res["final_url"] = jina_url
                return res
        except Exception as je:
            print(f"[jina] Error: {je}", flush=True)

        # ── Capa 3: Claude Vision ─────────────────────────────────────────────
        print(f"[check] Capa 2 sin señal → Claude Vision", flush=True)
        vision_res = await _check_via_vision(url)
        if vision_res:
            vision_res["final_url"] = url
            return vision_res

        return {"status": "INCIERTA", "detail": "Sin señal en las 3 capas",
                "evidence": [f"{final_url[:80]}", f"{len(html)} chars HTML"],
                "review_text": "", "final_url": final_url}

    except httpx.TimeoutException:
        return {"status": "INCIERTA", "detail": "Timeout", "evidence": [], "review_text": "", "final_url": final_url}
    except Exception as exc:
        return {"status": "INCIERTA", "detail": str(exc)[:120], "evidence": [], "review_text": "", "final_url": final_url}


# ─── Stream ───────────────────────────────────────────────────────────────────

async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 3,
    delay_seconds: float = 0.5,
) -> AsyncGenerator[dict, None]:
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:

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
                result = await asyncio.wait_for(result_queue.get(), timeout=120.0)
                yield result
                received += 1
            except asyncio.TimeoutError:
                break

        await asyncio.gather(*tasks, return_exceptions=True)


async def check_reviews(url_items, max_concurrent=3, delay_seconds=0.5, progress_callback=None):
    results = {}
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), len(url_items), result)
    return [results.get(i) for i in range(len(url_items))]
