"""
Measured soil evaporation, and the drying response that drives it.

Two sensors, two things they can say together that neither says alone:

  1. The soil moisture probe MEASURES evaporation. Between wetting events the
     only way water leaves the top layer is upward, so the drawdown is the
     evaporation. No reference ET, no radiation sensor, no crop coefficient —
     this is an observation, not a model output.

  2. The DHT11 explains it. Air temperature and humidity give vapour pressure
     deficit, the atmospheric demand pulling that water out.

Relating the two separates the two stages of soil drying, which is the part a
single sensor cannot see:

    Stage 1  energy-limited   the surface is wet, evaporation keeps up with
                              demand, and drawdown tracks VPD
    Stage 2  supply-limited   the surface has dried, water must travel up
                              through the profile, and drawdown decouples from
                              VPD entirely

FAO-56 puts the transition at REW (readily evaporable water). Regressing
drawdown against VPD locates it for the soil actually in the pot, rather than
trusting the table.

A caveat that governs how the output should be read: the node carries a
resistive probe calibrated between air and tap water, which is a relative index
and not volumetric water content. The millimetre figures below are therefore
only as real as that calibration. The SHAPE of the curve — the stages, the
breakpoint, the correlation with VPD — holds either way, because it depends on
relative change rather than absolute water content.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from shared.influx_io import query_recent

# Depth of the layer that actually evaporates. FAO-56 uses 0.10–0.15 m; the
# shallower figure suits a probe sitting in the top few centimetres.
Ze_MM = 100.0

# Readily evaporable water, mm — FAO-56 Table 19. How much can leave before the
# surface dries and stage 2 begins.
REW_MM = {"loamy": 9.0, "sandy": 4.0, "clay": 11.0, "silty": 10.0}

# Field capacity / wilting point, volumetric %. Mirrors model_runner so the two
# models cannot disagree about the same soil.
_FC = {"loamy": 32.0, "sandy": 20.0, "clay": 42.0, "silty": 30.0}
_WP = {"loamy": 12.0, "sandy": 7.0, "clay": 18.0, "silty": 11.0}

# A bucket must lose more than this (in VWC %) to count as drying rather than
# sensor noise, and gain more than the wetting figure to count as rain or
# irrigation rather than drift.
_NOISE_PCT = 0.05
_WETTING_PCT = 0.50


def saturation_vapour_pressure_kpa(air_temp_c: float) -> float:
    """Tetens. The vapour pressure of air fully saturated at this temperature."""
    return 0.6108 * math.exp(17.27 * air_temp_c / (air_temp_c + 237.3))


def vapour_pressure_deficit_kpa(air_temp_c: float, relative_humidity_pct: float) -> float:
    """
    How much more water the air could hold, in kPa. The demand side of drying.

    Humidity alone cannot say this: 80 % at 24 °C and 80 % at 32 °C are very
    different demands, because saturation pressure more than doubles across
    that range.
    """
    rh = max(0.0, min(100.0, relative_humidity_pct))
    es = saturation_vapour_pressure_kpa(air_temp_c)
    return max(0.0, es - es * rh / 100.0)


def total_evaporable_water_mm(soil_type: str, ze_mm: float = Ze_MM) -> float:
    """
    TEW — everything the evaporation layer can lose, FAO-56 Eq. 73.

    Half the wilting point, not the whole of it: evaporation can dry soil below
    the point at which roots can still extract, because it is a physical
    process rather than a biological one.
    """
    fc = _FC.get(soil_type, _FC["loamy"]) / 100.0
    wp = _WP.get(soil_type, _WP["loamy"]) / 100.0
    return (fc - 0.5 * wp) * ze_mm


def _bucketed(field: str, minutes: int, bucket_min: int) -> pd.DataFrame:
    """Mean of `field` per bucket, indexed by bucket start. Empty if no data."""
    df = query_recent(field, minutes=minutes)
    if df.empty:
        return pd.DataFrame(columns=["_value"])
    df = df.copy()
    df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return (df.set_index("_time")
              .resample(f"{bucket_min}min")["_value"]
              .agg(["first", "last", "mean"])
              .dropna(how="all"))


def evaporation_series(hours: int = 48,
                       bucket_min: int = 60,
                       soil_type: str = "loamy",
                       ze_mm: float = Ze_MM) -> dict:
    """
    Per-bucket evaporation measured from the probe, with the VPD that drove it.

    Depletion accumulates from the last wetting event, because that is what
    determines which stage the soil is in — not the absolute moisture level.

    Returns {"status", "buckets": [...], "summary": {...}}. `status` is
    "ok", or "no_data"/"insufficient" when the history cannot support a figure,
    so callers never have to guess whether a zero means dry or means missing.
    """
    minutes = hours * 60
    vwc = _bucketed("soil_moisture_pct", minutes, bucket_min)
    if vwc.empty:
        return {"status": "no_data", "reason": "no soil_moisture_pct history",
                "buckets": [], "summary": {}}

    air = _bucketed("air_temperature_c", minutes, bucket_min)
    rh = _bucketed("relative_humidity_pct", minutes, bucket_min)
    have_forcing = not air.empty and not rh.empty

    rew = REW_MM.get(soil_type, REW_MM["loamy"])
    tew = total_evaporable_water_mm(soil_type, ze_mm)

    # Change is measured BETWEEN buckets, not within one. A bucket holding a
    # single sample has first == last and would otherwise always look static —
    # which is exactly what happens whenever the upload interval is close to the
    # bucket width.
    level = vwc["last"].astype(float)
    deltas = level.diff()

    buckets, depletion_mm = [], 0.0
    for ts, row in vwc.iterrows():
        delta = deltas.loc[ts]
        if pd.isna(delta):
            continue                  # first bucket has nothing to compare against
        delta_pct = float(delta)

        if delta_pct > _WETTING_PCT:
            event, evap_mm = "wetting", 0.0
            depletion_mm = 0.0            # surface re-wet: stage 1 restarts
        elif delta_pct < -_NOISE_PCT:
            event = "drying"
            evap_mm = -delta_pct / 100.0 * ze_mm
            depletion_mm = min(tew, depletion_mm + evap_mm)
        else:
            event, evap_mm = "static", 0.0

        vpd = None
        if have_forcing and ts in air.index and ts in rh.index:
            t_mean, rh_mean = air.loc[ts, "mean"], rh.loc[ts, "mean"]
            if pd.notna(t_mean) and pd.notna(rh_mean):
                vpd = round(vapour_pressure_deficit_kpa(float(t_mean), float(rh_mean)), 3)

        buckets.append({
            "time": ts.isoformat(),
            "vwc_pct": round(float(level.loc[ts]), 2),
            "delta_pct": round(delta_pct, 3),
            "evaporation_mm": round(evap_mm, 3),
            "depletion_mm": round(depletion_mm, 2),
            "vpd_kpa": vpd,
            "event": event,
            "stage": 1 if depletion_mm <= rew else 2,
        })

    drying = [b for b in buckets if b["event"] == "drying"]
    span_h = len(buckets) * bucket_min / 60.0
    total_mm = sum(b["evaporation_mm"] for b in drying)

    return {
        "status": "ok" if drying else "insufficient",
        "soil_type": soil_type,
        "evaporation_layer_mm": ze_mm,
        "rew_mm": rew,
        "tew_mm": round(tew, 1),
        "forcing_available": have_forcing,
        "buckets": buckets,
        "summary": {
            "window_hours": round(span_h, 1),
            "drying_buckets": len(drying),
            "wetting_events": sum(1 for b in buckets if b["event"] == "wetting"),
            "total_evaporation_mm": round(total_mm, 2),
            "mean_rate_mm_per_day": round(total_mm / span_h * 24, 2) if span_h else None,
            "current_depletion_mm": round(depletion_mm, 2),
            "current_stage": buckets[-1]["stage"] if buckets else None,
        },
    }


def drying_response(hours: int = 168,
                    bucket_min: int = 60,
                    soil_type: str = "loamy",
                    ze_mm: float = Ze_MM) -> dict:
    """
    Fit evaporation against VPD, separately per stage.

    The expected result, and the thing worth reporting either way:
      stage 1  a real positive slope with meaningful r² — drying is keeping up
               with demand
      stage 2  a slope near zero — the air is still pulling, the soil can no
               longer answer

    If stage 2 correlates with VPD as strongly as stage 1, the transition has
    not been reached in this window, or the probe is reading below the
    evaporation layer and tracking drainage instead.
    """
    series = evaporation_series(hours, bucket_min, soil_type, ze_mm)
    if series["status"] != "ok":
        return {"status": series["status"], "reason": series.get("reason"), "stages": {}}
    if not series["forcing_available"]:
        return {"status": "no_forcing",
                "reason": "no air_temperature_c / relative_humidity_pct history",
                "stages": {}}

    usable = [b for b in series["buckets"]
              if b["event"] == "drying" and b["vpd_kpa"] is not None]

    stages = {}
    for stage in (1, 2):
        pts = [(b["vpd_kpa"], b["evaporation_mm"]) for b in usable if b["stage"] == stage]
        if len(pts) < 3:
            stages[f"stage_{stage}"] = {"n": len(pts), "status": "insufficient"}
            continue
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        if x.std() < 1e-6:
            stages[f"stage_{stage}"] = {"n": len(pts), "status": "no_vpd_variation"}
            continue
        slope, intercept = np.polyfit(x, y, 1)
        pred = slope * x + intercept
        ss_res = float(((y - pred) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        stages[f"stage_{stage}"] = {
            "n": len(pts),
            "status": "ok",
            "slope_mm_per_kpa": round(float(slope), 4),
            "intercept_mm": round(float(intercept), 4),
            "r_squared": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None,
            "mean_vpd_kpa": round(float(x.mean()), 3),
            "mean_evaporation_mm": round(float(y.mean()), 3),
        }

    s1, s2 = stages.get("stage_1", {}), stages.get("stage_2", {})
    decoupled = (s1.get("status") == "ok" and s2.get("status") == "ok"
                 and s1.get("slope_mm_per_kpa", 0) > 0
                 and s2.get("slope_mm_per_kpa", 1) < s1.get("slope_mm_per_kpa", 0) * 0.5)

    return {
        "status": "ok",
        "soil_type": soil_type,
        "rew_mm_assumed": series["rew_mm"],
        "window_hours": series["summary"]["window_hours"],
        "stages": stages,
        "transition_observed": decoupled,
        "interpretation": (
            "Stage 2 drying has decoupled from atmospheric demand, which is the "
            "signature of a supply-limited surface."
            if decoupled else
            "No clear decoupling yet — either the soil has not dried past REW in "
            "this window, or the probe is below the evaporation layer."
        ),
    }
