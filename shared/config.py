# shared/config.py
"""
All asset-specific constants for the Agentic Soil Digital Twin (ASDT).
This is the ONE file you edit to change behaviour across all layers.

Situation: A soil parcel has been continuously cropped for multiple seasons
without adequate soil management. The ASDT detects soil depletion across
physical, chemical, and biological parameters and recommends soil-focused
management actions.

Phase 6 changes:
  - P0: Added get_parcel_soil_type() / get_thresholds_for_soil_type() /
         get_healthy_ranges_for_soil_type() helpers so every module can
         look up per-user soil type from MongoDB instead of the global.
         ACTIVE_SOIL_TYPE and THRESHOLDS remain as system-level fallbacks
         (used by simulator, reactive layer for global telemetry).
  - P1: Added HEALTHY_RANGES_BY_SOIL_TYPE — per-soil-type healthy ranges.
  - P2: Added PARAMETER_WEIGHTS for weighted health score calculation.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Asset Identity ────────────────────────────────────────────────────────────
ASSET_ID   = os.getenv("ASSET_ID",   "soil_parcel_001")
ASSET_TYPE = os.getenv("ASSET_TYPE", "soil_parcel")

# ── MQTT ──────────────────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))
# Hosted brokers (HiveMQ Cloud and friends) are TLS-only and require auth.
# Empty username keeps the local anonymous broker working unchanged.
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_TLS      = os.getenv("MQTT_TLS", "").lower() in ("1", "true", "yes")
TOPIC_TELEMETRY = f"dt/{ASSET_ID}/telemetry"
TOPIC_CONTROL   = f"dt/{ASSET_ID}/control"
TOPIC_STATE     = f"dt/{ASSET_ID}/state"
TOPIC_CROSS_DOMAIN = f"dt/{ASSET_ID}/cross_domain"

# ── Sensor fields ─────────────────────────────────────────────────────────────
# Complete set of soil parameters: Physical (3), Chemical (6), Biological (2)
SENSOR_FIELDS = [
    # Physical
    "soil_moisture_pct",                # Volumetric water content (%)
    "bulk_density_g_cm3",               # Bulk density (g/cm³)
    "soil_temp_c",                      # Soil temperature at 10 cm depth (°C)
]

# Atmospheric forcing — measured by the node's DHT11, stored alongside the soil
# parameters because soil evaporation is driven by them. Deliberately NOT in
# SENSOR_FIELDS: thresholds, health scoring and depletion detection describe the
# state of the soil, and air is not a soil parameter. These are inputs, not
# things that can be "depleted".
FORCING_FIELDS = [
    "air_temperature_c",                # °C, DHT11
    "relative_humidity_pct",            # %,  DHT11
]

# Texture fields — measured once per soil type, not continuously monitored
TEXTURE_FIELDS = ["sand_pct", "silt_pct", "clay_pct"]

# ── Soil Types ────────────────────────────────────────────────────────────────
SOIL_TYPES = {
    "sandy": {"sand_pct": 75.0, "silt_pct": 15.0, "clay_pct": 10.0,
              "description": "Low water holding, low CEC, prone to nutrient leaching",
              "depletion_sensitivity": "very_high"},
    "loamy": {"sand_pct": 40.0, "silt_pct": 40.0, "clay_pct": 20.0,
              "description": "Balanced properties, good water holding, moderate CEC",
              "depletion_sensitivity": "moderate"},
    "clay":  {"sand_pct": 20.0, "silt_pct": 35.0, "clay_pct": 45.0,
              "description": "High water holding, high CEC, prone to compaction",
              "depletion_sensitivity": "low"},
    "silty": {"sand_pct": 10.0, "silt_pct": 82.0, "clay_pct":  8.0,
              "description": "Moderate water holding, erosion prone",
              "depletion_sensitivity": "moderate_high"},
}

# ── Soil-type-specific thresholds ─────────────────────────────────────────────
# low=True  → alert fires when value FALLS below threshold
# low=False → alert fires when value RISES above threshold
THRESHOLDS_BY_SOIL_TYPE = {
    "loamy": {
        "soil_moisture_pct":              {"warn": 15.0,  "crit": 10.0,  "low": True},
        "soil_moisture_pct_high":         {"warn": 45.0,  "crit": 50.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.6,   "crit": 1.8,   "low": False},
    },
    "sandy": {
        "soil_moisture_pct":              {"warn": 12.0,  "crit": 8.0,   "low": True},
        "soil_moisture_pct_high":         {"warn": 40.0,  "crit": 45.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.7,   "crit": 1.85,  "low": False},
    },
    "clay": {
        "soil_moisture_pct":              {"warn": 18.0,  "crit": 12.0,  "low": True},
        "soil_moisture_pct_high":         {"warn": 48.0,  "crit": 55.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.5,   "crit": 1.7,   "low": False},
    },
    "silty": {
        "soil_moisture_pct":              {"warn": 14.0,  "crit": 9.0,   "low": True},
        "soil_moisture_pct_high":         {"warn": 44.0,  "crit": 50.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.55,  "crit": 1.75,  "low": False},
    },
}

# Default thresholds — uses loamy as safe default
# NOTE: This global is used ONLY by the simulator, reactive rule engine, and
# other system-level components that operate on global telemetry (not per-user).
# Per-user code should call get_thresholds_for_soil_type(soil_type) instead.
ACTIVE_SOIL_TYPE = os.getenv("ACTIVE_SOIL_TYPE", "loamy")
THRESHOLDS = THRESHOLDS_BY_SOIL_TYPE.get(ACTIVE_SOIL_TYPE, THRESHOLDS_BY_SOIL_TYPE["loamy"])

# ── Healthy ranges per soil type (P1) ────────────────────────────────────────
# Each soil type has different optimal ranges for parameters.
HEALTHY_RANGES_BY_SOIL_TYPE = {
    "loamy": {
        "soil_moisture_pct":              {"min": 20.0,  "max": 40.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.2,   "max": 1.4,   "unit": "g/cm³"},
    },
    "sandy": {
        "soil_moisture_pct":              {"min": 10.0,  "max": 30.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.4,   "max": 1.6,   "unit": "g/cm³"},
    },
    "clay": {
        "soil_moisture_pct":              {"min": 25.0,  "max": 45.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.0,   "max": 1.3,   "unit": "g/cm³"},
    },
    "silty": {
        "soil_moisture_pct":              {"min": 18.0,  "max": 38.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.1,   "max": 1.4,   "unit": "g/cm³"},
    },
}

# Legacy global HEALTHY_RANGES — defaults to loamy for backward compatibility.
# Per-user code should call get_healthy_ranges_for_soil_type(soil_type) instead.
HEALTHY_RANGES = HEALTHY_RANGES_BY_SOIL_TYPE["loamy"]

# ── Parameter importance weights for health score (P2) ───────────────────────
# Higher weight = more impact on the overall health score.
# Nutrient and pH parameters are weighted higher because they directly affect
# crop yield and soil function. Physical/biological parameters are important
# but have a slower impact trajectory.
PARAMETER_WEIGHTS = {
    "soil_moisture_pct":              1.5,   # water availability
    "bulk_density_g_cm3":             1.0,   # physical compaction
    "soil_temp_c":                    0.5,   # least controllable
}

# ── Soil Depletion States (physical only) ────────────────────────────────────────────
DEPLETION_STATES = {
    "healthy": {
        "name":       "Healthy",
        "definition": "All soil parameters within acceptable ranges",
    },
    "S4": {
        "name":       "Compacted",
        "definition": "Bulk density too high",
        "trigger":    "BD > depleted threshold for soil type",
        "recovery":   "Deep ripping, reduced tillage",
        "category":   "physical",
    },
    "S5": {
        "name":       "Water-Stressed",
        "definition": "Moisture extremes (too dry or waterlogged)",
        "trigger":    "VWC < depleted (dry) OR VWC > depleted (wet)",
        "recovery":   "Irrigation (dry) or drainage (wet)",
        "category":   "physical",
    },
    "S8": {
        "name":       "Multi-Factor Depleted",
        "definition": "Two or more depletion states active simultaneously",
        "trigger":    "Two or more of S1–S7 active",
        "recovery":   "Integrated soil restoration plan",
        "category":   "multi",
    },
}

# ── Auth / JWT ───────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "asdt-super-secret-jwt-key-change-in-production")

# ── InfluxDB ──────────────────────────────────────────────────────────────────
INFLUX_URL    = os.getenv("INFLUXDB_URL",   "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUXDB_TOKEN", "my-super-secret-token")
INFLUX_ORG    = os.getenv("INFLUXDB_ORG",   "asdt")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET","soil_telemetry")

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://admin:password@localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "asdt")

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ── Eclipse Ditto / Twin API ─────────────────────────────────────────────────
DITTO_URL  = os.getenv("DITTO_URL",      "http://localhost:8080")
DITTO_USER = os.getenv("DITTO_USER",     "ditto")
DITTO_PASS = os.getenv("DITTO_PASSWORD", "ditto")
DITTO_NS   = os.getenv("DITTO_NAMESPACE","org.asdt")
THING_ID   = f"{DITTO_NS}:{ASSET_ID}"

# ── Neo4j ─────────────────────────────────────────────────────────────────────
NEO4J_URI  = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "password")

# ── MinIO ─────────────────────────────────────────────────────────────────────
MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET     = f"asdt-{ASSET_ID.replace('_','-')}"

# ── Ollama Cloud (native Ollama client) ──────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen3.5")
OLLAMA_API_KEY  = os.getenv("OLLAMA_API_KEY",  "")


# ═══════════════════════════════════════════════════════════════════════════
# PER-USER SOIL TYPE HELPERS (P0)
# ═══════════════════════════════════════════════════════════════════════════
# These functions allow any module to resolve a parcel's soil type from
# MongoDB instead of using the global ACTIVE_SOIL_TYPE. They are safe to
# call from any layer — they create their own MongoDB connection lazily.

_parcel_db = None

def _get_parcel_db():
    """Lazy MongoDB connection for parcel lookups (avoids import-time side effects)."""
    global _parcel_db
    if _parcel_db is None:
        from pymongo import MongoClient
        _parcel_db = MongoClient(MONGO_URI)[MONGO_DB]
    return _parcel_db


def get_parcel_soil_type(user_id: str, parcel_id: str) -> str:
    """
    Look up the soil type for a user's parcel from MongoDB.
    Falls back to ACTIVE_SOIL_TYPE if not set.

    The soil type is stored in the user_parcels collection, set during
    onboarding or profile update.
    """
    db = _get_parcel_db()
    doc = db.user_parcels.find_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"soil_type": 1},
    )
    if doc and doc.get("soil_type") in SOIL_TYPES:
        return doc["soil_type"]
    return ACTIVE_SOIL_TYPE


def set_parcel_soil_type(user_id: str, parcel_id: str, soil_type: str) -> bool:
    """
    Store the soil type for a user's parcel in MongoDB.
    Returns True on success, False if soil_type is invalid.
    """
    if soil_type not in SOIL_TYPES:
        return False
    db = _get_parcel_db()
    db.user_parcels.update_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"$set": {"soil_type": soil_type}},
        upsert=True,
    )
    return True


def get_thresholds_for_soil_type(soil_type: str) -> dict:
    """
    Get the threshold dict for a given soil type.
    Falls back to loamy if the soil type is unknown.
    """
    return THRESHOLDS_BY_SOIL_TYPE.get(soil_type, THRESHOLDS_BY_SOIL_TYPE["loamy"])


def get_healthy_ranges_for_soil_type(soil_type: str) -> dict:
    """
    Get healthy ranges for a given soil type.
    Falls back to loamy if the soil type is unknown.
    """
    return HEALTHY_RANGES_BY_SOIL_TYPE.get(soil_type, HEALTHY_RANGES_BY_SOIL_TYPE["loamy"])


def get_parcel_thresholds(user_id: str, parcel_id: str) -> dict:
    """
    Get thresholds for a specific parcel, applying any scientist-defined
    overrides on top of the soil-type defaults.
    """
    import copy
    soil_type = get_parcel_soil_type(user_id, parcel_id)
    base = copy.deepcopy(get_thresholds_for_soil_type(soil_type))
    db = _get_parcel_db()
    doc = db.parcel_thresholds.find_one(
        {"user_id": user_id, "parcel_id": parcel_id}, {"_id": 0, "thresholds": 1}
    )
    if doc and doc.get("thresholds"):
        for field, overrides in doc["thresholds"].items():
            if field in base:
                base[field].update(overrides)
            else:
                base[field] = overrides
    return base


def get_parcel_healthy_ranges(user_id: str, parcel_id: str) -> dict:
    """
    Get healthy ranges for a specific parcel, applying scientist-defined
    overrides on top of the soil-type defaults.
    """
    import copy
    soil_type = get_parcel_soil_type(user_id, parcel_id)
    base = copy.deepcopy(get_healthy_ranges_for_soil_type(soil_type))
    db = _get_parcel_db()
    doc = db.parcel_thresholds.find_one(
        {"user_id": user_id, "parcel_id": parcel_id}, {"_id": 0, "healthy_ranges": 1}
    )
    if doc and doc.get("healthy_ranges"):
        for field, overrides in doc["healthy_ranges"].items():
            if field in base:
                base[field].update(overrides)
            else:
                base[field] = overrides
    return base


def save_parcel_soil_params(user_id: str, parcel_id: str,
                             thresholds: dict = None,
                             healthy_ranges: dict = None) -> None:
    """Persist scientist-defined thresholds and/or healthy ranges for a parcel."""
    from datetime import datetime, timezone
    db = _get_parcel_db()
    update: dict = {"updated_at": datetime.now(timezone.utc)}
    if thresholds is not None:
        update["thresholds"] = thresholds
    if healthy_ranges is not None:
        update["healthy_ranges"] = healthy_ranges
    db.parcel_thresholds.update_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"$set": update},
        upsert=True,
    )


def _find_parcel_owner(asset_id: str) -> str | None:
    """
    Return the user_id of the scientist who owns this parcel.
    Checks user_parcels first (populated by onboarding agent), then falls
    back to the parcels array stored directly on the users document.
    """
    db = _get_parcel_db()
    doc = db.user_parcels.find_one({"parcel_id": asset_id}, {"user_id": 1})
    if doc and doc.get("user_id"):
        return doc["user_id"]
    user = db.users.find_one({"parcels": asset_id}, {"user_id": 1})
    if user and user.get("user_id"):
        return user["user_id"]
    return None


def get_asset_thresholds(asset_id: str) -> dict:
    """
    Get effective thresholds for a physical asset (parcel ID) used by
    background layers (rule engine, pipeline). Applies any scientist-defined
    overrides on top of soil-type defaults; falls back to ACTIVE_SOIL_TYPE
    if the parcel has no registered owner yet.
    """
    user_id = _find_parcel_owner(asset_id)
    if user_id:
        return get_parcel_thresholds(user_id, asset_id)
    return get_thresholds_for_soil_type(ACTIVE_SOIL_TYPE)


def get_asset_healthy_ranges(asset_id: str) -> dict:
    """
    Get effective healthy ranges for a physical asset (parcel ID).
    Same ownership lookup as get_asset_thresholds().
    """
    user_id = _find_parcel_owner(asset_id)
    if user_id:
        return get_parcel_healthy_ranges(user_id, asset_id)
    return get_healthy_ranges_for_soil_type(ACTIVE_SOIL_TYPE)