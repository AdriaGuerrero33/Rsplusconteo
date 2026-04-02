"""
Servidor web — modelo polling (no SSE) para robustez en Railway.

Flujo:
  POST /start-job-sheets  →  devuelve job_id, lanza tarea en background
  POST /start-job-direct  →  devuelve job_id, lanza tarea en background
  GET  /job-status/{id}   →  devuelve resultados nuevos desde ?since=N
"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
from pydantic import BaseModel

import sheets_handler
from review_checker import check_reviews_stream

load_dotenv()

app = FastAPI()

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
RESULTS_TAB      = os.getenv("RESULTS_TAB", "Estado_Reseñas")
MAX_CONCURRENT   = int(os.getenv("MAX_CONCURRENT_CHECKS", "3"))
DELAY            = float(os.getenv("DELAY_BETWEEN_CHECKS", "1.5"))


# ── Estado de trabajos en memoria ─────────────────────────────────────────────

@dataclass
class JobState:
    total:       int       = 0
    results:     list      = field(default_factory=list)
    counters:    dict      = field(default_factory=lambda: {"activas": 0, "eliminadas": 0, "inciertas": 0})
    done:        bool      = False
    error:       str|None  = None
    sheet_url:   str|None  = None
    results_tab: str|None  = None
    log:         list      = field(default_factory=list)

_jobs: dict[str, JobState] = {}


def _count(counters: dict, status: str) -> None:
    if   status == "ACTIVA":    counters["activas"]    += 1
    elif status == "ELIMINADA": counters["eliminadas"] += 1
    else:                       counters["inciertas"]  += 1


# ── Trabajos en background ────────────────────────────────────────────────────

async def _run_sheets_job(job_id: str, sheet_url: str) -> None:
    job = _jobs[job_id]
    try:
        job.log.append("Conectando con Google Sheets...")
        gc          = sheets_handler.authenticate(CREDENTIALS_PATH)
        spreadsheet = sheets_handler.open_sheet(gc, sheet_url)
        worksheet   = sheets_handler.get_worksheet(spreadsheet, None)
        job.log.append(f'Conectado a "{spreadsheet.title}"')
        job.log.append("Leyendo datos...")

        rows = sheets_handler.read_all_rows(worksheet)
        if not rows:
            job.error = "La hoja está vacía."; job.done = True; return

        col_idx = sheets_handler.detect_url_column(rows)
        if col_idx is None:
            job.error = "No se encontró columna con URLs de Google Maps."; job.done = True; return

        headers   = rows[0] if rows else []
        col_name  = headers[col_idx] if col_idx < len(headers) else f"Columna {col_idx+1}"
        url_items = sheets_handler.extract_urls(rows, col_idx)
        job.total = len(url_items)
        job.log.append(f'Columna "{col_name}" — {job.total} reseñas')

        async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
            status = result.get("status", "INCIERTA")
            _count(job.counters, status)
            job.results.append({
                "url":         result.get("url", ""),
                "status":      status,
                "detail":      result.get("detail", ""),
                "evidence":    result.get("evidence", []),
                "review_text": result.get("review_text", ""),
            })

        job.log.append(f'Escribiendo en "{RESULTS_TAB}"...')
        ws_out = sheets_handler.get_or_create_results_tab(spreadsheet, RESULTS_TAB)
        sheets_handler.write_results(ws_out,
            [{**r, "row_data": [r["url"]]} for r in job.results],
            source_headers=rows[0] if rows else None)
        sheets_handler.write_summary(ws_out,
            {"total": job.total, **job.counters}, start_row=job.total + 3)

        job.sheet_url   = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit"
        job.results_tab = RESULTS_TAB
        job.done        = True

    except Exception as exc:
        job.error = str(exc)[:300]
        job.done  = True


async def _run_direct_job(job_id: str, urls: list[str]) -> None:
    job = _jobs[job_id]
    try:
        url_items = [{"url": u.strip(), "row_data": [u.strip()]} for u in urls if u.strip()]
        job.total = len(url_items)
        job.log.append(f"Verificando {job.total} URLs...")

        async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
            status = result.get("status", "INCIERTA")
            _count(job.counters, status)
            job.results.append({
                "url":         result.get("url", ""),
                "status":      status,
                "detail":      result.get("detail", ""),
                "evidence":    result.get("evidence", []),
                "review_text": result.get("review_text", ""),
            })

        job.done = True

    except Exception as exc:
        job.error = str(exc)[:300]
        job.done  = True


# ── Endpoints HTTP ────────────────────────────────────────────────────────────

class SheetsRequest(BaseModel):
    sheet_url: str

class DirectRequest(BaseModel):
    urls: list[str]


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    has_creds = bool(os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip())
    return JSONResponse({"status": "ok", "credentials_env_set": has_creds})


@app.post("/start-job-sheets")
async def start_job_sheets(body: SheetsRequest):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobState()
    asyncio.create_task(_run_sheets_job(job_id, body.sheet_url))
    return {"job_id": job_id}


@app.post("/start-job-direct")
async def start_job_direct(body: DirectRequest):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobState()
    asyncio.create_task(_run_direct_job(job_id, body.urls))
    return {"job_id": job_id}


@app.get("/job-status/{job_id}")
async def job_status(job_id: str, since: int = 0):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {
        "total":       job.total,
        "counters":    job.counters,
        "done":        job.done,
        "error":       job.error,
        "new_results": job.results[since:],
        "offset":      len(job.results),
        "log":         job.log,
        "sheet_url":   job.sheet_url,
        "results_tab": job.results_tab,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
