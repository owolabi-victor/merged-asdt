"""SDT Reactive Layer — SoilRuleEngine.

5-state FSM: OPTIMAL → DEPLETING → CRITICAL → WILTING_RISK (and SENSOR_FAULT at any time).

Threshold rules R-SDT-01 through R-SDT-07 drive state selection.
Custom rules R-SDT-C1/C2/C3 inject synthetic signals that bias threshold evaluation.

State priority (highest wins when multiple thresholds breach):
    SENSOR_FAULT  > WILTING_RISK > CRITICAL > DEPLETING > OPTIMAL

References:
    Allen et al. (1998) FAO-56 — depletion thresholds
    Jones (2014) Plants and Microclimate — wilting criteria
    Doorenbos & Kassam (1979) — critical depletion fractions
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from dt_forge.reactive.fsm_engine import MultiStateFSMRuleEngine
from dt_forge.reactive.base import Rule

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.core.events import EventBus
    from dt_forge.data.storage.base import TimeSeriesStore, CacheStore, DocumentStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold constants (matching scratchpad + design doc)
# ---------------------------------------------------------------------------

# R-SDT-01: 10 cm depth
_VWC_10_WARN  = 0.045
_VWC_10_CRIT  = 0.030

# R-SDT-02: 30 cm depth
_VWC_30_WARN  = 0.042
_VWC_30_CRIT  = 0.028

# R-SDT-03: 60 cm depth
_VWC_60_WARN  = 0.038
_VWC_60_CRIT  = 0.025

# R-SDT-04: depletion rate [m³/m³/hr]
_DEPL_WARN    = 0.0015
_DEPL_CRIT    = 0.002

# R-SDT-05: soil water potential [kPa] (negative — more negative = drier)
_PSI_WARN     = -50.0
_PSI_CRIT     = -80.0

# R-SDT-06: inter-sensor divergence
_DIV_WARN     = 0.03
_DIV_CRIT     = 0.05

# R-SDT-07: ET loss rate [mm/day]
# Calibrated for the arid ~25°N climate used by WeatherSimulator.
# ETo = 7-12 mm/day, Kc = 1.10 → ETc = 7.7-13.2 mm/day is normal.
# DEPLETING fires when demand is above the typical mid-range baseline.
# ET alone does NOT trigger CRITICAL — low VWC thresholds (R-SDT-01/02) handle that.
_ET_WARN      = 9.5
_ET_CRIT      = 12.5   # kept for reference; not used in is_critical

# Near PWP: 0.055 m³/m³ (PWP ~0.050 + 10%)
_WILTING_THRESHOLD = 0.055

# FC = 0.10, 60% FC = 0.06 (OPTIMAL boundary)
_OPTIMAL_VWC  = 0.060


# ---------------------------------------------------------------------------
# Custom Rule R-SDT-C1: Rate-of-change slope at 10 cm (3-point regression)
# ---------------------------------------------------------------------------

class _RateOfChangeSlopeRule:
    """R-SDT-C1 — 3-point VWC slope at 10 cm.

    Injects ``_rule_roc_slope`` into readings (positive = depleting fast).
    Value > _DEPL_CRIT signals accelerated depletion.
    """

    rule_name = "roc_slope"

    def __init__(self):
        self._history: deque[float] = deque(maxlen=3)

    def evaluate(self, readings: dict[str, float | None]) -> str | None:
        v = readings.get("vwc_10cm")
        if v is None:
            return None
        self._history.append(v)
        if len(self._history) < 2:
            return None
        vals = list(self._history)
        # Simple linear slope over last N points (per-step, not per-hour)
        n = len(vals)
        slope = (vals[0] - vals[-1]) / max(n - 1, 1)  # positive = decreasing
        # Return numeric signal as string so FSM engine can store it
        return str(slope)


# ---------------------------------------------------------------------------
# Custom Rule R-SDT-C2: Depth Profile Inversion
# ---------------------------------------------------------------------------

class _DepthProfileInversionRule:
    """R-SDT-C2 — Detects when 60 cm is wetter than 10 cm by > 0.015.

    Injects ``_rule_profile_inversion`` = "1.0" when anomaly detected.
    Can indicate upwelling, sensor fault, or perched water table.
    """

    rule_name = "profile_inversion"

    def evaluate(self, readings: dict[str, float | None]) -> str | None:
        v10 = readings.get("vwc_10cm")
        v60 = readings.get("vwc_60cm")
        if v10 is None or v60 is None:
            return None
        inversion = v60 - v10
        return "1.0" if inversion > 0.015 else None


# ---------------------------------------------------------------------------
# Custom Rule R-SDT-C3: Temperature-Modulated Depletion
# ---------------------------------------------------------------------------

class _TempModulatedDepletionRule:
    """R-SDT-C3 — Tightens effective VWC thresholds when soil_temp > 35°C.

    At high temperatures stomatal conductance collapses, so plants effectively
    experience wilting stress at higher VWC than the cold-calibrated thresholds.
    Injects ``_rule_temp_stress`` = "1.0" when temp > 35°C.
    """

    rule_name = "temp_stress"

    def evaluate(self, readings: dict[str, float | None]) -> str | None:
        temp = readings.get("soil_temp_10cm")
        if temp is None:
            return None
        return "1.0" if temp > 35.0 else None


# ---------------------------------------------------------------------------
# SoilRuleEngine
# ---------------------------------------------------------------------------

class SoilRuleEngine(MultiStateFSMRuleEngine):
    """
    5-state soil moisture FSM for the Soil Digital Twin.

    States:
        OPTIMAL      — all depths well watered
        DEPLETING    — moderate depletion, monitor closely
        CRITICAL     — severe depletion, irrigation advised
        WILTING_RISK — near permanent wilting point, urgent
        SENSOR_FAULT — inter-sensor divergence or data gap detected

    Transitions use ``_goto_<state>`` wildcard triggers from MultiStateFSMRuleEngine.
    """

    _states = ["OPTIMAL", "DEPLETING", "CRITICAL", "WILTING_RISK", "SENSOR_FAULT"]
    _transitions = [
        {"trigger": "to_optimal",      "source": "*",           "dest": "OPTIMAL"},
        {"trigger": "to_depleting",    "source": "*",           "dest": "DEPLETING"},
        {"trigger": "to_critical",     "source": "*",           "dest": "CRITICAL"},
        {"trigger": "to_wilting",      "source": "CRITICAL",    "dest": "WILTING_RISK"},
        {"trigger": "to_fault",        "source": "*",           "dest": "SENSOR_FAULT"},
        {"trigger": "recover_optimal", "source": "SENSOR_FAULT","dest": "OPTIMAL"},
    ]
    _initial_state = "OPTIMAL"
    _severity_map = {
        "OPTIMAL":      "info",
        "DEPLETING":    "warning",
        "CRITICAL":     "warning",
        "WILTING_RISK": "critical",
        "SENSOR_FAULT": "critical",
    }

    def __init__(
        self,
        config: "TwinConfig",
        event_bus: "EventBus",
        *,
        ts_store: "TimeSeriesStore",
        cache: "CacheStore",
        doc_store: "DocumentStore",
        eval_interval: int = 900,   # 15 minutes matches sensor publish rate
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store,
            cache=cache,
            doc_store=doc_store,
            custom_rules=[
                _RateOfChangeSlopeRule(),
                _DepthProfileInversionRule(),
                _TempModulatedDepletionRule(),
            ],
            eval_interval=eval_interval,
        )
        # Track consecutive sensor divergence readings for fault detection
        self._divergence_count: int = 0

    def compute_desired_state(self, readings: dict[str, float | None]) -> str | None:
        """
        Apply R-SDT-01 through R-SDT-07 and custom rules C1-C3.

        Priority: SENSOR_FAULT > WILTING_RISK > CRITICAL > DEPLETING > OPTIMAL
        """
        vwc_10  = readings.get("vwc_10cm")
        vwc_30  = readings.get("vwc_30cm")
        vwc_60  = readings.get("vwc_60cm")
        depl    = readings.get("depletion_rate_avg")
        psi_10  = readings.get("soil_water_potential_10")
        div_flg = readings.get("sensor_divergence_flag")
        et_rate = readings.get("et_loss_rate")

        # Custom rule signals
        temp_stress    = readings.get("_rule_temp_stress") == "1.0"
        profile_inv    = readings.get("_rule_profile_inversion") == "1.0"

        # Apply temperature-modulated threshold tightening (R-SDT-C3)
        # At high temperature, wilting begins 10% earlier
        temp_factor = 1.10 if temp_stress else 1.0

        # -------------------------------------------------------
        # R-SDT-06: SENSOR_FAULT — divergence for 2 consecutive
        # -------------------------------------------------------
        if div_flg is not None and div_flg > _DIV_CRIT:
            self._divergence_count += 1
        elif div_flg is not None and div_flg > _DIV_WARN and profile_inv:
            # Profile inversion + moderate divergence also suspicious
            self._divergence_count += 1
        else:
            self._divergence_count = 0

        if self._divergence_count >= 2:
            return "SENSOR_FAULT"

        # -------------------------------------------------------
        # WILTING_RISK — near PWP (entered only from CRITICAL)
        # -------------------------------------------------------
        near_pwp = (
            (vwc_10 is not None and vwc_10 <= _WILTING_THRESHOLD * temp_factor) or
            (vwc_30 is not None and vwc_30 <= (_WILTING_THRESHOLD - 0.005) * temp_factor)
        )
        if near_pwp:
            # Only enter WILTING_RISK if already in CRITICAL or WILTING_RISK
            current = self.state  # type: ignore[attr-defined]
            if current in ("CRITICAL", "WILTING_RISK"):
                return "WILTING_RISK"

        # -------------------------------------------------------
        # CRITICAL — R-SDT-01 crit, R-SDT-02 crit, R-SDT-04 crit, R-SDT-05 crit
        # ET is intentionally excluded: high atmospheric demand does not make
        # soil critical when VWC is adequate — depletion rate and VWC do that.
        # -------------------------------------------------------
        is_critical = (
            (vwc_10 is not None  and vwc_10 < _VWC_10_CRIT  * temp_factor) or
            (vwc_30 is not None  and vwc_30 < _VWC_30_CRIT  * temp_factor) or
            (vwc_60 is not None  and vwc_60 < _VWC_60_CRIT  * temp_factor) or
            (depl   is not None  and depl   > _DEPL_CRIT)                   or
            (psi_10 is not None  and psi_10 < _PSI_CRIT)
        )
        if is_critical:
            return "CRITICAL"

        # -------------------------------------------------------
        # DEPLETING — R-SDT-01 warn, R-SDT-02 warn, R-SDT-04 warn, etc.
        # High ET (R-SDT-07) contributes here: signals rapid depletion risk
        # even when VWC has not yet crossed its own warn threshold.
        # -------------------------------------------------------
        is_depleting = (
            (vwc_10  is not None and vwc_10  < _VWC_10_WARN) or
            (vwc_30  is not None and vwc_30  < _VWC_30_WARN) or
            (vwc_60  is not None and vwc_60  < _VWC_60_WARN) or
            (depl    is not None and depl    > _DEPL_WARN)    or
            (psi_10  is not None and psi_10  < _PSI_WARN)     or
            (et_rate is not None and et_rate > _ET_WARN)
        )
        if is_depleting:
            return "DEPLETING"

        # -------------------------------------------------------
        # OPTIMAL — all thresholds clear
        # -------------------------------------------------------
        if (
            (vwc_10 is not None and vwc_10 >= _OPTIMAL_VWC) and
            (vwc_30 is not None and vwc_30 >= _OPTIMAL_VWC - 0.005) and
            (vwc_60 is not None and vwc_60 >= _OPTIMAL_VWC - 0.010)
        ):
            return "OPTIMAL"

        return None  # no change
