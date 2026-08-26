# ui/data_sources.py
"""
Per-User Data Source Management.

Each user-parcel pairing has ONE active data source mode:
  - "sensor"  : Real-time IoT sensor connection (MQTT-based)
  - "manual"  : Manually-entered measurements (with optional onboarding agent)
  - "none"    : Not yet configured (user must complete onboarding)

This module is the single source of truth for whether a parcel has live data,
what fields are populated, and how stale the data is.

NEVER assume data exists. ALWAYS check.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from pymongo import MongoClient

from shared.config import MONGO_URI, MONGO_DB, SENSOR_FIELDS

_client = MongoClient(MONGO_URI)
_db = _client[MONGO_DB]


# ── Data source CRUD ────────────────────────────────────────────────────────

def get_user_data_source(user_id: str, parcel_id: str) -> Optional[dict]:
    """Get the active data source for a user-parcel pair. Returns None if not configured."""
    return _db.user_data_sources.find_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"_id": 0},
    )


def set_user_data_source(user_id: str, parcel_id: str, mode: str,
                          config: dict = None) -> dict:
    """
    Set/update a user's data source mode.
    mode: 'sensor' | 'manual' | 'none'
    config: { mqtt_broker, mqtt_port, mqtt_username, mqtt_password, mqtt_topic, ... }
            for sensor mode, or empty for manual.
    """
    if mode not in ("sensor", "manual", "none"):
        raise ValueError(f"Invalid mode: {mode}")

    doc = {
        "user_id": user_id,
        "parcel_id": parcel_id,
        "mode": mode,
        "config": config or {},
        "updated_at": datetime.now(timezone.utc),
        "status": "configured" if mode != "none" else "pending",
    }

    _db.user_data_sources.update_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return doc


def has_configured_source(user_id: str, parcel_id: str) -> bool:
    """Check if user has finished onboarding for this parcel."""
    src = get_user_data_source(user_id, parcel_id)
    return src is not None and src.get("mode") in ("sensor", "manual")


# ── Per-user sensor data storage (manual entries) ──────────────────────────

def save_manual_reading(user_id: str, parcel_id: str, field: str,
                         value: float, source: str = "manual_entry",
                         notes: str = "") -> str:
    """
    Save a manually-entered sensor reading for a user.
    These are stored separately from machine telemetry.
    """
    if field not in SENSOR_FIELDS:
        raise ValueError(f"Invalid field: {field}")

    doc = {
        "user_id": user_id,
        "parcel_id": parcel_id,
        "field": field,
        "value": float(value),
        "source": source,
        "notes": notes,
        "timestamp": datetime.now(timezone.utc),
    }
    result = _db.manual_readings.insert_one(doc)
    return str(result.inserted_id)


def save_manual_batch(user_id: str, parcel_id: str, readings: dict,
                       source: str = "onboarding_agent") -> int:
    """
    Save multiple readings at once (e.g., from onboarding session).
    readings = {field_name: value, ...}
    Returns count of readings saved.
    """
    docs = []
    now = datetime.now(timezone.utc)
    for field, value in readings.items():
        if field not in SENSOR_FIELDS or value is None:
            continue
        docs.append({
            "user_id": user_id,
            "parcel_id": parcel_id,
            "field": field,
            "value": float(value),
            "source": source,
            "timestamp": now,
        })

    if docs:
        _db.manual_readings.insert_many(docs)
    return len(docs)


def get_live_sensor_readings(asset_id: str) -> dict:
    """
    Latest readings for a parcel bound to a physical sensor asset.

    Telemetry from the node lands on the asset (Redis cache, InfluxDB history),
    not in manual_readings, so a parcel in sensor mode has to be read from
    there. Fields the node does not measure are omitted entirely rather than
    returned as zero, so the caller can distinguish "no instrument" from "a
    reading of zero".
    """
    from shared.redis_io import get_latest_cached
    from shared.influx_io import get_latest

    out = {}
    for field in SENSOR_FIELDS:
        value = get_latest_cached(field)
        if value is None:
            value = get_latest(field)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        out[field] = {
            "value": value,
            "timestamp": None,
            "source": "sensor",
            "age_minutes": 0.0,
        }
    return out


def get_user_latest_readings(user_id: str, parcel_id: str) -> dict:
    """
    Get the latest reading for each sensor field for a specific user-parcel.
    Returns {field: {value, timestamp, source, age_minutes}, ...}
    Only returns fields that have actual data — no assumptions.
    """
    # A parcel bound to a sensor reads live telemetry; manual_readings is the
    # fallback for parcels with no instrument attached.
    cfg = get_user_data_source(user_id, parcel_id) or {}
    if cfg.get("mode") == "sensor":
        asset = (cfg.get("config") or {}).get("asset_id") or cfg.get("asset_id")
        live = get_live_sensor_readings(asset or parcel_id)
        if live:
            return live

    result = {}
    for field in SENSOR_FIELDS:
        latest = _db.manual_readings.find_one(
            {"user_id": user_id, "parcel_id": parcel_id, "field": field},
            sort=[("timestamp", -1)],
        )
        if latest:
            ts = latest["timestamp"]
            # MongoDB may return naive datetimes — make timezone-aware for subtraction
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            result[field] = {
                "value": latest["value"],
                "timestamp": ts.isoformat(),
                "source": latest.get("source", "manual"),
                "age_minutes": round(age, 1),
            }
    return result


def get_user_field_history(user_id: str, parcel_id: str, field: str,
                            days: int = 30) -> list:
    """Get historical readings for a specific field."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = _db.manual_readings.find(
        {
            "user_id": user_id,
            "parcel_id": parcel_id,
            "field": field,
            "timestamp": {"$gte": cutoff},
        },
        {"_id": 0},
    ).sort("timestamp", 1)
    return [
        {
            "timestamp": doc["timestamp"].isoformat(),
            "value": doc["value"],
            "source": doc.get("source", "manual"),
        }
        for doc in cursor
    ]


# ── Aggregate data availability check ──────────────────────────────────────

def get_data_availability(user_id: str, parcel_id: str) -> dict:
    """
    Returns a strict report of what data exists for this user-parcel.
    NEVER assumes — only reports what is actually in the database.
    """
    source = get_user_data_source(user_id, parcel_id)

    if source is None or source.get("mode") == "none":
        return {
            "configured": False,
            "mode": "none",
            "message": "No data source configured. Please complete onboarding.",
            "fields_with_data": [],
            "fields_missing": SENSOR_FIELDS.copy(),
            "completion_pct": 0.0,
        }

    mode = source.get("mode")
    readings = get_user_latest_readings(user_id, parcel_id)

    fields_with_data = list(readings.keys())
    fields_missing = [f for f in SENSOR_FIELDS if f not in fields_with_data]
    completion = len(fields_with_data) / len(SENSOR_FIELDS) * 100

    # Check freshness
    stale_threshold_minutes = 60 * 24  # 24 hours
    stale_fields = [
        f for f, info in readings.items()
        if info["age_minutes"] > stale_threshold_minutes
    ]

    return {
        "configured": True,
        "mode": mode,
        "fields_with_data": fields_with_data,
        "fields_missing": fields_missing,
        "stale_fields": stale_fields,
        "completion_pct": round(completion, 1),
        "readings": readings,
        "config": source.get("config", {}),
        "last_updated": source.get("updated_at").isoformat() if source.get("updated_at") else None,
    }