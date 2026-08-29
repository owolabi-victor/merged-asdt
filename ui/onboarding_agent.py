# ui/onboarding_agent.py
"""
Onboarding Agent — Plain-language soil data collector.

Walks a farmer through entering soil data manually, asking questions
in everyday language. Stores answers per-user, per-parcel.

Flow:
  1. Greet user, ask which soil type they have (P0: NEW)
  2. For each field, ask in plain language with helpful context
  3. Validate input (range checks, unit conversions if needed)
  4. Save to manual_readings collection
  5. Persist soil type to user_parcels collection (P0: NEW)
  6. Provide summary at the end

This is NOT the diagnostic agent — it does NOT use the LLM.
It is a deterministic Q&A flow so it always works, even offline.

Phase 6 changes:
  - P0: Added soil type question as question 0 (sandy/loamy/clay/silty).
         commit_session() now persists soil_type via set_parcel_soil_type().
"""
from typing import Optional
from datetime import datetime, timezone

from ui.data_sources import save_manual_batch
from shared.config import SENSOR_FIELDS, SOIL_TYPES, set_parcel_soil_type


# ── Question definitions (plain-language + technical) ───────────────────────
# P0: Soil type question is first — it determines which thresholds/ranges
#     are used for all subsequent analysis on this parcel.

QUESTIONS = [
    {
        "field": "soil_type",
        "question": "What type of soil do you have?",
        "help": "Sandy soil feels gritty and drains fast. Clay soil is sticky and holds water. "
                "Loamy soil is a balanced mix (most common farmland). Silty soil feels smooth like flour.",
        "unit": "",
        "category": "Setup",
        "icon": "🌍",
        "skip_label": "I'm not sure (defaults to loamy)",
        "preset_choices": [
            {"label": "Sandy — gritty, drains fast, light colour", "value": "sandy"},
            {"label": "Loamy — balanced, dark, crumbly (most common)", "value": "loamy"},
            {"label": "Clay — sticky, heavy, holds water", "value": "clay"},
            {"label": "Silty — smooth, flour-like, holds moisture", "value": "silty"},
        ],
    },
    {
        "field": "soil_moisture_pct",
        "question": "How wet does your soil feel right now?",
        "help": "Squeeze a handful: dry crumbles (10%), forms a loose ball (25%), feels wet (40%+)",
        "unit": "%",
        "min": 0.0,
        "max": 100.0,
        "category": "Physical",
        "icon": "💧",
        "skip_label": "I'll measure later",
        "preset_choices": [
            {"label": "Very dry / dusty", "value": 10},
            {"label": "Slightly moist", "value": 20},
            {"label": "Moist (forms ball)", "value": 30},
            {"label": "Wet / waterlogged", "value": 50},
        ],
    },
    {
        "field": "soil_temp_c",
        "question": "What is your soil temperature?",
        "help": "Measure a few inches deep. Most crops grow best between 18°C and 28°C.",
        "unit": "°C",
        "min": -30.0,
        "max": 70.0,
        "category": "Physical",
        "icon": "🌡️",
        "skip_label": "Skip",
    },
    {
        "field": "bulk_density_g_cm3",
        "question": "What is your soil bulk density?",
        "help": "Measure of soil compaction. Above 1.6 g/cm³ means heavily compacted soil.",
        "unit": "g/cm³",
        "min": 0.5,
        "max": 3.0,
        "category": "Physical",
        "icon": "🪨",
        "skip_label": "Not measured",
    },
]


# ── Validation ──────────────────────────────────────────────────────────────

