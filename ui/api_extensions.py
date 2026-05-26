# ui/api_extensions.py
"""
API extensions for ASDT UI — mounts onto the existing Twin API FastAPI app.

Endpoints:
  - Auth:        register, login, profile (GET/PUT), change-password
  - Parcels:     list, add
  - Data Sources: get, set (manual/sensor mode)
  - Onboarding:  start, next, submit, commit (manual data collection Q&A)
  - Sensor:      test connection, start, stop, status
  - Scientist:   summary, history, layer status, override, threshold, KG, etc.
  - Farmer:      dashboard (per-user data), feedback, observation, escalate, chat

Phase 6 changes:
  - P0: _compute_status(), _compute_farmer_indicators_from_readings(),
         _compute_alerts_from_readings() now accept a soil_type parameter
         and use per-soil-type thresholds/ranges instead of globals.
         farmer_dashboard resolves the parcel's soil type from MongoDB.
         scientist_summary shows the parcel soil type.
  - P2: _compute_health_score_from_readings() now uses weighted average
         with PARAMETER_WEIGHTS from config.
"""
import json
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from pymongo import MongoClient

from shared.config import (
    MONGO_URI, MONGO_DB, ASSET_ID, SENSOR_FIELDS, THRESHOLDS,
    ACTIVE_SOIL_TYPE, DEPLETION_STATES, HEALTHY_RANGES,
    THRESHOLDS_BY_SOIL_TYPE, SOIL_TYPES,
    PARAMETER_WEIGHTS, HEALTHY_RANGES_BY_SOIL_TYPE,
    get_parcel_soil_type, get_thresholds_for_soil_type,
    get_healthy_ranges_for_soil_type,
    get_parcel_thresholds, get_parcel_healthy_ranges, save_parcel_soil_params,
)
from shared.influx_io import get_latest, query_recent, write_point
from shared.redis_io import (
    get_latest_cached, set_latest, get_state,
    get_active_diagnosis, set_state,
)
from shared.mongo_io import (
    get_recent_events, get_asset_metadata,
    save_recommendation, save_outcome, log_event,
    get_recent_cross_domain, get_recent_transitions,
)

from ui.auth import (
    register_user, login_user, verify_token, get_user,
    log_audit, update_profile, change_password, add_parcel,
)
from ui.data_sources import (
    get_user_data_source, set_user_data_source,
    get_user_latest_readings, get_user_field_history,
    get_data_availability, save_manual_reading, save_manual_batch,
    has_configured_source,
)
from ui.onboarding_agent import (
    QUESTIONS, start_session, get_next_question, submit_answer,
    commit_session, validate_answer, total_questions,
)
from ui.sensor_connection import (
    test_mqtt_connection, start_user_sensor, stop_user_sensor, get_sensor_status,
)


_client = MongoClient(MONGO_URI)
_db = _client[MONGO_DB]

router = APIRouter(prefix="/api/v1")
security = HTTPBearer(auto_error=False)


# ── Auth dependencies ────────────────────────────────────────────────────────

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(401, "Authentication required")
    payload = verify_token(credentials.credentials)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return payload


def require_role(required_role: str):
    def check(user=Depends(get_current_user)):
        if user["role"] != required_role:
            raise HTTPException(403, f"Requires {required_role} role")
        return user
    return check


def get_user_profile(user=Depends(get_current_user)) -> dict:
    """Get the full user profile (with parcels, name, etc.)."""
    profile = get_user(user["user_id"])
    if not profile:
        raise HTTPException(404, "User profile not found")
    return profile


# ── Pydantic models ─────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str
    role: str
    parcels: list[str] = []

