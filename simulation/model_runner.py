# simulation/model_runner.py
"""
Simulation & Model Layer — live soil process model.

Runs a simplified soil water balance model in parallel with the real sensor
stream. Every 60 seconds it:
  1. Reads current soil parameters from InfluxDB
  2. Steps the model forward one time step
  3. Writes predicted values to InfluxDB (measurement: soil_simulation)
  4. Writes residuals (real - predicted) to InfluxDB (measurement: soil_residuals)

Covers the physical soil processes only; the chemical and biological ones were
removed with the parameters they modelled.
"""
import time
import numpy as np

from shared.influx_io import write_point, get_latest
from shared.config    import ASSET_ID, ACTIVE_SOIL_TYPE


class SoilProcessModel:
    """
    Simplified soil process model covering the water balance (moisture).
    """

    # ── Model parameters (loamy soil defaults) ────────────────────────────────
    FIELD_CAPACITY_PCT     = 32.0
    WILTING_POINT_PCT      = 12.0
    CLAY_PCT               = 20.0
    ROOTING_DEPTH_CM       = 30.0
    DAILY_ET_MM            = 4.5

    def __init__(self):
        self.moisture_pct  = self.FIELD_CAPACITY_PCT

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

        return {
            "sim_moisture_pct":      round(self.moisture_pct, 2),
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


# ═══════════════════════════════════════════════════════════════════════════════
# PHYSICAL MODELS — callable independently via API
# ═══════════════════════════════════════════════════════════════════════════════

# Field-capacity and wilting-point per soil type (volumetric %)
_FC  = {"loamy": 32.0, "sandy": 20.0, "clay": 42.0, "silty": 30.0}
_WP  = {"loamy": 12.0, "sandy":  7.0, "clay": 18.0, "silty": 11.0}
# Compaction thresholds (warn) per soil type
_BD_WARN = {"loamy": 1.6, "sandy": 1.7, "clay": 1.5, "silty": 1.55}
_BD_CRIT = {"loamy": 1.8, "sandy": 1.85, "clay": 1.7, "silty": 1.75}


class WaterBalanceModel:
    """
    Simplified FAO-56 daily water balance with 7-day forecast.

    VWC_new = VWC_old + (rain_mm + irrig_mm - ET_c_mm) / (rooting_depth_m × 1000) × 100

    ET_c = ET₀ × Kc
    ET₀  = 0.0135 × (T + 17.8) × sqrt(max(ΔT, 0)) × Ra_factor
    Ra_factor estimated from latitude (fixed 12 MJ/m²/day for West Africa ~7°N).
    """
    _RA = 12.0  # MJ/m²/day — solar radiation proxy for Ashanti Region, Ghana

    def forecast(
        self,
        soil_type: str,
        current_vwc: float,
        temp_c: float,
        precipitation_mm_day: float = 0.0,
        irrigation_mm: float = 0.0,
        rooting_depth_cm: float = 30.0,
        Kc: float = 1.0,
        days: int = 7,
    ) -> list[dict]:
        """
        Returns a list of `days` dicts:
          {day, vwc_pct, et_mm, surplus_deficit_mm, status}
        """
        fc = _FC.get(soil_type, _FC["loamy"])
        wp = _WP.get(soil_type, _WP["loamy"])
        rooting_depth_m = max(rooting_depth_cm / 100.0, 0.10)
        depth_factor = rooting_depth_m * 1000  # convert to mm depth of soil water

        # Estimate diurnal range ~8 °C for tropical humid
        delta_t = 8.0
        # Hargreaves ET₀ (mm/day)
        et0 = 0.0135 * (temp_c + 17.8) * (delta_t ** 0.5) * self._RA
        et_c = et0 * Kc

        vwc = current_vwc
        forecast = []
        for d in range(1, days + 1):
            # Effective rain + irrigation
            supply_mm = precipitation_mm_day + (irrigation_mm if d == 1 else 0.0)
            # Change in VWC (%)
            delta_vwc = (supply_mm - et_c) / depth_factor * 100.0
            vwc = max(wp, min(fc + 5.0, vwc + delta_vwc))

            surplus = supply_mm - et_c
            if vwc >= fc:
                status = "field_capacity"
            elif vwc <= wp + 2.0:
                status = "wilting_point"
            elif vwc < (wp + fc) / 2:
                status = "dry_stress"
            else:
                status = "adequate"

            forecast.append({
                "day": d,
                "vwc_pct": round(vwc, 2),
                "et_mm": round(et_c, 2),
                "surplus_deficit_mm": round(surplus, 2),
                "status": status,
            })

        return forecast


class CompactionModel:
    """
    Assess compaction risk from bulk density, moisture, and machinery passes.

    Risk score 0–100:
      - BD deviation above warning threshold → base risk
      - High moisture amplifies risk (wet soils compact more easily)
      - Additional machinery passes increase risk linearly
    """

    def assess(
        self,
        bulk_density: float,
        soil_moisture: float,
        soil_type: str,
        machinery_passes: int = 1,
    ) -> dict:
        bd_warn = _BD_WARN.get(soil_type, 1.6)
        bd_crit = _BD_CRIT.get(soil_type, 1.8)
        fc      = _FC.get(soil_type, 32.0)

        # Base score from BD deviation
        if bulk_density <= bd_warn:
            base = max(0.0, (bulk_density - (bd_warn - 0.2)) / 0.2) * 30
        elif bulk_density <= bd_crit:
            base = 30 + (bulk_density - bd_warn) / (bd_crit - bd_warn) * 40
        else:
            base = 70 + min(30.0, (bulk_density - bd_crit) / 0.1 * 10)

        # Moisture amplifier: wet soils (>80% FC) are more susceptible
        moisture_ratio = soil_moisture / max(fc, 1.0)
        moisture_factor = 1.0 + max(0.0, (moisture_ratio - 0.8)) * 1.5

        # Machinery passes factor
        pass_factor = 1.0 + min(1.0, (machinery_passes - 1) * 0.15)

        score = min(100.0, base * moisture_factor * pass_factor)

        if score < 30:
            risk_level = "low"
            action = "No immediate action required. Continue monitoring."
        elif score < 60:
            risk_level = "medium"
            action = "Reduce heavy machinery traffic. Consider subsoiling if score rises."
        else:
            risk_level = "high"
            action = f"Deep rip to 40 cm. Bulk density {bulk_density:.2f} g/cm³ restricts root growth."

        return {
            "score": round(score, 1),
            "risk_level": risk_level,
            "bulk_density": bulk_density,
            "bd_warn_threshold": bd_warn,
            "bd_crit_threshold": bd_crit,
            "moisture_amplifier": round(moisture_factor, 2),
            "machinery_passes": machinery_passes,
            "action": action,
        }


class ErosionModel:
    """
    Simplified RUSLE (Revised Universal Soil Loss Equation).

    A = R × K × LS × C × P

    R  — rainfall erosivity (MJ·mm/ha/h/yr)  estimated from annual rainfall
    K  — soil erodibility (t·h/MJ/mm)         from texture
    LS — slope length-steepness factor        from slope_pct
    C  — cover management factor              from land use input (0–1)
    P  — support practice factor              default 1.0 (no conservation practice)
    """

    # Soil erodibility K by soil type (approximate, for West African tropical soils)
    _K = {
        "sandy": 0.05,   # low erodibility (coarse, low runoff)
        "loamy": 0.28,   # moderate
        "clay":  0.12,   # low erodibility but high runoff
        "silty": 0.45,   # highest erodibility
    }

    def estimate(
        self,
        sand_pct: float,
        silt_pct: float,
        clay_pct: float,
        slope_pct: float = 5.0,
        rainfall_mm_yr: float = 1200.0,
        cover_factor: float = 0.3,
        soil_type: str = "loamy",
        practice_factor: float = 1.0,
    ) -> dict:
        # R — rainfall erosivity (Wischmeier & Smith approximation)
        R = 0.0483 * (rainfall_mm_yr ** 1.61)

        # K — erodibility from texture (override from lookup)
        K = self._K.get(soil_type, 0.28)

        # LS — slope factor (simplified McCool equation for field slopes)
        slope_rad = np.arctan(slope_pct / 100.0)
        sinθ = np.sin(slope_rad)
        LS = (slope_pct / 9.0) ** 0.6 * (sinθ / 0.09) ** 1.3 if slope_pct > 0 else 0.5

        # C — cover management (user-supplied, 0.01 well-covered to 1.0 bare)
        C = max(0.001, min(1.0, cover_factor))

        # P — support practice factor
        P = max(0.1, min(1.0, practice_factor))

        # A — annual soil loss (t/ha/yr)
        A = R * K * LS * C * P

        if A < 5:
            risk_class = "low"
        elif A < 15:
            risk_class = "moderate"
        elif A < 30:
            risk_class = "high"
        else:
            risk_class = "very_high"

        return {
            "soil_loss_t_ha_yr": round(A, 2),
            "risk_class": risk_class,
            "R_factor": round(R, 1),
            "K_factor": round(K, 3),
            "LS_factor": round(LS, 3),
            "C_factor": round(C, 3),
            "P_factor": round(P, 2),
            "dominant_factor": _dominant_rusle_factor(R, K, LS, C),
            "note": (
                "Reduce tillage and maintain crop residue cover."
                if A >= 15 else
                "Maintain current cover management practices."
            ),
        }


def _dominant_rusle_factor(R: float, K: float, LS: float, C: float) -> str:
    factors = {"rainfall (R)": R, "erodibility (K)": K * 100,
               "topography (LS)": LS * 10, "cover (C)": C * 100}
    return max(factors, key=factors.get)


# ── Module-level convenience functions ───────────────────────────────────────

_wb_model  = WaterBalanceModel()
_comp_model = CompactionModel()
_eros_model = ErosionModel()


def get_water_balance_forecast(
    soil_type: str = "loamy",
    rooting_depth_cm: float = 30.0,
    Kc: float = 1.0,
    precipitation_mm_day: float = 0.0,
    irrigation_mm: float = 0.0,
    days: int = 7,
) -> list[dict]:
    """Return 7-day VWC forecast using current sensor readings."""
    # `or` treats a genuine 0.0 % reading as missing and substitutes field
    # capacity, so bone-dry soil silently forecasts as well-watered. Only a
    # real absence (None) may fall back.
    _vwc = get_latest("soil_moisture_pct")
    current_vwc = _FC.get(soil_type, 32.0) if _vwc is None else float(_vwc)
    _temp = get_latest("soil_temp_c")
    current_temp = 24.5 if _temp is None else float(_temp)
    return _wb_model.forecast(
        soil_type=soil_type,
        current_vwc=current_vwc,
        temp_c=current_temp,
        precipitation_mm_day=precipitation_mm_day,
        irrigation_mm=irrigation_mm,
        rooting_depth_cm=rooting_depth_cm,
        Kc=Kc,
        days=days,
    )


def get_compaction_risk(
    soil_type: str = "loamy",
    machinery_passes: int = 1,
) -> dict:
    """Return compaction risk assessment using current sensor readings."""
    bd  = get_latest("bulk_density_g_cm3")
    mst = get_latest("soil_moisture_pct")
    if bd is None:
        return {"error": "No bulk density data available"}
    return _comp_model.assess(
        bulk_density=bd,
        soil_moisture=mst or 25.0,
        soil_type=soil_type,
        machinery_passes=machinery_passes,
    )


def get_erosion_risk(
    soil_type: str = "loamy",
    sand_pct: float = 40.0,
    silt_pct: float = 40.0,
    clay_pct: float = 20.0,
    slope_pct: float = 5.0,
    rainfall_mm_yr: float = 1200.0,
    cover_factor: float = 0.3,
) -> dict:
    """Return RUSLE erosion estimate."""
    return _eros_model.estimate(
        sand_pct=sand_pct, silt_pct=silt_pct, clay_pct=clay_pct,
        slope_pct=slope_pct, rainfall_mm_yr=rainfall_mm_yr,
        cover_factor=cover_factor, soil_type=soil_type,
    )