def validate_answer(field: str, raw_value) -> tuple[Optional[float], Optional[str]]:
    """
    Validate a single answer. Returns (value_or_None, error_or_None).

    For the soil_type field, returns the string value (not float) if valid.
    """
    if raw_value is None or raw_value == "":
        return None, None  # Skipped

    # P0: Special handling for soil_type — it's a string choice, not a number
    if field == "soil_type":
        raw_str = str(raw_value).strip().lower()
        if raw_str in SOIL_TYPES:
            return raw_str, None
        return None, f"Please choose one of: sandy, loamy, clay, silty (got '{raw_value}')"

    # Find question definition
    q = next((q for q in QUESTIONS if q["field"] == field), None)
    if not q:
        return None, f"Unknown field: {field}"

    # Try to parse as float
    try:
        value = float(raw_value)
    except (ValueError, TypeError):
        return None, f"Please enter a number, not '{raw_value}'"

    # Range check
    if value < q.get("min", float("-inf")) or value > q.get("max", float("inf")):
        return None, f"Value must be between {q['min']} and {q['max']} {q['unit']}"

    return value, None


# ── Session management ──────────────────────────────────────────────────────

def start_session(user_id: str, parcel_id: str) -> dict:
    """Initialize a new onboarding session."""
    return {
        "user_id": user_id,
        "parcel_id": parcel_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_index": 0,
        "answers": {},
        "skipped": [],
        "soil_type": None,      # P0: Tracks the chosen soil type
        "complete": False,
    }


def get_next_question(session: dict) -> Optional[dict]:
    """Get the next question to ask. Returns None when all questions answered."""
    idx = session["current_index"]
    if idx >= len(QUESTIONS):
        return None
    return QUESTIONS[idx]


def submit_answer(session: dict, raw_value, skip: bool = False) -> dict:
    """
    Process an answer to the current question.
    Returns updated session and any error message.
    """
    q = get_next_question(session)
    if q is None:
        return {"session": session, "error": "All questions already answered", "done": True}

    field = q["field"]

    if skip:
        session["skipped"].append(field)
        # P0: If soil_type is skipped, default to loamy
        if field == "soil_type":
            session["soil_type"] = "loamy"
        session["current_index"] += 1
    else:
        value, error = validate_answer(field, raw_value)
        if error:
            return {"session": session, "error": error, "done": False}

        if value is not None:
            # P0: soil_type goes into its own session field, not into answers
            #     (answers dict is for numeric sensor readings only)
            if field == "soil_type":
                session["soil_type"] = value
            else:
                session["answers"][field] = value
        else:
            session["skipped"].append(field)
            # P0: If soil_type is effectively skipped, default to loamy
            if field == "soil_type":
                session["soil_type"] = "loamy"
        session["current_index"] += 1

    if session["current_index"] >= len(QUESTIONS):
        session["complete"] = True

    return {"session": session, "error": None, "done": session["complete"]}


def commit_session(session: dict) -> dict:
    """
    Save all answers to the database.

    P0: Also persists the soil_type to user_parcels via set_parcel_soil_type().
    """
    user_id = session["user_id"]
    parcel_id = session["parcel_id"]

    # P0: Persist soil type choice
    soil_type = session.get("soil_type") or "loamy"
    set_parcel_soil_type(user_id, parcel_id, soil_type)

    # Save numeric sensor readings
    if not session.get("answers"):
        return {
            "saved": 0,
            "soil_type": soil_type,
            "message": f"Soil type set to '{soil_type}'. No soil measurements to save.",
        }

    count = save_manual_batch(
        user_id=user_id,
        parcel_id=parcel_id,
        readings=session["answers"],
        source="onboarding_agent",
    )

    summary = {
        "saved": count,
        "soil_type": soil_type,
        "skipped": len(session.get("skipped", [])),
        "fields_saved": list(session["answers"].keys()),
        "fields_skipped": session.get("skipped", []),
        "message": f"Soil type: {soil_type}. Saved {count} soil measurements. You can update them anytime.",
    }

    return summary


# ── Stateless helpers for UI ───────────────────────────────────────────────

def get_question_by_index(idx: int) -> Optional[dict]:
    """Get question by index (for direct UI access)."""
    if 0 <= idx < len(QUESTIONS):
        return QUESTIONS[idx]
    return None


def total_questions() -> int:
    return len(QUESTIONS)