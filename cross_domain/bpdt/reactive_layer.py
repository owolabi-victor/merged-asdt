"""BPDT Reactive Layer — BioticPodRuleEngine.

7-state FSM: NOMINAL → SOIL_ALERT → PLANT_ALERT → IRRIGATION_PENDING →
             IRRIGATION_ACTIVE → EMERGENCY → MAINTENANCE

PID controller (Kp=15.0, Ki=0.8, Kd=2.0) is embedded and active ONLY
in IRRIGATION_ACTIVE state. It computes irrigation flow rate [L/hr] from
the VWC error signal (SP=0.08 m³/m³ = 80% FC).

State transitions are driven by SDT FSM state, PDT FSM state, OODA decisions,
and actuator confirmations.

References:
    Design spec: BPDT_Component_Overseer_Design.pdf §4.2
    PID tuning: Ziegler-Nichols method adapted for drip irrigation control
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from simple_pid import PID

from dt_forge.reactive.fsm_engine import MultiStateFSMRuleEngine
from dt_forge.core.events import DomainEvent

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.core.events import EventBus
    from dt_forge.data.storage.base import TimeSeriesStore, CacheStore, DocumentStore

log = logging.getLogger(__name__)

# PID parameters (design spec §5.3)
_PID_KP  = 15.0
_PID_KI  = 0.8
_PID_KD  = 2.0
_PID_SP  = 0.08    # setpoint: 80% FC [m³/m³]
_PID_MIN = 0.0     # [L/hr]
_PID_MAX = 20.0    # [L/hr]


class BioticPodRuleEngine(MultiStateFSMRuleEngine):
    """
    7-state FSM for the Biotic Pod Digital Twin.

    State transitions depend on the sub-twin FSM states (SDT, PDT)
    and external signals (OODA decisions, actuator confirmations).

    The PID controller is embedded here and computed each evaluation cycle
    when in IRRIGATION_ACTIVE state. Output is stored in cache for the OODA
    loop and actuator interface to consume.

    States:
        NOMINAL             — SDT OPTIMAL + PDT UNSTRESSED
        SOIL_ALERT          — SDT in DEPLETING or CRITICAL
        PLANT_ALERT         — PDT in MODERATE_STRESS or SEVERE_STRESS
        IRRIGATION_PENDING  — OODA decided to irrigate (valve not open yet)
        IRRIGATION_ACTIVE   — valve confirmed open; PID running
        EMERGENCY           — SDT CRITICAL AND PDT SEVERE_STRESS or WILTING
        MAINTENANCE         — sensor fault or component swap in progress
    """

    _states = [
        "NOMINAL", "SOIL_ALERT", "PLANT_ALERT",
        "IRRIGATION_PENDING", "IRRIGATION_ACTIVE",
        "EMERGENCY", "MAINTENANCE",
    ]
    _transitions = [
        {"trigger": "to_nominal",       "source": "*",                 "dest": "NOMINAL"},
        {"trigger": "to_soil_alert",    "source": ["NOMINAL", "SOIL_ALERT", "PLANT_ALERT"],
                                                                        "dest": "SOIL_ALERT"},
        {"trigger": "to_plant_alert",   "source": ["NOMINAL", "SOIL_ALERT", "PLANT_ALERT"],
                                                                        "dest": "PLANT_ALERT"},
        {"trigger": "to_pending",       "source": ["SOIL_ALERT", "PLANT_ALERT", "NOMINAL"],
                                                                        "dest": "IRRIGATION_PENDING"},
        {"trigger": "to_active",        "source": "IRRIGATION_PENDING","dest": "IRRIGATION_ACTIVE"},
        {"trigger": "stop_irrigation",  "source": "IRRIGATION_ACTIVE", "dest": "NOMINAL"},
        {"trigger": "to_emergency",     "source": "*",                 "dest": "EMERGENCY"},
        {"trigger": "to_maintenance",   "source": "*",                 "dest": "MAINTENANCE"},
        {"trigger": "exit_maintenance", "source": "MAINTENANCE",       "dest": "NOMINAL"},
    ]
    _initial_state = "NOMINAL"
    _severity_map  = {
        "NOMINAL":             "info",
        "SOIL_ALERT":          "warning",
        "PLANT_ALERT":         "warning",
        "IRRIGATION_PENDING":  "info",
        "IRRIGATION_ACTIVE":   "info",
        "EMERGENCY":           "critical",
        "MAINTENANCE":         "warning",
    }

    def __init__(
        self,
        config: "TwinConfig",
        event_bus: "EventBus",
        *,
        ts_store: "TimeSeriesStore",
        cache: "CacheStore",
        doc_store: "DocumentStore",
        eval_interval: int = 600,   # 10-min FSM cycle
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store, cache=cache, doc_store=doc_store,
            eval_interval=eval_interval,
        )

        # PID controller — active only in IRRIGATION_ACTIVE
        self._pid = PID(
            Kp=_PID_KP, Ki=_PID_KI, Kd=_PID_KD,
            setpoint=_PID_SP,
            output_limits=(_PID_MIN, _PID_MAX),
        )
        self._pid.auto_mode = False   # disabled until IRRIGATION_ACTIVE

        # External state injected by BPDT
        self._sdt_state: str = "OPTIMAL"
        self._pdt_state: str = "UNSTRESSED"
        self._ooda_decision: str = "no_action"
        self._valve_open: bool  = False
        self._maintenance_mode: bool = False
        self._current_irrigation_L_hr: float = 0.0

    # ------------------------------------------------------------------
    # External signals (called by BPDT orchestration)
    # ------------------------------------------------------------------

    def update_sub_twin_states(self, sdt_state: str, pdt_state: str) -> None:
        self._sdt_state = sdt_state
        self._pdt_state = pdt_state

    def signal_ooda_decision(self, decision: str) -> None:
        """'no_action' | 'irrigate' | 'emergency' | 'maintenance'"""
        self._ooda_decision = decision

    def confirm_valve_open(self, open_: bool) -> None:
        self._valve_open = open_

    def enter_maintenance(self) -> None:
        self._maintenance_mode = True

    def exit_maintenance_mode(self) -> None:
        self._maintenance_mode = False

    @property
    def irrigation_rate_L_hr(self) -> float:
        return self._current_irrigation_L_hr

    # ------------------------------------------------------------------
    # FSM state selection
    # ------------------------------------------------------------------

    def compute_desired_state(self, readings: dict) -> str | None:
        # Use in-memory state vars, set by update_sub_twin_states() every 30 s.
        # Do NOT read from Redis: Redis persists to disk across restarts, so stale
        # DEPLETING/WILTING_RISK values from a previous run would cause false alerts.
        sdt = self._sdt_state
        pdt = self._pdt_state
        decision  = self._ooda_decision
        valve     = self._valve_open
        maint     = self._maintenance_mode
        vwc_10    = readings.get("vwc_10cm")

        current = self.state  # type: ignore[attr-defined]

        # MAINTENANCE — highest priority override
        if maint:
            return "MAINTENANCE"
        if current == "MAINTENANCE" and not maint:
            return "NOMINAL"

        # EMERGENCY — both twins in severe state simultaneously
        emergency = (
            sdt in ("CRITICAL", "WILTING_RISK") and
            pdt in ("SEVERE_STRESS", "WILTING")
        )
        if emergency:
            return "EMERGENCY"

        # IRRIGATION_ACTIVE — valve confirmed open
        if current == "IRRIGATION_PENDING" and valve:
            return "IRRIGATION_ACTIVE"

        # Return from IRRIGATION_ACTIVE when VWC target reached
        if current == "IRRIGATION_ACTIVE":
            target_reached = vwc_10 is not None and vwc_10 >= _PID_SP
            if target_reached or decision == "no_action":
                return "NOMINAL"
            return None   # stay IRRIGATION_ACTIVE

        # IRRIGATION_PENDING — OODA decided to irrigate
        if decision == "irrigate" and current not in ("IRRIGATION_ACTIVE",):
            return "IRRIGATION_PENDING"

        # SOIL_ALERT or PLANT_ALERT based on sub-twin states
        if sdt in ("CRITICAL", "WILTING_RISK"):
            return "SOIL_ALERT"
        if pdt in ("MODERATE_STRESS", "SEVERE_STRESS", "WILTING"):
            return "PLANT_ALERT"
        if sdt in ("DEPLETING",):
            return "SOIL_ALERT"

        # NOMINAL — all clear
        if sdt == "OPTIMAL" and pdt in ("UNSTRESSED", "MILD_STRESS"):
            return "NOMINAL"

        return None

    # ------------------------------------------------------------------
    # Override evaluate to also run PID when IRRIGATION_ACTIVE
    # ------------------------------------------------------------------

    async def evaluate(self) -> str:
        state = await super().evaluate()

        vwc_10 = self.ts.get_latest("vwc_10cm")

        if state == "IRRIGATION_ACTIVE":
            self._pid.auto_mode = True
            output = self._pid(vwc_10) if vwc_10 is not None else 0.0
            self._current_irrigation_L_hr = round(output or 0.0, 3)
            self.cache.set_latest("irrigation_rate_L_hr", self._current_irrigation_L_hr)
        else:
            self._pid.auto_mode = False
            self._current_irrigation_L_hr = 0.0
            self.cache.set_latest("irrigation_rate_L_hr", 0.0)

        return state
