# dt_network/ingestor.py
"""
DT Network Layer — the entry point of the digital twin platform.

Subscribes to the MQTT telemetry topic, validates every message using
Pydantic, and forwards clean data to the Data Layer.
Invalid messages are re-published to a dead-letter topic for debugging.
"""
import json
import paho.mqtt.client as mqtt
from pydantic import BaseModel, ValidationError, field_validator
from typing import Optional

from shared.config import (
    MQTT_BROKER, MQTT_PORT, TOPIC_TELEMETRY, ASSET_ID,
)
from data_layer.writer import route_telemetry
from shared.redis_io import set_latest


# ── Validation schema ─────────────────────────────────────────────────────────
class SoilTelemetryMessage(BaseModel):
    asset_id:               str
    timestamp:              float

    # Physical parameters
    soil_moisture_pct:              Optional[float] = None   # % VWC
    bulk_density_g_cm3:             Optional[float] = None   # g/cm³
    soil_temp_c:                    Optional[float] = None   # °C

    # Chemical parameters
    soil_ph:                        Optional[float] = None   # 0–14
    nitrogen_ppm:                   Optional[float] = None   # ppm available N
    phosphorus_ppm:                 Optional[float] = None   # mg/kg available P
    potassium_ppm:                  Optional[float] = None   # mg/kg available K
    ec_ds_m:                        Optional[float] = None   # dS/m
    organic_matter_pct:             Optional[float] = None   # %

    # Biological parameters
    microbial_biomass_mg_c_kg:      Optional[float] = None   # mg C/kg
    soil_respiration_mg_co2_kg_day: Optional[float] = None   # mg CO₂/kg/day

    # Texture (static per soil type, included for completeness)
    sand_pct:                       Optional[float] = None
    silt_pct:                       Optional[float] = None
    clay_pct:                       Optional[float] = None

    # Metadata
    soil_type:              Optional[str]   = None
    fault_mode:             Optional[bool]  = False
    spike_injected:         Optional[bool]  = False

    # Provenance. Pydantic drops undeclared fields, so without these the
    # distinction between a measured reading and a simulated one is lost the
    # moment it reaches the data layer.
    source:                 Optional[str]        = None   # "esp32_node" | None
    measured_fields:        Optional[list[str]]  = None

    @field_validator("*", mode="before")
    @classmethod
    def reject_nan(cls, v):
        """Reject NaN values — these come from faulty sensor firmware."""
        if isinstance(v, float) and v != v:
            raise ValueError("NaN sensor value not allowed")
        return v

    @field_validator("soil_ph", mode="before")
    @classmethod
    def ph_range(cls, v):
        if v is not None and not (0.0 <= float(v) <= 14.0):
            raise ValueError(f"pH value {v} is outside 0–14 range")
        return v

    @field_validator("soil_moisture_pct", mode="before")
    @classmethod
    def moisture_range(cls, v):
        if v is not None and not (0.0 <= float(v) <= 100.0):
            raise ValueError(f"Moisture {v}% is outside 0–100 range")
        return v

    @field_validator("organic_matter_pct", mode="before")
    @classmethod
    def om_range(cls, v):
        if v is not None and not (0.0 <= float(v) <= 100.0):
            raise ValueError(f"Organic matter {v}% is outside 0–100 range")
        return v


# ── MQTT callbacks ────────────────────────────────────────────────────────────

_mqttc = None

def _on_message(client, userdata, msg):
    import datetime
    set_latest("layer_heartbeat_dt_network.ingestor",
               datetime.datetime.utcnow().isoformat())
    raw = msg.payload.decode("utf-8", errors="replace")
    try:
        data      = json.loads(raw)
        validated = SoilTelemetryMessage(**data)
        route_telemetry(validated.model_dump())
    except (json.JSONDecodeError, ValidationError) as e:
        dead_topic = f"dt/{ASSET_ID}/dead_letter"
        client.publish(
            dead_topic,
            json.dumps({"error": str(e), "raw": raw[:300]}),
            qos=0,
        )
        print(f"[INGESTOR] ❌ Rejected message: {e}")

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(TOPIC_TELEMETRY, qos=1)
        import datetime
        set_latest("layer_heartbeat_dt_network.ingestor",
                   datetime.datetime.utcnow().isoformat())
        print(f"[INGESTOR] ✅ Connected. Listening on {TOPIC_TELEMETRY}")
    else:
        print(f"[INGESTOR] ❌ MQTT connection failed with code {rc}")


def start():
    global _mqttc
    _mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, f"ingestor_{ASSET_ID}")
    _mqttc.on_connect = _on_connect
    _mqttc.on_message = _on_message
    _mqttc.connect(MQTT_BROKER, MQTT_PORT)
    _mqttc.loop_forever()


if __name__ == "__main__":
    start()