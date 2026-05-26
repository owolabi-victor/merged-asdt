# service_layer/ditto_client.py
"""
Eclipse Ditto / Twin API client — creates and manages the Soil Parcel Digital Twin Thing.
Updated for the soil depletion situation with all 11 sensor fields and depletion state tracking.
"""
import time
import httpx
import json

from shared.config   import DITTO_URL, DITTO_USER, DITTO_PASS, DITTO_NS, THING_ID, ASSET_ID
from shared.mongo_io import get_asset_metadata

AUTH = (DITTO_USER, DITTO_PASS)
HDR  = {"Content-Type": "application/json"}


def _put(url: str, payload: dict, retries: int = 5, delay: int = 15) -> int:
    """PUT with retry — handles connection resets during startup."""
    for attempt in range(1, retries + 1):
        try:
            r = httpx.put(url, auth=AUTH, headers=HDR, json=payload, timeout=30)
            if r.status_code in (200, 201, 204):
                return r.status_code
            print(f"[DITTO] PUT → HTTP {r.status_code} (attempt {attempt}/{retries})")
        except Exception as e:
            print(f"[DITTO] Attempt {attempt}/{retries} failed: {type(e).__name__}")
        if attempt < retries:
            print(f"[DITTO] Retrying in {delay}s...")
            time.sleep(delay)
    print(f"[DITTO] ⚠️  Could not reach Twin API after {retries} attempts — skipping.")
    return 0


def create_policy():
    """Create a permissive development policy."""
    policy = {
        "policyId": f"{DITTO_NS}:default_policy",
        "entries": {
            "DEFAULT": {
                "subjects": {"nginx:ditto": {"type": "nginx basic auth user"}},
                "resources": {
                    "thing:/":   {"grant": ["READ", "WRITE"], "revoke": []},
                    "policy:/":  {"grant": ["READ", "WRITE"], "revoke": []},
                    "message:/": {"grant": ["READ", "WRITE"], "revoke": []},
                },
            }
        },
    }
    status = _put(f"{DITTO_URL}/api/2/policies/{DITTO_NS}:default_policy", policy)
    print(f"[DITTO] Create policy: {status if status else 'skipped'}")


def build_thing_template() -> dict:
    meta = get_asset_metadata()
    return {
        "thingId":  THING_ID,
        "policyId": f"{DITTO_NS}:default_policy",
        "attributes": {
            "name":         meta.get("name",         "Soil Parcel 001"),
            "location":     meta.get("location",     "unknown"),
            "area_ha":      meta.get("area_ha",      0.0),
            "soil_type":    meta.get("soil_type",     "loamy"),
            "soil_texture": meta.get("soil_texture",  "unknown"),
            "asset_type":   meta.get("asset_type",    "soil_parcel"),
        },
        "features": {
            "telemetry": {"properties": {
                # Physical
                "soil_moisture_pct": 0.0,
                "bulk_density_g_cm3": 0.0,
                "soil_temp_c": 0.0,
                # Chemical
                "soil_ph": 0.0,
                "nitrogen_ppm": 0.0,
                "phosphorus_ppm": 0.0,
                "potassium_ppm": 0.0,
                "ec_ds_m": 0.0,
                "organic_matter_pct": 0.0,
                # Biological
                "microbial_biomass_mg_c_kg": 0.0,
                "soil_respiration_mg_co2_kg_day": 0.0,
            }},
            "texture": {"properties": {
                "sand_pct": 0.0,
                "silt_pct": 0.0,
                "clay_pct": 0.0,
            }},
            "health": {"properties": {
                "soil_health_score": 100.0,
                "is_anomaly": False,
                "operational_state": "running",
                "active_depletion_states": [],
            }},
            "depletion": {"properties": {
                "active_states": [],
                "primary_state": None,
                "confidence": 0.0,
                "last_updated": None,
            }},
            "recommendation": {"properties": {
                "recommendation_type": None,
                "product": None,
                "rate_kg_ha": None,
                "timing": None,
                "method": None,
                "last_updated": None,
            }},
            "cross_domain": {"properties": {
                "publishes_to": ["plant_dt", "biotic_pod_dt"],
                "subscribes_from": ["plant_dt", "biotic_pod_dt"],
                "last_published": None,
                "plant_water_demand_mm_day": None,
                "crop_phenology_stage": None,
                "microbial_diversity_index": None,
            }},
        },
    }


def create_thing():
    thing = build_thing_template()
    status = _put(f"{DITTO_URL}/api/2/things/{THING_ID}", thing)
    print(f"[DITTO] Create thing: {status if status else 'skipped'} — {THING_ID}")


def update_feature(feature: str, props: dict) -> int:
    try:
        r = httpx.patch(
            f"{DITTO_URL}/api/2/things/{THING_ID}/features/{feature}/properties",
            auth=AUTH, headers=HDR, json=props, timeout=10,
        )
        return r.status_code
    except Exception:
        return 0


def get_thing() -> dict:
    try:
        r = httpx.get(f"{DITTO_URL}/api/2/things/{THING_ID}", auth=AUTH, timeout=10)
        return r.json()
    except Exception:
        return {}


if __name__ == "__main__":
    create_policy()
    create_thing()
    print(json.dumps(get_thing(), indent=2))