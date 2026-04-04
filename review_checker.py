"""
Verificador de reseñas Google Maps — capas de detección:

  1. Fetch directo  — HTML estático + JSON-LD
  2. Playwright     — Chromium headless (renderiza JS completo)
  3. Claude Vision  — analiza el screenshot de Playwright
  4. Jina.ai        — fallback si Playwright no está disponible
  5. Vision thum.io — último recurso

Clasificación final: ACTIVA | ELIMINADA | INCIERTA | ERRONEA | DUPLICADA
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
# Solo enteros exactos 1-5 — los ratings agregados del negocio son decimales (4.2, 3.7…)
RATING_RE = re.compile(r'"ratingValue"\s*:\s*"?([1-5])"?(?!\d|[,.])', re.IGNORECASE)

# ─── Extracción de texto desde HTML ───────────────────────────────────────────

def _ld_review_body(obj) -> str:
    """Extrae texto SOLO de objetos JSON-LD @type='Review'."""
    if isinstance(obj, dict):
        obj_type = str(obj.get("@type", ""))
        if "Review" in obj_type:
            for k in ("reviewBody", "text", "description"):
                v = obj.get(k, "")
                if isinstance(v, str) and len(v) > 1:
                    return v
        for k, v in obj.items():
            if k in ("aggregateRating", "address", "geo", "openingHours", "image"):
                continue
            r = _ld_review_body(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _ld_review_body(item)
            if r:
                return r
    return ""


def _ld_rating(obj) -> int:
    """Extrae rating SOLO de objetos @type='Review'. Solo acepta enteros exactos."""
    if isinstance(obj, dict):
        obj_type = str(obj.get("@type", ""))
        if "Review" in obj_type:
            for source in [obj.get("reviewRating", {}), obj]:
                if not isinstance(source, dict):
                    continue
                v = source.get("ratingValue")
                if v is not None:
                    try:
                        fv = float(str(v))
                        n  = int(fv)
                        if 1 <= n <= 5 and fv == n:  # entero exacto
                            return n
                    except (ValueError, TypeError):
                        pass
        for k, v in obj.items():
            if k in ("aggregateRating", "address", "geo"):
                continue
            r = _ld_rating(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _ld_rating(item)
            if r:
                return r
    return 0


def _extract_rating(html: str) -> int:
    for ld_raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            rating = _ld_rating(_json.loads(ld_raw.strip()))
            if rating:
                return rating
        except Exception:
            pass

    m = RATING_RE.search(html)
    if m:
        try:
            n = int(float(m.group(1).replace(",", ".")))
            if 1 <= n <= 5:
                return n
        except (ValueError, TypeError):
            pass

    return 0


def _extract_review_text(html: str) -> str:
    # 1. JSON-LD (más fiable)
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

    # 2. Strings JS embebidos (fallback)
    seen: set[str] = set()
    _biz_name_re = re.compile(r'^[A-ZÁÉÍÓÚÑ][^a-záéíóúñ]{2,}\s*[-–—]\s*\w', re.UNICODE)
    for raw in re.findall(r'"([^"\\]{10,400})"', html):
        text = raw.replace("\\n", " ").replace("\\t", " ").strip()
        if text in seen:
            continue
        seen.add(text)
        if text.startswith(("http", "/", "{", "M", "m", "data:", "function")):
            continue
        if any(c in text for c in ("<", ">", "\\", "=", ";", "{", "}", "%", "@", ".")):
            continue
        if _biz_name_re.match(text):
            continue
        letters = sum(1 for c in text if c.isalpha())
        if letters / max(len(text), 1) < 0.70:
            continue
        words = text.split()
        if len(words) >= 2 and all(len(w) <= 25 for w in words):
            return text[:350]

    return ""


# ─── Capa 1: Fetch directo con bypass consent GDPR ────────────────────────────

async def _fetch_bypassing_consent(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
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

            r = await client.post(
                action,
                data=form_data,
                headers={**HEADERS, "Referer": str(r.url)},
                follow_redirects=True,
                timeout=15,
            )

    return r.text, str(r.url)


# ─── Capa 2: Playwright (Chromium headless real) ─────────────────────────────

_PLAYWRIGHT_AVAILABLE: bool | None = None  # None = no comprobado aún

async def _fetch_via_playwright(url: str) -> tuple[str, bytes | None]:
    """
    Renderiza la URL con Chromium headless real.
    Devuelve (texto_de_la_página, screenshot_jpeg_bytes).
    """
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is False:
        return "", None

    try:
        from playwright.async_api import async_playwright  # type: ignore

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="networkidle", timeout=25000)
            except Exception:
                # networkidle puede timeout; nos quedamos con lo que cargó
                pass

            # Esperar que el contenido dinámico termine de renderizar
            await page.wait_for_timeout(2000)

            text = await page.inner_text("body")
            screenshot = await page.screenshot(
                full_page=False, type="jpeg", quality=80
            )
            await browser.close()

        _PLAYWRIGHT_AVAILABLE = True
        print(f"[playwright] OK: {len(text)} chars | {len(screenshot)} bytes screenshot", flush=True)
        return text, screenshot

    except ImportError:
        _PLAYWRIGHT_AVAILABLE = False
        print("[playwright] No disponible (no instalado)", flush=True)
        return "", None
    except Exception as e:
        print(f"[playwright] Error: {e}", flush=True)
        return "", None


def _classify_text(text: str, source: str) -> dict | None:
    """Clasifica desde texto plano (Playwright, Jina, Vision)."""
    text_lower = text.lower()

    for phrase, label in DELETED_PHRASES:
        if phrase in text_lower:
            return {"status": "ELIMINADA", "detail": f"{label} [{source}]",
                    "evidence": [f"'{phrase}'"], "review_text": ""}

    # Palabras a ignorar al buscar texto de reseña
    skip = {
        "sign in", "open in", "jina", "http", "©", "terms", "privacy",
        "directions from", "directions to", "get directions",
    }
    review_text = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()
        if any(s in line_lower for s in skip):
            continue
        words = line.split()
        letters = sum(1 for c in line if c.isalpha())
        # Mínimo 3 palabras y >50% letras — más permisivo que antes
        if (len(words) >= 3
                and letters / max(len(line), 1) > 0.50
                and all(len(w) <= 30 for w in words)):
            review_text = line[:350]
            break

    star_m = STAR_RE.search(text_lower)

    if review_text:
        return {"status": "ACTIVA", "detail": f"Texto encontrado [{source}]",
                "evidence": [f'"{review_text[:100]}"'], "review_text": review_text}
    if star_m:
        return {"status": "ACTIVA", "detail": f"Estrellas [{source}]: '{star_m.group()}'",
                "evidence": [f"'{star_m.group()}'"], "review_text": ""}
    return None


# ─── Capa 3: Claude Vision ────────────────────────────────────────────────────

_VISION_PROMPT = """\
This is a screenshot of a Google Maps review URL.

