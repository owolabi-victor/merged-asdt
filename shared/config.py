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
    # Chemical
    "soil_ph",                          # pH (0–14)
    "nitrogen_ppm",                     # Available nitrogen (ppm)
    "phosphorus_ppm",                   # Available phosphorus (mg/kg)
    "potassium_ppm",                    # Available potassium (mg/kg)
    "ec_ds_m",                          # Electrical conductivity / salinity (dS/m)
    "organic_matter_pct",               # Soil organic matter (%)
    # Biological
    "microbial_biomass_mg_c_kg",        # Microbial biomass carbon (mg C/kg)
    "soil_respiration_mg_co2_kg_day",   # Soil respiration (mg CO₂/kg/day)
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
        "soil_ph":                        {"warn": 5.0,   "crit": 4.5,   "low": True},
        "soil_ph_high":                   {"warn": 8.0,   "crit": 8.5,   "low": False},
        "nitrogen_ppm":                   {"warn": 10.0,  "crit": 5.0,   "low": True},
        "phosphorus_ppm":                 {"warn": 8.0,   "crit": 5.0,   "low": True},
        "potassium_ppm":                  {"warn": 100.0, "crit": 70.0,  "low": True},
        "ec_ds_m":                        {"warn": 3.0,   "crit": 4.0,   "low": False},
        "organic_matter_pct":             {"warn": 1.5,   "crit": 1.0,   "low": True},
        "microbial_biomass_mg_c_kg":      {"warn": 150.0, "crit": 100.0, "low": True},
        "soil_respiration_mg_co2_kg_day": {"warn": 25.0,  "crit": 15.0,  "low": True},
    },
    "sandy": {
        "soil_moisture_pct":              {"warn": 12.0,  "crit": 8.0,   "low": True},
        "soil_moisture_pct_high":         {"warn": 40.0,  "crit": 45.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.7,   "crit": 1.85,  "low": False},
        "soil_ph":                        {"warn": 5.0,   "crit": 4.5,   "low": True},
        "soil_ph_high":                   {"warn": 8.0,   "crit": 8.5,   "low": False},
        "nitrogen_ppm":                   {"warn": 8.0,   "crit": 4.0,   "low": True},
        "phosphorus_ppm":                 {"warn": 6.0,   "crit": 3.0,   "low": True},
        "potassium_ppm":                  {"warn": 80.0,  "crit": 50.0,  "low": True},
        "ec_ds_m":                        {"warn": 2.5,   "crit": 3.5,   "low": False},
        "organic_matter_pct":             {"warn": 1.0,   "crit": 0.5,   "low": True},
        "microbial_biomass_mg_c_kg":      {"warn": 120.0, "crit": 80.0,  "low": True},
        "soil_respiration_mg_co2_kg_day": {"warn": 20.0,  "crit": 12.0,  "low": True},
    },
    "clay": {
        "soil_moisture_pct":              {"warn": 18.0,  "crit": 12.0,  "low": True},
        "soil_moisture_pct_high":         {"warn": 48.0,  "crit": 55.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.5,   "crit": 1.7,   "low": False},
        "soil_ph":                        {"warn": 5.0,   "crit": 4.5,   "low": True},
        "soil_ph_high":                   {"warn": 8.0,   "crit": 8.5,   "low": False},
        "nitrogen_ppm":                   {"warn": 12.0,  "crit": 6.0,   "low": True},
        "phosphorus_ppm":                 {"warn": 10.0,  "crit": 6.0,   "low": True},
        "potassium_ppm":                  {"warn": 120.0, "crit": 80.0,  "low": True},
        "ec_ds_m":                        {"warn": 3.5,   "crit": 4.5,   "low": False},
        "organic_matter_pct":             {"warn": 2.0,   "crit": 1.2,   "low": True},
        "microbial_biomass_mg_c_kg":      {"warn": 180.0, "crit": 120.0, "low": True},
        "soil_respiration_mg_co2_kg_day": {"warn": 30.0,  "crit": 18.0,  "low": True},
    },
    "silty": {
        "soil_moisture_pct":              {"warn": 14.0,  "crit": 9.0,   "low": True},
        "soil_moisture_pct_high":         {"warn": 44.0,  "crit": 50.0,  "low": False},
        "bulk_density_g_cm3":             {"warn": 1.55,  "crit": 1.75,  "low": False},
        "soil_ph":                        {"warn": 5.0,   "crit": 4.5,   "low": True},
        "soil_ph_high":                   {"warn": 8.0,   "crit": 8.5,   "low": False},
        "nitrogen_ppm":                   {"warn": 9.0,   "crit": 4.5,   "low": True},
        "phosphorus_ppm":                 {"warn": 7.0,   "crit": 4.0,   "low": True},
        "potassium_ppm":                  {"warn": 90.0,  "crit": 60.0,  "low": True},
        "ec_ds_m":                        {"warn": 2.8,   "crit": 3.8,   "low": False},
        "organic_matter_pct":             {"warn": 1.3,   "crit": 0.8,   "low": True},
        "microbial_biomass_mg_c_kg":      {"warn": 140.0, "crit": 90.0,  "low": True},
        "soil_respiration_mg_co2_kg_day": {"warn": 22.0,  "crit": 14.0,  "low": True},
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
        "soil_ph":                        {"min": 5.5,   "max": 7.0,   "unit": "pH"},
        "nitrogen_ppm":                   {"min": 15.0,  "max": 25.0,  "unit": "ppm"},
        "phosphorus_ppm":                 {"min": 15.0,  "max": 30.0,  "unit": "mg/kg"},
        "potassium_ppm":                  {"min": 150.0, "max": 250.0, "unit": "mg/kg"},
        "ec_ds_m":                        {"min": 0.0,   "max": 1.5,   "unit": "dS/m"},
        "organic_matter_pct":             {"min": 2.0,   "max": 4.0,   "unit": "%"},
        "microbial_biomass_mg_c_kg":      {"min": 300.0, "max": 600.0, "unit": "mg C/kg"},
        "soil_respiration_mg_co2_kg_day": {"min": 50.0,  "max": 150.0, "unit": "mg CO₂/kg/day"},
    },
    "sandy": {
        "soil_moisture_pct":              {"min": 10.0,  "max": 30.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.4,   "max": 1.6,   "unit": "g/cm³"},
        "soil_ph":                        {"min": 5.5,   "max": 7.0,   "unit": "pH"},
        "nitrogen_ppm":                   {"min": 10.0,  "max": 20.0,  "unit": "ppm"},
        "phosphorus_ppm":                 {"min": 10.0,  "max": 25.0,  "unit": "mg/kg"},
        "potassium_ppm":                  {"min": 100.0, "max": 200.0, "unit": "mg/kg"},
        "ec_ds_m":                        {"min": 0.0,   "max": 1.2,   "unit": "dS/m"},
        "organic_matter_pct":             {"min": 1.0,   "max": 3.0,   "unit": "%"},
        "microbial_biomass_mg_c_kg":      {"min": 200.0, "max": 450.0, "unit": "mg C/kg"},
        "soil_respiration_mg_co2_kg_day": {"min": 30.0,  "max": 100.0, "unit": "mg CO₂/kg/day"},
    },
    "clay": {
        "soil_moisture_pct":              {"min": 25.0,  "max": 45.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.0,   "max": 1.3,   "unit": "g/cm³"},
        "soil_ph":                        {"min": 5.5,   "max": 7.5,   "unit": "pH"},
        "nitrogen_ppm":                   {"min": 18.0,  "max": 30.0,  "unit": "ppm"},
        "phosphorus_ppm":                 {"min": 15.0,  "max": 35.0,  "unit": "mg/kg"},
        "potassium_ppm":                  {"min": 160.0, "max": 280.0, "unit": "mg/kg"},
        "ec_ds_m":                        {"min": 0.0,   "max": 2.0,   "unit": "dS/m"},
        "organic_matter_pct":             {"min": 2.5,   "max": 5.0,   "unit": "%"},
        "microbial_biomass_mg_c_kg":      {"min": 350.0, "max": 700.0, "unit": "mg C/kg"},
        "soil_respiration_mg_co2_kg_day": {"min": 60.0,  "max": 180.0, "unit": "mg CO₂/kg/day"},
    },
    "silty": {
        "soil_moisture_pct":              {"min": 18.0,  "max": 38.0,  "unit": "%"},
        "bulk_density_g_cm3":             {"min": 1.1,   "max": 1.4,   "unit": "g/cm³"},
        "soil_ph":                        {"min": 5.5,   "max": 7.0,   "unit": "pH"},
        "nitrogen_ppm":                   {"min": 12.0,  "max": 22.0,  "unit": "ppm"},
        "phosphorus_ppm":                 {"min": 12.0,  "max": 28.0,  "unit": "mg/kg"},
        "potassium_ppm":                  {"min": 120.0, "max": 220.0, "unit": "mg/kg"},
        "ec_ds_m":                        {"min": 0.0,   "max": 1.5,   "unit": "dS/m"},
        "organic_matter_pct":             {"min": 1.5,   "max": 3.5,   "unit": "%"},
        "microbial_biomass_mg_c_kg":      {"min": 250.0, "max": 550.0, "unit": "mg C/kg"},
        "soil_respiration_mg_co2_kg_day": {"min": 40.0,  "max": 130.0, "unit": "mg CO₂/kg/day"},
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
    "nitrogen_ppm":                   3.0,   # critical nutrient
    "phosphorus_ppm":                 2.5,   # critical nutrient
    "potassium_ppm":                  2.5,   # critical nutrient
    "soil_ph":                        2.5,   # controls nutrient availability
    "organic_matter_pct":             2.0,   # foundation of soil health
    "ec_ds_m":                        1.5,   # salinity stress
    "soil_moisture_pct":              1.5,   # water availability
    "microbial_biomass_mg_c_kg":      1.5,   # biological activity
    "soil_respiration_mg_co2_kg_day": 1.5,   # biological activity
    "bulk_density_g_cm3":             1.0,   # physical compaction
    "soil_temp_c":                    0.5,   # least controllable
}

