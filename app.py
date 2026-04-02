"""
Servidor web para el agente de verificación de reseñas de Google Maps.
Ejecutar con: python app.py
Abrir en el navegador: http://localhost:8000
"""

import asyncio
import json
import os
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from dotenv import load_dotenv

import sheets_handler
from review_checker import check_reviews

load_dotenv()

app = FastAPI()

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
RESULTS_TAB = os.getenv("RESULTS_TAB", "Estado_Reseñas")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CHECKS", "3"))
DELAY = float(os.getenv("DELAY_BETWEEN_CHECKS", "2"))


def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def run_agent(sheet_url: str) -> AsyncGenerator[str, None]:
    async def emit(event_type: str, **kwargs):
        yield sse_event({"type": event_type, **kwargs})

    async for chunk in emit("status", message="Conectando con Google Sheets..."):
        yield chunk

    try:
        gc = sheets_handler.authenticate(CREDENTIALS_PATH)
        spreadsheet = sheets_handler.open_sheet(gc, sheet_url)
        worksheet = sheets_handler.get_worksheet(spreadsheet, None)
    except Exception as e:
        async for chunk in emit("error", message=f"Error al conectar con Google Sheets: {str(e)}"):
            yield chunk
        return

    sheet_title = spreadsheet.title
    async for chunk in emit("status", message=f'Conectado a "{sheet_title}" → pestaña "{worksheet.title}"'):
        yield chunk

    async for chunk in emit("status", message="Leyendo datos de la hoja..."):
        yield chunk

    try:
        rows = sheets_handler.read_all_rows(worksheet)
    except Exception as e:
        async for chunk in emit("error", message=f"Error al leer la hoja: {str(e)}"):
            yield chunk
        return

    if not rows:
        async for chunk in emit("error", message="La hoja está vacía."):
            yield chunk
        return

    col_idx = sheets_handler.detect_url_column(rows)
    if col_idx is None:
        async for chunk in emit("error", message="No se encontró ninguna columna con URLs de Google Maps."):
            yield chunk
        return

    headers = rows[0] if rows else []
    col_name = headers[col_idx] if col_idx < len(headers) else f"Columna {col_idx + 1}"
    url_items = sheets_handler.extract_urls(rows, col_idx)
    total = len(url_items)

    async for chunk in emit("status", message=f'Columna detectada: "{col_name}" — {total} reseñas encontradas'):
        yield chunk
    async for chunk in emit("total", total=total):
        yield chunk

    if total == 0:
        async for chunk in emit("error", message="No hay URLs de reseñas para verificar."):
            yield chunk
        return

    async for chunk in emit("status", message=f"Verificando {total} reseñas con Playwright..."):
        yield chunk

    results_so_far = []
    counters = {"activas": 0, "eliminadas": 0, "errores": 0}
    progress_queue: asyncio.Queue = asyncio.Queue()

    def on_progress(current: int, total_: int, item: dict):
        status = item.get("status", "ERROR")
        if status == "ACTIVA":
            counters["activas"] += 1
        elif status == "ELIMINADA":
            counters["eliminadas"] += 1
        else:
            counters["errores"] += 1
        results_so_far.append(item)
        progress_queue.put_nowait({
            "type": "progress",
            "current": current,
            "total": total_,
            "status": status,
            "url": item.get("url", "")[:80],
            "detail": item.get("detail", ""),
            "activas": counters["activas"],
            "eliminadas": counters["eliminadas"],
            "errores": counters["errores"],
        })

    agent_task = asyncio.create_task(
        check_reviews(url_items, MAX_CONCURRENT, DELAY, progress_callback=on_progress)
    )

    processed = 0
    while processed < total:
        try:
            event = await asyncio.wait_for(progress_queue.get(), timeout=60.0)
            yield sse_event(event)
            processed += 1
        except asyncio.TimeoutError:
            break

    results = await agent_task

    async for chunk in emit("status", message=f'Escribiendo resultados en la pestaña "{RESULTS_TAB}"...'):
        yield chunk

    try:
        results_ws = sheets_handler.get_or_create_results_tab(spreadsheet, RESULTS_TAB)
        source_headers = rows[0] if rows else None
        sheets_handler.write_results(results_ws, results, source_headers=source_headers)
        summary = {
            "total": total,
            "activas": counters["activas"],
            "eliminadas": counters["eliminadas"],
            "errores": counters["errores"],
        }
        sheets_handler.write_summary(results_ws, summary, start_row=total + 3)
    except Exception as e:
        async for chunk in emit("error", message=f"Error al escribir resultados: {str(e)}"):
            yield chunk
        return

    sheet_id = spreadsheet.id
    results_sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    async for chunk in emit("done",
        total=total,
        activas=counters["activas"],
        eliminadas=counters["eliminadas"],
        errores=counters["errores"],
        sheet_url=results_sheet_url,
        results_tab=RESULTS_TAB,
    ):
        yield chunk


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/check")
async def check(sheet_url: str, request: Request):
    return StreamingResponse(
        run_agent(sheet_url),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def run_direct(urls: list[str]) -> AsyncGenerator[str, None]:
    """Verifica una lista de URLs directas sin necesitar Google Sheets."""

    async def emit(event_type: str, **kwargs):
        yield sse_event({"type": event_type, **kwargs})

    total = len(urls)
    if total == 0:
        async for chunk in emit("error", message="No hay URLs para verificar."):
            yield chunk
        return

    url_items = [{"url": u.strip(), "row_data": [u.strip()]} for u in urls if u.strip()]
    total = len(url_items)

    async for chunk in emit("total", total=total):
        yield chunk
    async for chunk in emit("status", message=f"Verificando {total} URLs con Playwright..."):
        yield chunk

    counters = {"activas": 0, "eliminadas": 0, "errores": 0}
    progress_queue: asyncio.Queue = asyncio.Queue()

    def on_progress(current: int, total_: int, item: dict):
        status = item.get("status", "ERROR")
        if status == "ACTIVA":
            counters["activas"] += 1
        elif status == "ELIMINADA":
            counters["eliminadas"] += 1
        else:
            counters["errores"] += 1
        progress_queue.put_nowait({
            "type": "progress",
            "current": current,
            "total": total_,
            "status": status,
            "url": item.get("url", "")[:80],
            "detail": item.get("detail", ""),
            "activas": counters["activas"],
            "eliminadas": counters["eliminadas"],
            "errores": counters["errores"],
        })

    agent_task = asyncio.create_task(
        check_reviews(url_items, MAX_CONCURRENT, DELAY, progress_callback=on_progress)
    )

    processed = 0
    while processed < total:
        try:
            event = await asyncio.wait_for(progress_queue.get(), timeout=60.0)
            yield sse_event(event)
            processed += 1
        except asyncio.TimeoutError:
            break

    results = await agent_task

    # Construir tabla de resultados para mostrar en la UI
    rows_output = []
    for r in results:
        if r:
            rows_output.append({
                "url": r.get("url", ""),
                "status": r.get("status", "ERROR"),
                "detail": r.get("detail", ""),
            })

    async for chunk in emit("done",
        total=total,
        activas=counters["activas"],
        eliminadas=counters["eliminadas"],
        errores=counters["errores"],
        sheet_url=None,
        results_tab=None,
        rows=rows_output,
    ):
        yield chunk


from pydantic import BaseModel

class DirectCheckRequest(BaseModel):
    urls: list[str]


@app.post("/check-direct")
async def check_direct(body: DirectCheckRequest, request: Request):
    return StreamingResponse(
        run_direct(body.urls),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
