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
import learning as _learning

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
    ("no longer available",          "Reseña eliminada (EN)"),
    ("review is no longer",          "Reseña eliminada (EN)"),
    ("ya no está disponible",        "Reseña eliminada (ES)"),
    ("esta reseña ya no",            "Reseña eliminada (ES)"),
    ("reseña no disponible",         "Reseña no disponible (ES)"),
    ("cette avis n'est plus",        "Reseña eliminada (FR)"),
    ("diese rezension ist nicht",    "Reseña eliminada (DE)"),
    ("this review has been removed", "Reseña eliminada (EN)"),
    ("review not found",             "Reseña no encontrada (EN)"),
]

# Cadenas de la interfaz de Google Maps que nunca son texto de reseña real
GOOGLE_UI_STRINGS: set[str] = {
    # Navegación y botones
    "contraer panel lateral", "expandir panel lateral", "cerrar",
    "abrir en google maps", "ver en google maps", "open in google maps",
    "search google maps", "search maps", "buscar en google maps",
    "directions", "cómo llegar", "cómo llegar desde aquí", "cómo llegar hasta aquí",
    "directions from", "directions to", "get directions", "ver indicaciones",
    "compartir", "share", "guardar", "save", "añadir a favoritos",
    "send to phone", "enviar al teléfono", "llamar",
    "suggest an edit", "sugerir una edición", "editar este lugar",
    "add a missing place", "añadir lugar",
    "nearby", "lugares cercanos", "cerca de aquí",
    # Reseñas / fotos
    "see photos", "ver fotos", "ver todas las fotos",
    "write a review", "escribir una reseña", "añadir una reseña",
    "add a review", "add review",
    "more reviews", "ver más reseñas", "all reviews", "todas las reseñas",
    "sort reviews", "ordenar reseñas", "translate review", "traducir reseña",
    "see all reviews", "ver todas las reseñas",
    "photos", "fotos", "overview", "resumen",
    "menu", "menú", "about", "acerca de", "updates", "actualizaciones",
    "questions & answers", "preguntas y respuestas",
    # UI general de Google
    "sign in", "iniciar sesión", "google maps",
    "open in", "abrir en", "jina", "http", "©",
    "terms", "términos", "privacy", "privacidad",
    "report a problem", "informar de un problema",
    # Nombres de pestañas comunes en páginas de negocios
    "resumen", "opiniones", "productos", "preguntas",
    # Frases de UI de panel lateral
    "contraer", "expandir", "panel lateral",
}

STAR_RE = re.compile(r"\b([1-5])\s*(estrellas?|stars?)\b", re.IGNORECASE)
# Detecta "X de 5 estrellas", "X/5", "rated X" etc. en texto renderizado por Playwright
STAR_SCORE_RE = re.compile(
    r'\b([1-5])\s*(?:de\s*5|\/5|out\s+of\s*5)?\s*(?:estrellas?|stars?|★)|\b([1-5])\s*★',
    re.IGNORECASE,
)
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
    bad_texts = set(t.lower() for t in _learning.get_bad_texts())
    for raw in re.findall(r'"([^"\\]{10,400})"', html):
        text = raw.replace("\\n", " ").replace("\\t", " ").strip()
        if text in seen:
            continue
        seen.add(text)
        if text.lower() in bad_texts:
            continue
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

_BROWSER_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


async def _create_browser():
    """Lanza un navegador Chromium y devuelve (playwright_ctx, browser)."""
    from playwright.async_api import async_playwright  # type: ignore
    ctx = async_playwright()
    p   = await ctx.__aenter__()
    browser = await p.chromium.launch(headless=True, args=_BROWSER_LAUNCH_ARGS)
    return ctx, browser


async def _fetch_page(url: str, browser) -> tuple[str, bytes | None, str, str]:
    """
    Abre una nueva página en el browser dado, navega a url y devuelve
    (texto, screenshot_jpeg, url_final, html_completo).
    Cierra el contexto (no el browser) al terminar.
    """
    context = await browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="en-US",
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    try:
        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception:
            # networkidle puede fallar por timeout; la página suele estar suficientemente cargada
            pass

        final_url  = page.url
        text       = await page.inner_text("body")
        html       = await page.content()           # HTML completo renderizado
        screenshot = await page.screenshot(full_page=False, type="jpeg", quality=80)
        print(f"[playwright] OK: {len(text)} chars | final={final_url[:70]}", flush=True)
        return text, screenshot, final_url, html
    finally:
        await context.close()


