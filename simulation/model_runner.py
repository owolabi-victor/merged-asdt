# simulation/model_runner.py
"""
Simulation & Model Layer — live soil process model.

Runs a simplified soil water balance + nitrogen cycling + organic matter
decomposition + biological activity model in parallel with the real
sensor stream. Every 60 seconds it:
  1. Reads current soil parameters from InfluxDB
  2. Steps the model forward one time step
  3. Writes predicted values to InfluxDB (measurement: soil_simulation)
  4. Writes residuals (real - predicted) to InfluxDB (measurement: soil_residuals)

Covers physical, chemical, and biological soil processes.
"""
import time
import numpy as np

from shared.influx_io import write_point, get_latest
from shared.config    import ASSET_ID, ACTIVE_SOIL_TYPE


class SoilProcessModel:
    """
    Simplified soil process model covering:
    - Water balance (moisture)
    - Nitrogen cycling (mineralisation, leaching, uptake)
    - pH drift (acidification from continuous cropping)
    - Organic matter decomposition
    - Microbial biomass dynamics
    - Soil respiration
    """

    # ── Model parameters (loamy soil defaults) ────────────────────────────────
    FIELD_CAPACITY_PCT     = 32.0
    WILTING_POINT_PCT      = 12.0
    ORGANIC_MATTER_PCT     = 3.0      # baseline OM %
    CLAY_PCT               = 20.0
    ROOTING_DEPTH_CM       = 30.0
    DAILY_ET_MM            = 4.5
    N_MINERALISATION_RATE  = 0.25     # ppm N released per day from SOM
    N_LEACHING_RATE        = 0.04
    OM_DECOMPOSITION_RATE  = 0.0005   # fraction of OM lost per day
    MB_GROWTH_RATE         = 0.01     # fraction per day from OM
    MB_DEATH_RATE          = 0.005    # fraction per day
    RESP_PER_MB            = 0.15     # mg CO₂ per mg C microbial biomass

    def __init__(self):
        self.moisture_pct  = self.FIELD_CAPACITY_PCT
        self.nitrogen_ppm  = 20.0
        self.ph            = 6.2
        self.om_pct        = self.ORGANIC_MATTER_PCT
        self.mb_mg_c_kg    = 420.0
        self.respiration    = 85.0

    def step(self, dt_hours: float = 1.0,
             temp_c: float = 24.5,
             live_moisture: float = None) -> dict:
        """Advance the model by dt_hours."""
        dt_days = dt_hours / 24.0

        # ── Water balance ──────────────────────────────────────────────────
        et_factor = 1.0 + max(0.0, (temp_c - 20.0) * 0.05)
        et_loss   = self.DAILY_ET_MM * et_factor * dt_days
        moisture_change = -(et_loss / (self.ROOTING_DEPTH_CM / 10.0)) * 0.1
        self.moisture_pct = max(self.WILTING_POINT_PCT,
                                 self.moisture_pct + moisture_change)
        if live_moisture is not None:
            self.moisture_pct = 0.7 * self.moisture_pct + 0.3 * live_moisture

        # ── Nitrogen cycling ───────────────────────────────────────────────
        mineralisation = (self.N_MINERALISATION_RATE *
                           (temp_c / 25.0) *
                           (self.moisture_pct / self.FIELD_CAPACITY_PCT) *
                           (self.om_pct / self.ORGANIC_MATTER_PCT) *
                           dt_days)
        if self.moisture_pct > 0.8 * self.FIELD_CAPACITY_PCT:
            leaching = self.nitrogen_ppm * self.N_LEACHING_RATE * dt_days
        else:
            leaching = 0.0
        uptake = 2.0 * dt_days
        self.nitrogen_ppm = max(0.0,
                                 self.nitrogen_ppm + mineralisation - leaching - uptake)

        # ── pH drift ──────────────────────────────────────────────────────
        self.ph = max(4.0, self.ph - 0.001 * dt_days)

        # ── Organic matter decomposition ──────────────────────────────────
        om_loss = self.om_pct * self.OM_DECOMPOSITION_RATE * (temp_c / 25.0) * dt_days
        self.om_pct = max(0.1, self.om_pct - om_loss)

        # ── Microbial biomass dynamics ────────────────────────────────────
        mb_growth = self.mb_mg_c_kg * self.MB_GROWTH_RATE * (self.om_pct / 3.0) * dt_days
        mb_death  = self.mb_mg_c_kg * self.MB_DEATH_RATE * dt_days
        self.mb_mg_c_kg = max(10.0, self.mb_mg_c_kg + mb_growth - mb_death)

        # ── Soil respiration ──────────────────────────────────────────────
        self.respiration = self.mb_mg_c_kg * self.RESP_PER_MB * (temp_c / 25.0)

        return {
            "sim_moisture_pct":      round(self.moisture_pct, 2),
            "sim_nitrogen_ppm":      round(self.nitrogen_ppm, 2),
            "sim_ph":                round(self.ph, 3),
            "sim_om_pct":            round(self.om_pct, 3),
            "sim_mb_mg_c_kg":        round(self.mb_mg_c_kg, 1),
            "sim_respiration":       round(self.respiration, 1),
        }


def run():
    model = SoilProcessModel()
    print(f"[SIMULATION] Soil process model running (type: {ACTIVE_SOIL_TYPE})...")

    while True:
        from shared.redis_io import set_latest as _set_hb
        import datetime as _dt
        _set_hb("layer_heartbeat_simulation.model_runner",
                _dt.datetime.utcnow().isoformat())
        live_temp     = get_latest("soil_temp_c")
        live_moisture = get_latest("soil_moisture_pct")
        temp_c = live_temp if live_temp is not None else 24.5

        predicted = model.step(
            dt_hours=1.0,
            temp_c=temp_c,
            live_moisture=live_moisture,
        )

        write_point("soil_simulation", predicted)

        # Compute residuals
        field_map = {
            "sim_moisture_pct":  "soil_moisture_pct",
            "sim_nitrogen_ppm":  "nitrogen_ppm",
            "sim_ph":            "soil_ph",
            "sim_om_pct":        "organic_matter_pct",
            "sim_mb_mg_c_kg":    "microbial_biomass_mg_c_kg",
            "sim_respiration":   "soil_respiration_mg_co2_kg_day",
        }
        residuals = {}
        for sim_key, real_key in field_map.items():
            real_val = get_latest(real_key)
            if real_val is not None:
                residuals[f"res_{real_key}"] = round(real_val - predicted[sim_key], 4)

        if residuals:
            write_point("soil_residuals", residuals)

        time.sleep(3600)


if __name__ == "__main__":
    run()