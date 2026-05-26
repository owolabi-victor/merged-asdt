"""PDT Reactive Layer — PlantRuleEngine.

6-state FSM: UNSTRESSED → MILD_STRESS → MODERATE_STRESS → SEVERE_STRESS →
             WILTING → RECOVERY

Threshold rules R-PDT-01 through R-PDT-08 drive state selection.
Custom rules R-PDT-C1/C2/C3 inject synthetic signals.

State priority (highest wins):
    WILTING > SEVERE_STRESS > MODERATE_STRESS > MILD_STRESS > RECOVERY > UNSTRESSED

References:
    Jackson et al. (1981) — CWSI thresholds
    Lecoeur & Sinclair (1996) — maize gs decline curves
    Westgate & Boyer (1985) — maize leaf water potential at wilting
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from dt_forge.reactive.fsm_engine import MultiStateFSMRuleEngine

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.core.events import EventBus
    from dt_forge.data.storage.base import TimeSeriesStore, CacheStore, DocumentStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------

# CWSI thresholds (Jackson et al. 1981, Idso et al. 1981 for maize)
_CWSI_MILD     = 0.20
_CWSI_MODERATE = 0.45
_CWSI_SEVERE   = 0.70

# Leaf water potential [MPa] (Westgate & Boyer 1985)
_PSI_MILD      = -0.8
_PSI_MODERATE  = -1.2
_PSI_SEVERE    = -1.8
_PSI_WILTING   = -2.5

# Stomatal conductance fraction of nominal (260 mmol/m²/s)
_GS_NOMINAL    = 260.0
_GS_MILD_FRAC  = 0.75    # <75% nominal = mild
_GS_SEVERE_FRAC = 0.40   # <40% nominal = severe
_GS_WILTING_FRAC = 0.15  # <15% nominal = wilting

# Sap flow: fraction of field-maximum
_SAP_NOMINAL   = 0.25    # L/hr/plant at full growth (approximate V6-V8 midday)
_SAP_WILTING   = 0.10    # <10% nominal = wilting

# RWC
_RWC_MODERATE  = 0.90
_RWC_SEVERE    = 0.85
_RWC_WILTING   = 0.80


# ---------------------------------------------------------------------------
# Custom Rules
# ---------------------------------------------------------------------------

class _CWSISlopeRule:
    """R-PDT-C1 — 3-point CWSI slope (positive = stress worsening)."""
    rule_name = "cwsi_slope"

    def __init__(self):
        self._hist: deque[float] = deque(maxlen=3)

    def evaluate(self, readings: dict) -> str | None:
        cwsi = readings.get("cwsi")
        if cwsi is None:
            return None
        self._hist.append(cwsi)
        if len(self._hist) < 2:
            return None
        vals = list(self._hist)
        slope = (vals[-1] - vals[0]) / max(len(vals) - 1, 1)
        return str(round(slope, 5))


class _NocturnalStressRule:
    """R-PDT-C2 — Detects stress persisting into night hours (VPD < 0.5 kPa
    but gs still depressed). Indicates osmotic adjustment failure.
    """
    rule_name = "nocturnal_stress"

    def evaluate(self, readings: dict) -> str | None:
        vpd = readings.get("vpd_kpa")
        gs  = readings.get("stomatal_conductance_mmol")
        if vpd is None or gs is None:
            return None
        # Night-like VPD but stomata still closed
        if vpd < 0.5 and gs < _GS_NOMINAL * 0.60:
            return "1.0"
        return None


class _RecoveryDetectionRule:
    """R-PDT-C3 — Detects recovery: sap flow rising > 20% from recent minimum."""
    rule_name = "recovery_signal"

    def __init__(self):
        self._sap_hist: deque[float] = deque(maxlen=6)
        self._vwc_hist: deque[float] = deque(maxlen=4)

    def evaluate(self, readings: dict) -> str | None:
        sap = readings.get("sap_flow_L_hr")
        vwc = readings.get("vwc_root_zone")
        if sap is not None:
            self._sap_hist.append(sap)
        if vwc is not None:
            self._vwc_hist.append(vwc)

        if len(self._sap_hist) < 3:
            return None

        vals = list(self._sap_hist)
        min_sap = min(vals[:-1])
        current = vals[-1]
        sap_rising = (current - min_sap) / max(min_sap, 0.001) > 0.20

        vwc_improving = False
        if len(self._vwc_hist) >= 2:
            vwc_vals = list(self._vwc_hist)
            vwc_improving = vwc_vals[-1] > vwc_vals[0]

        if sap_rising and vwc_improving:
            return "1.0"
        return None


class _WiltingTimeRule:
    """R-PDT-08 — Estimate time-to-wilting from ψ trajectory (Westgate & Boyer 1985).

    Uses a rolling 2-hour ψ window (4 × 30-min steps) to extrapolate when ψ
    will reach the permanent wilting threshold of -2.5 MPa.
    Returns hours-to-wilting as a string, or None if ψ is stable/recovering.
    """
    rule_name = "time_to_wilting_h"

    def __init__(self):
        self._psi_hist: deque[float] = deque(maxlen=4)   # 4 × 30-min = 2h window

    def evaluate(self, readings: dict) -> str | None:
        psi = readings.get("leaf_water_potential_mpa")
        if psi is None:
            return None
        self._psi_hist.append(psi)
        if len(self._psi_hist) < 2:
            return None
        vals = list(self._psi_hist)
        # Decline rate per step (30 min), scaled to per-hour
        slope_mpa_hr = (vals[-1] - vals[0]) / max(len(vals) - 1, 1) * 2.0
        if slope_mpa_hr >= -1e-4:   # stable or recovering — no wilting forecast
            return None
        ttp_h = (psi - (-2.5)) / abs(slope_mpa_hr)
        return str(round(max(0.0, ttp_h), 2))


# ---------------------------------------------------------------------------
# PlantRuleEngine
# ---------------------------------------------------------------------------

class PlantRuleEngine(MultiStateFSMRuleEngine):
    """
    6-state plant stress FSM for the Plant Digital Twin.

    States:
        UNSTRESSED      — all indicators nominal
        MILD_STRESS     — early stress signs (CWSI > 0.20 or gs < 75%)
        MODERATE_STRESS — moderate drought (CWSI > 0.45 or ψ < -1.2 MPa)
        SEVERE_STRESS   — severe drought (CWSI > 0.70 or gs < 40%)
        WILTING         — near permanent wilting (ψ < -2.5 MPa)
        RECOVERY        — VWC improving + sap flow rising
    """

    _states = [
        "UNSTRESSED", "MILD_STRESS", "MODERATE_STRESS",
        "SEVERE_STRESS", "WILTING", "RECOVERY",
    ]
    _transitions = [
        {"trigger": "to_unstressed",      "source": "*",             "dest": "UNSTRESSED"},
        {"trigger": "to_mild",            "source": "*",             "dest": "MILD_STRESS"},
        {"trigger": "to_moderate",        "source": "*",             "dest": "MODERATE_STRESS"},
        {"trigger": "to_severe",          "source": "*",             "dest": "SEVERE_STRESS"},
        {"trigger": "to_wilting",         "source": "SEVERE_STRESS", "dest": "WILTING"},
        {"trigger": "enter_recovery",     "source": [
            "MILD_STRESS", "MODERATE_STRESS", "SEVERE_STRESS"
        ],                                                            "dest": "RECOVERY"},
        {"trigger": "recovery_complete",  "source": "RECOVERY",      "dest": "UNSTRESSED"},
    ]
    _initial_state = "UNSTRESSED"
    _severity_map = {
        "UNSTRESSED":      "info",
        "MILD_STRESS":     "info",
        "MODERATE_STRESS": "warning",
        "SEVERE_STRESS":   "warning",
        "WILTING":         "critical",
        "RECOVERY":        "info",
    }

    def __init__(
        self,
        config: "TwinConfig",
        event_bus: "EventBus",
        *,
        ts_store: "TimeSeriesStore",
        cache: "CacheStore",
        doc_store: "DocumentStore",
        eval_interval: int = 1800,   # 30-minute FSM cycle
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store,
            cache=cache,
            doc_store=doc_store,
            custom_rules=[
                _CWSISlopeRule(),
                _NocturnalStressRule(),
                _RecoveryDetectionRule(),
                _WiltingTimeRule(),
            ],
            eval_interval=eval_interval,
        )

    def compute_desired_state(self, readings: dict) -> str | None:
        cwsi     = readings.get("cwsi")
        psi      = readings.get("leaf_water_potential_mpa")
        gs       = readings.get("stomatal_conductance_mmol")
        sap      = readings.get("sap_flow_L_hr")
        rwc      = readings.get("relative_water_content")
        recovery = readings.get("_rule_recovery_signal") == "1.0"
        nocturnal= readings.get("_rule_nocturnal_stress") == "1.0"

        # R-PDT-08: time-to-wilting forecast (Westgate & Boyer 1985)
        _ttp_str = readings.get("_rule_time_to_wilting_h")
        ttp_h    = float(_ttp_str) if _ttp_str is not None else None

        current = self.state  # type: ignore[attr-defined]

        # -------------------------------------------------------
        # WILTING — hardest threshold, entered from SEVERE_STRESS
        # -------------------------------------------------------
        wilting = (
            (psi   is not None and psi   < _PSI_WILTING)                  or
            (sap   is not None and sap   < _SAP_NOMINAL * _SAP_WILTING)   or
            (rwc   is not None and rwc   < _RWC_WILTING)                   or
            # R-PDT-08: < 4h to wilting is a critical alarm
            (ttp_h is not None and ttp_h < 4.0 and current == "SEVERE_STRESS")
        )
        if wilting and current in ("SEVERE_STRESS", "WILTING"):
            return "WILTING"

        # -------------------------------------------------------
        # SEVERE_STRESS
        # -------------------------------------------------------
        severe = (
            (cwsi  is not None and cwsi  > _CWSI_SEVERE)                   or
            (gs    is not None and gs    < _GS_NOMINAL * _GS_SEVERE_FRAC)  or
            (psi   is not None and psi   < _PSI_SEVERE)                    or
            (rwc   is not None and rwc   < _RWC_SEVERE)                    or
            nocturnal                                                        or
            # R-PDT-08: < 12h to wilting — escalate to SEVERE immediately
            (ttp_h is not None and ttp_h < 12.0)
        )
        if severe:
            return "SEVERE_STRESS"

        # -------------------------------------------------------
        # RECOVERY — detected before checking lower stress states
        # -------------------------------------------------------
        if recovery and current in ("MILD_STRESS", "MODERATE_STRESS", "SEVERE_STRESS"):
            return "RECOVERY"
        if current == "RECOVERY":
            # Stay in recovery until fully unstressed
            if cwsi is not None and cwsi < _CWSI_MILD and (gs is None or gs > _GS_NOMINAL * 0.85):
                return "UNSTRESSED"
            return None  # remain in RECOVERY

        # -------------------------------------------------------
        # MODERATE_STRESS
        # -------------------------------------------------------
        moderate = (
            (cwsi is not None and cwsi > _CWSI_MODERATE)                  or
            (psi  is not None and psi  < _PSI_MODERATE)                   or
            (rwc  is not None and rwc  < _RWC_MODERATE)
        )
        if moderate:
            return "MODERATE_STRESS"

        # -------------------------------------------------------
        # MILD_STRESS
        # -------------------------------------------------------
        mild = (
            (cwsi is not None and cwsi > _CWSI_MILD)                      or
            (gs   is not None and gs   < _GS_NOMINAL * _GS_MILD_FRAC)    or
            (psi  is not None and psi  < _PSI_MILD)
        )
        if mild:
            return "MILD_STRESS"

        # -------------------------------------------------------
        # UNSTRESSED
        # -------------------------------------------------------
        return "UNSTRESSED"
