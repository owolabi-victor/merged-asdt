# data_layer/writer.py
"""
Data Layer — storage routing for all validated soil telemetry.

Every validated message from the ingestor passes through route_telemetry().
Each piece of data is written to exactly the right backend:
  • Sensor field values  → InfluxDB (time-series)
  • Latest values        → Redis (fast cache for agent lookups)
  • Spike/fault events   → MongoDB events collection (discrete log)
  • Arrival heartbeat    → Redis (connectivity monitoring)
"""
import time
from shared.influx_io import write_point
from shared.mongo_io  import log_event, upsert_asset_metadata
from shared.redis_io  import set_latest, set_state
from shared.config    import SENSOR_FIELDS, ASSET_ID


def route_telemetry(data: dict):
    """
    Called once per validated MQTT message.
    Routes each field to the appropriate storage backend.
    """
    # 1. Extract only the declared sensor fields (ignore simulator metadata)
    fields = {
        k: v for k, v in data.items()
        if k in SENSOR_FIELDS and v is not None
    }

    # 2. Write all numeric sensor values to InfluxDB time-series
    if fields:
        write_point("soil_telemetry", fields)

    # 3. Cache latest values in Redis for fast agent lookups
    for field, value in fields.items():
        set_latest(field, value)

    # 4. Log spike events (QA/QC: these are anomalous sensor readings)
    if data.get("spike_injected"):
        log_event(
            "sensor_spike_injected",
            {"readings": fields, "ts": data.get("timestamp")},
            severity="warning",
        )

    # 5. Update arrival heartbeat so other layers can detect silent failures
    set_latest("last_seen", time.time())


def seed_static_metadata(metadata: dict):
    """
    Called once during --setup to register the soil parcel in MongoDB.
    This data feeds the Eclipse Ditto Thing attributes and the knowledge graph.
    Edit the dict in main.py first_time_setup() for your real parcel.
    """
    upsert_asset_metadata(metadata)
    print(f"[DATA LAYER] ✅ Seeded static metadata for {ASSET_ID}")
