"""
Módulo para verificar si una reseña de Google Maps sigue activa o fue eliminada.
Usa httpx para seguir redirecciones HTTP directamente — mucho más rápido que Playwright.
"""

import asyncio
import re
from typing import AsyncGenerator
import httpx


DELETED_TEXT_PATTERNS = [
    r"content (is|not) available",
    r"no se (pudo|puede) encontrar",
    r"this (page|content) (doesn't|does not|isn't|is not) exist",
    r"error 404", r"not found", r"no encontrado", r"page not found",
    r"something went wrong", r"algo sali[oó] mal",
]
DELETED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DELETED_TEXT_PATTERNS]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _classify_final_url(final_url: str, status_code: int) -> dict:
    """Clasifica el resultado según la URL final y el código HTTP."""
    if status_code >= 400:
        return {"status": "ELIMINADA", "detail": f"HTTP {status_code}", "final_url": final_url}

    url_lower = final_url.lower()

    # Indicadores de reseña activa en la URL
    active_indicators = [
        "/maps/reviews/",
        "!3e1",
        "!3e2",
        "!3e3",
        "contrib",
        "reviewid",
        "/reviews/",
    ]
    for indicator in active_indicators:
        if indicator in url_lower:
            return {"status": "ACTIVA", "detail": "Reseña encontrada", "final_url": final_url}

    # Si redirige a Maps genérico sin reseña
    if "google.com/maps" in url_lower or "maps.google.com" in url_lower:
        return {"status": "ELIMINADA", "detail": "Redirige a Maps sin reseña", "final_url": final_url}

    # Si redirige a la página principal de Google
    if final_url.rstrip("/") in ("https://www.google.com", "https://google.com"):
        return {"status": "ELIMINADA", "detail": "Redirige a Google.com", "final_url": final_url}

    return {"status": "ERROR", "detail": f"Redirección inesperada: {final_url[:80]}", "final_url": final_url}


async def _check_single_review(client: httpx.AsyncClient, url: str) -> dict:
    try:
        response = await client.get(url, follow_redirects=True, timeout=12)
        final_url = str(response.url)
        return _classify_final_url(final_url, response.status_code)
    except httpx.TimeoutException:
        return {"status": "ERROR", "detail": "Tiempo de espera agotado", "final_url": url}
    except httpx.TooManyRedirects:
        return {"status": "ERROR", "detail": "Demasiadas redirecciones", "final_url": url}
    except Exception as exc:
        return {"status": "ERROR", "detail": str(exc)[:100], "final_url": url}


async def check_reviews_stream(
    url_items: list[dict],
    max_concurrent: int = 5,
    delay_seconds: float = 0.5,
) -> AsyncGenerator[dict, None]:
    """
    Async generator que verifica URLs y devuelve resultados uno a uno conforme terminan.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    result_queue: asyncio.Queue = asyncio.Queue()
    total = len(url_items)

    async with httpx.AsyncClient(headers=HEADERS, max_redirects=10) as client:

        async def check_one(idx: int, item: dict):
            async with semaphore:
                if idx > 0 and delay_seconds > 0:
                    await asyncio.sleep(delay_seconds * (idx % max_concurrent))
                try:
                    check_result = await _check_single_review(client, item["url"])
                    result = {**item, **check_result, "index": idx}
                except Exception as e:
                    result = {**item, "status": "ERROR", "detail": str(e)[:100], "index": idx}
            await result_queue.put(result)

        tasks = [asyncio.create_task(check_one(i, item)) for i, item in enumerate(url_items)]

        received = 0
        while received < total:
            try:
                result = await asyncio.wait_for(result_queue.get(), timeout=30.0)
                yield result
                received += 1
            except asyncio.TimeoutError:
                break

        await asyncio.gather(*tasks, return_exceptions=True)


# Compatibilidad con CLI (main.py)
async def check_reviews(url_items, max_concurrent=5, delay_seconds=0.5, progress_callback=None):
    results = {}
    total = len(url_items)
    async for result in check_reviews_stream(url_items, max_concurrent, delay_seconds):
        idx = result.get("index", 0)
        results[idx] = result
        if progress_callback:
            progress_callback(len(results), total, result)
    return [results.get(i) for i in range(total)]
