"""
Video cost prediction + accuracy tracking for the Command Center.

The workflow this supports: predict a video's $ cost BEFORE generating any
shots, re-estimate at up to two checkpoints while cutting (as real spend
becomes visible), then log the actual final cost. Over time this builds a
real accuracy record -- "we predicted $X, we were off by Y%, here's why" --
so future predictions get sharper instead of staying a guess forever.

Dollars only (AI-gen credits converted to $, ElevenLabs character cost) --
deliberately NOT tracking labor/time, per the owner's call.

Same Airtable-table-per-feature pattern as social.py/assets.py: one row per
project, auto-created table, graceful "not connected" if Airtable isn't
configured.
"""

import httpx

from . import crm
from .crm import _formula_literal

COST_TABLE = "Video Cost Log"

_cost_table_id_cache = None


def _ensure_cost_table() -> str:
    """Return the Video Cost Log table id, creating it if needed."""
    global _cost_table_id_cache
    if _cost_table_id_cache:
        return _cost_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables", headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == COST_TABLE.lower():
                _cost_table_id_cache = t["id"]
                return _cost_table_id_cache
        # Primary (first) field must be a plain text type in Airtable.
        fields = [
            {"name": "Project", "type": "singleLineText"},
            {"name": "Predicted Cost", "type": "number", "options": {"precision": 2}},
            {"name": "Prediction Notes", "type": "multilineText"},
            {"name": "Checkpoint 1 Cost", "type": "number", "options": {"precision": 2}},
            {"name": "Checkpoint 1 Notes", "type": "multilineText"},
            {"name": "Checkpoint 2 Cost", "type": "number", "options": {"precision": 2}},
            {"name": "Checkpoint 2 Notes", "type": "multilineText"},
            {"name": "Actual Cost", "type": "number", "options": {"precision": 2}},
            {"name": "Variance", "type": "singleLineText"},
            {"name": "Lesson", "type": "multilineText"},
            {"name": "Status", "type": "singleSelect", "options": {"choices": [
                {"name": "Predicted"}, {"name": "In Progress"}, {"name": "Completed"}]}},
        ]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": COST_TABLE, "fields": fields})
        r.raise_for_status()
        _cost_table_id_cache = r.json()["id"]
        return _cost_table_id_cache


def _find_project_record(project: str):
    """Return the Airtable record dict for a project name (case-insensitive
    exact match), or None. Raises on transport failure."""
    tid = _ensure_cost_table()
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                  params={"filterByFormula": f"LOWER({{Project}})='{_formula_literal(project.strip().lower())}'",
                          "pageSize": "1"})
        r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0] if recs else None


def predict_cost(project: str, predicted_cost: float, notes: str = "") -> str:
    """Log the initial cost prediction for a new video project, BEFORE any
    generation starts. Creates the project row. Never raises."""
    if not crm.is_configured():
        return "The cost log isn't available (Airtable not connected)."
    if not project.strip():
        return "I need a project name to log a prediction."
    try:
        existing = _find_project_record(project)
        if existing:
            return (f"\"{project.strip()}\" already has a prediction logged "
                     f"(${existing['fields'].get('Predicted Cost', '?')}). Use a checkpoint "
                     f"instead, or start a new project under a different name.")
        tid = _ensure_cost_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {
                           "Project": project.strip()[:200],
                           "Predicted Cost": round(predicted_cost, 2),
                           "Prediction Notes": notes.strip()[:2000],
                           "Status": "Predicted",
                       }, "typecast": True})
            r.raise_for_status()
        return f"Logged: \"{project.strip()}\" predicted at ${predicted_cost:.2f}."
    except Exception as e:
        return f"Couldn't log that prediction: {type(e).__name__}: {e}"


