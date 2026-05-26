"""PDT Autonomous Layer — PlantOODALoop.

30-minute OODA cycle that publishes stress alerts and irrigation requests
to the BPDT (Biotic Pod Digital Twin) which controls the actuators.

Observation vector (10D):
    [cwsi, psi_leaf, gs_fraction, rwc, sap_fraction, eta_fraction,
     yield_penalty, stress_days, time_to_wilting, fsm_state_encoded]

Action space (4):
    0 = no_action
    1 = publish_advisory    (mild/moderate stress)
    2 = request_irrigation  (severe stress — signal to BPDT)
    3 = emergency_alert     (wilting / near PWP)

References:
    Doorenbos & Kassam (1979) — irrigation scheduling for yield protection
    Steduto et al. (2009) — AquaCrop decision rules
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from dt_forge.autonomous.ooda import OODALoop
from dt_forge.autonomous.planner import GoalPlanner, Goal
from dt_forge.core.events import DomainEvent

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.core.events import EventBus
    from dt_forge.data.storage.base import TimeSeriesStore, CacheStore, DocumentStore
    from dt_forge.services.ditto.client import DittoClient
    from dt_forge.intelligent.mas import MultiAgentSystem
    from cross_domain.pdt.reactive_layer import PlantRuleEngine

log = logging.getLogger(__name__)

_STATE_ENCODE = {
    "UNSTRESSED":      0.0,
    "MILD_STRESS":     0.2,
    "MODERATE_STRESS": 0.5,
    "SEVERE_STRESS":   0.75,
    "WILTING":         1.0,
    "RECOVERY":        0.1,
}

_GS_NOMINAL = 260.0
_SAP_NOMINAL = 0.25   # L/hr/plant reference


def build_plant_planner() -> GoalPlanner:
    return GoalPlanner(goals=[
        Goal(
            name="prevent_wilting",
            description="Keep ψ_leaf > -2.0 MPa at all times during V6-V8",
            priority=10,
            success_condition="leaf_water_potential_mpa > -2.0",
        ),
        Goal(
            name="maintain_stomatal_function",
            description="Keep gs > 40% nominal to sustain photosynthesis",
            priority=8,
            success_condition="stomatal_conductance_mmol > 104",
        ),
        Goal(
            name="limit_yield_penalty",
            description="Keep cumulative yield penalty below 10%",
            priority=7,
            success_condition="yield_penalty_pct < 10",
        ),
    ])


class PlantOODALoop(OODALoop):
    """
    Plant-specific OODA loop.

    Observe: builds 10D plant stress observation vector.
    Orient:  assesses drought risk using planner + FSM state.
    Decide:  selects from 4 plant-specific actions.
    Act:     publishes DomainEvents consumed by BPDT for irrigation control.
    """

    def __init__(
        self,
        config: "TwinConfig",
        event_bus: "EventBus",
        *,
        ts_store: "TimeSeriesStore",
        cache: "CacheStore",
        doc_store: "DocumentStore",
        ditto_client: "DittoClient",
        models: dict,
        reactive: "PlantRuleEngine",
        mas: "MultiAgentSystem",
        connectors: list,
        planner: GoalPlanner,
        simulator=None,
        loop_interval: int = 1800,
        notifier=None,
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store, cache=cache, doc_store=doc_store,
            ditto_client=ditto_client, models=models,
            reactive=reactive, mas=mas, connectors=connectors,
            planner=planner, loop_interval=loop_interval, notifier=notifier,
        )
        self._plant_reactive = reactive
        self._simulator = simulator

    async def observe(self) -> dict:
        base = await super().observe()

        def _get(f: str, default: float = 0.0) -> float:
            v = self.ts.get_latest(f)
            return v if v is not None else default

        cwsi        = _get("cwsi", 0.0)
        psi         = _get("leaf_water_potential_mpa", -0.3)
        gs          = _get("stomatal_conductance_mmol", _GS_NOMINAL)
        rwc         = _get("relative_water_content", 0.95)
        sap         = _get("sap_flow_L_hr", _SAP_NOMINAL)
        eta_frac    = _get("eta_fraction", 1.0)
        yield_pen   = _get("yield_penalty_pct", 0.0)
        stress_days = _get("stress_duration_days", 0.0)

        # Time to wilting estimate
        try:
            df = self.ts.query_recent("leaf_water_potential_mpa", minutes=120)
            if df is not None and len(df) >= 2:
                vals = df["value"].tolist() if hasattr(df, "value") else list(df)
                slope = (vals[-1] - vals[0]) / 2.0   # MPa/hr
            else:
                slope = -0.03
        except Exception:
            slope = -0.03

        ttp = (_get("leaf_water_potential_mpa", -0.3) - (-2.5)) / abs(slope) if slope < -1e-4 else 999.0
        ttp = max(0.0, ttp)

        try:
            fsm_state = self._plant_reactive.get_state()
        except Exception:
            fsm_state = "UNSTRESSED"

        base.update({
            "obs_vector": [
                cwsi, abs(psi) / 2.5, gs / _GS_NOMINAL, rwc,
                sap / _SAP_NOMINAL, eta_frac, yield_pen / 100.0,
                min(stress_days, 30.0) / 30.0,
                min(ttp, 48.0) / 48.0,
                _STATE_ENCODE.get(fsm_state, 0.0),
            ],
            "fsm_state":   fsm_state,
            "cwsi":        cwsi,
            "psi_mpa":     psi,
            "gs_mmol":     gs,
            "eta_fraction":eta_frac,
            "yield_penalty_pct": yield_pen,
            "stress_days": stress_days,
            "time_to_wilting_hr": ttp,
        })
        return base

    async def orient(self, observation: dict) -> dict:
        assessment = await self.planner.assess(observation)

        fsm   = observation.get("fsm_state", "UNSTRESSED")
        ttp   = observation.get("time_to_wilting_hr", 999.0)
        psi   = observation.get("psi_mpa", -0.3)
        pen   = observation.get("yield_penalty_pct", 0.0)

        # Threshold-based risk assignment
        if fsm == "WILTING" or ttp < 3:
            assessment["risk_level"]                  = "critical"
            assessment["requires_human_intervention"] = True
            assessment["reason"] = f"Wilting imminent: FSM={fsm}, TTP={ttp:.1f}hr"
        elif fsm == "SEVERE_STRESS" or ttp < 12:
            assessment["risk_level"]         = "high"
            assessment["request_irrigation"] = True
            assessment["reason"] = f"Severe plant stress: ψ={psi:.2f} MPa"
        elif fsm in ("MODERATE_STRESS",) or pen > 10:
            assessment["risk_level"] = "medium"
            assessment["reason"]     = f"Moderate stress, yield penalty={pen:.1f}%"
        elif fsm == "MILD_STRESS":
            assessment["risk_level"] = "low_warning"
        else:
            assessment["risk_level"] = "low"

        # Incorporate MAS agent findings — agents may surface patterns beyond FSM rules
        mas_findings = observation.get("mas_findings", {})
        agent_anomalies = [
            name for name, detail in mas_findings.items()
            if detail and detail.get("anomaly") and not detail.get("error")
        ]
        if agent_anomalies and assessment["risk_level"] in ("low", "low_warning"):
            assessment["risk_level"] = "medium"
            assessment["reason"] = (
                f"MAS agents flagged sub-threshold anomaly: {', '.join(agent_anomalies)}"
            )
        assessment["agent_anomalies"] = agent_anomalies

        assessment["fsm_state"]          = fsm
        assessment["time_to_wilting_hr"] = ttp
        return assessment

    async def decide(self, observation: dict, assessment: dict) -> dict:
        risk = assessment.get("risk_level", "low")
        fsm  = assessment.get("fsm_state", "UNSTRESSED")

        if assessment.get("requires_human_intervention") or fsm == "WILTING":
            return {
                "action":    "emergency_alert",
                "fsm_state": fsm,
                "reason":    assessment.get("reason", "wilting / human intervention"),
            }
        if risk == "high" or assessment.get("request_irrigation"):
            return {
                "action":    "request_irrigation",
                "fsm_state": fsm,
                "reason":    assessment.get("reason", "severe drought stress"),
            }
        if risk in ("medium", "low_warning"):
            return {
                "action":    "publish_advisory",
                "fsm_state": fsm,
                "reason":    assessment.get("reason", "moderate stress"),
            }
        return {"action": "no_action", "fsm_state": fsm}

    async def act(self, plan: dict) -> None:
        action = plan.get("action")

        if action == "emergency_alert":
            payload = {"fsm_state": plan["fsm_state"], "reason": plan["reason"]}
            self.doc.log_event("plant_emergency", payload, severity="critical")
            await self.bus.publish(DomainEvent(
                event_type="plant.wilting_emergency",
                source_layer="autonomous",
                source_asset=self.config.asset_id,
                payload=payload, severity="critical",
            ))
            self.log.critical("PLANT EMERGENCY: %s", plan["reason"])

        elif action == "request_irrigation":
            payload = {"fsm_state": plan["fsm_state"], "reason": plan["reason"]}
            self.doc.log_event("plant_irrigation_request", payload, severity="warning")
            await self.bus.publish(DomainEvent(
                event_type="plant.irrigation_requested",
                source_layer="autonomous",
                source_asset=self.config.asset_id,
                payload=payload, severity="warning",
            ))
            self.log.warning("IRRIGATION REQUEST: %s", plan["reason"])

        elif action == "publish_advisory":
            payload = {"fsm_state": plan["fsm_state"], "reason": plan["reason"]}
            self.doc.log_event("plant_stress_advisory", payload, severity="info")
            await self.bus.publish(DomainEvent(
                event_type="plant.stress_advisory",
                source_layer="autonomous",
                source_asset=self.config.asset_id,
                payload=payload, severity="info",
            ))

        else:
            self.log.debug("Plant OODA no-action: FSM=%s", plan.get("fsm_state"))
