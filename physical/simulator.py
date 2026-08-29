# physical/simulator.py
"""
Soil Parcel Sensor Simulator.

Generates physically plausible soil readings for soil_parcel_001 every 30 seconds.
Supports multiple soil types (sandy, loamy, clay, silty) and simulates a
multi-factor depletion scenario: continuous cropping without adequate management.

Fault injection: simulates a depleted soil with degraded physical, chemical,
and biological properties — exactly the situation described in the project:
  → Nitrogen depleted, pH acidified, organic matter low
  → Microbial activity suppressed, bulk density elevated
  → Multiple depletion states (S1 + S2 + S4 + S6 + S7 → S8)
"""
import time
import json
import random
import paho.mqtt.client as mqtt
from shared.mqtt_io import make_client

from shared.config import (
    MQTT_BROKER, MQTT_PORT, TOPIC_TELEMETRY,
    ASSET_ID, SENSOR_FIELDS, ACTIVE_SOIL_TYPE, SOIL_TYPES,
)
from shared.redis_io import set_latest

# ── Healthy baseline values per soil type ────────────────────────────────────
NOMINAL_BY_TYPE = {
    "loamy": {
        "soil_moisture_pct":              30.0,
        "soil_temp_c":                    24.5,
        "bulk_density_g_cm3":              1.30,
    },
    "sandy": {
        "soil_moisture_pct":              18.0,
        "soil_temp_c":                    26.0,
        "bulk_density_g_cm3":              1.50,
    },
    "clay": {
        "soil_moisture_pct":              38.0,
        "soil_temp_c":                    23.0,
        "bulk_density_g_cm3":              1.25,
    },
    "silty": {
        "soil_moisture_pct":              28.0,
        "soil_temp_c":                    24.0,
        "bulk_density_g_cm3":              1.35,
    },
}

# ── Gaussian noise standard deviations ───────────────────────────────────────
NOISE = {
    "soil_moisture_pct":              0.5,
    "soil_temp_c":                    0.2,
    "bulk_density_g_cm3":             0.008,
}

# ── Fault profile: multi-factor soil depletion scenario ──────────────────────
# Simulates a parcel that has been continuously cropped without management:
#   → N depleted (S1), pH acidified (S2), compacted (S4),
#   → OM depleted (S6), biologically inactive (S7) → overall S8
FAULT_OVERRIDES = {
    "bulk_density_g_cm3":              1.65,   # compacted — above 1.6 warn
    "soil_moisture_pct":              28.0,    # adequate (not water stressed)
}

# ── Physical-only fault profile (S4 + S5) ────────────────────────────────────
# Chemical and biological parameters stay healthy.
# Use fault_mode_physical=True to activate.
PHYSICAL_FAULT_OVERRIDES = {
    "bulk_density_g_cm3": 1.72,   # severely compacted → S4 critical
    "soil_moisture_pct":  11.5,   # dry water stress → S5 dry
}


