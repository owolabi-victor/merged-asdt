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
        "soil_ph":                         6.2,
        "nitrogen_ppm":                   20.0,
        "phosphorus_ppm":                 18.0,
        "potassium_ppm":                 180.0,
        "ec_ds_m":                         0.8,
        "bulk_density_g_cm3":              1.30,
        "organic_matter_pct":              3.0,
        "microbial_biomass_mg_c_kg":     420.0,
        "soil_respiration_mg_co2_kg_day": 85.0,
    },
    "sandy": {
        "soil_moisture_pct":              18.0,
        "soil_temp_c":                    26.0,
        "soil_ph":                         5.8,
        "nitrogen_ppm":                   12.0,
        "phosphorus_ppm":                 10.0,
        "potassium_ppm":                 110.0,
        "ec_ds_m":                         0.5,
        "bulk_density_g_cm3":              1.50,
        "organic_matter_pct":              1.5,
        "microbial_biomass_mg_c_kg":     250.0,
        "soil_respiration_mg_co2_kg_day": 45.0,
    },
    "clay": {
        "soil_moisture_pct":              38.0,
        "soil_temp_c":                    23.0,
        "soil_ph":                         6.5,
        "nitrogen_ppm":                   22.0,
        "phosphorus_ppm":                 20.0,
        "potassium_ppm":                 200.0,
        "ec_ds_m":                         1.0,
        "bulk_density_g_cm3":              1.25,
        "organic_matter_pct":              3.5,
        "microbial_biomass_mg_c_kg":     480.0,
        "soil_respiration_mg_co2_kg_day":100.0,
    },
    "silty": {
        "soil_moisture_pct":              28.0,
        "soil_temp_c":                    24.0,
        "soil_ph":                         6.0,
        "nitrogen_ppm":                   16.0,
        "phosphorus_ppm":                 14.0,
        "potassium_ppm":                 140.0,
        "ec_ds_m":                         0.7,
        "bulk_density_g_cm3":              1.35,
        "organic_matter_pct":              2.5,
        "microbial_biomass_mg_c_kg":     350.0,
        "soil_respiration_mg_co2_kg_day": 70.0,
    },
}

# ── Gaussian noise standard deviations ───────────────────────────────────────
NOISE = {
    "soil_moisture_pct":              0.5,
    "soil_temp_c":                    0.2,
    "soil_ph":                        0.03,
    "nitrogen_ppm":                   0.5,
    "phosphorus_ppm":                 0.4,
    "potassium_ppm":                  3.0,
    "ec_ds_m":                        0.02,
    "bulk_density_g_cm3":             0.008,
    "organic_matter_pct":             0.03,
    "microbial_biomass_mg_c_kg":      5.0,
    "soil_respiration_mg_co2_kg_day": 2.0,
}

# ── Fault profile: multi-factor soil depletion scenario ──────────────────────
# Simulates a parcel that has been continuously cropped without management:
#   → N depleted (S1), pH acidified (S2), compacted (S4),
#   → OM depleted (S6), biologically inactive (S7) → overall S8
FAULT_OVERRIDES = {
    "nitrogen_ppm":                    7.0,    # depleted — below 10 ppm warn
    "phosphorus_ppm":                  6.5,    # borderline depleted
    "potassium_ppm":                  85.0,    # borderline depleted
    "soil_ph":                         4.8,    # acidified — below 5.0 warn
    "bulk_density_g_cm3":              1.65,   # compacted — above 1.6 warn
    "organic_matter_pct":              1.2,    # depleted — below 1.5 warn
    "microbial_biomass_mg_c_kg":     120.0,    # inactive — below 150 warn
    "soil_respiration_mg_co2_kg_day": 18.0,    # inactive — below 25 warn
    "ec_ds_m":                         0.9,    # normal (not salinized)
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
        print(f"[SIMULATOR]   N depleted, pH acidified, compacted, OM depleted, biologically inactive")

    while True:
        set_latest("layer_heartbeat_physical.simulator",
                   __import__("datetime").datetime.utcnow().isoformat())
        payload = sim.next_reading()
        mqttc.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)

        bd  = payload.get("bulk_density_g_cm3", "?")
        mst = payload.get("soil_moisture_pct", "?")
        tag = " [SPIKE]" if payload.get("spike_injected") else ""
        if fault_mode_physical:
            print(f"  BD={bd} g/cm³ | moisture={mst}%{tag}")
        else:
            n   = payload.get("nitrogen_ppm", "?")
            ph  = payload.get("soil_ph", "?")
            om  = payload.get("organic_matter_pct", "?")
            mb  = payload.get("microbial_biomass_mg_c_kg", "?")
            print(f"  N={n} ppm | pH={ph} | OM={om}% | MB={mb} mgC/kg | BD={bd} g/cm³ | mst={mst}%{tag}")

        time.sleep(30)


if __name__ == "__main__":
    main()