Your task: decide if the specific user review is still ACTIVE or has been DELETED.

Classification:
- ACTIVA  → You can see a real user-written review (any text written by a person about a place).
            A user profile page showing their review counts as ACTIVA.
            Even a short review like "Great place!" is ACTIVA.
- ELIMINADA → The page explicitly shows a deletion notice:
              "no longer available", "review not found", "has been removed", etc.
              OR the URL redirected to a generic place/business page (address, hours, photos)
              with NO specific review text visible.
- INCIERTA  → Only if: cookie/consent wall is blocking the view, blank page, 404 error,
              or you genuinely cannot determine.

Key signal: if the page shows a PLACE (restaurant, hotel, shop) with overview/photos/hours
but NO specific review by a single user → that usually means the review was DELETED (ELIMINADA).

Be generous: any visible user review text → ACTIVA.

Reply ONLY with valid JSON (no markdown, no extra text):
{"status":"ACTIVA|ELIMINADA|INCIERTA","review_text":"the review text you can read or empty","reason":"one-line explanation"}"""


async def _check_via_vision(
    url: str,
    screenshot_bytes: bytes | None = None,
) -> dict | None:
    """
    Analiza una captura de pantalla con Claude Vision.
    Si se pasan screenshot_bytes (de Playwright), los usa directamente.
    Si no, intenta thum.io como fallback.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[vision] Sin ANTHROPIC_API_KEY — saltando capa Vision", flush=True)
        return None

    img_data = screenshot_bytes

    # Fallback: thum.io si no tenemos screenshot de Playwright
    if not img_data:
        try:
            screenshot_url = f"https://image.thum.io/get/width/1280/crop/900/{url}"
            print(f"[vision] Capturando con thum.io: {screenshot_url[:80]}", flush=True)
            async with httpx.AsyncClient(timeout=40, follow_redirects=True) as sc:
                r = await sc.get(screenshot_url)
            if r.status_code == 200 and len(r.content) >= 5000:
                img_data = r.content
                print(f"[vision] thum.io OK: {len(img_data)} bytes", flush=True)
            else:
                print(f"[vision] thum.io fallido: {r.status_code} / {len(r.content)} bytes", flush=True)
        except Exception as e:
            print(f"[vision] thum.io error: {e}", flush=True)

    if not img_data or len(img_data) < 1000:
        return None

    try:
        from anthropic import AsyncAnthropic  # type: ignore

        img_b64 = base64.standard_b64encode(img_data).decode()
        source_label = "Playwright" if screenshot_bytes else "thum.io"
        print(f"[vision] Enviando a Claude Vision ({source_label}, {len(img_data)} bytes)", flush=True)

        ai = AsyncAnthropic(api_key=api_key)
        resp = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }],
        )

        raw = resp.content[0].text.strip()
        print(f"[vision] Claude respuesta: {raw[:300]}", flush=True)

        json_m = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_m:
            data = _json.loads(json_m.group())
            status = data.get("status", "INCIERTA")
            if status not in ("ACTIVA", "ELIMINADA", "INCIERTA"):
                status = "INCIERTA"
            return {
                "status": status,
                "detail": f"Claude Vision ({source_label}): {data.get('reason', '')}",
                "review_text": data.get("review_text", ""),
                "evidence": [f"Screenshot analizado por Claude Vision ({source_label})"],
            }

    except Exception as e:
        print(f"[vision] Error Claude: {e}", flush=True)

    return None


