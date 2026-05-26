"""SDT Autonomous Layer — SoilOODALoop.

15-minute OODA cycle that decides irrigation advisories and alerts.
Decision logic uses a heuristic policy with optional PPO (Stable-Baselines3)
when a trained policy checkpoint exists in MinIO.

Observation vector (9D):
    [vwc_10, vwc_30, vwc_60, depletion_rate, soil_potential,
     et_rate, model_rmse, time_to_pwp, fsm_state_encoded]

Action space (3):
    0 = no_action
    1 = publish_advisory  (DEPLETING or moderate risk)
    2 = publish_alert     (CRITICAL or WILTING_RISK)

References:
    Allen et al. (1998) FAO-56 — irrigation scheduling
    Shyam & Gupta (1993) — deficit irrigation under arid conditions
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
    from cross_domain.sdt.reactive_layer import SoilRuleEngine

log = logging.getLogger(__name__)

# State encoding for RL observation vector
_STATE_ENCODE = {
    "OPTIMAL":      0.0,
    "DEPLETING":    0.25,
    "CRITICAL":     0.75,
    "WILTING_RISK": 1.0,
    "SENSOR_FAULT": 0.5,
}

_PWP = 0.050
_FC  = 0.100


def build_soil_planner() -> GoalPlanner:
    """Construct the goal planner with soil-specific goals."""
    return GoalPlanner(goals=[
        Goal(
            name="maintain_optimal_moisture",
            description="Keep VWC above 60% FC (0.06 m³/m³) at all depths",
            priority=10,
            success_condition="vwc_10cm >= 0.06",
        ),
        Goal(
            name="prevent_wilting",
            description="Prevent VWC from reaching PWP (0.050 m³/m³)",
            priority=9,
            success_condition="vwc_10cm > 0.055",
        ),
        Goal(
            name="sensor_integrity",
            description="Detect and flag sensor faults within 2 evaluation cycles",
            priority=7,
            success_condition="sensor_divergence_flag < 0.03",
        ),
    ])


class SoilOODALoop(OODALoop):
    """
    Soil-specific OODA loop.

    Extends OODALoop with domain-aware observe (9D soil obs vector),
    orient (soil risk assessment), and decide (3-action irrigation policy).
    Acts by publishing DomainEvents that BPDT and external systems subscribe to.

    Parameters
    ----------
    simulator : object
        SoilSensorSimulator with set_irrigation() / stop_irrigation() API.
        May be None if running without a simulator (physical sensors mode).
    reactive : SoilRuleEngine
        The FSM rule engine so OODA can read the current soil state.
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
        reactive: "SoilRuleEngine",
        mas: "MultiAgentSystem",
        connectors: list,
        planner: GoalPlanner,
        simulator=None,
        loop_interval: int = 900,   # 15-minute OODA cycle
        notifier=None,
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store,
            cache=cache,
            doc_store=doc_store,
            ditto_client=ditto_client,
            models=models,
            reactive=reactive,
            mas=mas,
            connectors=connectors,
            planner=planner,
            loop_interval=loop_interval,
            notifier=notifier,
        )
        self._simulator = simulator
        self._soil_reactive = reactive

    # ------------------------------------------------------------------
    # O — Observe: build 9D soil observation
    # ------------------------------------------------------------------

    async def observe(self) -> dict:
        base_obs = await super().observe()

        def _get(field: str) -> float:
            v = self.ts.get_latest(field)
            return v if v is not None else 0.0

        vwc_10   = _get("vwc_10cm")
        vwc_30   = _get("vwc_30cm")
        vwc_60   = _get("vwc_60cm")
        depl     = _get("depletion_rate_avg")
        psi_10   = _get("soil_water_potential_10")
        et_rate  = _get("et_loss_rate")

        # Model-sensor residual as RMSE proxy (simplified)
        model_rmse = abs(vwc_10 - 0.06) if vwc_10 else 0.0

        # Time to PWP estimate [hours]
        if vwc_10 > _PWP and depl > 1e-6:
            time_to_pwp = (vwc_10 - _PWP) / depl
        else:
            time_to_pwp = 999.0

        # Current FSM state
        try:
            fsm_state = self._soil_reactive.get_state()
        except Exception:
            fsm_state = "OPTIMAL"

        obs_vector = [vwc_10, vwc_30, vwc_60, depl, psi_10,
                      et_rate, model_rmse, min(time_to_pwp, 999.0),
                      _STATE_ENCODE.get(fsm_state, 0.0)]

        base_obs.update({
            "obs_vector": obs_vector,
            "fsm_state":  fsm_state,
            "vwc_10cm":   vwc_10,
            "vwc_30cm":   vwc_30,
            "vwc_60cm":   vwc_60,
            "depl_rate":  depl,
            "psi_10_kpa": psi_10,
            "et_rate":    et_rate,
            "time_to_pwp_hr": time_to_pwp,
        })
        return base_obs

    # ------------------------------------------------------------------
    # Or — Orient: soil risk assessment
    # ------------------------------------------------------------------

    async def orient(self, observation: dict) -> dict:
        assessment = await self.planner.assess(observation)

        fsm_state   = observation.get("fsm_state", "OPTIMAL")
        time_to_pwp = observation.get("time_to_pwp_hr", 999.0)
        vwc_10      = observation.get("vwc_10cm", 0.06)

        # Threshold-based risk assignment
        if fsm_state == "WILTING_RISK" or time_to_pwp < 3:
            assessment["risk_level"]                  = "critical"
            assessment["requires_human_intervention"] = True
            assessment["reason"] = (
                f"Wilting imminent: FSM={fsm_state}, TTP={time_to_pwp:.1f}hr"
            )
        elif fsm_state == "CRITICAL" or time_to_pwp < 12:
            assessment["risk_level"]          = "high"
            assessment["requires_irrigation"] = True
            assessment["reason"] = f"Critical soil moisture: TTP={time_to_pwp:.1f}hr"
        elif fsm_state == "DEPLETING":
            assessment["risk_level"]       = "medium"
            assessment["publish_advisory"] = True
        elif fsm_state == "SENSOR_FAULT":
            assessment["risk_level"]                  = "high"
            assessment["reason"]                      = "Sensor fault — manual check required"
            assessment["requires_human_intervention"] = True
        else:
            assessment["risk_level"] = "low"

        # Incorporate MAS agent findings — agents may detect patterns the FSM rules miss
        mas_findings = observation.get("mas_findings", {})
        agent_anomalies = [
            name for name, detail in mas_findings.items()
            if detail and detail.get("anomaly") and not detail.get("error")
        ]
        if agent_anomalies and assessment["risk_level"] == "low":
            assessment["risk_level"]       = "medium"
            assessment["publish_advisory"] = True
            assessment["reason"] = (
                f"MAS agents flagged sub-threshold anomaly: {', '.join(agent_anomalies)}"
            )
        assessment["agent_anomalies"] = agent_anomalies

        assessment["fsm_state"]   = fsm_state
        assessment["time_to_pwp"] = time_to_pwp
        return assessment

    # ------------------------------------------------------------------
    # D — Decide: select action from heuristic policy
    # ------------------------------------------------------------------

    async def decide(self, observation: dict, assessment: dict) -> dict:
        risk = assessment.get("risk_level", "low")
        fsm  = assessment.get("fsm_state", "OPTIMAL")

        if assessment.get("requires_human_intervention"):
            return {
                "action": "publish_alert",
                "alert_level": "critical",
                "fsm_state":   fsm,
                "reason": assessment.get("reason", "human intervention required"),
            }

        if risk == "critical" or fsm in ("WILTING_RISK",):
            return {
                "action":      "publish_alert",
                "alert_level": "critical",
                "fsm_state":   fsm,
                "reason":      assessment.get("reason", "critical soil moisture"),
            }

        if risk == "high" or fsm == "CRITICAL":
            return {
                "action":      "publish_alert",
                "alert_level": "warning",
                "fsm_state":   fsm,
                "reason":      assessment.get("reason", "severe depletion"),
            }

        if risk == "medium" or fsm == "DEPLETING":
            return {
                "action":      "publish_advisory",
                "fsm_state":   fsm,
                "reason":      "moderate depletion detected",
            }

        return {"action": "no_action", "fsm_state": fsm}

    # ------------------------------------------------------------------
    # A — Act: publish events
    # ------------------------------------------------------------------

    async def act(self, plan: dict) -> None:
        action = plan.get("action")

        if action == "publish_alert":
            severity = (
                "critical" if plan.get("alert_level") == "critical" else "warning"
            )
            payload = {
                "fsm_state":  plan.get("fsm_state"),
                "reason":     plan.get("reason"),
                "alert_level": plan.get("alert_level"),
            }
            self.doc.log_event("soil_moisture_alert", payload, severity=severity)
            await self.bus.publish(
                DomainEvent(
                    event_type="soil.moisture_alert",
                    source_layer="autonomous",
                    source_asset=self.config.asset_id,
                    payload=payload,
                    severity=severity,
                )
            )
            self.log.warning("SOIL ALERT [%s]: %s", severity.upper(), plan.get("reason"))

        elif action == "publish_advisory":
            payload = {
                "fsm_state": plan.get("fsm_state"),
                "reason":    plan.get("reason"),
            }
            self.doc.log_event("soil_moisture_advisory", payload, severity="info")
            await self.bus.publish(
                DomainEvent(
                    event_type="soil.moisture_advisory",
                    source_layer="autonomous",
                    source_asset=self.config.asset_id,
                    payload=payload,
                    severity="info",
                )
            )
            self.log.info("SOIL ADVISORY: %s", plan.get("reason"))

        else:
            self.log.debug("OODA no-action: FSM=%s", plan.get("fsm_state"))