class LoginRequest(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    farm_name: Optional[str] = None
    parcels: Optional[list[str]] = None

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class AddParcel(BaseModel):
    parcel_id: str
    parcel_name: Optional[str] = None

class DataSourceConfig(BaseModel):
    parcel_id: str
    mode: str  # 'sensor' | 'manual'
    config: dict = {}

class OnboardingAnswer(BaseModel):
    parcel_id: str
    answer: Optional[str] = None
    skip: bool = False
    session_state: dict = {}

class ManualReading(BaseModel):
    parcel_id: str
    field: str
    value: float
    notes: str = ""

class ManualBatchRequest(BaseModel):
    parcel_id: str
    readings: dict  # {field_name: value}

class SensorTestRequest(BaseModel):
    broker: str
    port: int = 1883
    username: str = ""
    password: str = ""
    topic: str
    use_tls: bool = False

class OverrideRequest(BaseModel):
    field: str
    value: float
    reason: str = ""

class ThresholdUpdateRequest(BaseModel):
    field: str
    warn: float
    crit: float

class FeedbackRequest(BaseModel):
    recommendation_id: str
    accepted: bool
    outcome: str = ""
    rating: int = 0

class FarmerObservationRequest(BaseModel):
    text: str
    structured_fields: dict = {}

class DataSourceRequest(BaseModel):
    name: str
    source_type: str
    connection_uri: str
    mqtt_topic: str = ""
    auth_type: str = "none"
    credentials: dict = {}
    field_mappings: dict = {}

class ChatRequest(BaseModel):
    question: str
    parcel_id: Optional[str] = None
    history: Optional[list] = None


# ═══════════════════════════════════════════════════════════════════════════
# AUTH & PROFILE
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/auth/register")
async def api_register(req: RegisterRequest):
    parcels = req.parcels or [f"parcel_{uuid.uuid4().hex[:8]}"]
    result = register_user(req.email, req.password, req.role, req.name, parcels)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/auth/login")
async def api_login(req: LoginRequest):
    result = login_user(req.email, req.password)
    if "error" in result:
        raise HTTPException(401, result["error"])
    return result


@router.get("/auth/profile")
async def api_get_profile(user=Depends(get_current_user)):
    profile = get_user(user["user_id"])
    if not profile:
        raise HTTPException(404, "User not found")
    return profile


@router.put("/auth/profile")
async def api_update_profile(req: ProfileUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    result = update_profile(user["user_id"], updates)
    if "error" in result:
        raise HTTPException(400, result["error"])

    log_audit(user["user_id"], "profile_update", updates)
    return result


@router.post("/auth/change-password")
async def api_change_password(req: PasswordChange, user=Depends(get_current_user)):
    result = change_password(user["user_id"], req.old_password, req.new_password)
    if "error" in result:
        raise HTTPException(400, result["error"])
    log_audit(user["user_id"], "password_change", {})
    return result




@router.get("/parcels")
async def api_list_parcels(user=Depends(get_current_user)):
    """List all parcels owned by the current user."""
    profile = get_user(user["user_id"])
    parcels = []
    for pid in profile.get("parcels", []):
        meta = _db.user_parcels.find_one(
            {"user_id": user["user_id"], "parcel_id": pid},
            {"_id": 0},
        )
        source = get_user_data_source(user["user_id"], pid)
        # P0: Include soil type per parcel
        soil_type = get_parcel_soil_type(user["user_id"], pid)
        parcels.append({
            "parcel_id": pid,
            "parcel_name": (meta or {}).get("parcel_name", pid),
            "soil_type": soil_type,
            "data_source_mode": (source or {}).get("mode", "none"),
            "configured": has_configured_source(user["user_id"], pid),
        })
    return {"parcels": parcels}


@router.post("/parcels")
async def api_add_parcel(req: AddParcel, user=Depends(get_current_user)):
    result = add_parcel(user["user_id"], req.parcel_id, req.parcel_name)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ═══════════════════════════════════════════════════════════════════════════
# DATA SOURCES (per-user, per-parcel)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/data-source/{parcel_id}")
async def api_get_data_source(parcel_id: str, user=Depends(get_current_user)):
    """Get the data source configuration for a parcel."""
    source = get_user_data_source(user["user_id"], parcel_id)
    availability = get_data_availability(user["user_id"], parcel_id)
    return {
        "source": source,
        "availability": availability,
    }


@router.post("/data-source")
async def api_set_data_source(req: DataSourceConfig, user=Depends(get_current_user)):
    """Set or change the data source mode for a parcel."""
    if req.mode not in ("sensor", "manual"):
        raise HTTPException(400, "Mode must be 'sensor' or 'manual'")

    result = set_user_data_source(user["user_id"], req.parcel_id, req.mode, req.config)

    log_audit(user["user_id"], "data_source_change", {
        "parcel_id": req.parcel_id,
        "mode": req.mode,
    })

    # If sensor mode, attempt to start the connection
    if req.mode == "sensor":
        start_result = start_user_sensor(user["user_id"], req.parcel_id)
        result["sensor_start"] = start_result

    return result


# ═══════════════════════════════════════════════════════════════════════════
# ONBOARDING (manual data collection Q&A)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/onboarding/questions")
async def api_onboarding_questions(user=Depends(get_current_user)):
    """Get the full list of onboarding questions (for previewing)."""
    return {
        "questions": QUESTIONS,
        "total": total_questions(),
    }


@router.post("/onboarding/start")
async def api_onboarding_start(parcel_id: str = Query(...),
                                user=Depends(get_current_user)):
    """Start a new onboarding session."""
    session = start_session(user["user_id"], parcel_id)
    next_q = get_next_question(session)

    # Set parcel mode to manual when onboarding starts
    set_user_data_source(user["user_id"], parcel_id, "manual", {})

    return {
        "session": session,
        "next_question": next_q,
        "progress": f"0 / {total_questions()}",
    }


@router.post("/onboarding/answer")
async def api_onboarding_answer(req: OnboardingAnswer,
                                  user=Depends(get_current_user)):
    """Submit an answer to the current question and get the next one."""
    session = req.session_state or start_session(user["user_id"], req.parcel_id)

    result = submit_answer(session, req.answer, skip=req.skip)
    if result.get("error"):
        return {"error": result["error"], "session": result["session"]}

    next_q = get_next_question(result["session"])
    progress = f"{result['session']['current_index']} / {total_questions()}"

    response = {
        "session": result["session"],
        "next_question": next_q,
        "done": result["done"],
        "progress": progress,
    }

    # Auto-commit when done
    if result["done"]:
        commit_result = commit_session(result["session"])
        response["commit_result"] = commit_result
        log_audit(user["user_id"], "onboarding_complete", {
            "parcel_id": req.parcel_id,
            "saved": commit_result["saved"],
            "skipped": commit_result["skipped"],
        })

    return response


@router.post("/onboarding/commit")
async def api_onboarding_commit(req: OnboardingAnswer,
                                  user=Depends(get_current_user)):
    """Manually commit the current session (save partial answers)."""
    session = req.session_state
    if not session:
        raise HTTPException(400, "No session to commit")
    return commit_session(session)


# ═══════════════════════════════════════════════════════════════════════════
# MANUAL READINGS (direct entry, single field)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/readings/manual")
async def api_manual_reading(req: ManualReading, user=Depends(get_current_user)):
    """Record a single manual sensor reading."""
    if req.field not in SENSOR_FIELDS:
        raise HTTPException(400, f"Invalid field: {req.field}")

    reading_id = save_manual_reading(
        user["user_id"], req.parcel_id, req.field, req.value,
        source="manual_entry", notes=req.notes,
    )

    log_audit(user["user_id"], "manual_reading", {
        "parcel_id": req.parcel_id,
        "field": req.field,
        "value": req.value,
    })

    return {"reading_id": reading_id, "status": "saved"}


@router.post("/readings/manual/batch")
async def api_manual_batch(req: ManualBatchRequest, user=Depends(get_current_user)):
    """Save multiple manual readings at once and mark data source as manual."""
    invalid = [f for f in req.readings if f not in SENSOR_FIELDS]
    if invalid:
        raise HTTPException(400, f"Invalid fields: {invalid}")

    saved = save_manual_batch(
        user["user_id"], req.parcel_id, req.readings, source="manual_entry",
    )

    if not has_configured_source(user["user_id"], req.parcel_id):
        set_user_data_source(user["user_id"], req.parcel_id, "manual", {})

    log_audit(user["user_id"], "manual_batch_reading", {
        "parcel_id": req.parcel_id,
        "fields": list(req.readings.keys()),
        "saved": saved,
    })
    return {"status": "ok", "saved": saved, "parcel_id": req.parcel_id}


@router.get("/readings/{parcel_id}")
async def api_get_readings(parcel_id: str, user=Depends(get_current_user)):
    """Get latest readings for a parcel."""
    return {
        "parcel_id": parcel_id,
        "readings": get_user_latest_readings(user["user_id"], parcel_id),
    }


@router.get("/readings/{parcel_id}/history/{field}")
async def api_get_reading_history(parcel_id: str, field: str,
                                    days: int = Query(30),
                                    user=Depends(get_current_user)):
    """Get historical readings for a specific field."""
    history = get_user_field_history(user["user_id"], parcel_id, field, days)
    return {"parcel_id": parcel_id, "field": field, "data": history}


# ═══════════════════════════════════════════════════════════════════════════
# SENSOR CONNECTION (production-grade)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/sensor/test")
async def api_sensor_test(req: SensorTestRequest, user=Depends(get_current_user)):
    """Test an MQTT sensor connection before saving it."""
    result = test_mqtt_connection(
        broker=req.broker,
        port=req.port,
        username=req.username,
        password=req.password,
        topic=req.topic,
        use_tls=req.use_tls,
    )
    return result


@router.post("/sensor/start/{parcel_id}")
async def api_sensor_start(parcel_id: str, user=Depends(get_current_user)):
    """Start the MQTT subscriber for a user's sensor."""
    result = start_user_sensor(user["user_id"], parcel_id)
    log_audit(user["user_id"], "sensor_start", {"parcel_id": parcel_id})
    return result


@router.post("/sensor/stop/{parcel_id}")
async def api_sensor_stop(parcel_id: str, user=Depends(get_current_user)):
    """Stop the MQTT subscriber for a user's sensor."""
    result = stop_user_sensor(user["user_id"], parcel_id)
    log_audit(user["user_id"], "sensor_stop", {"parcel_id": parcel_id})
    return result


@router.get("/sensor/status/{parcel_id}")
async def api_sensor_status(parcel_id: str, user=Depends(get_current_user)):
    """Get the status of a user's sensor connection."""
    return get_sensor_status(user["user_id"], parcel_id)


# ═══════════════════════════════════════════════════════════════════════════
# SCIENTIST ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/ui/scientist/summary")
async def scientist_summary(parcel_id: Optional[str] = Query(None),
                             user=Depends(require_role("scientist"))):
    """Full scientist dashboard summary — uses ONLY this user's actual sensor data."""
    profile = get_user(user["user_id"])
    parcels = profile.get("parcels", [])
    if not parcel_id:
        parcel_id = parcels[0] if parcels else None
    elif parcel_id not in parcels:
        raise HTTPException(403, "You don't have access to this parcel")

    soil_type = get_parcel_soil_type(user["user_id"], parcel_id) if parcel_id else ACTIVE_SOIL_TYPE
    thresholds = get_parcel_thresholds(user["user_id"], parcel_id) if parcel_id else get_thresholds_for_soil_type(soil_type)
    healthy_ranges = get_parcel_healthy_ranges(user["user_id"], parcel_id) if parcel_id else get_healthy_ranges_for_soil_type(soil_type)

    # Read ONLY this user's actual readings — never global simulator data
    readings = get_user_latest_readings(user["user_id"], parcel_id) if parcel_id else {}

    sensors = {}
    for field in SENSOR_FIELDS:
        reading = readings.get(field, {})
        val = reading.get("value")
        sensors[field] = {
            "value": round(val, 3) if val is not None else None,
            "unit": healthy_ranges.get(field, {}).get("unit", ""),
            "healthy_min": healthy_ranges.get(field, {}).get("min"),
            "healthy_max": healthy_ranges.get(field, {}).get("max"),
            "threshold_warn": thresholds.get(field, {}).get("warn"),
            "threshold_crit": thresholds.get(field, {}).get("crit"),
            "status": _compute_status(field, val, soil_type),
            "source": reading.get("source"),
            "age_minutes": reading.get("age_minutes"),
        }

    has_data = bool(readings)
    health_score = _compute_health_score_from_readings(readings, soil_type) if readings else None
    fields_with_data = list(readings.keys())
    fields_missing = [f for f in SENSOR_FIELDS if f not in readings]

    return {
        "asset_id": parcel_id or ASSET_ID,
        "parcel_id": parcel_id,
        "soil_type": soil_type,
        "no_data": not has_data,
        "system_state": get_state(),
        "health_score": health_score,
        "sensors": sensors,
        "depletion_states": {},
        "active_diagnosis": {},
        "recent_events": get_recent_events(20),
        "recent_transitions": get_recent_transitions(10),
        "thresholds": thresholds,
        "soil_types": SOIL_TYPES,
        "data_availability": {
            "fields_with_data": fields_with_data,
            "fields_missing": fields_missing,
            "completion_pct": round(len(fields_with_data) / len(SENSOR_FIELDS) * 100, 1) if SENSOR_FIELDS else 0,
        },
    }


@router.get("/ui/scientist/sensor_history")
async def sensor_history(field: str = Query(...), minutes: int = Query(60),
                          user=Depends(require_role("scientist"))):
    if field not in SENSOR_FIELDS:
        raise HTTPException(400, f"Invalid field: {field}")
    df = query_recent(field, minutes=minutes)
    if df.empty:
        return {"field": field, "data": []}
    records = [
        {"time": row["_time"].isoformat(), "value": round(row["_value"], 3)}
        for _, row in df.iterrows()
    ]
    return {"field": field, "minutes": minutes, "data": records}


@router.get("/ui/scientist/layer_status")
async def layer_status(user=Depends(require_role("scientist"))):
    layers = [
        {"name": "Physical Simulator",  "module": "physical.simulator",              "stale_after": 120},
        {"name": "DT Network Ingestor", "module": "dt_network.ingestor",             "stale_after": 300},
        {"name": "Data Mgmt Pipeline",  "module": "data_mgmt.pipeline",              "stale_after": 240},
        {"name": "Simulation Runner",   "module": "simulation.model_runner",         "stale_after": 7200},
        {"name": "Service Ditto Sync",  "module": "service_layer.ditto_sync",        "stale_after": 120},
        {"name": "Reactive Rule Engine","module": "reactive.rule_engine",            "stale_after": 240},
        {"name": "Cross-Domain Sync",   "module": "service_layer.cross_domain_sync", "stale_after": 240},
    ]
    now = datetime.now(timezone.utc)
    for layer in layers:
        heartbeat_raw = get_latest_cached(f"layer_heartbeat_{layer['module']}")
        layer["last_heartbeat"] = heartbeat_raw
        if not heartbeat_raw:
            layer["status"] = "stopped"
        else:
            try:
                hb_time = datetime.fromisoformat(heartbeat_raw.replace("Z", "+00:00"))
                if hb_time.tzinfo is None:
                    hb_time = hb_time.replace(tzinfo=timezone.utc)
                age_seconds = (now - hb_time).total_seconds()
                layer["status"] = "running" if age_seconds < layer["stale_after"] else "stale"
            except (ValueError, AttributeError):
                layer["status"] = "unknown"
        layer.pop("stale_after")
    return {"layers": layers, "system_state": get_state()}


@router.post("/ui/scientist/override")
async def scientist_override(req: OverrideRequest, user=Depends(require_role("scientist"))):
    if req.field not in SENSOR_FIELDS:
        raise HTTPException(400, f"Invalid field: {req.field}")

    old_value = get_latest(req.field)
    write_point("soil_telemetry", {req.field: req.value}, tags={"source": "manual_override"})
    set_latest(req.field, req.value)

    log_audit(user["user_id"], "sensor_override", {
        "field": req.field, "old_value": old_value, "new_value": req.value, "reason": req.reason,
    })
    log_event("manual_override", {
        "field": req.field, "old_value": old_value, "new_value": req.value,
        "user_id": user["user_id"], "reason": req.reason,
    }, severity="warning")

    return {"status": "ok", "field": req.field, "old_value": old_value, "new_value": req.value}


@router.post("/ui/scientist/threshold")
async def update_threshold(req: ThresholdUpdateRequest, user=Depends(require_role("scientist"))):
    profile = get_user(user["user_id"])
    parcel_id = (profile.get("parcels") or [None])[0]
    soil_type = get_parcel_soil_type(user["user_id"], parcel_id) if parcel_id else ACTIVE_SOIL_TYPE
    current = get_parcel_thresholds(user["user_id"], parcel_id) if parcel_id else get_thresholds_for_soil_type(soil_type)

    if req.field not in current:
        raise HTTPException(400, f"Unknown field: {req.field}")

    old_thresh = current[req.field].copy()
    new_thresh = {**old_thresh, "warn": req.warn, "crit": req.crit}

    # Persist override to MongoDB
    if parcel_id:
        existing_doc = _db.parcel_thresholds.find_one(
            {"user_id": user["user_id"], "parcel_id": parcel_id}, {"thresholds": 1}
        ) or {}
        all_overrides = existing_doc.get("thresholds", {})
        all_overrides[req.field] = {"warn": req.warn, "crit": req.crit}
        save_parcel_soil_params(user["user_id"], parcel_id, thresholds=all_overrides)

    log_audit(user["user_id"], "threshold_update", {
        "parcel_id": parcel_id, "field": req.field,
        "old": old_thresh, "new": new_thresh,
    })
    return {"status": "ok", "field": req.field, "old": old_thresh, "new": new_thresh}


class SoilParametersUpdate(BaseModel):
    parcel_id: str
    thresholds: dict = {}   # {field: {warn: float, crit: float}}
    healthy_ranges: dict = {}  # {field: {min: float, max: float}}


# ── Parcel Geometry ───────────────────────────────────────────────────────────
_DEFAULT_GEOMETRY = {
    "width_m":          10.0,   # East-West extent of the parcel
    "length_m":         10.0,   # North-South extent
    "depth_m":          1.0,    # Monitoring depth
    "sensor_count":     4,      # 1, 2, 4, or 9
    "sensor_depth_cm":  10.0,   # Depth at which sensors are installed
    "topsoil_depth_cm": 20.0,   # Topsoil / subsoil boundary
    "subsoil_depth_cm": 50.0,   # Subsoil / substratum boundary
}

class ParcelGeometry(BaseModel):
    width_m:          float = 10.0
    length_m:         float = 10.0
    depth_m:          float = 1.0
    sensor_count:     int   = 4
    sensor_depth_cm:  float = 10.0
    topsoil_depth_cm: float = 20.0
    subsoil_depth_cm: float = 50.0


@router.get("/ui/scientist/parcel_geometry")
async def get_parcel_geometry(parcel_id: str = Query(...),
                               user=Depends(require_role("scientist"))):
    """Return the physical geometry configuration for a parcel."""
    doc = _db.user_parcels.find_one(
        {"user_id": user["user_id"], "parcel_id": parcel_id},
        {"_id": 0, "geometry": 1},
    )
    geo = {**_DEFAULT_GEOMETRY}
    if doc and doc.get("geometry"):
        geo.update(doc["geometry"])
    return {"parcel_id": parcel_id, "geometry": geo}


@router.patch("/ui/scientist/parcel_geometry")
async def update_parcel_geometry(parcel_id: str = Query(...),
                                  body: ParcelGeometry = ...,
                                  user=Depends(require_role("scientist"))):
    """Save parcel physical geometry. Creates the user_parcels record if absent."""
    geo = body.model_dump()
    _db.user_parcels.update_one(
        {"user_id": user["user_id"], "parcel_id": parcel_id},
        {"$set": {"geometry": geo, "updated_at": datetime.now(timezone.utc),
                  "user_id": user["user_id"], "parcel_id": parcel_id}},
        upsert=True,
    )
    log_audit(user["user_id"], "parcel_geometry_update", {"parcel_id": parcel_id, **geo})
    return {"parcel_id": parcel_id, "geometry": geo}


@router.get("/ui/scientist/soil_parameters")
async def get_soil_parameters(parcel_id: Optional[str] = Query(None),
                               user=Depends(require_role("scientist"))):
    """
    Return the full set of soil parameters for a parcel — thresholds and
    healthy ranges — merging any scientist-defined overrides with the
    soil-type defaults. Each field indicates whether it is custom or default.
    """
    profile = get_user(user["user_id"])
    pid = parcel_id or (profile.get("parcels") or [None])[0]
    soil_type = get_parcel_soil_type(user["user_id"], pid) if pid else ACTIVE_SOIL_TYPE

    defaults_t = get_thresholds_for_soil_type(soil_type)
    defaults_r = get_healthy_ranges_for_soil_type(soil_type)
    effective_t = get_parcel_thresholds(user["user_id"], pid) if pid else defaults_t
    effective_r = get_parcel_healthy_ranges(user["user_id"], pid) if pid else defaults_r

    # Load stored overrides to know which fields are custom
    stored_doc = (_db.parcel_thresholds.find_one(
        {"user_id": user["user_id"], "parcel_id": pid}, {"_id": 0}
    ) or {}) if pid else {}
    custom_t = set(stored_doc.get("thresholds", {}).keys())
    custom_r = set(stored_doc.get("healthy_ranges", {}).keys())

    parameters = {}
    all_fields = set(list(defaults_t.keys()) + list(defaults_r.keys()))
    # Skip the high-side moisture/ph auxiliary keys from the top-level list
    display_fields = [f for f in SENSOR_FIELDS]

    for field in display_fields:
        t = effective_t.get(field, {})
        r = effective_r.get(field, {})
        parameters[field] = {
            "unit":            r.get("unit", ""),
            "threshold_warn":  t.get("warn"),
            "threshold_crit":  t.get("crit"),
            "threshold_low":   t.get("low", True),
            "healthy_min":     r.get("min"),
            "healthy_max":     r.get("max"),
            "weight":          PARAMETER_WEIGHTS.get(field, 1.0),
            "threshold_custom": field in custom_t,
            "range_custom":    field in custom_r,
        }

    return {
        "parcel_id": pid,
        "soil_type": soil_type,
        "parameters": parameters,
    }


@router.post("/ui/scientist/soil_parameters")
async def save_soil_parameters(req: SoilParametersUpdate,
                                user=Depends(require_role("scientist"))):
    """
    Save scientist-defined thresholds and/or healthy ranges for a parcel.
    Only the fields provided are updated; others keep their current values.
    """
    profile = get_user(user["user_id"])
    if req.parcel_id not in profile.get("parcels", []):
        raise HTTPException(403, "You don't have access to this parcel")

    # Merge with existing overrides so a partial update doesn't erase other fields
    stored_doc = (_db.parcel_thresholds.find_one(
        {"user_id": user["user_id"], "parcel_id": req.parcel_id}, {"_id": 0}
    ) or {})

    merged_t = {**stored_doc.get("thresholds", {}), **req.thresholds} if req.thresholds else None
    merged_r = {**stored_doc.get("healthy_ranges", {}), **req.healthy_ranges} if req.healthy_ranges else None

    save_parcel_soil_params(user["user_id"], req.parcel_id,
                             thresholds=merged_t, healthy_ranges=merged_r)

    log_audit(user["user_id"], "soil_parameters_update", {
        "parcel_id": req.parcel_id,
        "threshold_fields": list(req.thresholds.keys()),
        "range_fields": list(req.healthy_ranges.keys()),
    })
    return {"status": "ok", "parcel_id": req.parcel_id,
            "thresholds_saved": len(req.thresholds),
            "ranges_saved": len(req.healthy_ranges)}


@router.delete("/ui/scientist/soil_parameters/{parcel_id}")
async def reset_soil_parameters(parcel_id: str, user=Depends(require_role("scientist"))):
    """Remove all scientist-defined overrides — reverts to soil-type defaults."""
    profile = get_user(user["user_id"])
    if parcel_id not in profile.get("parcels", []):
        raise HTTPException(403, "You don't have access to this parcel")
    _db.parcel_thresholds.delete_one({"user_id": user["user_id"], "parcel_id": parcel_id})
    log_audit(user["user_id"], "soil_parameters_reset", {"parcel_id": parcel_id})
    return {"status": "ok", "message": "Reverted to soil-type defaults"}


@router.post("/ui/scientist/run_diagnosis")
async def trigger_diagnosis(user=Depends(require_role("scientist"))):
    from intelligent.soil_intelligence_agent import SoilIntelligenceAgent
    # P0: Resolve soil type for the scientist's parcel
    profile = get_user(user["user_id"])
    parcels = profile.get("parcels", [])
    parcel_id = parcels[0] if parcels else None
    soil_type = get_parcel_soil_type(user["user_id"], parcel_id) if parcel_id else None
    agent = SoilIntelligenceAgent(soil_type=soil_type)
    result = agent.run_full_pipeline()
    log_audit(user["user_id"], "manual_diagnosis", {
        "primary_state": result.get("primary_state"),
    })
    return result


@router.get("/ui/scientist/knowledge_graph")
async def get_knowledge_graph(user=Depends(require_role("scientist"))):
    try:
        from intelligent.neo4j_kg import get_full_graph
        return get_full_graph()
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}


@router.get("/ui/scientist/cross_domain")
async def cross_domain_dashboard(user=Depends(require_role("scientist"))):
    messages = get_recent_cross_domain(20)
    data_sources = list(_db.data_sources.find({}, {"_id": 0}))
    plant_raw = get_latest_cached("cross_domain_plant_demand")
    biotic_raw = get_latest_cached("cross_domain_biotic_data")

    return {
        "recent_messages": messages,
        "data_sources": data_sources,
        "plant_demand": json.loads(plant_raw) if plant_raw else None,
        "biotic_data": json.loads(biotic_raw) if biotic_raw else None,
    }


@router.post("/ui/scientist/data_source")
async def add_data_source(req: DataSourceRequest, user=Depends(require_role("scientist"))):
    source_id = str(uuid.uuid4())
    doc = {
        "source_id": source_id, "name": req.name,
        "source_type": req.source_type, "connection_uri": req.connection_uri,
        "mqtt_topic": req.mqtt_topic, "auth_type": req.auth_type,
        "field_mappings": req.field_mappings, "status": "pending",
        "created_by": user["user_id"], "created_at": datetime.now(timezone.utc),
        "last_sync": None,
    }
    _db.data_sources.insert_one(doc)
    log_audit(user["user_id"], "add_data_source", {"source_id": source_id, "name": req.name})
    return {"status": "ok", "source_id": source_id}


@router.get("/ui/scientist/audit_log")
async def get_audit_log(limit: int = Query(50), user=Depends(require_role("scientist"))):
    entries = list(_db.audit_log.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit))
    return {"entries": entries}


