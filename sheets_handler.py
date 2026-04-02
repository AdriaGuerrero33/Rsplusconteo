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

# Patrón para detectar URLs de reseñas de Google Maps
GOOGLE_MAPS_URL_PATTERN = re.compile(
    r"https?://(maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|www\.google\.com/maps|g\.page)",
    re.IGNORECASE,
)


def authenticate(credentials_path: str) -> gspread.Client:
    """
    Autentica con la API de Google Sheets usando un Service Account.
    Soporta credenciales desde archivo JSON o desde la variable de entorno
    GOOGLE_CREDENTIALS_JSON (para despliegues en la nube).
    """
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(gc: gspread.Client, sheet_url_or_id: str) -> gspread.Spreadsheet:
    """Abre una hoja de cálculo por URL o por ID."""
    if sheet_url_or_id.startswith("http"):
        return gc.open_by_url(sheet_url_or_id)
    return gc.open_by_key(sheet_url_or_id)


def get_worksheet(spreadsheet: gspread.Spreadsheet, tab_name: Optional[str]) -> gspread.Worksheet:
    """Devuelve la pestaña indicada o la primera si no se especifica nombre."""
    if tab_name:
        return spreadsheet.worksheet(tab_name)
    return spreadsheet.get_worksheet(0)


def read_all_rows(worksheet: gspread.Worksheet) -> list[list[str]]:
    """Lee todas las filas de la hoja como lista de listas."""
    return worksheet.get_all_values()


def detect_url_column(rows: list[list[str]]) -> Optional[int]:
    """
    Detecta automáticamente qué columna contiene URLs de Google Maps.
    Devuelve el índice de columna (0-based) o None si no encuentra ninguna.
    """
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
    """
    Extrae las URLs de la columna indicada junto con su número de fila.
    Omite la primera fila si parece ser un encabezado.
    """
    results = []
    for row_idx, row in enumerate(rows):
        cell_value = row[col_idx].strip() if col_idx < len(row) else ""
        if not cell_value:
            continue
        if row_idx == 0 and not GOOGLE_MAPS_URL_PATTERN.search(cell_value):
            continue
        if GOOGLE_MAPS_URL_PATTERN.search(cell_value):
            results.append({
                "row": row_idx + 1,
                "url": cell_value,
                "row_data": row,
            })
    return results


def get_or_create_results_tab(
    spreadsheet: gspread.Spreadsheet, tab_name: str
) -> gspread.Worksheet:
    """Crea la pestaña de resultados si no existe, o la limpia si ya existe."""
    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.clear()
        return ws
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=10)


def write_results(
    worksheet: gspread.Worksheet,
    results: list[dict],
    source_headers: Optional[list[str]] = None,
) -> None:
    header_row = []
    if source_headers:
        header_row = list(source_headers)
    extra_cols = ["Estado", "Detalle", "URL_Verificada"]
    header_row.extend(extra_cols)
    rows_to_write = [header_row]
    for item in results:
        row_data = list(item.get("row_data", []))
        row_data.append(item.get("status", "ERROR"))
        row_data.append(item.get("detail", ""))
        row_data.append(item.get("url", ""))
        rows_to_write.append(row_data)
    if rows_to_write:
        worksheet.update(rows_to_write, value_input_option="RAW")
    try:
        worksheet.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def write_summary(worksheet: gspread.Worksheet, summary: dict, start_row: int) -> None:
    summary_rows = [
        [],
        ["=== RESUMEN ==="],
        ["Total de enlaces procesados", summary.get("total", 0)],
        ["Reseñas ACTIVAS", summary.get("activas", 0)],
        ["Reseñas ELIMINADAS", summary.get("eliminadas", 0)],
        ["Errores / No verificadas", summary.get("errores", 0)],
    ]
    worksheet.append_rows(summary_rows, value_input_option="RAW")