# Detecta el rating en aria-labels de Google Maps renderizados por Playwright
# Ejemplos: aria-label="Puntuación: 4 de 5 estrellas"  aria-label="Rated 3 out of 5"
_ARIA_RATING_RE = re.compile(
    r'aria-label="[^"]*?(?:[Pp]untuaci[oó]n|[Rr]ated?|[Cc]alificaci[oó]n)[^"]*?'
    r'([1-5])(?:[,.]0)?\s*(?:de\s*5|out\s+of\s*5|\/5)?[^"]*?"',
    re.IGNORECASE,
)
# También detecta atributos data-rating="4" que usa Google en algunos contextos
_DATA_RATING_RE = re.compile(r'data-(?:rating|value)="([1-5])(?:[.,]0)?"', re.IGNORECASE)


def _extract_aria_rating(html: str) -> int:
    """Extrae rating de atributos aria-label o data-rating del HTML renderizado."""
    m = _ARIA_RATING_RE.search(html)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 5:
                print(f"[playwright] aria-label rating={n}", flush=True)
                return n
        except (ValueError, TypeError):
            pass
    m = _DATA_RATING_RE.search(html)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 5:
                print(f"[playwright] data-rating={n}", flush=True)
                return n
        except (ValueError, TypeError):
            pass
    return 0


async def _fetch_via_playwright(url: str, browser=None) -> tuple[str, bytes | None, str, str]:
    """
    Renderiza la URL con Chromium headless real.
    Si se pasa `browser`, lo reutiliza (pool). Si no, crea uno temporal.
    Devuelve (texto, screenshot_jpeg, url_final, html_completo).
    """
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is False:
        return "", None, url, ""

    _own_browser = browser is None
    _ctx = None
    try:
        if _own_browser:
            from playwright.async_api import async_playwright  # type: ignore  # noqa
            _ctx, browser = await _create_browser()

        text, screenshot, final_url, html = await _fetch_page(url, browser)
        _PLAYWRIGHT_AVAILABLE = True
        return text, screenshot, final_url, html

    except ImportError:
        _PLAYWRIGHT_AVAILABLE = False
        print("[playwright] No disponible (no instalado)", flush=True)
        return "", None, url, ""
    except Exception as e:
        print(f"[playwright] Error: {e}", flush=True)
        return "", None, url, ""
    finally:
        if _own_browser:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if _ctx:
                try:
                    await _ctx.__aexit__(None, None, None)
                except Exception:
                    pass


def _classify_text(text: str, source: str) -> dict | None:
    """Clasifica desde texto plano (Playwright, Jina, Vision)."""
    text_lower = text.lower()

    for phrase, label in DELETED_PHRASES:
        if phrase in text_lower:
            return {"status": "ELIMINADA", "detail": label,
                    "evidence": [f"'{phrase}'"], "review_text": ""}

    bad_texts = set(t.lower() for t in _learning.get_bad_texts())

    review_text = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()

        # Filtrar strings de UI de Google (exacto o contenido)
        if line_lower in GOOGLE_UI_STRINGS:
            continue
        if any(ui in line_lower for ui in GOOGLE_UI_STRINGS if len(ui) > 6):
            continue
        # Filtrar textos aprendidos como malos
        if line_lower in bad_texts or any(b in line_lower for b in bad_texts if len(b) > 5):
            continue
        # Filtrar URLs y código
        if line.startswith(("http", "//", "{", "function", "<")):
            continue

        words = line.split()
        letters = sum(1 for c in line if c.isalpha())
        if (len(words) >= 3
                and letters / max(len(line), 1) > 0.50
                and all(len(w) <= 30 for w in words)):
            review_text = line[:350]
            break

    star_m = STAR_RE.search(text_lower)

    # Extraer la puntuación numérica de estrellas del texto renderizado
    star_rating = 0
    if star_m:
        try:
            star_rating = int(star_m.group(1))
        except (ValueError, TypeError):
            pass

    # Si encontramos texto de reseña, verificar si hay puntuación baja asociada
    if review_text:
        if 0 < star_rating < 5:
            return {
                "status": "ERRONEA",
                "detail": f"Puntuación {star_rating}/5 — solo se validan las de 5 estrellas",
                "evidence": [f'"{review_text[:100]}"', f"rating={star_rating}"],
                "review_text": review_text,
                "rating": star_rating,
            }
        return {"status": "ACTIVA", "detail": "Reseña visible en la página",
                "evidence": [f'"{review_text[:100]}"'], "review_text": review_text,
                "rating": star_rating}

    if star_m:
        if 0 < star_rating < 5:
            return {
                "status": "ERRONEA",
                "detail": f"Puntuación {star_rating}/5 — solo se validan las de 5 estrellas",
                "evidence": [f"'{star_m.group()}'"],
                "review_text": "",
                "rating": star_rating,
            }
        return {"status": "ACTIVA", "detail": "Puntuación de estrellas detectada",
                "evidence": [f"'{star_m.group()}'"], "review_text": "",
                "rating": star_rating}
    return None


