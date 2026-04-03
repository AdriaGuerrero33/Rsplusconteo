"""
Módulo para leer y escribir en Google Sheets usando gspread.
"""

import json
import os
import re
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

GOOGLE_MAPS_URL_PATTERN = re.compile(
    r"https?://(maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|www\.google\.com/maps|g\.page)",
    re.IGNORECASE,
)


def authenticate(credentials_path: str) -> gspread.Client:
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if credentials_json:
        try:
            info = json.loads(credentials_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_CREDENTIALS_JSON contiene JSON inválido: {e}")
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(credentials_path):
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    else:
        raise FileNotFoundError(
            "No se encontraron credenciales. "
            "Configura la variable GOOGLE_CREDENTIALS_JSON en Railway."
        )
    return gspread.authorize(creds)


def open_sheet(gc: gspread.Client, sheet_url_or_id: str) -> gspread.Spreadsheet:
    if sheet_url_or_id.startswith("http"):
        return gc.open_by_url(sheet_url_or_id)
    return gc.open_by_key(sheet_url_or_id)


def get_worksheet(spreadsheet: gspread.Spreadsheet, tab_name: Optional[str]) -> gspread.Worksheet:
    if tab_name:
        return spreadsheet.worksheet(tab_name)
    return spreadsheet.get_worksheet(0)


def read_all_rows(worksheet: gspread.Worksheet) -> list[list[str]]:
    return worksheet.get_all_values()


def detect_url_column(rows: list[list[str]]) -> Optional[int]:
    if not rows:
        return None
    sample_rows = rows[:20]
    num_cols = max(len(row) for row in sample_rows) if sample_rows else 0
    col_hits = [0] * num_cols
    for row in sample_rows:
        for col_idx, cell in enumerate(row):
            if GOOGLE_MAPS_URL_PATTERN.search(str(cell)):
                col_hits[col_idx] += 1
    best_col = max(range(num_cols), key=lambda i: col_hits[i]) if num_cols else None
    if best_col is not None and col_hits[best_col] > 0:
        return best_col
    return None


def extract_urls(rows: list[list[str]], col_idx: int) -> list[dict]:
    results = []
    for row_idx, row in enumerate(rows):
        cell_value = row[col_idx].strip() if col_idx < len(row) else ""
        if not cell_value:
            continue
        if row_idx == 0 and not GOOGLE_MAPS_URL_PATTERN.search(cell_value):
            continue
        if GOOGLE_MAPS_URL_PATTERN.search(cell_value):
            results.append({"row": row_idx + 1, "col": col_idx, "url": cell_value, "row_data": row})
    return results


def get_or_create_results_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.clear()
        return ws
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=10)


def write_results(worksheet: gspread.Worksheet, results: list[dict], source_headers: Optional[list[str]] = None) -> None:
    header_row = list(source_headers) if source_headers else []
    header_row.extend(["Estado", "Detalle", "URL_Verificada"])
    rows_to_write = [header_row]
    for item in results:
        row_data = list(item.get("row_data", []))
        row_data.extend([item.get("status", "ERROR"), item.get("detail", ""), item.get("url", "")])
        rows_to_write.append(row_data)
    if rows_to_write:
        worksheet.update(rows_to_write, value_input_option="RAW")
    try:
        worksheet.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def write_si_no_to_source(
    worksheet: gspread.Worksheet,
    url_items: list[dict],
    results: list[dict],
) -> None:
    """
    Escribe SI/NO directamente en la hoja original, en la columna
    inmediatamente a la derecha de la URL.
      ACTIVA   → SI  (verde)
      cualquier otro estado → NO  (rojo)
    """
    import gspread.utils as gu

    updates = []
    fmt_green = {"backgroundColor": {"red": 0.85, "green": 0.97, "blue": 0.87},
                 "textFormat": {"bold": True, "foregroundColor": {"red": 0.02, "green": 0.37, "blue": 0.27}}}
    fmt_red   = {"backgroundColor": {"red": 1.0,  "green": 0.89, "blue": 0.89},
                 "textFormat": {"bold": True, "foregroundColor": {"red": 0.60, "green": 0.07, "blue": 0.07}}}

    fmt_requests = []
    for item, result in zip(url_items, results):
        row     = item["row"]           # 1-based
        col     = item["col"] + 2       # columna siguiente a la URL (1-based)
        status  = result.get("status", "INCIERTA")
        value   = "SI" if status == "ACTIVA" else "NO"
        cell_a1 = gu.rowcol_to_a1(row, col)
        updates.append({"range": cell_a1, "values": [[value]]})

        # Formato de color
        fmt = fmt_green if value == "SI" else fmt_red
        fmt_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col - 1,
                    "endColumnIndex": col,
                },
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")

    if fmt_requests:
        worksheet.spreadsheet.batch_update({"requests": fmt_requests})


def write_summary(worksheet: gspread.Worksheet, summary: dict, start_row: int) -> None:
    worksheet.append_rows([
        [], ["=== RESUMEN ==="],
        ["Total de enlaces procesados", summary.get("total", 0)],
        ["Reseñas ACTIVAS", summary.get("activas", 0)],
        ["Reseñas ELIMINADAS", summary.get("eliminadas", 0)],
        ["Errores / No verificadas", summary.get("errores", 0)],
    ], value_input_option="RAW")