# ─── Capa 4: Jina.ai Reader ───────────────────────────────────────────────────

async def _fetch_via_jina(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fallback: Jina.ai renderiza JS desde servidores propios."""
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


# ─── Verificación de una URL ──────────────────────────────────────────────────

async def _check_single_review(client: httpx.AsyncClient, url: str) -> dict:
    final_url = url
    try:
        # ── Capa 1: fetch directo + JSON-LD ───────────────────────────────────
        html, final_url = await _fetch_bypassing_consent(client, url)
        html_lower = html.lower()
        print(f"[check] {url[:55]} | {len(html)} chars | {final_url[:70]}", flush=True)

        on_consent = "consent.google.com" in final_url

        if not on_consent:
            for phrase, label in DELETED_PHRASES:
                if phrase in html_lower:
                    return {"status": "ELIMINADA", "detail": label,
                            "evidence": [f"'{phrase}'"], "review_text": "", "rating": 0,
                            "final_url": final_url}
            rating = _extract_rating(html)
            rev = _extract_review_text(html)
            if rev or rating:
                status = "ACTIVA" if rating == 5 or (rating == 0 and rev) else "ERRONEA"
                detail = (
                    f"Reseña de {rating}/5 — solo se validan 5 estrellas" if 0 < rating < 5
                    else ("Texto en HTML" if rev else f"Puntuación {rating}/5 detectada")
                )
                return {"status": status, "detail": detail,
                        "evidence": [f'"{rev[:100]}"'] if rev else [f"rating={rating}"],
                        "review_text": rev, "rating": rating, "final_url": final_url}
            star_m = STAR_RE.search(html_lower)
            if star_m:
                return {"status": "ACTIVA", "detail": f"Estrellas: '{star_m.group()}'",
                        "evidence": [f"'{star_m.group()}'"], "review_text": "", "rating": 0,
                        "final_url": final_url}

        # ── Capa 2: Playwright (Chromium headless) ────────────────────────────
        print(f"[check] Capa 1 sin señal → Playwright", flush=True)
        pw_text, pw_screenshot = await _fetch_via_playwright(url)

        if pw_text:
            # 2a: clasificar el texto renderizado por Playwright
            res = _classify_text(pw_text, "Playwright")
            if res:
                res["final_url"] = url
                res["rating"] = 0
                return res

            # 2b: si el texto no fue concluyente, enviar screenshot a Claude Vision
            if pw_screenshot:
                print(f"[check] Playwright texto sin señal → Claude Vision", flush=True)
                vision_res = await _check_via_vision(url, screenshot_bytes=pw_screenshot)
                if vision_res:
                    vision_res["final_url"] = url
                    vision_res["rating"] = 0
                    return vision_res
        else:
            # Playwright no disponible → fallback a Jina
            print(f"[check] Playwright no disponible → Jina.ai", flush=True)
            try:
                jina_text, jina_url = await _fetch_via_jina(client, url)
                res = _classify_text(jina_text, "Jina")
                if res:
                    res["final_url"] = jina_url
                    res["rating"] = 0
                    return res
            except Exception as je:
                print(f"[jina] Error: {je}", flush=True)

            # Último recurso: thum.io + Vision
            print(f"[check] Jina sin señal → Vision thum.io", flush=True)
            vision_res = await _check_via_vision(url)
            if vision_res:
                vision_res["final_url"] = url
                vision_res["rating"] = 0
                return vision_res

        return {"status": "INCIERTA", "detail": "Sin señal en todas las capas",
                "evidence": [f"{final_url[:80]}", f"{len(html)} chars HTML"],
                "review_text": "", "rating": 0, "final_url": final_url}

    except httpx.TimeoutException:
        return {"status": "INCIERTA", "detail": "Timeout", "evidence": [], "review_text": "",
                "rating": 0, "final_url": final_url}
    except Exception as exc:
        return {"status": "INCIERTA", "detail": str(exc)[:120], "evidence": [], "review_text": "",
                "rating": 0, "final_url": final_url}


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
