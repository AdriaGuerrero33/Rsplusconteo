#!/usr/bin/env python3
"""
Agente de verificación de reseñas de Google Maps.

Uso:
    python main.py --sheet <URL_O_ID_DEL_SHEET>

    O configurar SHEET_URL en el archivo .env y ejecutar:
    python main.py
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

import sheets_handler
from review_checker import check_reviews

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verifica qué reseñas de Google Maps siguen activas o han sido eliminadas."
    )
    parser.add_argument(
        "--sheet",
        default=os.getenv("SHEET_URL", ""),
        help="URL o ID de la hoja de Google Sheets.",
    )
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
        help="Ruta al archivo JSON de credenciales del Service Account.",
    )
    parser.add_argument(
        "--tab",
        default=os.getenv("SOURCE_SHEET_TAB", ""),
        help="Nombre de la pestaña fuente (vacío = primera pestaña).",
    )
    parser.add_argument(
        "--results-tab",
        default=os.getenv("RESULTS_TAB", "Estado_Reseñas"),
        help="Nombre de la pestaña donde se escribirán los resultados.",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=int(os.getenv("MAX_CONCURRENT_CHECKS", "3")),
        help="Número máximo de verificaciones en paralelo.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.getenv("DELAY_BETWEEN_CHECKS", "2")),
        help="Segundos de espera entre verificaciones.",
    )
    return parser.parse_args()


def print_progress(current: int, total: int, item: dict):
    status = item.get("status", "?")
    url = item.get("url", "")[:60]
    status_icon = {"ACTIVA": "✓", "ELIMINADA": "✗", "ERROR": "?"}.get(status, "?")
    print(f"  [{current:>3}/{total}] {status_icon} {status:<10}  {url}")


async def run(args):
    # --- Validaciones previas ---
    if not args.sheet:
        print("ERROR: Debes indicar la URL o ID del Sheet con --sheet o en el .env (SHEET_URL).")
        sys.exit(1)

    if not os.path.exists(args.credentials):
        print(f"ERROR: No se encontró el archivo de credenciales: {args.credentials}")
        print("Consulta el README.md para instrucciones sobre cómo crear un Service Account.")
        sys.exit(1)

    print("=" * 60)
    print("  Agente de Verificación de Reseñas de Google Maps")
    print("=" * 60)

    # --- Autenticación y apertura del Sheet ---
    print("\n[1/4] Conectando con Google Sheets...")
    gc = sheets_handler.authenticate(args.credentials)
    spreadsheet = sheets_handler.open_sheet(gc, args.sheet)
    print(f"      Hoja: \"{spreadsheet.title}\"")

    worksheet = sheets_handler.get_worksheet(spreadsheet, args.tab or None)
    print(f"      Pestaña fuente: \"{worksheet.title}\"")

    # --- Lectura de datos ---
    print("\n[2/4] Leyendo datos de la hoja...")
    rows = sheets_handler.read_all_rows(worksheet)
    if not rows:
        print("ERROR: La hoja está vacía.")
        sys.exit(1)
    print(f"      {len(rows)} filas encontradas.")

    # Detectar columna de URLs
    col_idx = sheets_handler.detect_url_column(rows)
    if col_idx is None:
        print("ERROR: No se encontró ninguna columna con URLs de Google Maps.")
        print("Asegúrate de que la hoja contiene enlaces tipo https://maps.app.goo.gl/...")
        sys.exit(1)

    # Determinar nombre de columna
    headers = rows[0] if rows else []
    col_name = headers[col_idx] if col_idx < len(headers) else f"Columna {col_idx + 1}"
    print(f"      Columna de URLs detectada: \"{col_name}\" (columna {col_idx + 1})")

    url_items = sheets_handler.extract_urls(rows, col_idx)
    print(f"      {len(url_items)} URL(s) de reseñas encontradas.")

    if not url_items:
        print("No hay URLs de reseñas para verificar. Saliendo.")
        sys.exit(0)

    # --- Verificación con Playwright ---
    print(f"\n[3/4] Verificando {len(url_items)} reseñas (paralelo={args.concurrent}, "
          f"delay={args.delay}s)...")
    print("      Esto puede tardar varios minutos dependiendo del número de reseñas.\n")

    start_time = datetime.now()
    results = await check_reviews(
        url_items,
        max_concurrent=args.concurrent,
        delay_seconds=args.delay,
        progress_callback=print_progress,
    )
    elapsed = (datetime.now() - start_time).total_seconds()

    # --- Estadísticas ---
    activas = sum(1 for r in results if r and r.get("status") == "ACTIVA")
    eliminadas = sum(1 for r in results if r and r.get("status") == "ELIMINADA")
    errores = sum(1 for r in results if r and r.get("status") == "ERROR")
    total = len(results)

    print(f"\n      Completado en {elapsed:.1f}s")
    print(f"      Total procesadas : {total}")
    print(f"      ✓ Activas         : {activas}")
    print(f"      ✗ Eliminadas      : {eliminadas}")
    print(f"      ? Errores         : {errores}")

    # --- Escritura de resultados en el Sheet ---
    print(f"\n[4/4] Escribiendo resultados en la pestaña \"{args.results_tab}\"...")
    results_ws = sheets_handler.get_or_create_results_tab(spreadsheet, args.results_tab)
    source_headers = rows[0] if rows else None
    sheets_handler.write_results(results_ws, results, source_headers=source_headers)

    summary = {"total": total, "activas": activas, "eliminadas": eliminadas, "errores": errores}
    sheets_handler.write_summary(results_ws, summary, start_row=len(results) + 3)

    print(f"      Resultados escritos correctamente.")
    print(f"\n  Abre la pestaña \"{args.results_tab}\" en tu hoja para ver el detalle.\n")
    print("=" * 60)


def main():
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
