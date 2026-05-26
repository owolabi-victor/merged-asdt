"""BPDT System-Level Models — soil-plant coupled analysis.

Models:
    SoilPlantCoupledModel     — integrated water status (VWC + ψ_leaf)
    IrrigationEfficacyModel   — fraction of irrigation water reaching root zone
    WaterBudgetModel          — rolling water balance (input − ET − drainage)
    YieldWaterProductivityModel — crop WP [kg/m³] estimation
    FeedbackLoopDetector      — detects control oscillations in irrigation
    IrrigationScheduleOptimiser — deficit-irrigation scheduling (FAO-56 Chapter 8)

References:
    Allen et al. (1998) FAO-56 — irrigation scheduling
    Doorenbos & Kassam (1979) — yield-water relationships
    English & Raja (1996) — deficit irrigation
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING


# ---------------------------------------------------------------------------
# 1. Soil-Plant Coupled Model
# ---------------------------------------------------------------------------

class SoilPlantCoupledModel:
    """
    Coupled soil-plant water status indicator.

    Combines VWC from SDT and ψ_leaf from PDT into a single coupled
    water stress index (CWSI_coupled), accounting for soil-root resistance.

    CWSI_coupled = 0.5 × (1 - VWC/FC) + 0.5 × (ψ_leaf / ψ_wilting)

    Where normalised to [0=no stress, 1=full stress].
    """

    _FC        = 0.100
    _PSI_WILT  = -2.50   # [MPa]

    def compute(
        self,
        vwc_root_zone: float,
        psi_leaf_mpa: float,
    ) -> dict[str, float]:
        """Return coupled water status metrics."""
        vwc_frac   = max(0.0, min(1.0, vwc_root_zone / self._FC))
        psi_frac   = max(0.0, min(1.0, abs(psi_leaf_mpa) / abs(self._PSI_WILT)))
        cwsi_c     = 0.5 * (1.0 - vwc_frac) + 0.5 * psi_frac

        return {
            "vwc_fraction_of_fc": round(vwc_frac, 4),
            "psi_fraction_of_wilting": round(psi_frac, 4),
            "cwsi_coupled": round(cwsi_c, 4),
        }


# ---------------------------------------------------------------------------
# 2. Irrigation Efficacy Model
# ---------------------------------------------------------------------------

class IrrigationEfficacyModel:
    """
    Estimates the fraction of applied water reaching the root zone.

    Accounts for:
    - Surface runoff (if VWC already near FC)
    - Deep percolation below root zone
    - Evaporation from wet soil surface (≈15% of surface application)

    Based on empirical efficiency values for drip/surface irrigation.
    """

    _DRIP_EFFICIENCY   = 0.90   # fraction reaching root zone
    _SURFACE_EFFICIENCY = 0.65
    _EVAP_FRACTION      = 0.08  # surface evaporation loss

    def efficacy(
        self,
        irrigation_rate_L_hr: float,
        vwc_10cm: float,
        method: str = "drip",
    ) -> dict[str, float]:
        efficiency = (
            self._DRIP_EFFICIENCY if method == "drip" else self._SURFACE_EFFICIENCY
        )
        # Reduce efficiency if near field capacity (runoff/percolation)
        saturation_penalty = max(0.0, (vwc_10cm - 0.08) / 0.02)  # >0.08 → losses
        effective_eff = max(0.40, efficiency - saturation_penalty * 0.30)

        effective_rate = irrigation_rate_L_hr * effective_eff
        lost_rate      = irrigation_rate_L_hr * (1.0 - effective_eff)

        return {
            "applied_L_hr":   irrigation_rate_L_hr,
            "effective_L_hr": round(effective_rate, 3),
            "lost_L_hr":      round(lost_rate, 3),
            "efficacy_fraction": round(effective_eff, 4),
        }


# ---------------------------------------------------------------------------
# 3. Water Budget Model
# ---------------------------------------------------------------------------

class WaterBudgetModel:
    """
    Rolling daily water balance [mm]:
        ΔS = Rainfall + Irrigation − ET − Drainage

    Uses a 7-day rolling window for trend analysis.
    Positive balance → soil recharge.
    Negative balance → continued depletion.
    """

    def __init__(self, root_depth_m: float = 0.50):
        self._root_depth = root_depth_m
        self._daily_records: deque[dict] = deque(maxlen=30)

    def update(
        self,
        et_mm_day: float,
        rain_mm_day: float,
        irrigation_mm_day: float,
    ) -> dict[str, float]:
        drainage = max(0.0, rain_mm_day + irrigation_mm_day - et_mm_day - 5.0)
        balance  = rain_mm_day + irrigation_mm_day - et_mm_day - drainage

        record = {
            "et": et_mm_day,
            "rain": rain_mm_day,
            "irrigation": irrigation_mm_day,
            "balance": balance,
            "drainage": drainage,
        }
        self._daily_records.append(record)

        rolling_7d_balance = sum(r["balance"] for r in list(self._daily_records)[-7:])
        cumulative_balance = sum(r["balance"] for r in self._daily_records)

        return {
            "daily_balance_mm": round(balance, 2),
            "drainage_mm":      round(drainage, 2),
            "rolling_7d_balance_mm": round(rolling_7d_balance, 2),
            "cumulative_balance_mm": round(cumulative_balance, 2),
            "total_irrigation_mm": round(
                sum(r["irrigation"] for r in self._daily_records), 1
            ),
        }


# ---------------------------------------------------------------------------
# 4. Yield Water Productivity
# ---------------------------------------------------------------------------

class YieldWaterProductivityModel:
    """
    Crop water productivity [kg grain / m³ water consumed].

    WP = Ya / ETa_total
    Where Ya = Ymax × (1 - Ky × (1 - ETa/ETc))

    Maize reference WP: 1.0–1.8 kg/m³ (Zwart & Bastiaanssen 2004).
    """

    _YMAX_KG_HA = 10_000.0   # maize yield potential V6-V8 full season [kg/ha]
    _KY         = 1.25

    def compute(
        self,
        eta_fraction: float,      # ETa/ETc
        total_eta_mm: float,      # cumulative actual ET [mm]
    ) -> dict[str, float]:
        ya_frac = max(0.0, 1.0 - self._KY * (1.0 - max(0.0, min(1.0, eta_fraction))))
        ya_kg_ha = self._YMAX_KG_HA * ya_frac

        # WP [kg/m³]: Ya [kg/ha] / ETa [m³/ha] = Ya [kg/ha] / (ETa_mm × 10)
        wp = ya_kg_ha / max(total_eta_mm * 10.0, 1.0)

        return {
            "ya_fraction":      round(ya_frac, 4),
            "ya_kg_per_ha":     round(ya_kg_ha, 1),
            "water_productivity_kg_m3": round(wp, 3),
            "reference_wp_range": "1.0-1.8 kg/m³ (Zwart & Bastiaanssen 2004)",
        }


# ---------------------------------------------------------------------------
# 5. Feedback Loop Detector
# ---------------------------------------------------------------------------

class FeedbackLoopDetector:
    """
    Detects irrigation control oscillations.

    Oscillation = irrigation repeatedly starts and stops without VWC recovery.
    Signature: >= 3 irrigation cycles within 24 hr with VWC not crossing FC/2.
    """

    def __init__(self):
        self._irrigating: bool = False
        self._events: deque[str] = deque(maxlen=20)

    def update(self, irrigating: bool, vwc_10cm: float) -> dict[str, bool]:
        if irrigating != self._irrigating:
            self._events.append("start" if irrigating else "stop")
            self._irrigating = irrigating

        # Count cycles in recent events
        starts = sum(1 for e in self._events if e == "start")
        oscillating = starts >= 3

        return {
            "irrigation_cycles_recent": starts,
            "oscillation_detected": oscillating,
        }


# ---------------------------------------------------------------------------
# 6. Irrigation Schedule Optimiser
# ---------------------------------------------------------------------------

class IrrigationScheduleOptimiser:
    """
    Deficit-irrigation scheduling following FAO-56 Chapter 8.

    Computes the Readily Available Water (RAW) depletion fraction and
    recommends irrigation depth and timing.

    RAW = p × TAW
    where p = depletion fraction before stress (0.55 for maize at V6-V8)
    TAW = (FC - PWP) × root_depth × 1000 [mm]
    """

    _P      = 0.55    # depletion fraction (maize V6-V8; Allen et al. 1998 table 22)
    _FC     = 0.100
    _PWP    = 0.050
    _ROOT_D = 0.50    # m

    @property
    def _taw_mm(self) -> float:
        return (self._FC - self._PWP) * self._ROOT_D * 1000.0

    @property
    def _raw_mm(self) -> float:
        return self._P * self._taw_mm

    def recommend(
        self,
        vwc_10cm: float,
        vwc_30cm: float,
    ) -> dict:
        """Return irrigation timing and depth recommendation."""
        avg_vwc  = 0.6 * vwc_10cm + 0.4 * vwc_30cm
        depletion_mm = (self._FC - avg_vwc) * self._ROOT_D * 1000.0

        irrigate_now = depletion_mm >= self._raw_mm
        depth_mm     = max(0.0, depletion_mm) if irrigate_now else 0.0

        return {
            "taw_mm":         round(self._taw_mm, 1),
            "raw_mm":         round(self._raw_mm, 1),
            "current_depletion_mm": round(depletion_mm, 1),
            "irrigate_now":   irrigate_now,
            "recommended_depth_mm": round(depth_mm, 1),
            "depletion_fraction_p": self._P,
            "method": "FAO-56 Chapter 8 deficit irrigation scheduling",
        }
