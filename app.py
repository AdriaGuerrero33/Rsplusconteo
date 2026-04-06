"""
Servidor web — modelo polling para robustez en Railway.

Flujo:
  POST /start-job-sheets  →  devuelve job_id, lanza tarea en background
  POST /start-job-direct  →  devuelve job_id, lanza tarea en background
  GET  /job-status/{id}   →  devuelve resultados nuevos desde ?since=N
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from dotenv import load_dotenv
from pydantic import BaseModel

import sheets_handler
import learning  as _learning
import contacts  as _contacts
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
    total:        int       = 0
    results:      list      = field(default_factory=list)
    counters:     dict      = field(default_factory=lambda: {
                                  "activas": 0, "eliminadas": 0, "inciertas": 0,
                                  "duplicadas": 0, "erroneas": 0})
    done:         bool      = False
    error:        str|None  = None
    sheet_url:    str|None  = None
    results_tab:  str|None  = None
    log:          list      = field(default_factory=list)
    screenshots:  dict      = field(default_factory=dict)  # idx → jpeg bytes
    contact_name: str       = ""

_jobs: dict[str, JobState] = {}


def _count(counters: dict, status: str) -> None:
    if   status == "ACTIVA":     counters["activas"]    += 1
    elif status == "ELIMINADA":  counters["eliminadas"] += 1
    elif status == "DUPLICADA":  counters["duplicadas"] += 1
    elif status == "ERRONEA":    counters["erroneas"]   += 1
    else:                        counters["inciertas"]  += 1


def _store_result(job: "JobState", result: dict, contact_name: str = "") -> None:
    """
    Almacena un resultado en el job, aplicando:
    - detección de conflicto entre contactos (misma URL enviada por personas distintas)
    - detección de conflicto por texto similar entre contactos distintos
    - guardado de screenshot si la reseña es INCIERTA
    """
    status      = result.get("status", "INCIERTA")
    url         = result.get("url", "")
    review_text = result.get("review_text", "")

    # ── Detección de conflicto entre contactos ────────────────────────────────
    conflict_submitters: list[dict] = []
    match_type = "url"

    if contact_name and url:
        # 1. Conflicto por URL idéntica
        conflict_submitters = _contacts.record_submission(url, contact_name, review_text)

        # 2. Conflicto por texto similar (URLs distintas, misma reseña)
        if not conflict_submitters and review_text:
            text_conflicts = _contacts.find_text_conflicts(review_text, contact_name)
            if text_conflicts:
                conflict_submitters = text_conflicts
                match_type = "text"

    if conflict_submitters:
        note = _contacts.conflict_note(conflict_submitters, match_type=match_type)
        status = "ERRONEA"
        result["status"] = "ERRONEA"
        result["detail"] = note
        result["contact_conflict"] = True
        result["conflict_submitters"] = conflict_submitters

    _count(job.counters, status)

    # ── Screenshot para revisión manual ──────────────────────────────────────
    sc_bytes = result.pop("_screenshot", None)
    idx = len(job.results)
    if sc_bytes and status == "INCIERTA":
        job.screenshots[idx] = sc_bytes

    job.results.append({
        "url":                result.get("url", ""),
        "status":             status,
        "detail":             result.get("detail", ""),
        "evidence":           result.get("evidence", []),
        "review_text":        result.get("review_text", ""),
        "rating":             result.get("rating", 0),
        "contact_name":       contact_name,
        "contact_conflict":   result.get("contact_conflict", False),
        "conflict_submitters": result.get("conflict_submitters", []),
        "has_screenshot":     idx in job.screenshots,
    })


def _mark_duplicates(job: "JobState") -> None:
    """
    Detecta duplicadas dentro del mismo job:
    1. Misma URL exacta → el segundo es DUPLICADA del primero
    2. Texto de reseña ≥95% similar → el posterior es DUPLICADA del anterior
    Los conflictos entre contactos (ERRONEA) no se sobrescriben.
    """
    import re as _re
    from difflib import SequenceMatcher

    _biz = _re.compile(r'^[A-ZÁÉÍÓÚÑ][^a-záéíóúñ]{2,}\s*[-–—]', _re.UNICODE)

    def _norm(t: str) -> str:
        return " ".join(t.lower().split())

    def _apply(dup_idx: int, orig_idx: int, reason: str) -> None:
        r = job.results[dup_idx]
        if r.get("contact_conflict"):
            return  # no sobreescribir conflictos entre contactos
        old = r["status"]
        if old == "ACTIVA":    job.counters["activas"]    -= 1
        elif old == "ERRONEA": job.counters["erroneas"]   -= 1
        elif old == "INCIERTA":job.counters["inciertas"]  -= 1
        r["status"] = "DUPLICADA"
        r["detail"] = reason
        job.counters["duplicadas"] += 1

    duplicate_of: dict[int, int] = {}

    # 1. URL idéntica
    url_seen: dict[str, int] = {}
    for i, r in enumerate(job.results):
        url = (r.get("url") or "").strip().lower()
        if not url:
            continue
        if url in url_seen:
            duplicate_of[i] = url_seen[url]
        else:
            url_seen[url] = i

    # 2. Texto similar
    candidates: list[tuple[int, str]] = []
    for i, r in enumerate(job.results):
        if i in duplicate_of or r.get("contact_conflict"):
            continue
        text = (r.get("review_text") or "").strip()
        if not text or len(text.split()) < 6 or _biz.match(text):
            continue
        candidates.append((i, _norm(text)))

    for a in range(len(candidates)):
        idx_a, txt_a = candidates[a]
        if idx_a in duplicate_of:
            continue
        for b in range(a + 1, len(candidates)):
            idx_b, txt_b = candidates[b]
            if idx_b in duplicate_of:
                continue
            if SequenceMatcher(None, txt_a, txt_b).ratio() >= 0.95:
                duplicate_of[idx_b] = idx_a

    for dup_idx, orig_idx in duplicate_of.items():
        url_d = (job.results[dup_idx].get("url") or "").strip().lower()
        url_o = (job.results[orig_idx].get("url") or "").strip().lower()
        reason = (f"URL idéntica a reseña #{orig_idx+1}"
                  if url_d == url_o else
                  f"Texto idéntico a reseña #{orig_idx+1}")
        _apply(dup_idx, orig_idx, reason)


# ── Trabajos en background ────────────────────────────────────────────────────

async def _run_sheets_job(job_id: str, sheet_url: str) -> None:
    job = _jobs[job_id]
    contact = job.contact_name
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
        job.log.append(f'Columna "{col_name}" — {job.total} reseñas'
                       + (f' · Contacto: {contact}' if contact else ''))

        async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
            _store_result(job, result, contact)

        job.log.append("Escribiendo SI/NO en la hoja original...")
        sheets_handler.write_si_no_to_source(worksheet, url_items, job.results)

        job.log.append(f'Escribiendo resumen en "{RESULTS_TAB}"...')
        ws_out = sheets_handler.get_or_create_results_tab(spreadsheet, RESULTS_TAB)
        sheets_handler.write_results(ws_out,
            [{**r, "row_data": [r["url"]]} for r in job.results],
            source_headers=rows[0] if rows else None)
        sheets_handler.write_summary(ws_out,
            {"total": job.total, **job.counters}, start_row=job.total + 3)

        _mark_duplicates(job)
        job.sheet_url   = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit"
        job.results_tab = RESULTS_TAB
        job.done        = True

    except Exception as exc:
        job.error = str(exc)[:300]
        job.done  = True


async def _run_direct_job(job_id: str, urls: list[str]) -> None:
    job = _jobs[job_id]
    contact = job.contact_name
    try:
        url_items = [{"url": u.strip(), "row_data": [u.strip()]} for u in urls if u.strip()]
        job.total = len(url_items)
        job.log.append(f"Verificando {job.total} URLs"
                       + (f" · Contacto: {contact}" if contact else "") + "...")

        async for result in check_reviews_stream(url_items, MAX_CONCURRENT, DELAY):
            _store_result(job, result, contact)

        _mark_duplicates(job)
        job.done = True

    except Exception as exc:
        job.error = str(exc)[:300]
        job.done  = True


# ── Endpoints HTTP ────────────────────────────────────────────────────────────

class SheetsRequest(BaseModel):
    sheet_url:    str
    contact_name: str = ""

class DirectRequest(BaseModel):
    urls:         list[str]
    contact_name: str = ""

class FeedbackRequest(BaseModel):
    url:            str
    auto_status:    str
    correct_status: str
    review_text:    str = ""
    detail:         str = ""
    source:         str = "manual"


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    has_creds   = bool(os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip())
    has_api_key = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return JSONResponse({"status": "ok", "credentials_env_set": has_creds,
                         "vision_active": has_api_key})


@app.get("/job/{job_id}/screenshot/{idx}")
async def get_screenshot(job_id: str, idx: int):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    sc = job.screenshots.get(idx)
    if sc is None:
        raise HTTPException(status_code=404, detail="Screenshot not available")
    return Response(content=sc, media_type="image/jpeg")


@app.post("/start-job-sheets")
async def start_job_sheets(body: SheetsRequest):
    job_id = str(uuid.uuid4())
    job = JobState()
    job.contact_name = body.contact_name.strip()
    _jobs[job_id] = job
    asyncio.create_task(_run_sheets_job(job_id, body.sheet_url))
    return {"job_id": job_id}


@app.post("/start-job-direct")
async def start_job_direct(body: DirectRequest):
    job_id = str(uuid.uuid4())
    job = JobState()
    job.contact_name = body.contact_name.strip()
    _jobs[job_id] = job
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
        "job_id":      job_id,
        "all_results": job.results if job.done else None,
    }


@app.post("/feedback")
async def save_feedback(body: FeedbackRequest):
    _learning.add_correction(
        url=body.url,
        auto_status=body.auto_status,
        correct_status=body.correct_status,
        review_text=body.review_text,
        detail=body.detail,
        source=body.source,
    )
    return {"ok": True, "stats": _learning.get_stats()}


@app.get("/learning-stats")
async def learning_stats():
    return _learning.get_stats()


@app.get("/contacts/recent")
async def contacts_recent():
    """Retorna contactos recientes para autocompletado."""
    return {"contacts": _contacts.recent_contacts(days=14)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
