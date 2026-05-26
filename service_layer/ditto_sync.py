# service_layer/ditto_sync.py
"""
Keeps the Twin API Soil Parcel Thing updated every 30 seconds.
Reads live values from InfluxDB and Redis, PATCHes the Thing via REST.
Syncs telemetry, health, and depletion state information.
"""
import time
import json
from datetime import datetime, timezone

from shared.influx_io    import get_latest
from shared.redis_io     import get_state, get_latest_cached, set_latest
from shared.config       import SENSOR_FIELDS
from service_layer.ditto_client import update_feature


def sync_once():
    # Sync live soil telemetry (all 11 parameters)
    telemetry = {f: get_latest(f) for f in SENSOR_FIELDS}
    telemetry = {k: v for k, v in telemetry.items() if v is not None}
    if telemetry:
        update_feature("telemetry", telemetry)

    # Sync soil health
    health_score = get_latest_cached("soil_health_score") or 100.0
    is_anomaly   = bool(get_latest_cached("is_anomaly") or False)
    current_state = get_state()

    # Get active depletion states from Redis
    depletion_raw = get_latest_cached("active_depletion_states")
    active_depletion = []
    if depletion_raw:
        try:
            active_depletion = json.loads(depletion_raw) if isinstance(depletion_raw, str) else depletion_raw
        except (json.JSONDecodeError, TypeError):
            active_depletion = []

    update_feature("health", {
        "soil_health_score":       health_score,
        "is_anomaly":              is_anomaly,
        "operational_state":       current_state,
        "active_depletion_states": active_depletion,
    })

    # Sync active diagnosis / depletion info
    diag = get_latest_cached("active_diagnosis_summary")
    if diag:
        update_feature("depletion", {
            **diag,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    # Sync cross-domain data
    plant_raw = get_latest_cached("cross_domain_plant_demand")
    biotic_raw = get_latest_cached("cross_domain_biotic_data")
    cross_domain_props = {
        "publishes_to": ["plant_dt", "biotic_pod_dt"],
        "subscribes_from": ["plant_dt", "biotic_pod_dt"],
        "last_published": datetime.now(timezone.utc).isoformat(),
    }
    if plant_raw:
        try:
            plant_data = json.loads(plant_raw) if isinstance(plant_raw, str) else plant_raw
            cross_domain_props["plant_water_demand_mm_day"] = plant_data.get("plant_water_demand_mm_day")
            cross_domain_props["crop_phenology_stage"] = plant_data.get("crop_phenology_stage")
        except (json.JSONDecodeError, TypeError):
            pass
    if biotic_raw:
        try:
            biotic_data = json.loads(biotic_raw) if isinstance(biotic_raw, str) else biotic_raw
            cross_domain_props["microbial_diversity_index"] = biotic_data.get("microbial_diversity_index")
        except (json.JSONDecodeError, TypeError):
            pass
    update_feature("cross_domain", cross_domain_props)


def run():
    print("[DITTO SYNC] Syncing every 30s (all soil parameters + depletion states)...")
    while True:
        try:
            from datetime import datetime, timezone
            set_latest("layer_heartbeat_service_layer.ditto_sync",
                       datetime.now(timezone.utc).isoformat())
            sync_once()
        except Exception as e:
            print(f"[DITTO SYNC] Error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    run()