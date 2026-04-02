"""
Verificador de reseñas Google Maps — lógica robusta con evidencia real.

Clasificación:
  ACTIVA    — evidencia positiva fuerte (estrellas visibles del review, texto confirmado)
  ELIMINADA — evidencia negativa clara (texto "no longer available" u equivalentes)
  INCIERTA  — no hay evidencia suficiente (carga parcial, timeout, contenido ambiguo)

Estrategia:
  1. Navegar con domcontentloaded
  2. Esperar señal específica: mensaje de eliminación O elemento de estrellas del review
     (NO usar longitud de body, que dispara con contenido de cabecera)
  3. Descartar pantallas de consentimiento GDPR si aparecen
  4. Analizar evidencias positivas y negativas por separado
  5. Clasificar de forma conservadora: ACTIVA solo con evidencia fuerte
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

# ─── Frases que Google Maps muestra cuando la reseña fue eliminada ────────────
DELETED_PHRASES: list[tuple[str, str]] = [
    ("no longer available",         "EN: 'no longer available'"),
    ("review is no longer",         "EN: 'review is no longer'"),
    ("ya no está disponible",       "ES: 'ya no está disponible'"),
    ("esta reseña ya no",           "ES: 'esta reseña ya no'"),
    ("reseña no disponible",        "ES: 'reseña no disponible'"),
    ("cette avis n'est plus",       "FR: 'cette avis n'est plus'"),
    ("cette critique n'est plus",   "FR: 'cette critique n'est plus'"),
    ("diese rezension ist nicht",   "DE: 'diese rezension ist nicht'"),
]

# ─── Selectores CSS que aparecen SOLO si hay contenido de reseña activa ───────
ACTIVE_STAR_SELECTORS = [
    "span[aria-label$=' stars']",
    "span[aria-label$=' star']",
    "span[aria-label$=' estrellas']",
    "span[aria-label$=' estrella']",
    "div[aria-label$=' stars']",
    "div[aria-label$=' estrellas']",
    "[aria-label*='Valoración de']",
    "[aria-label*='Rated ']",
    "[aria-label*='rating of']",
]

STAR_TEXT_RE = re.compile(r"\b\d[,.]?\d?\s*(estrellas?|stars?)\b", re.IGNORECASE)

_WAIT_CONDITION_JS = """() => {
    const text = (document.body && document.body.innerText || '').toLowerCase();

    if (text.includes('no longer available')
        || text.includes('review is no longer')
        || text.includes('ya no está disponible')
        || text.includes('esta reseña ya no')
        || text.includes('reseña no disponible')) {
        return true;
    }

    const labeled = document.querySelectorAll('[aria-label]');
    for (const el of labeled) {
        const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
        if (/\\d[,.]?\\d?\\s*(star|estrella)/.test(lbl)
            || lbl.includes('valoración de')
            || lbl.includes('rated ')
            || lbl.includes('rating of')) {
            return true;
        }
    }

    return false;
}"""

_CONSENT_SELECTORS = [
    "button[aria-label='Accept all']",
    "button[aria-label='Aceptar todo']",
    "button[aria-label='Tout accepter']",
    "form[action*='consent'] button:last-of-type",
    "#L2AGLb",
]


async def _dismiss_consent(page: Page) -> None:
    for sel in _CONSENT_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def _check_single_review(page: Page, url: str) -> dict:
    final_url = url
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        final_url = page.url

        if response and response.status >= 400:
            return {
                "status": "INCIERTA",
                "detail": f"HTTP {response.status} al cargar la URL",
                "evidence": [f"HTTP status: {response.status}"],
                "final_url": final_url,
            }

        await _dismiss_consent(page)

        wait_triggered = False
        try:
            await page.wait_for_function(_WAIT_CONDITION_JS, timeout=12_000)
            wait_triggered = True
        except PlaywrightTimeout:
            pass

        page_text = ""
        try:
            page_text = await page.inner_text("body")
        except Exception:
            pass
        text_lower = page_text.lower()

        deletion_evidence: list[str] = []
        for phrase, label in DELETED_PHRASES:
            if phrase in text_lower:
                deletion_evidence.append(label)

        active_evidence: list[str] = []

        for sel in ACTIVE_STAR_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    aria = await el.get_attribute("aria-label") or ""
                    active_evidence.append(f"Elemento con aria-label: '{aria}' ({sel})")
                    break
            except Exception:
                continue

        if not active_evidence:
            m = STAR_TEXT_RE.search(text_lower)
            if m:
                active_evidence.append(f"Puntuación en texto visible: '{m.group()}'")

        if deletion_evidence:
            return {
                "status": "ELIMINADA",
                "detail": deletion_evidence[0],
                "evidence": deletion_evidence,
                "final_url": final_url,
            }

        if active_evidence:
            return {
                "status": "ACTIVA",
                "detail": active_evidence[0],
                "evidence": active_evidence,
                "final_url": final_url,
            }

        reason = (
            "Cargó Maps pero sin señales de reseña activa ni eliminada"
            if wait_triggered
            else "Tiempo de espera agotado sin señal de contenido"
        )
        return {
            "status": "INCIERTA",
            "detail": reason,
            "evidence": [f"URL final: {final_url[:100]}", f"wait_triggered={wait_triggered}"],
            "final_url": final_url,
        }

    except PlaywrightTimeout:
        return {
            "status": "INCIERTA",
            "detail": "Tiempo de navegación agotado (>20s)",
            "evidence": ["PlaywrightTimeout en goto"],
            "final_url": final_url,
        }
    except Exception as exc:
        return {
            "status": "INCIERTA",
            "detail": f"Error inesperado: {str(exc)[:120]}",
            "evidence": [repr(exc)[:200]],
            "final_url": final_url,
        }


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
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,mp3,avi}",
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
                        "detail": f"Fallo interno: {str(exc)[:100]}",
                        "evidence": [],
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


async def check_reviews(url_items, max_concurrent=3, delay_seconds=1.5, progress_callback=None):
    results = {}
    total = len(url_items)
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), total, result)
    return [results.get(i) for i in range(total)]
