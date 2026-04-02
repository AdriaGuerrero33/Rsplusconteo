"""
Verificador de reseñas Google Maps.

Clasificación:
  ACTIVA    — se encontró texto de reseña real en la página
  ELIMINADA — se encontró el mensaje "no longer available" u equivalentes
  INCIERTA  — no hay evidencia suficiente (timeout, consent bloqueado, etc.)

Estrategia de detección:
  1. Navegar → detectar y descartar GDPR consent (servidores EU)
  2. Esperar señal específica: mensaje de eliminación O estrellas de reseña
  3. Intentar extraer el texto real de la reseña
  4. Si hay texto de reseña → ACTIVA (con snippet)
  5. Si hay mensaje de eliminación → ELIMINADA
  6. Si ninguno → INCIERTA
"""

import asyncio
import re
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ─── Frases de eliminación (varios idiomas) ───────────────────────────────────
DELETED_PHRASES: list[tuple[str, str]] = [
    ("no longer available",       "EN: 'no longer available'"),
    ("review is no longer",       "EN: 'review is no longer'"),
    ("ya no está disponible",     "ES: 'ya no está disponible'"),
    ("esta reseña ya no",         "ES: 'esta reseña ya no'"),
    ("reseña no disponible",      "ES: 'reseña no disponible'"),
    ("cette avis n'est plus",     "FR: avis n'est plus"),
    ("diese rezension ist nicht", "DE: rezension ist nicht"),
]

# ─── Selectores de estrellas (señal positiva auxiliar) ────────────────────────
STAR_SELECTORS = [
    "span[aria-label$=' stars']",
    "span[aria-label$=' star']",
    "span[aria-label$=' estrellas']",
    "span[aria-label$=' estrella']",
    "div[aria-label$=' stars']",
    "div[aria-label$=' estrellas']",
    "[aria-label*='Valoración de']",
    "[aria-label*='Rated ']",
]

# ─── Selectores donde puede estar el texto de la reseña ──────────────────────
REVIEW_TEXT_SELECTORS = [
    "span.wiI7pd",            # clase estable en muchas versiones de Maps
    "span[jsname='bN97Pc']",  # span de texto de reseña individual
    "[data-expandable-section]",
    "span.MyEned",
    "div.MyEned",
    "span[jsname='fbQN7e']",
]

STAR_TEXT_RE = re.compile(r"\b\d[,.]?\d?\s*(estrellas?|stars?)\b", re.IGNORECASE)

# ─── Condición JS: esperar señal de eliminación O de estrellas ────────────────
_WAIT_JS = """() => {
    const t = (document.body && document.body.innerText || '').toLowerCase();
    if (t.includes('no longer available') || t.includes('review is no longer')
        || t.includes('ya no está disponible') || t.includes('esta reseña ya no')) {
        return 'deleted';
    }
    const labeled = document.querySelectorAll('[aria-label]');
    for (const el of labeled) {
        const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
        if (/\\d[,.]?\\d?\\s*(star|estrella)/.test(lbl)
            || lbl.includes('valoración de') || lbl.includes('rated ')) {
            return 'stars';
        }
    }
    return false;
}"""


# ─── Manejo de pantalla de consentimiento GDPR ───────────────────────────────
async def _handle_consent(page: Page) -> None:
    """
    Detecta y descarta la pantalla de consentimiento GDPR de Google.
    Obligatoria en servidores europeos (Railway europe-west4).
    """
    if "consent.google" not in page.url:
        return

    print(f"[consent] Detectada en: {page.url[:80]}", flush=True)

    # Intentar hacer clic en "Aceptar todo"
    for sel in [
        "#L2AGLb",
        "button.tHlp8d",
        "form[action*='consent'] button[jsname='b3VHJd']",
        "form[action*='consent'] button[type='submit']:last-of-type",
        "form[action*='consent'] button:last-of-type",
        "button[aria-label='Accept all']",
        "button[aria-label='Aceptar todo']",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                print(f"[consent] Clic en {sel}", flush=True)
                # Esperar a que la URL deje de ser consent.google
                try:
                    await page.wait_for_function(
                        "() => !window.location.href.includes('consent.google')",
                        timeout=10_000,
                    )
                except PlaywrightTimeout:
                    pass
                # Dar tiempo extra para que Maps cargue
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except Exception:
                    pass
                print(f"[consent] Tras clic: {page.url[:80]}", flush=True)
                return
        except Exception:
            continue

    print("[consent] No se encontró botón de aceptar", flush=True)


# ─── Extracción de texto de reseña ───────────────────────────────────────────
async def _extract_review_text(page: Page, page_text: str) -> str:
    """
    Intenta extraer el texto real de la reseña.
    Devuelve un snippet (≤300 chars) o cadena vacía si no encuentra nada.
    """
    # 1. Selectores específicos
    for sel in REVIEW_TEXT_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                text = (await el.inner_text()).strip()
                # El texto de la reseña tiene al menos 10 caracteres y varias palabras
                if len(text) >= 10 and len(text.split()) >= 3:
                    return text[:300]
        except Exception:
            continue

    # 2. Heurística: buscar en el texto de la página párrafos "tipo reseña"
    #    El texto de la reseña aparece después de las estrellas/autor, antes de "Útil"/"Helpful"
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]

    # Buscar dónde están las estrellas
    star_line_idx = -1
    for i, line in enumerate(lines):
        if STAR_TEXT_RE.search(line.lower()):
            star_line_idx = i
            break

    if star_line_idx >= 0:
        # Tomar las siguientes líneas como candidatas a texto de reseña
        candidates = []
        for line in lines[star_line_idx + 1 : star_line_idx + 10]:
            # Filtrar líneas que son botones/UI (muy cortas o palabras únicas)
            if len(line) >= 15 and len(line.split()) >= 4:
                # Excluir patrones UI conocidos
                lower = line.lower()
                if not any(p in lower for p in ["translate", "helpful", "útil", "flag", "google", "maps"]):
                    candidates.append(line)
        if candidates:
            return candidates[0][:300]

    return ""