@router.get("/ui/scientist/recommendations")
async def get_recommendations(limit: int = Query(20), user=Depends(require_role("scientist"))):
    recs = list(
        _db.recommendations.find({"asset_id": ASSET_ID}, {"_id": 0})
        .sort("delivered_at", -1).limit(limit)
    )
    return {"recommendations": recs}


# ═══════════════════════════════════════════════════════════════════════════
# FARMER ENDPOINTS — uses PER-USER data only
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/ui/farmer/dashboard")
async def farmer_dashboard(parcel_id: Optional[str] = Query(None),
                            user=Depends(require_role("farmer"))):
    """Farmer dashboard — uses ONLY this user's actual data, never assumes."""
    try:
        profile = get_user(user["user_id"])
        parcels = profile.get("parcels", [])

        if not parcels:
            return {
                "configured": False,
                "message": "No parcels associated with this account.",
                "needs_onboarding": True,
            }

        # Default to first parcel if not specified
        if not parcel_id:
            parcel_id = parcels[0]
        elif parcel_id not in parcels:
            raise HTTPException(403, "You don't have access to this parcel")

        availability = get_data_availability(user["user_id"], parcel_id)

        # P0: Resolve the parcel's soil type for all computations
        soil_type = get_parcel_soil_type(user["user_id"], parcel_id)

        if not availability.get("configured"):
            return {
                "configured": False,
                "parcel_id": parcel_id,
                "soil_type": soil_type,
                "user_name": profile.get("name"),
                "message": "Welcome! Please complete onboarding to start tracking your soil.",
                "needs_onboarding": True,
            }

        if not availability.get("readings"):
            sensor_info = None
            if availability.get("mode") == "sensor":
                sensor_info = get_sensor_status(user["user_id"], parcel_id)
            return {
                "configured": True,
                "parcel_id": parcel_id,
                "soil_type": soil_type,
                "user_name": profile.get("name"),
                "mode": availability.get("mode"),
                "has_data": False,
                "sensor_status": sensor_info,
                "message": f"Your {availability.get('mode')} data source is set up, but no readings yet.",
                "needs_onboarding": availability.get("mode") == "manual",
            }

        # We have actual data — P0: pass soil_type to all helpers
        indicators = _compute_farmer_indicators_from_readings(
            availability["readings"], soil_type)
        health_score = _compute_health_score_from_readings(
            availability["readings"], soil_type)
        alerts = _compute_alerts_from_readings(
            availability["readings"], soil_type)

        # Latest user-specific recommendation (if any)
        latest_rec = _db.user_recommendations.find_one(
            {"user_id": user["user_id"], "parcel_id": parcel_id},
            {"_id": 0},
            sort=[("created_at", -1)],
        )

        # Auto-generate recommendations if we have alerts but no recommendations yet
        if not latest_rec and alerts:
            try:
                auto_recs = _auto_diagnose_from_readings(
                    availability["readings"], soil_type,
                    user["user_id"], parcel_id,
                )
                if auto_recs:
                    latest_rec = auto_recs[0]
            except Exception as e:
                print(f"[DASHBOARD] Auto-diagnosis error: {e}")

        # Recent recommendations
        recent_recs = list(
            _db.user_recommendations.find(
                {"user_id": user["user_id"], "parcel_id": parcel_id},
                {"_id": 0},
            ).sort("created_at", -1).limit(10)
        )

        # Sensor status (if in sensor mode)
        sensor_info = None
        if availability.get("mode") == "sensor":
            sensor_info = get_sensor_status(user["user_id"], parcel_id)

        # Cross-domain sync status — check if data has been received from other DTs
        cross_domain_active = False
        cross_domain_info = None
        plant_raw = get_latest_cached("cross_domain_plant_demand")
        biotic_raw = get_latest_cached("cross_domain_biotic_data")
        if plant_raw or biotic_raw:
            cross_domain_active = True
            try:
                plant_data = json.loads(plant_raw) if plant_raw else None
                biotic_data = json.loads(biotic_raw) if biotic_raw else None
                cross_domain_info = {
                    "plant_demand": plant_data,
                    "biotic_data": biotic_data,
                    "last_received": (plant_data or biotic_data or {}).get("received_at"),
                }
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "configured": True,
            "parcel_id": parcel_id,
            "soil_type": soil_type,
            "user_name": profile.get("name"),
            "user_email": profile.get("email"),
            "mode": availability.get("mode"),
            "has_data": True,
            "health_score": health_score,
            "indicators": indicators,
            "alerts": alerts,
            "completion_pct": availability.get("completion_pct", 0),
            "readings": availability["readings"],
            "fields_with_data": availability.get("fields_with_data", []),
            "fields_missing": availability.get("fields_missing", []),
            "stale_fields": availability.get("stale_fields", []),
            "latest_recommendation": latest_rec,
            "recent_recommendations": recent_recs,
            "sensor_status": sensor_info,
            "cross_domain_active": cross_domain_active,
            "cross_domain_info": cross_domain_info,
            "last_updated": availability.get("last_updated"),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Dashboard error: {type(e).__name__}: {e}")