# ── Soil Depletion States (S1–S8) ────────────────────────────────────────────
DEPLETION_STATES = {
    "healthy": {
        "name":       "Healthy",
        "definition": "All soil parameters within acceptable ranges",
    },
    "S1": {
        "name":       "Nutrient Depleted",
        "definition": "N, P, or K below threshold",
        "trigger":    "N < depleted OR P < depleted OR K < depleted",
        "recovery":   "Apply specific fertiliser (N, P, or K)",
        "category":   "chemical",
    },
    "S2": {
        "name":       "Acidified",
        "definition": "pH too low for optimal soil function",
        "trigger":    "pH < 5.0",
        "recovery":   "Apply agricultural lime",
        "category":   "chemical",
    },
    "S3": {
        "name":       "Salinized",
        "definition": "Salt accumulation affecting soil",
        "trigger":    "EC > 3.0 dS/m",
        "recovery":   "Leaching, gypsum application",
        "category":   "chemical",
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
    "S6": {
        "name":       "Organic Matter Depleted",
        "definition": "Soil organic carbon below threshold",
        "trigger":    "OM < depleted threshold",
        "recovery":   "Organic amendments, cover crops",
        "category":   "chemical",
    },
    "S7": {
        "name":       "Biologically Inactive",
        "definition": "Microbial activity too low",
        "trigger":    "MB < depleted OR Resp < depleted",
        "recovery":   "Organic matter addition, reduced tillage",
        "category":   "biological",
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