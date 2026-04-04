"""
Sistema de aprendizaje continuo.

Almacena correcciones del usuario y las usa para mejorar futuras clasificaciones:
  - Textos que fueron marcados como ACTIVA pero corregidos a otra cosa → lista negra
  - Ejemplos de correcciones → few-shot prompting en Claude Vision
  - Estadísticas de precisión

Persistencia: archivo JSON en LEARNING_FILE (por defecto learning_data.json).
En Railway, montar un volumen en /data y configurar LEARNING_FILE=/data/learned.json
para persistencia entre deploys.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

LEARNING_FILE = os.getenv("LEARNING_FILE", "learning_data.json")

# ─── Carga / Guardado ─────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(LEARNING_FILE):
        try:
            with open(LEARNING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "corrections": [],          # lista de {url, auto_status, correct_status, review_text, ...}
        "bad_texts": [],            # textos que causaron falsos ACTIVA
        "bad_text_fragments": [],   # fragmentos/subcadenas de UI de Google
        "stats": {"total": 0, "auto_correct": 0, "corrected": 0},
    }


def _save(data: dict) -> None:
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[learning] Error guardando: {e}", flush=True)


# ─── API pública ──────────────────────────────────────────────────────────────

def add_correction(
    url: str,
    auto_status: str,
    correct_status: str,
    review_text: str = "",
    detail: str = "",
    source: str = "manual",
) -> None:
    """
    Guarda una corrección del usuario.
    Si el texto era un falso ACTIVA, lo añade a la lista negra.
    """
    data = _load()

    # Estadísticas
    data["stats"]["total"] += 1
    if auto_status == correct_status:
        data["stats"]["auto_correct"] += 1
    else:
        data["stats"]["corrected"] += 1

    correction = {
        "url": url,
        "auto_status": auto_status,
        "correct_status": correct_status,
        "review_text": review_text,
        "detail": detail,
        "source": source,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    data["corrections"].append(correction)

    # Si fue ACTIVA pero el usuario dijo INCIERTA/ELIMINADA/ERRONEA:
    # el texto que usamos como "reseña" era basura → lista negra
    if auto_status == "ACTIVA" and correct_status in ("INCIERTA", "ELIMINADA", "ERRONEA"):
        rt = (review_text or "").strip()
        if rt and rt not in data["bad_texts"] and len(rt.split()) <= 8:
            # Solo textos cortos (los largos son probablemente reseñas reales mal clasificadas)
            data["bad_texts"].append(rt)
            print(f"[learning] Texto añadido a lista negra: '{rt}'", flush=True)

    _save(data)
    print(
        f"[learning] Corrección guardada: {auto_status}→{correct_status} | {url[:60]}",
        flush=True,
    )


def get_bad_texts() -> list[str]:
    """Textos que nunca deben clasificarse como ACTIVA."""
    return _load().get("bad_texts", [])


def get_few_shot_examples(limit: int = 4) -> list[dict]:
    """
    Retorna las últimas correcciones donde auto != correct.
    Usadas como ejemplos few-shot en el prompt de Claude Vision.
    """
    corrections = _load().get("corrections", [])
    relevant = [c for c in corrections if c["auto_status"] != c["correct_status"]]
    return relevant[-limit:]


def get_stats() -> dict:
    data = _load()
    stats = data.get("stats", {})
    total = stats.get("total", 0)
    corrected = stats.get("corrected", 0)
    accuracy = round((total - corrected) / total * 100, 1) if total > 0 else None
    return {
        "total_feedback": total,
        "auto_correct": stats.get("auto_correct", 0),
        "corrected_by_user": corrected,
        "accuracy_pct": accuracy,
        "bad_texts_learned": len(data.get("bad_texts", [])),
        "total_corrections": len(data.get("corrections", [])),
    }


def build_vision_few_shot() -> str:
    """
    Construye texto adicional para el prompt de Vision con ejemplos aprendidos.
    """
    examples = get_few_shot_examples(limit=4)
    if not examples:
        return ""

    lines = ["\n\nLearned corrections from previous human reviews (use these to calibrate):"]
    for ex in examples:
        rt = ex.get("review_text", "")
        snippet = f' (text seen: "{rt[:60]}")' if rt else ""
        lines.append(
            f'- URL auto-classified as {ex["auto_status"]} but human corrected to '
            f'{ex["correct_status"]}{snippet}'
        )
    return "\n".join(lines)
