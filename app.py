"""
Servidor web para el agente de verificación de reseñas de Google Maps.
"""

import asyncio
import json
import os
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from dotenv import load_dotenv
from pydantic import BaseModel

import sheets_handler
from review_checker import check_reviews_stream

load_dotenv()

app = FastAPI()

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
RESULTS_TAB = os.getenv("RESULTS_TAB", "Estado_Reseñas")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CHECKS", "3"))
DELAY = float(os.getenv("DELAY_BETWEEN_CHECKS", "1.5"))


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_PING = "data: {\"type\":\"ping\"}\n\n"
KEEPALIVE_INTERVAL = 8.0  # segundos entre pings si no llega ningún resultado


async def _with_keepalive(source: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """
    Envuelve un async generator de SSE y emite pings periódicos si no llega
    ningún evento en KEEPALIVE_INTERVAL segundos. Evita que Railway/nginx
    corte la conexión SSE por inactividad durante el procesamiento con Playwright.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _consume():
        try:
            async for item in source:
                await queue.put(("item", item))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(_consume())
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                if kind == "done":
                    break
                yield value
            except asyncio.TimeoutError:
                yield SSE_PING
    finally:
        await task


def _empty_counters() -> dict:
    return {"activas": 0, "eliminadas": 0, "inciertas": 0}


def _count(counters: dict, status: str) -> None:
    if status == "ACTIVA":
        counters["activas"] += 1
    elif status == "ELIMINADA":
        counters["eliminadas"] += 1
    else:
        counters["inciertas"] += 1


async def run_agent(sheet_url: str) -> AsyncGenerator[str, None]:
    yield sse({"type": "status", "message": "Conectando con Google Sheets..."})

    try:
        gc = sheets_handler.authenticate(CREDENTIALS_PATH)
        spreadsheet = sheets_handler.open_sheet(gc, sheet_url)
        worksheet = sheets_handler.get_worksheet(spreadsheet, None)
    except Exception as e:
        yield sse({"type": "error", "message": f"Error al conectar: {str(e)}"})
        return

    yield sse({"type": "status", "message": f'Conectado a "{spreadsheet.title}"'})
    yield sse({"type": "status", "message": "Leyendo datos..."})

    try:
        rows = sheets_handler.read_all_rows(worksheet)
    except Exception as e:
        yield sse({"type": "error", "message": f"Error al leer: {str(e)}"})
        return

    if not rows:
        yield sse({"type": "error", "message": "La hoja está vacía."})
        return

    col_idx = sheets_handler.detect_url_column(rows)
    if col_idx is None:
        yield sse({"type": "error", "message": "No se encontró columna con URLs de Google Maps."})
        return

    headers = rows[0] if rows else []
    col_name = headers[col_idx] if col_idx < len(headers) else f"Columna {col_idx + 1}"
    url_items = sheets_handler.extract_urls(rows, col_idx)
    total = len(url_items)

    yield sse({"type": "status", "message": f'Columna "{col_name}" — {total} reseñas'})
    yield sse({"type": "total", "total": total})

    if total == 0:
        yield sse({"type": "error", "message": "No hay URLs para verificar."})
        return

    yield sse({"type": "status", "message": f"Verificando {total} reseñas..."})

    counters = _empty_counters()
    results = []
    current = 0

    async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
        current += 1
        status = result.get("status", "INCIERTA")
        _count(counters, status)
        results.append(result)

        yield sse({
            "type": "progress",
            "current": current,
            "total": total,
            "status": status,
            "url": result.get("url", "")[:80],
            "detail": result.get("detail", ""),
            "evidence": result.get("evidence", []),
            **counters,
        })

    yield sse({"type": "status", "message": f'Escribiendo en "{RESULTS_TAB}"...'})

    try:
        results_ws = sheets_handler.get_or_create_results_tab(spreadsheet, RESULTS_TAB)
        sheets_handler.write_results(results_ws, results, source_headers=rows[0] if rows else None)
        sheets_handler.write_summary(results_ws, {"total": total, **counters}, start_row=total + 3)
    except Exception as e:
        yield sse({"type": "error", "message": f"Error al escribir: {str(e)}"})
        return

    yield sse({
        "type": "done",
        "total": total,
        **counters,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit",
        "results_tab": RESULTS_TAB,
    })


async def run_direct(urls: list[str]) -> AsyncGenerator[str, None]:
    url_items = [{"url": u.strip(), "row_data": [u.strip()]} for u in urls if u.strip()]
    total = len(url_items)

    if total == 0:
        yield sse({"type": "error", "message": "No hay URLs para verificar."})
        return

    yield sse({"type": "total", "total": total})
    yield sse({"type": "status", "message": f"Iniciando verificación de {total} URLs..."})

    counters = _empty_counters()
    results = []
    current = 0

    async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
        current += 1
        status = result.get("status", "INCIERTA")
        _count(counters, status)
        results.append(result)

        yield sse({
            "type": "progress",
            "current": current,
            "total": total,
            "status": status,
            "url": result.get("url", "")[:80],
            "detail": result.get("detail", ""),
            "evidence": result.get("evidence", []),
            **counters,
        })

    rows_output = [
        {
            "url": r.get("url", ""),
            "status": r.get("status", "INCIERTA"),
            "detail": r.get("detail", ""),
            "evidence": r.get("evidence", []),
        }
        for r in results if r
    ]

    yield sse({
        "type": "done",
        "total": total,
        **counters,
        "sheet_url": None,
        "results_tab": None,
        "rows": rows_output,
    })


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    has_creds = bool(os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip())
    return JSONResponse({"status": "ok", "credentials_env_set": has_creds})


@app.get("/check")
async def check(sheet_url: str, request: Request):
    return StreamingResponse(
        _with_keepalive(run_agent(sheet_url)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class DirectCheckRequest(BaseModel):
    urls: list[str]


@app.post("/check-direct")
async def check_direct(body: DirectCheckRequest):
    return StreamingResponse(
        _with_keepalive(run_direct(body.urls)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
