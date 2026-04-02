"""
Módulo para verificar si una reseña de Google Maps sigue activa o fue eliminada.
Usa Playwright (navegador headless) para cargar la página y analizar el contenido.
"""

import asyncio
import re
from typing import Optional
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout


# --- Indicadores de reseña ELIMINADA o no disponible ---
DELETED_TEXT_PATTERNS = [
    r"content (is|not) available",
    r"no se (pudo|puede) encontrar",
    r"this (page|content) (doesn't|does not|isn't|is not) exist",
    r"error 404",
    r"not found",
    r"no encontrado",
    r"page not found",
    r"something went wrong",
    r"algo sali[oó] mal",
    r"no (hay|existe) (ninguna|esta) rese[ñn]a",
]

DELETED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DELETED_TEXT_PATTERNS]

# Selectores CSS que indican que la reseña está cargada y visible
REVIEW_SELECTORS = [
    # Contenedor principal de reseña en vista de ficha de negocio
    "[data-review-id]",
    # Texto de la reseña
    "span[data-expandable-section]",
    # Puntuación de estrella en una reseña individual
    "span[aria-label*='estrellas']",
    "span[aria-label*='stars']",
    # Nombre del autor en la vista de reseña
    "div[class*='reviewer']",
    # Contenedor de reseña en la vista de Maps
    "div[jscontroller][data-hveid]",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _check_single_review(
    page: Page,
    url: str,
    timeout_ms: int = 20000,
) -> dict:
    """
    Navega a una URL de reseña y determina si está activa o eliminada.

    Retorna un dict con:
        status: "ACTIVA" | "ELIMINADA" | "ERROR"
        detail: descripción adicional
        final_url: URL final tras redirecciones
    """
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Breve espera para que cargue el contenido dinámico
        await page.wait_for_timeout(3000)

        http_status = response.status if response else None
        final_url = page.url

        # 1. Verificar HTTP status explícitamente incorrecto
        if http_status and http_status >= 400:
            return {
                "status": "ELIMINADA",
                "detail": f"HTTP {http_status}",
                "final_url": final_url,
            }

        page_text = await page.inner_text("body")
        page_text_lower = page_text.lower()

        # 2. Buscar patrones de contenido eliminado/no disponible
        for pattern in DELETED_PATTERNS:
            if pattern.search(page_text_lower):
                return {
                    "status": "ELIMINADA",
                    "detail": f"Texto indicador encontrado: '{pattern.pattern}'",
                    "final_url": final_url,
                }

        # 3. Comprobar si la URL final perdió los parámetros de reseña
        #    Los links de reseña suelen redirigir a una URL con /reviews o data= con la reseña
        is_review_url = (
            "/maps/reviews" in final_url
            or "!3e" in final_url  # indicador de review en encoded URL
            or "review" in final_url.lower()
        )

        # 4. Buscar elementos DOM específicos de reseña
        review_element_found = False
        for selector in REVIEW_SELECTORS:
            try:
                elem = await page.query_selector(selector)
                if elem:
                    review_element_found = True
                    break
            except Exception:
                continue

        if review_element_found or is_review_url:
            return {
                "status": "ACTIVA",
                "detail": "Reseña encontrada en la página",
                "final_url": final_url,
            }

        # 5. Heurística de fallback: si la página cargó correctamente sin señales de eliminación,
        #    necesitamos más contexto. Revisamos si parece una página de negocio sin review destacada.
        if "google.com/maps" in final_url or "maps.app.goo.gl" in final_url:
            # La URL sigue siendo de Maps pero no encontramos la reseña
            return {
                "status": "ELIMINADA",
                "detail": "La página cargó pero no se encontró la reseña",
                "final_url": final_url,
            }

        # Si redirigió a algo completamente diferente, marcar como error
        return {
            "status": "ERROR",
            "detail": f"Redirección inesperada a: {final_url[:100]}",
            "final_url": final_url,
        }

    except PlaywrightTimeout:
        return {
            "status": "ERROR",
            "detail": "Tiempo de espera agotado al cargar la página",
            "final_url": url,
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "detail": f"Excepción: {str(exc)[:100]}",
            "final_url": url,
        }


async def check_reviews(
    url_items: list[dict],
    max_concurrent: int = 3,
    delay_seconds: float = 2.0,
    progress_callback=None,
) -> list[dict]:
    """
    Verifica una lista de reseñas en paralelo (con límite de concurrencia).

    Args:
        url_items: Lista de dicts con al menos la clave "url".
        max_concurrent: Cuántos navegadores/páginas abrir en paralelo.
        delay_seconds: Pausa entre lanzamientos para no sobrecargar Google.
        progress_callback: Función opcional callback(current, total, item) para mostrar progreso.

    Returns:
        La misma lista con los campos "status", "detail" y "final_url" añadidos.
    """
    results = [None] * len(url_items)
    semaphore = asyncio.Semaphore(max_concurrent)
    total = len(url_items)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="es-ES",
        )

        # Bloquear recursos innecesarios para mayor velocidad
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )

        async def verify_item(idx: int, item: dict):
            async with semaphore:
                if idx > 0:
                    await asyncio.sleep(delay_seconds)

                page = await context.new_page()
                try:
                    check_result = await _check_single_review(page, item["url"])
                    result = {**item, **check_result}
                    results[idx] = result

                    if progress_callback:
                        progress_callback(idx + 1, total, result)
                finally:
                    await page.close()

        tasks = [verify_item(i, item) for i, item in enumerate(url_items)]
        await asyncio.gather(*tasks)

        await context.close()
        await browser.close()

    return results