@router.post("/ui/farmer/feedback")
async def farmer_feedback(req: FeedbackRequest, user=Depends(require_role("farmer"))):
    outcome_id = save_outcome({
        "recommendation_id": req.recommendation_id,
        "accepted": req.accepted,
        "outcome": req.outcome,
        "rating": req.rating,
        "user_id": user["user_id"],
    })
    _db.feedback.insert_one({
        "user_id": user["user_id"],
        "recommendation_id": req.recommendation_id,
        "accepted": req.accepted, "outcome": req.outcome, "rating": req.rating,
        "timestamp": datetime.now(timezone.utc), "processed": False,
    })
    log_audit(user["user_id"], "recommendation_feedback", {
        "recommendation_id": req.recommendation_id,
        "accepted": req.accepted, "rating": req.rating,
    })
    return {"status": "ok", "outcome_id": outcome_id}


@router.post("/ui/farmer/observation")
async def farmer_observation(req: FarmerObservationRequest, user=Depends(require_role("farmer"))):
    obs_id = str(uuid.uuid4())
    _db.farmer_observations.insert_one({
        "observation_id": obs_id, "user_id": user["user_id"],
        "asset_id": ASSET_ID, "text": req.text,
        "structured_fields": req.structured_fields,
        "timestamp": datetime.now(timezone.utc),
    })
    log_audit(user["user_id"], "farmer_observation", {"observation_id": obs_id})
    return {"status": "ok", "observation_id": obs_id}