class SoilParcelSimulator:
    """
    Simulates a soil parcel sensor suite.

    soil_type: one of 'sandy', 'loamy', 'clay', 'silty'
    fault_mode=True  → publishes multi-factor depletion readings
    fault_mode=False → publishes healthy baseline readings
    fault_probability  → chance per reading of injecting a spike anomaly
    """

    def __init__(self, soil_type: str = "loamy",
                 fault_mode: bool = True,
                 fault_mode_physical: bool = False,
                 fault_probability: float = 0.0):
        self.soil_type           = soil_type
        self.fault_mode          = fault_mode
        self.fault_mode_physical = fault_mode_physical
        self.fault_prob          = fault_probability
        self._drift              = {f: 0.0 for f in SENSOR_FIELDS}
        self._nominal            = NOMINAL_BY_TYPE.get(soil_type, NOMINAL_BY_TYPE["loamy"])
        self._texture            = SOIL_TYPES.get(soil_type, SOIL_TYPES["loamy"])

    def _apply_drift(self):
        """Slow random walk on each field — simulates gradual soil change."""
        for f in self._drift:
            self._drift[f] += random.gauss(0, 0.003)
            self._drift[f] *= 0.98

    def next_reading(self) -> dict:
        self._apply_drift()
        spike = random.random() < self.fault_prob

        reading = {
            "asset_id":       ASSET_ID,
            "timestamp":      time.time(),
            "soil_type":      self.soil_type,
            "fault_mode":     self.fault_mode,
            "spike_injected": spike,
            # Include texture (static per soil type)
            "sand_pct":       self._texture["sand_pct"],
            "silt_pct":       self._texture["silt_pct"],
            "clay_pct":       self._texture["clay_pct"],
        }

        for f in SENSOR_FIELDS:
            if self.fault_mode and f in FAULT_OVERRIDES:
                base = FAULT_OVERRIDES[f]
            elif self.fault_mode_physical and f in PHYSICAL_FAULT_OVERRIDES:
                base = PHYSICAL_FAULT_OVERRIDES[f]
            else:
                base = self._nominal.get(f, 0.0)

            noise = NOISE.get(f, 0.01)
            val   = base + self._drift[f] + random.gauss(0, noise)

            # Sensor spike injection (simulates a bad reading for QA/QC testing)
            if spike:
                if "ph" in f:
                    val += random.uniform(1.5, 2.5)
                if "ec" in f:
                    val += random.uniform(1.0, 3.0)

            # Clamp to physically plausible ranges
            val = max(0.0, val)
            if "ph" in f:
                val = min(val, 14.0)
            if "moisture" in f:
                val = min(val, 100.0)
            if "organic_matter" in f:
                val = min(val, 15.0)
            if "microbial_biomass" in f:
                val = min(val, 1500.0)
            if "respiration" in f:
                val = min(val, 500.0)

            reading[f] = round(val, 3 if val < 10 else 2)

        return reading


def main():
    import os
    soil_type            = ACTIVE_SOIL_TYPE
    fault_mode_physical  = os.getenv("FAULT_MODE_PHYSICAL", "").lower() in ("1", "true", "yes")
    # physical-only mode takes precedence over full multi-factor mode
    fault_mode_full      = not fault_mode_physical

    sim = SoilParcelSimulator(
        soil_type=soil_type,
        fault_mode=fault_mode_full,
        fault_mode_physical=fault_mode_physical,
        fault_probability=0.05,
    )
    mqttc = make_client(f"simulator_{ASSET_ID}")
    mqttc.connect(MQTT_BROKER, MQTT_PORT)
    mqttc.loop_start()

    print(f"[SIMULATOR] Publishing soil readings to {TOPIC_TELEMETRY}")
    print(f"[SIMULATOR] Soil type: {soil_type}")
    if fault_mode_physical:
        print(f"[SIMULATOR] fault_mode_physical=True → S4 (compacted) + S5 (dry) only")
        print(f"[SIMULATOR]   BD={PHYSICAL_FAULT_OVERRIDES['bulk_density_g_cm3']} g/cm³, "
              f"moisture={PHYSICAL_FAULT_OVERRIDES['soil_moisture_pct']}%  (chemical/bio healthy)")
    else:
        print(f"[SIMULATOR] fault_mode=True → multi-factor depletion scenario active")
        print(f"[SIMULATOR]   compacted, water-stressed")

    while True:
        set_latest("layer_heartbeat_physical.simulator",
                   __import__("datetime").datetime.utcnow().isoformat())
        payload = sim.next_reading()
        mqttc.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)

        bd  = payload.get("bulk_density_g_cm3", "?")
        mst = payload.get("soil_moisture_pct", "?")
        tag = " [SPIKE]" if payload.get("spike_injected") else ""
        tmp = payload.get("soil_temp_c", "?")
        print(f"  BD={bd} g/cm³ | moisture={mst}% | temp={tmp}°C{tag}")

        time.sleep(30)


if __name__ == "__main__":
    main()