def log_checkpoint(project: str, checkpoint: int, current_cost: float, notes: str = "") -> str:
    """Log a mid-cut cost re-estimate (checkpoint 1 or 2) against an existing
    prediction. Never raises."""
    if not crm.is_configured():
        return "The cost log isn't available (Airtable not connected)."
    if checkpoint not in (1, 2):
        return "Checkpoint must be 1 or 2."
    try:
        rec = _find_project_record(project)
        if not rec:
            return (f"No prediction found for \"{project.strip()}\" -- log one with "
                     f"predict_cost first.")
        tid = _ensure_cost_table()
        field_cost = f"Checkpoint {checkpoint} Cost"
        field_notes = f"Checkpoint {checkpoint} Notes"
        with httpx.Client(timeout=30) as c:
            c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{rec['id']}", headers=crm._headers(),
                    json={"fields": {
                        field_cost: round(current_cost, 2),
                        field_notes: notes.strip()[:2000],
                        "Status": "In Progress",
                    }, "typecast": True})
        predicted = rec["fields"].get("Predicted Cost")
        drift = f" ({current_cost - predicted:+.2f} vs. the ${predicted:.2f} prediction)" if predicted is not None else ""
        return f"Checkpoint {checkpoint} logged for \"{project.strip()}\": ${current_cost:.2f}{drift}."
    except Exception as e:
        return f"Couldn't log that checkpoint: {type(e).__name__}: {e}"


def log_actual(project: str, actual_cost: float, lesson: str = "") -> str:
    """Log the final actual cost once a project is done, and compute the
    variance against the original prediction. Never raises."""
    if not crm.is_configured():
        return "The cost log isn't available (Airtable not connected)."
    try:
        rec = _find_project_record(project)
        if not rec:
            return (f"No prediction found for \"{project.strip()}\" -- log one with "
                     f"predict_cost first, even if the video's already done.")
        tid = _ensure_cost_table()
        predicted = rec["fields"].get("Predicted Cost")
        if predicted:
            diff = actual_cost - predicted
            pct = (diff / predicted * 100) if predicted else 0
            variance = f"{diff:+.2f} ({pct:+.0f}%)"
        else:
            variance = "n/a (no prediction on file)"
        with httpx.Client(timeout=30) as c:
            c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{rec['id']}", headers=crm._headers(),
                    json={"fields": {
                        "Actual Cost": round(actual_cost, 2),
                        "Variance": variance,
                        "Lesson": lesson.strip()[:2000],
                        "Status": "Completed",
                    }, "typecast": True})
        return (f"\"{project.strip()}\" completed: predicted ${predicted or 0:.2f}, "
                f"actual ${actual_cost:.2f}, variance {variance}.")
    except Exception as e:
        return f"Couldn't log the actual cost: {type(e).__name__}: {e}"


def get_accuracy(project: str = "") -> str:
    """Report prediction-vs-actual accuracy for one project, or a summary
    across all completed ones if no project is named. Never raises."""
    if not crm.is_configured():
        return "The cost log isn't available (Airtable not connected)."
    try:
        tid = _ensure_cost_table()
        if project.strip():
            rec = _find_project_record(project)
            if not rec:
                return f"No cost log found for \"{project.strip()}\"."
            f = rec["fields"]
            lines = [f"{f.get('Project')}: predicted ${f.get('Predicted Cost', 0):.2f}"]
            if f.get("Checkpoint 1 Cost") is not None:
                lines.append(f"  Checkpoint 1: ${f['Checkpoint 1 Cost']:.2f} -- {f.get('Checkpoint 1 Notes', '')}")
            if f.get("Checkpoint 2 Cost") is not None:
                lines.append(f"  Checkpoint 2: ${f['Checkpoint 2 Cost']:.2f} -- {f.get('Checkpoint 2 Notes', '')}")
            if f.get("Actual Cost") is not None:
                lines.append(f"  Actual: ${f['Actual Cost']:.2f}, variance {f.get('Variance', 'n/a')}")
                lines.append(f"  Lesson: {f.get('Lesson', '(none logged)')}")
            else:
                lines.append("  Not completed yet -- no actual cost logged.")
            return "\n".join(lines)

        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                      params={"filterByFormula": "{Status}='Completed'", "pageSize": "25"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return "No completed video projects with logged actual costs yet."
        lines = []
        total_pred, total_actual = 0.0, 0.0
        for rec in recs:
            f = rec["fields"]
            pred, act = f.get("Predicted Cost", 0), f.get("Actual Cost", 0)
            total_pred += pred
            total_actual += act
            lines.append(f"{f.get('Project')}: predicted ${pred:.2f} -> actual ${act:.2f} ({f.get('Variance', 'n/a')})")
        overall_pct = ((total_actual - total_pred) / total_pred * 100) if total_pred else 0
        lines.append(f"\nOverall across {len(recs)} project(s): predicted ${total_pred:.2f}, "
                     f"actual ${total_actual:.2f} ({overall_pct:+.0f}% average drift).")
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read the cost log: {type(e).__name__}: {e}"