@router.post("/ui/farmer/escalate")
async def farmer_escalate(user=Depends(require_role("farmer"))):
    from shared.mongo_io import save_escalation
    esc_id = save_escalation({
        "source": "farmer_ui", "user_id": user["user_id"],
        "reason": "Farmer requested expert review",
        "asset_id": ASSET_ID,
    })
    log_event("farmer_escalation", {"user_id": user["user_id"]}, severity="warning")
    return {"status": "ok", "escalation_id": esc_id}


# ═══════════════════════════════════════════════════════════════════════════
# CHAT (with full user context)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/ui/farmer/chat")
async def farmer_chat(req: ChatRequest, user=Depends(get_current_user)):
    """Chat with the LLM agent — uses user context (real name, parcel, role)."""
    profile = get_user(user["user_id"])
    parcel_id = req.parcel_id or (profile.get("parcels", [None]) or [None])[0]

    user_context = {
        "user_id": user["user_id"],
        "parcel_id": parcel_id,
        "user_name": profile.get("name", "Farmer"),
        "role": "farmer",
    }

    try:
        from intelligent.agent import ask
        answer = ask(req.question, role="farmer", user_context=user_context)
        return {"answer": answer}
    except Exception as e:
        return {"answer": f"I'm having trouble right now. Please try again. ({type(e).__name__})"}


