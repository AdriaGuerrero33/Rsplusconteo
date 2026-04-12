"""
Seguimiento de envíos de URLs por contacto (persona).

Cada URL puede haber sido enviada por varias personas en las últimas 2 semanas.
Si la misma URL la envía una persona distinta a quien ya la había enviado antes,
se marca como conflicto (ERRONEA) con el motivo explicado.

Persistencia: contacts_data.json (14 días de retención automática).
En Railway con volumen persistente, configura CONTACTS_FILE=/data/contacts.json
"""

import json
import os
from datetime import datetime, timezone, timedelta

CONTACTS_FILE  = os.getenv("CONTACTS_FILE", "contacts_data.json")
RETENTION_DAYS = 14


# ─── Carga / Guardado ─────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(CONTACTS_FILE):
        try:
            with open(CONTACTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"submissions": {}}   # url_normalizada → [{name, ts, date}]


def _save(data: dict) -> None:
    try:
        with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[contacts] Error guardando: {e}", flush=True)


def _cleanup(data: dict) -> dict:
    """Elimina entradas más antiguas que RETENTION_DAYS días."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    for url in list(data["submissions"].keys()):
        data["submissions"][url] = [
            s for s in data["submissions"][url]
            if s.get("ts", "") >= cutoff
        ]
        if not data["submissions"][url]:
            del data["submissions"][url]
    return data


def _norm(url: str) -> str:
    return url.strip().lower().rstrip("/")


# ─── API pública ──────────────────────────────────────────────────────────────

def record_submission(url: str, contact_name: str, review_text: str = "") -> list[dict]:
    """
    Registra que `contact_name` envió `url` hoy (con texto de reseña opcional).

    Retorna lista de envíos previos de OTRAS personas distintas (conflicto por URL).
    Cada elemento: {"name": "Pepe", "date": "24/11/2024", "ts": "..."}
    """
    if not contact_name or not url:
        return []

    data  = _cleanup(_load())
    key   = _norm(url)
    now   = datetime.now(timezone.utc)

    if key not in data["submissions"]:
        data["submissions"][key] = []

    # Otros contactos que enviaron esta misma URL (diferente nombre)
    others = [
        s for s in data["submissions"][key]
        if s["name"].strip().lower() != contact_name.strip().lower()
    ]

    # Registrar este envío (deduplicar si el mismo contacto ya la envió hoy)
    same_person_today = any(
        s["name"].strip().lower() == contact_name.strip().lower()
        and s["ts"][:10] == now.date().isoformat()
        for s in data["submissions"][key]
    )
    if not same_person_today:
        data["submissions"][key].append({
            "name":        contact_name.strip(),
            "ts":          now.isoformat(),
            "date":        now.strftime("%d/%m/%Y"),
            "review_text": review_text.strip()[:500],
        })

    _save(data)
    return others


def find_text_conflicts(review_text: str, contact_name: str, threshold: float = 0.85) -> list[dict]:
    """
    Busca en toda la base de datos envíos de OTRAS personas cuyo texto de reseña
    sea similar al `review_text` dado (aunque la URL sea diferente).

    Retorna lista de {"name", "date", "url"} de los conflictos encontrados.
    Vacía si no hay coincidencias.
    """
    from difflib import SequenceMatcher

    if not review_text or len(review_text.strip().split()) < 6:
        return []

    def _norm_text(t: str) -> str:
        return " ".join(t.lower().split())

    me   = contact_name.strip().lower()
    data = _cleanup(_load())
    norm_new = _norm_text(review_text)
    conflicts: list[dict] = []

    for url_key, subs in data["submissions"].items():
        for s in subs:
            if s["name"].strip().lower() == me:
                continue
            stored_text = s.get("review_text", "").strip()
            if not stored_text or len(stored_text.split()) < 6:
                continue
            ratio = SequenceMatcher(None, norm_new, _norm_text(stored_text)).ratio()
            if ratio >= threshold:
                conflicts.append({
                    "name": s["name"],
                    "date": s["date"],
                    "url":  url_key,
                })
                break  # un conflicto por URL almacenada es suficiente

    return conflicts


def get_all_submissions(url: str) -> list[dict]:
    """Retorna todos los envíos conocidos para una URL (últimas 2 semanas)."""
    data = _cleanup(_load())
    return data["submissions"].get(_norm(url), [])


def conflict_note(others: list[dict], match_type: str = "url") -> str:
    """
    Genera el texto del motivo de conflicto a mostrar en la UI.
    match_type='url'  → "URL enviada también por Pepe (24/11/2024)"
    match_type='text' → "Texto de reseña idéntico enviado por Pepe (24/11/2024)"
    """
    if not others:
        return ""
    parts = [f"{o['name']} (el {o['date']})" for o in others]
    joined = " y ".join(parts)
    if match_type == "text":
        return "Texto de reseña idéntico enviado también por " + joined
    return "URL enviada también por " + joined


def reset_all() -> int:
    """
    Borra todos los registros de contactos/URLs guardados.
    Retorna el número de entradas eliminadas.
    """
    data  = _load()
    count = sum(len(v) for v in data.get("submissions", {}).values())
    _save({"submissions": {}})
    print(f"[contacts] Memoria reseteada — {count} registros eliminados", flush=True)
    return count


def recent_contacts(days: int = 14) -> list[dict]:
    """
    Retorna lista de contactos únicos que han enviado URLs en los últimos `days` días.
    Útil para el autocompletado del campo de nombre.
    """
    data    = _cleanup(_load())
    cutoff  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    seen: dict[str, str] = {}  # name_lower → name_original
    for subs in data["submissions"].values():
        for s in subs:
            if s.get("ts", "") >= cutoff:
                seen[s["name"].lower()] = s["name"]
    return sorted(seen.values(), key=str.lower)