# ─── Verificación de una URL ──────────────────────────────────────────────────
async def _check_single_review(page: Page, url: str) -> dict:
    """
    Devuelve:
      status      : ACTIVA | ELIMINADA | INCIERTA
      detail      : motivo legible
      evidence    : lista de señales encontradas
      review_text : snippet del texto de la reseña (o "" si no se encontró)
      final_url   : URL tras redirecciones
    """
    final_url = url
    review_text = ""
    try:
        # ── 1. Navegar ────────────────────────────────────────────────────────
        response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        final_url = page.url

        if response and response.status >= 400:
            return {
                "status": "INCIERTA",
                "detail": f"HTTP {response.status}",
                "evidence": [f"HTTP {response.status}"],
                "review_text": "",
                "final_url": final_url,
            }

        # ── 2. Descartar GDPR consent (crítico en EU) ─────────────────────────
        await _handle_consent(page)

        # ── 3. Esperar señal relevante (máx 15s) ─────────────────────────────
        wait_result = None
        try:
            wait_result = await page.wait_for_function(_WAIT_JS, timeout=15_000)
        except PlaywrightTimeout:
            pass

        # ── 4. Leer texto de la página ────────────────────────────────────────
        page_text = ""
        try:
            page_text = await page.inner_text("body")
        except Exception:
            pass
        text_lower = page_text.lower()

        print(f"[check] {url[:60]} → wait={wait_result} | text_len={len(page_text)}", flush=True)

        # ── 5. Buscar señales de ELIMINACIÓN ──────────────────────────────────
        deletion_evidence: list[str] = []
        for phrase, label in DELETED_PHRASES:
            if phrase in text_lower:
                deletion_evidence.append(label)

        if deletion_evidence:
            return {
                "status": "ELIMINADA",
                "detail": deletion_evidence[0],
                "evidence": deletion_evidence,
                "review_text": "",
                "final_url": final_url,
            }

        # ── 6. Intentar extraer texto de reseña ───────────────────────────────
        review_text = await _extract_review_text(page, page_text)
        print(f"[check] review_text='{review_text[:60]}'" , flush=True)

        if review_text:
            return {
                "status": "ACTIVA",
                "detail": f"Texto de reseña encontrado",
                "evidence": [f"Texto: \"{review_text[:80]}\""],
                "review_text": review_text,
                "final_url": final_url,
            }

        # ── 7. Señales de estrellas como respaldo ─────────────────────────────
        star_evidence: list[str] = []
        for sel in STAR_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    aria = await el.get_attribute("aria-label") or ""
                    star_evidence.append(f"aria-label: '{aria}'")
                    break
            except Exception:
                continue

        if not star_evidence:
            m = STAR_TEXT_RE.search(text_lower)
            if m:
                star_evidence.append(f"Puntuación en texto: '{m.group()}'")

        if star_evidence:
            return {
                "status": "ACTIVA",
                "detail": star_evidence[0],
                "evidence": star_evidence,
                "review_text": "",
                "final_url": final_url,
            }

        # ── 8. Sin evidencia clara → INCIERTA ─────────────────────────────────
        reason = (
            "Cargó Maps pero sin texto de reseña ni mensaje de eliminación"
            if len(page_text) > 200
            else "Timeout: la página no cargó contenido suficiente"
        )
        return {
            "status": "INCIERTA",
            "detail": reason,
            "evidence": [f"URL: {final_url[:100]}", f"Texto página: {len(page_text)} chars"],
            "review_text": "",
            "final_url": final_url,
        }

    except PlaywrightTimeout:
        return {
            "status": "INCIERTA",
            "detail": "Timeout de navegación (>20s)",
            "evidence": ["PlaywrightTimeout"],
            "review_text": "",
            "final_url": final_url,
        }
    except Exception as exc:
        return {
            "status": "INCIERTA",
            "detail": f"Error: {str(exc)[:120]}",
            "evidence": [repr(exc)[:200]],
            "review_text": "",
            "final_url": final_url,
        }


# ─── Stream de verificación ───────────────────────────────────────────────────
async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 3,
    delay_seconds: float = 1.5,
) -> AsyncGenerator[dict, None]:
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
        )
        # Bloquear recursos pesados para acelerar carga
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,mp3}",
            lambda route: route.abort(),
        )

        async def check_one(idx: int, item: dict):
            async with semaphore:
                if idx > 0:
                    await asyncio.sleep(delay_seconds)
                page = await context.new_page()
                try:
                    result = await _check_single_review(page, item["url"])
                    result = {**item, **result, "index": idx}
                except Exception as exc:
                    result = {
                        **item,
                        "status": "INCIERTA",
                        "detail": f"Fallo: {str(exc)[:100]}",
                        "evidence": [],
                        "review_text": "",
                        "index": idx,
                    }
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            await result_queue.put(result)

        tasks = [asyncio.create_task(check_one(i, item)) for i, item in enumerate(url_items)]

        received = 0
        while received < total:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=90.0)
                yield result
                received += 1
            except asyncio.TimeoutError:
                break

        await asyncio.gather(*tasks, return_exceptions=True)
        await context.close()
        await browser.close()


# ─── Compatibilidad con CLI ───────────────────────────────────────────────────
async def check_reviews(url_items, max_concurrent=3, delay_seconds=1.5, progress_callback=None):
    results = {}
    total = len(url_items)
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), total, result)
    return [results.get(i) for i in range(total)]