@router.post("/ui/scientist/chat")
async def scientist_chat(req: ChatRequest, user=Depends(get_current_user)):
    profile = get_user(user["user_id"])
    parcel_id = req.parcel_id or (profile.get("parcels", [None]) or [None])[0]

    user_context = {
        "user_id": user["user_id"],
        "parcel_id": parcel_id,
        "user_name": profile.get("name", "Scientist"),
        "role": "scientist",
    }

    try:
        from intelligent.agent import ask
        answer = ask(req.question, role="scientist", user_context=user_context)
        return {"answer": answer}
    except Exception as e:
        return {"answer": f"Agent error: {type(e).__name__}"}


# ═══════════════════════════════════════════════════════════════════════════
# SHARED
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/ui/health_score")
async def get_health_score(user=Depends(get_current_user)):
    return {"health_score": get_latest_cached("soil_health_score"), "system_state": get_state()}


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════

active_connections: dict[str, list[WebSocket]] = {"scientist": [], "farmer": []}


@router.websocket("/ws/{role}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, role: str, user_id: str):
    await websocket.accept()
    active_connections.setdefault(role, []).append(websocket)
    try:
        while True:
            data = _build_live_update(role, user_id)
            await websocket.send_json(data)
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        active_connections[role].remove(websocket)
    except Exception:
        if websocket in active_connections.get(role, []):
            active_connections[role].remove(websocket)


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS — STRICT NO-ASSUMPTION FUNCTIONS
# Phase 6: All helpers now accept soil_type for per-user thresholds/ranges.
# ═══════════════════════════════════════════════════════════════════════════

def _compute_status(field: str, value, soil_type: str = None) -> str:
    """
    Compute the status of a field value against soil-type-specific
    thresholds and healthy ranges.

    P0: Uses per-soil-type thresholds instead of the global THRESHOLDS.
    """
    if value is None:
        return "no_data"
    t = get_thresholds_for_soil_type(soil_type or ACTIVE_SOIL_TYPE).get(field, {})
    hr = get_healthy_ranges_for_soil_type(soil_type or ACTIVE_SOIL_TYPE).get(field, {})
    if not t and not hr:
        return "ok"
    if t:
        low = t.get("low", True)
        if low:
            if value < t.get("crit", float("-inf")):
                return "critical"
            if value < t.get("warn", float("-inf")):
                return "warning"
        else:
            if value > t.get("crit", float("inf")):
                return "critical"
            if value > t.get("warn", float("inf")):
                return "warning"
    if hr:
        if value < hr.get("min", float("-inf")) or value > hr.get("max", float("inf")):
            return "warning"
    return "healthy"


def _compute_farmer_indicators_from_readings(readings: dict,
                                              soil_type: str = None) -> dict:
    """
    Compute simplified indicators ONLY from actual readings.
    If a field has no data, the indicator is 'no_data' — never assumed.

    P0: Uses per-soil-type thresholds instead of globals.
    """
    thresholds = get_thresholds_for_soil_type(soil_type or ACTIVE_SOIL_TYPE)
    indicators = {}

    # Nutrients (N, P, K)
    n_val = readings.get("nitrogen_ppm", {}).get("value")
    p_val = readings.get("phosphorus_ppm", {}).get("value")
    k_val = readings.get("potassium_ppm", {}).get("value")
    nutrient_data = [(n_val, "nitrogen_ppm"), (p_val, "phosphorus_ppm"), (k_val, "potassium_ppm")]
    if all(v is None for v, _ in nutrient_data):
        indicators["nutrients"] = {"status": "no_data", "label": "Nutrient Availability"}
    else:
        scores = []
        for v, fname in nutrient_data:
            if v is not None:
                t = thresholds.get(fname, {})
                if t and v < t.get("crit", 0):
                    scores.append("poor")
                elif t and v < t.get("warn", 0):
                    scores.append("moderate")
                else:
                    scores.append("good")
        if not scores:
            indicators["nutrients"] = {"status": "no_data", "label": "Nutrient Availability"}
        elif "poor" in scores:
            indicators["nutrients"] = {"status": "poor", "label": "Nutrient Availability"}
        elif "moderate" in scores:
            indicators["nutrients"] = {"status": "moderate", "label": "Nutrient Availability"}
        else:
            indicators["nutrients"] = {"status": "good", "label": "Nutrient Availability"}

    # Moisture
    mst = readings.get("soil_moisture_pct", {}).get("value")
    if mst is None:
        indicators["moisture"] = {"status": "no_data", "label": "Soil Moisture"}
    else:
        lo = thresholds.get("soil_moisture_pct", {})
        hi = thresholds.get("soil_moisture_pct_high", {})
        if lo and mst < lo.get("warn", 0):
            indicators["moisture"] = {"status": "dry", "label": "Soil Moisture"}
        elif hi and mst > hi.get("warn", 100):
            indicators["moisture"] = {"status": "waterlogged", "label": "Soil Moisture"}
        else:
            indicators["moisture"] = {"status": "adequate", "label": "Soil Moisture"}

    # Acidity
    ph = readings.get("soil_ph", {}).get("value")
    if ph is None:
        indicators["acidity"] = {"status": "no_data", "label": "Soil Acidity"}
    else:
        t = thresholds.get("soil_ph", {})
        if t and ph < t.get("warn", 0):
            indicators["acidity"] = {"status": "needs_lime", "label": "Soil Acidity"}
        else:
            indicators["acidity"] = {"status": "optimal", "label": "Soil Acidity"}

    # Soil life
    mb = readings.get("microbial_biomass_mg_c_kg", {}).get("value")
    resp = readings.get("soil_respiration_mg_co2_kg_day", {}).get("value")
    if mb is None and resp is None:
        indicators["soil_life"] = {"status": "no_data", "label": "Soil Life"}
    else:
        bio_ok = True
        if mb is not None:
            t = thresholds.get("microbial_biomass_mg_c_kg", {})
            if t and mb < t.get("warn", 0):
                bio_ok = False
        if resp is not None:
            t = thresholds.get("soil_respiration_mg_co2_kg_day", {})
            if t and resp < t.get("warn", 0):
                bio_ok = False
        indicators["soil_life"] = {"status": "active" if bio_ok else "slow", "label": "Soil Life"}

    return indicators


def _compute_health_score_from_readings(readings: dict,
                                         soil_type: str = None) -> Optional[float]:
    """
    Compute health score ONLY from available readings. None if no data.

    P2: Now uses weighted average with PARAMETER_WEIGHTS instead of simple average.
    Higher-weight parameters (N, P, K, pH) have more impact on the score.
    """
    if not readings:
        return None

    weighted_sum = 0.0
    total_weight = 0.0

    for field, info in readings.items():
        val = info.get("value")
        if val is None:
            continue
        status = _compute_status(field, val, soil_type)
        weight = PARAMETER_WEIGHTS.get(field, 1.0)

        if status == "healthy":
            score = 100
        elif status == "warning":
            score = 60
        elif status == "critical":
            score = 20
        else:
            continue

        weighted_sum += score * weight
        total_weight += weight

    return round(weighted_sum / total_weight, 1) if total_weight > 0 else None


def _compute_alerts_from_readings(readings: dict,
                                   soil_type: str = None) -> list:
    """
    Compute alerts from actual readings only — never assumes.

    P0: Uses per-soil-type thresholds instead of globals.
    """
    thresholds = get_thresholds_for_soil_type(soil_type or ACTIVE_SOIL_TYPE)
    alerts = []

    n_val = readings.get("nitrogen_ppm", {}).get("value")
    p_val = readings.get("phosphorus_ppm", {}).get("value")
    k_val = readings.get("potassium_ppm", {}).get("value")
    if any(v is not None for v in (n_val, p_val, k_val)):
        for v, lbl in [(n_val, "Nitrogen"), (p_val, "Phosphorus"), (k_val, "Potassium")]:
            if v is not None:
                fname = {"Nitrogen": "nitrogen_ppm", "Phosphorus": "phosphorus_ppm", "Potassium": "potassium_ppm"}[lbl]
                t = thresholds.get(fname, {})
                if t and v < t.get("crit", 0):
                    alerts.append({"message": f"{lbl} is critically low", "severity": "critical"})
                elif t and v < t.get("warn", 0):
                    alerts.append({"message": f"{lbl} is low", "severity": "warning"})

    ph = readings.get("soil_ph", {}).get("value")
    if ph is not None:
        t = thresholds.get("soil_ph", {})
        if t and ph < t.get("crit", 0):
            alerts.append({"message": "Your soil is critically acidic", "severity": "critical"})
        elif t and ph < t.get("warn", 0):
            alerts.append({"message": "Your soil is too acidic", "severity": "warning"})

    om = readings.get("organic_matter_pct", {}).get("value")
    if om is not None:
        t = thresholds.get("organic_matter_pct", {})
        if t and om < t.get("warn", 0):
            alerts.append({"message": "Organic matter is low", "severity": "warning"})

    return alerts


def _auto_diagnose_from_readings(readings: dict, soil_type: str,
                                   user_id: str, parcel_id: str) -> list:
    """
    Run a lightweight diagnosis from the user's manual readings and
    store recommendations in user_recommendations.

    This bridges the gap between manual data entry and the full pipeline
    (which reads from InfluxDB sensors). For manually-entered data, we
    construct a soil_data dict from readings and run the intelligence agent.
    """
    from intelligent.soil_intelligence_agent import SoilIntelligenceAgent

    # Build soil_data dict from readings
    soil_data = {}
    for field, info in readings.items():
        val = info.get("value")
        if val is not None:
            soil_data[field] = val

    if not soil_data:
        return []

    # Run the intelligence agent with user's soil type
    agent = SoilIntelligenceAgent(soil_type=soil_type)
    depletion = agent.detect_depletion_states(soil_data)

    if not depletion:
        return []

    diagnoses = agent.differential_diagnosis(soil_data, depletion)
    if not diagnoses:
        return []

    recommendations = agent.generate_recommendations(diagnoses, soil_data)

    # Store each recommendation as a user_recommendation
    stored = []
    for rec in recommendations:
        rec_doc = {
            **rec,
            "user_id": user_id,
            "parcel_id": parcel_id,
            "soil_type": soil_type,
            "recommendation_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc),
            "source": "auto_diagnosis",
        }
        _db.user_recommendations.insert_one(rec_doc)
        rec_doc.pop("_id", None)
        stored.append(rec_doc)

    print(f"[DASHBOARD] Auto-generated {len(stored)} recommendations for {user_id}/{parcel_id}")
    return stored


def _build_live_update(role: str, user_id: str = None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "health_score": get_latest_cached("soil_health_score"),
        "system_state": get_state(),
    }