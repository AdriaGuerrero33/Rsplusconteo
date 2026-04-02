"""
Módulo para verificar si una reseña de Google Maps sigue activa o fue eliminada.
"""

import asyncio
import re
from typing import AsyncGenerator
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout


DELETED_TEXT_PATTERNS = [
    r"content (is|not) available",
    r"no se (pudo|puede) encontrar",
    r"this (page|content) (doesn't|does not|isn't|is not) exist",
    r"error 404", r"not found", r"no encontrado", r"page not found",
    r"something went wrong", r"algo sali[oó] mal",
]
DELETED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DELETED_TEXT_PATTERNS]

REVIEW_SELECTORS = [
    "[data-review-id]",
    "span[data-expandable-section]",
    "span[aria-label*='estrellas']",
    "span[aria-label*='stars']",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _check_single_review(page: Page, url: str, timeout_ms: int = 25000) -> dict:
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3000)

        http_status = response.status if response else None
        final_url = page.url

        if http_status and http_status >= 400:
            return {"status": "ELIMINADA", "detail": f"HTTP {http_status}", "final_url": final_url}

        page_text = (await page.inner_text("body")).lower()

        for pattern in DELETED_PATTERNS:
            if pattern.search(page_text):
                return {"status": "ELIMINADA", "detail": "Texto de error detectado", "final_url": final_url}

        is_review_url = "/maps/reviews" in final_url or "!3e" in final_url or "review" in final_url.lower()

        review_found = False
        for selector in REVIEW_SELECTORS:
            try:
                if await page.query_selector(selector):
                    review_found = True
                    break
            except Exception:
                continue

        if review_found or is_review_url:
            return {"status": "ACTIVA", "detail": "Reseña encontrada", "final_url": final_url}

        if "google.com/maps" in final_url:
            return {"status": "ELIMINADA", "detail": "Página Maps sin reseña", "final_url": final_url}

        return {"status": "ERROR", "detail": f"Redirección inesperada: {final_url[:80]}", "final_url": final_url}

    except PlaywrightTimeout:
        return {"status": "ERROR", "detail": "Tiempo de espera agotado", "final_url": url}
    except Exception as exc:
        return {"status": "ERROR", "detail": str(exc)[:100], "final_url": url}


async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 3,
    delay_seconds: float = 2.0,
) -> AsyncGenerator[dict, None]:
    """
    Async generator que verifica URLs y devuelve resultados uno a uno conforme terminan.
    Esto permite hacer streaming real de los resultados al cliente.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="es-ES",
        )
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )

        async def check_one(idx: int, item: dict):
            async with semaphore:
                if idx > 0:
                    await asyncio.sleep(delay_seconds)
                page = await context.new_page()
                try:
                    check_result = await _check_single_review(page, item["url"])
                    result = {**item, **check_result, "index": idx}
                except Exception as e:
                    result = {**item, "status": "ERROR", "detail": str(e)[:100], "index": idx}
                finally:
                    await page.close()
                await result_queue.put(result)

        # Lanzar todas las tareas
        tasks = [asyncio.create_task(check_one(i, item)) for i, item in enumerate(url_items)]

        # Emitir resultados conforme llegan
        received = 0
        while received < total:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=90.0)
                yield result
                received += 1
            except asyncio.TimeoutError:
                break

        # Esperar a que terminen todas
        await asyncio.gather(*tasks, return_exceptions=True)
        await context.close()
        await browser.close()


# Mantener compatibilidad con main.py (CLI)
async def check_reviews(url_items, max_concurrent=3, delay_seconds=2.0, progress_callback=None):
    results = {}
    total = len(url_items)
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), total, result)
    return [results.get(i) for i in range(total)]