# ─── Capa 3: Claude Vision ────────────────────────────────────────────────────

_VISION_PROMPT = """\
This is a screenshot of a Google Maps review page. Your job has TWO parts:

PART 1 — COUNT THE STARS:
Look for the star rating row next to the review. Count filled (yellow/orange) stars carefully.
- 5 filled stars = 5
- 4 filled stars = 4
- 3 filled stars = 3
- etc.
If you cannot see any stars at all, set rating to 0.

PART 2 — CLASSIFY:
- ACTIVA    → Review is visible AND has exactly 5 stars.
- ERRONEA   → Review is visible BUT has 1, 2, 3, or 4 stars. Only 5-star reviews are valid.
- ELIMINADA → Page shows a deletion notice ("no longer available", "ya no está disponible",
              "review not found", "has been removed") OR the page is a generic business
              overview (address, hours, photos) with NO specific user review text visible.
- INCIERTA  → Cookie wall blocking, blank page, 404, or genuinely impossible to determine.

CRITICAL: If you see review text + stars that are NOT all filled → ERRONEA.
A place overview page (not showing a specific user review) → ELIMINADA.

Reply ONLY with valid JSON, no markdown:
{"status":"ACTIVA|ERRONEA|ELIMINADA|INCIERTA","rating":5,"review_text":"text or empty","reason":"brief explanation"}"""


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
                    {"type": "text", "text": _VISION_PROMPT + _learning.build_vision_few_shot()},
                ],
            }],
        )

        raw = resp.content[0].text.strip()
        print(f"[vision] Claude respuesta: {raw[:300]}", flush=True)

        json_m = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_m:
            data = _json.loads(json_m.group())
            status = data.get("status", "INCIERTA")
            if status not in ("ACTIVA", "ERRONEA", "ELIMINADA", "INCIERTA"):
                status = "INCIERTA"
            vision_rating = 0
            try:
                vision_rating = int(data.get("rating", 0))
            except (ValueError, TypeError):
                pass
            # Doble verificación: si Vision dice ACTIVA pero rating < 5, corregir a ERRONEA
            if status == "ACTIVA" and 0 < vision_rating < 5:
                status = "ERRONEA"
            detail = f"Claude Vision ({source_label}): {data.get('reason', '')}"
            if status == "ERRONEA" and vision_rating:
                detail = f"Puntuación {vision_rating}/5 — solo se validan las de 5 estrellas"
            return {
                "status": status,
                "detail": detail,
                "review_text": data.get("review_text", ""),
                "rating": vision_rating,
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

async def _check_single_review(client: httpx.AsyncClient, url: str, browser=None) -> dict:
    final_url = url
    _capture: bytes | None = None  # screenshot de Playwright para revisión manual

    def _incierta(detail: str, evidence: list | None = None) -> dict:
        return {
            "status": "INCIERTA", "detail": detail,
            "evidence": evidence or [], "review_text": "",
            "rating": 0, "final_url": final_url,
            "_screenshot": _capture,        # None si no se capturó
        }

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
                if 0 < rating < 5:
                    detail = f"Puntuación {rating}/5 — solo válidas las de 5 estrellas"
                elif rev:
                    detail = "Reseña visible en la página"
                else:
                    detail = f"Puntuación {rating}/5 detectada"
                return {"status": status, "detail": detail,
                        "evidence": [f'"{rev[:100]}"'] if rev else [f"rating={rating}"],
                        "review_text": rev, "rating": rating, "final_url": final_url}
            star_m = STAR_RE.search(html_lower)
            if star_m:
                try:
                    star_num = int(star_m.group(1))
                except (ValueError, TypeError):
                    star_num = 0
                if 0 < star_num < 5:
                    return {
                        "status": "ERRONEA",
                        "detail": f"Puntuación {star_num}/5 — solo se validan las de 5 estrellas",
                        "evidence": [f"'{star_m.group()}'"],
                        "review_text": "", "rating": star_num, "final_url": final_url,
                    }
                return {"status": "ACTIVA", "detail": "Puntuación de estrellas detectada",
                        "evidence": [f"'{star_m.group()}'"], "review_text": "", "rating": star_num,
                        "final_url": final_url}

        # ── Capa 2: Playwright (Chromium headless) ────────────────────────────
        print(f"[check] Capa 1 sin señal → Playwright", flush=True)
        pw_text, _capture, pw_final_url, pw_html = await _fetch_via_playwright(url, browser=browser)

        if pw_text:
            # 2a: detectar redirección a página de negocio (reseña eliminada)
            redirected_to_place = (
                "/maps/place/" in pw_final_url
                and "/maps/contrib/" not in pw_final_url
                and "maps.app.goo.gl" not in pw_final_url
            )
            if redirected_to_place:
                print(f"[check] Playwright redirigió a página de negocio → ELIMINADA", flush=True)
                return {
                    "status": "ELIMINADA",
                    "detail": "Redirige a la página del negocio — reseña no encontrada",
                    "evidence": [f"URL final: {pw_final_url[:80]}"],
                    "review_text": "", "rating": 0,
                    "final_url": pw_final_url,
                    "_screenshot": _capture,
                }

            # 2b: extraer rating de aria-labels del HTML renderizado
            # (las estrellas de Google Maps son SVG — no aparecen en inner_text)
            aria_rating = _extract_aria_rating(pw_html) if pw_html else 0

            # 2c: clasificar el texto renderizado por Playwright
            res = _classify_text(pw_text, "Playwright")
            if res:
                res["final_url"] = pw_final_url or url
                res.setdefault("rating", 0)

                # Aplicar aria_rating si el texto no lo detectó
                if aria_rating and res.get("rating", 0) == 0:
                    res["rating"] = aria_rating
                    if 0 < aria_rating < 5:
                        res["status"] = "ERRONEA"
                        res["detail"] = f"Puntuación {aria_rating}/5 — solo se validan las de 5 estrellas"

                # Si el resultado es ACTIVA pero aún no tenemos rating, llamar Vision
                # para verificar las estrellas (son visuales, no siempre en texto)
                if res["status"] == "ACTIVA" and res.get("rating", 0) == 0 and _capture:
                    print(f"[check] ACTIVA sin rating → verificando estrellas con Vision", flush=True)
                    vision_res = await _check_via_vision(url, screenshot_bytes=_capture)
                    if vision_res:
                        v_rating = vision_res.get("rating", 0)
                        v_status = vision_res.get("status", "")
                        if v_status == "ERRONEA" or (0 < v_rating < 5):
                            res["status"]  = "ERRONEA"
                            res["detail"]  = vision_res.get("detail") or f"Puntuación {v_rating}/5 — solo se validan las de 5 estrellas"
                            res["rating"]  = v_rating
                        elif v_status in ("ACTIVA", "ELIMINADA"):
                            res["status"] = v_status
                            res["rating"] = v_rating
                            if v_status == "ELIMINADA":
                                res["detail"] = vision_res.get("detail", "Reseña eliminada según Vision IA")

                return res

            # 2d: texto no concluyente → Claude Vision
            if _capture and len(pw_text) >= 200:
                print(f"[check] Playwright texto sin señal → Claude Vision", flush=True)
                vision_res = await _check_via_vision(url, screenshot_bytes=_capture)
                if vision_res:
                    vision_res["final_url"] = pw_final_url or url
                    vision_res.setdefault("rating", aria_rating)
                    if vision_res["status"] == "INCIERTA":
                        vision_res["_screenshot"] = _capture
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
                vision_res.setdefault("rating", 0)
                return vision_res

        return _incierta("Sin señal en todas las capas",
                         [f"{final_url[:80]}", f"{len(html)} chars HTML"])

    except httpx.TimeoutException:
        return _incierta("Timeout")
    except Exception as exc:
        return _incierta(str(exc)[:120])


# ─── Stream ───────────────────────────────────────────────────────────────────

async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 3,
    delay_seconds: float = 0.5,
) -> AsyncGenerator[dict, None]:
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    # ── Browser pool: un solo navegador para todo el job ─────────────────────
    _pool_browser  = None
    _pool_ctx      = None
    if _PLAYWRIGHT_AVAILABLE is not False:
        try:
            _pool_ctx, _pool_browser = await _create_browser()
            print("[playwright] Browser pool listo para el job", flush=True)
        except Exception as e:
            print(f"[playwright] No se pudo crear browser pool: {e}", flush=True)
            _pool_browser = None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:

            async def check_one(idx: int, item: dict):
                async with semaphore:
                    if idx > 0:
                        await asyncio.sleep(delay_seconds)
                    result = await _check_single_review(
                        client, item["url"], browser=_pool_browser
                    )
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

    finally:
        # Cierre limpio del browser pool al terminar el job
        if _pool_browser:
            try:
                await _pool_browser.close()
                print("[playwright] Browser pool cerrado", flush=True)
            except Exception:
                pass
        if _pool_ctx:
            try:
                await _pool_ctx.__aexit__(None, None, None)
            except Exception:
                pass


async def check_reviews(url_items, max_concurrent=3, delay_seconds=0.5, progress_callback=None):
    results = {}
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), len(url_items), result)
    return [results.get(i) for i in range(len(url_items))]
