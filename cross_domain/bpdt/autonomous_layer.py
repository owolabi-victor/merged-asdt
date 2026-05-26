"""BPDT Autonomous Layer — BioticPodOODALoop.

Layer 6 overseer for the full soil-plant-biotic system.  Coordinates SDT + PDT
via BoundaryConditions and drives the PID irrigation actuator through the
BioticPodRuleEngine.

Orient is handled by an AutonomousOverseer LLM agent that reads MAS findings
from all three twins and may interrogate specific agents before deciding.

Decide enforces only hard safety constraints on top of the overseer's
recommendation.  Rule-based logic belongs in the reactive layer (Layer 4);
the strategic decision belongs here (Layer 6).

Observation vector (14D) — preserved for RL policy compatibility:
    [vwc_10, vwc_30, psi_leaf, cwsi, gs_frac, depl_rate, et_rate,
     irr_rate, yield_penalty, sdt_state_enc, pdt_state_enc,
     bpdt_state_enc, time_since_last_irr, water_budget_7d]

Action space (4):
    0 = no_action
    1 = irrigate           — open valve, start PID
    2 = emergency_halt     — close valve, signal human intervention
    3 = maintenance_mode   — pause automation for sensor/component work

Hot-swap protocol:
    8-step procedure: validate → drain → snapshot → deregister →
    register → BoundaryConditions → warm-up → resume
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from dt_forge.autonomous.ooda import OODALoop
from dt_forge.autonomous.overseer import AutonomousOverseer, OverseerDecision
from dt_forge.autonomous.planner import GoalPlanner, Goal
from dt_forge.core.events import DomainEvent

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.core.events import EventBus
    from dt_forge.data.storage.base import TimeSeriesStore, CacheStore, DocumentStore
    from dt_forge.services.ditto.client import DittoClient
    from dt_forge.intelligent.mas import MultiAgentSystem
    from cross_domain.bpdt.reactive_layer import BioticPodRuleEngine

log = logging.getLogger(__name__)

_SDT_ENCODE = {
    "OPTIMAL": 0.0, "DEPLETING": 0.3, "CRITICAL": 0.7,
    "WILTING_RISK": 1.0, "SENSOR_FAULT": 0.5,
}
_PDT_ENCODE = {
    "UNSTRESSED": 0.0, "MILD_STRESS": 0.2, "MODERATE_STRESS": 0.5,
    "SEVERE_STRESS": 0.8, "WILTING": 1.0, "RECOVERY": 0.1,
}
_BPDT_ENCODE = {
    "NOMINAL": 0.0, "SOIL_ALERT": 0.3, "PLANT_ALERT": 0.3,
    "IRRIGATION_PENDING": 0.5, "IRRIGATION_ACTIVE": 0.5,
    "EMERGENCY": 1.0, "MAINTENANCE": 0.4,
}

_AVAILABLE_ACTIONS = ["no_action", "irrigate", "emergency_halt", "advisory", "maintenance"]

# Soil VWC target for PID (RAW threshold)
_PID_SP = 0.08


def build_bpdt_planner() -> GoalPlanner:
    return GoalPlanner(goals=[
        Goal(
            name="prevent_crop_wilting",
            description="Ensure plant never reaches WILTING state",
            priority=10,
            success_condition="pdt_state != WILTING",
        ),
        Goal(
            name="maintain_vwc_above_raw",
            description="Keep soil VWC above RAW threshold (0.077 m³/m³)",
            priority=9,
            success_condition="vwc_10cm > 0.077",
        ),
        Goal(
            name="water_efficiency",
            description="Maintain irrigation WUE > 1.0 kg/m³",
            priority=6,
            success_condition="water_productivity_kg_m3 > 1.0",
        ),
    ])


class BioticPodOODALoop(OODALoop):
    """
    Biotic Pod OODA loop — the autonomous overseer for the full soil-plant system.

    Orient is driven by an AutonomousOverseer LLM agent that synthesises
    MAS findings from BPDT, SDT, and PDT agents, and may query them directly
    for deeper analysis before deciding.

    Decide enforces hard safety constraints; all other logic lives in the overseer.

    Parameters
    ----------
    llm    : LangChain chat model.  If None, falls back to rule-based orient.
    sdt    : SoilDigitalTwin reference (optional — for direct method access and MAS queries)
    pdt    : PlantDigitalTwin reference (optional)
    bpdt_reactive : BioticPodRuleEngine
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
        reactive: "BioticPodRuleEngine",
        mas: "MultiAgentSystem",
        connectors: list,
        planner: GoalPlanner,
        llm=None,
        sdt=None,
        pdt=None,
        loop_interval: int = 600,
        notifier=None,
    ):
        super().__init__(
            config, event_bus,
            ts_store=ts_store, cache=cache, doc_store=doc_store,
            ditto_client=ditto_client, models=models,
            reactive=reactive, mas=mas, connectors=connectors,
            planner=planner, loop_interval=loop_interval, notifier=notifier,
        )
        self._bpdt_reactive = reactive
        self._sdt = sdt
        self._pdt = pdt
        self._last_irrigation_time: float = 0.0
        self._total_water_L: float = 0.0
        self._last_overseer_decision: OverseerDecision | None = None

        if llm is not None:
            goal_names = [g.name for g in planner.goals]

            async def _cross_mas_query(agent_name: str, question: str) -> str:
                """Query any agent across BPDT, SDT, or PDT MAS layers."""
                for mas_obj in [
                    mas,
                    getattr(sdt, "mas", None),
                    getattr(pdt, "mas", None),
                ]:
                    if mas_obj is None:
                        continue
                    result = await mas_obj.ask_agent(agent_name, question)
                    if "not found" not in result.lower():
                        return result
                return f"Agent '{agent_name}' not found in any MAS layer."

            self._overseer = AutonomousOverseer(
                config, llm, mas,
                goals=goal_names,
                available_actions=_AVAILABLE_ACTIONS,
                extra_query_fn=_cross_mas_query,
            )
        else:
            self._overseer = None

    # ------------------------------------------------------------------
    # O — Observe (14D vector + all MAS findings)
    # ------------------------------------------------------------------

    async def observe(self) -> dict:
        base = await super().observe()   # includes own MAS findings via updated base

        def _get(f: str, d: float = 0.0) -> float:
            v = self.ts.get_latest(f)
            return v if v is not None else d

        vwc_10   = _get("vwc_10cm", 0.06)
        vwc_30   = _get("vwc_30cm", 0.06)
        psi      = _get("leaf_water_potential_mpa", -0.3)
        cwsi     = _get("cwsi", 0.0)
        gs       = _get("stomatal_conductance_mmol", 260.0)
        depl     = _get("depletion_rate_avg", 0.0)
        et_rate  = _get("et_loss_rate", 2.0)
        irr_rate = self.cache.get_latest_cached("irrigation_rate_L_hr") or 0.0
        yield_pen = _get("yield_penalty_pct", 0.0)

        try:
            sdt_state  = self._sdt.get_fsm_state() if self._sdt else "OPTIMAL"
            pdt_state  = self._pdt.get_fsm_state() if self._pdt else "UNSTRESSED"
            bpdt_state = self._bpdt_reactive.get_state()
        except Exception:
            sdt_state = pdt_state = bpdt_state = "NOMINAL"

        time_since_irr = (time.time() - self._last_irrigation_time) / 3600.0
        budget_7d      = self.cache.get_latest_cached("rolling_7d_balance_mm") or 0.0

        self._bpdt_reactive.update_sub_twin_states(sdt_state, pdt_state)
        await self._propagate_boundary_conditions()

        obs_vector = [
            vwc_10, vwc_30, abs(psi) / 2.5, cwsi, gs / 260.0,
            depl / 0.003, et_rate / 6.0,
            irr_rate / 20.0, yield_pen / 30.0,
            _SDT_ENCODE.get(sdt_state, 0.0),
            _PDT_ENCODE.get(pdt_state, 0.0),
            _BPDT_ENCODE.get(bpdt_state, 0.0),
            min(time_since_irr, 48.0) / 48.0,
            (budget_7d + 50.0) / 100.0,
        ]

        # Collect MAS findings from all three twins for the overseer
        all_mas: dict = {"bpdt": base.get("mas_findings", {})}
        if self._sdt and hasattr(self._sdt, "mas"):
            all_mas["sdt"] = {
                a.agent_name: self._sdt.mas.get_agent_detail(a.agent_name)
                for a in self._sdt.mas.agents
            }
        if self._pdt and hasattr(self._pdt, "mas"):
            all_mas["pdt"] = {
                a.agent_name: self._pdt.mas.get_agent_detail(a.agent_name)
                for a in self._pdt.mas.agents
            }

        base.update({
            "obs_vector":        obs_vector,
            "sdt_state":         sdt_state,
            "pdt_state":         pdt_state,
            "bpdt_state":        bpdt_state,
            "vwc_10cm":          vwc_10,
            "vwc_30cm":          vwc_30,
            "psi_mpa":           psi,
            "cwsi":              cwsi,
            "depl_rate":         depl,
            "et_rate":           et_rate,
            "irr_rate":          irr_rate,
            "yield_penalty":     yield_pen,
            "time_since_irr_hr": time_since_irr,
            "all_mas_findings":  all_mas,
        })
        return base

    async def _propagate_boundary_conditions(self) -> None:
        """Apply BoundaryConditions: SDT → PDT soil inputs."""
        if self._sdt is None or self._pdt is None:
            return
        try:
            sdt_r = self._sdt.get_latest_readings()
            self._pdt.set_soil_inputs(
                vwc_10cm=sdt_r.get("vwc_10cm"),
                vwc_30cm=sdt_r.get("vwc_30cm"),
                root_zone_potential_kpa=sdt_r.get("soil_water_potential_10"),
            )
        except Exception as e:
            log.debug("BC propagation error: %s", e)

    # ------------------------------------------------------------------
    # Or — Orient: overseer-driven (LLM) with rule-based fallback
    # ------------------------------------------------------------------

    async def orient(self, observation: dict) -> dict:
        if self._overseer is not None:
            decision = await self._overseer.run(observation)
            self._last_overseer_decision = decision
            log.debug(
                "Overseer decision: action=%s risk=%s",
                decision.action, decision.risk_level,
            )
            return {
                "action":          decision.action,
                "risk_level":      decision.risk_level,
                "reason":          decision.reasoning,
                "goals_addressed": decision.goals_addressed,
                "agent_queries":   decision.agent_queries,
                "overseer_driven": True,
                "sdt_state":       observation.get("sdt_state"),
                "pdt_state":       observation.get("pdt_state"),
                "bpdt_state":      observation.get("bpdt_state"),
                "emergency":       (
                    observation.get("sdt_state") in ("CRITICAL", "WILTING_RISK") and
                    observation.get("pdt_state") in ("SEVERE_STRESS", "WILTING")
                ),
            }
        return self._rule_based_orient(observation)

    def _rule_based_orient(self, observation: dict) -> dict:
        """Fallback when no LLM is configured — mirrors the original heuristic policy."""
        sdt   = observation.get("sdt_state", "OPTIMAL")
        pdt   = observation.get("pdt_state", "UNSTRESSED")
        bpdt  = observation.get("bpdt_state", "NOMINAL")
        vwc   = observation.get("vwc_10cm", 0.06)
        psi   = observation.get("psi_mpa", -0.3)

        emergency = (
            sdt in ("CRITICAL", "WILTING_RISK") and
            pdt in ("SEVERE_STRESS", "WILTING")
        )

        if emergency:
            action, risk, reason = "emergency_halt", "critical", f"SDT={sdt}, PDT={pdt}: dual critical state"
        elif pdt == "WILTING" or psi < -2.3:
            action, risk, reason = "irrigate", "critical", f"Plant wilting: ψ={psi:.2f} MPa"
        elif sdt in ("CRITICAL",) or pdt in ("SEVERE_STRESS",):
            action, risk, reason = "irrigate", "high", f"SDT={sdt}, PDT={pdt}"
        elif sdt == "DEPLETING" or pdt == "MODERATE_STRESS":
            action  = "irrigate" if vwc < 0.055 else "advisory"
            risk    = "medium"
            reason  = f"SDT={sdt}, PDT={pdt}"
        else:
            action, risk, reason = "no_action", "low", "nominal"

        return {
            "action": action, "risk_level": risk, "reason": reason,
            "overseer_driven": False,
            "sdt_state": sdt, "pdt_state": pdt, "bpdt_state": bpdt,
            "emergency": emergency,
        }

    # ------------------------------------------------------------------
    # D — Decide: safety constraints first, then strategy, then RL tactics
    # ------------------------------------------------------------------

    async def decide(self, observation: dict, assessment: dict) -> dict:
        action    = assessment.get("action", "no_action")
        emergency = assessment.get("emergency", False)
        risk      = assessment.get("risk_level", "low")
        sdt       = assessment.get("sdt_state", "OPTIMAL")
        pdt       = assessment.get("pdt_state", "UNSTRESSED")
        reason    = assessment.get("reason", "")

        # --- Layer 1: hard safety constraints (override everything) ---

        if emergency:
            return {
                "action":    "emergency_halt",
                "reason":    f"Dual critical override: SDT={sdt}, PDT={pdt}",
                "sdt_state": sdt,
                "pdt_state": pdt,
            }

        if action == "irrigate":
            time_since = observation.get("time_since_irr_hr", 999.0)
            if time_since < 2.0 and risk != "critical":
                return {
                    "action": "no_action",
                    "reason": f"Minimum interval enforced ({time_since:.1f}hr < 2hr)",
                }
            return {
                "action":    "irrigate",
                "reason":    reason,
                "sdt_state": sdt,
                "pdt_state": pdt,
            }

        # --- Layer 2: RL tactical rate optimisation ---
        # When the overseer is satisfied (no_action) but irrigation is already
        # active, hand rate control to the RL policy if one is loaded.
        # This separates strategic gating (overseer) from continuous optimisation (RL).
        if (action == "no_action"
                and self.policy
                and risk == "low"
                and observation.get("bpdt_state") == "IRRIGATION_ACTIVE"):
            return {
                "action": "rl_control",
                "reason": "RL rate optimisation during active low-risk irrigation",
            }

        # --- Layer 3: pass overseer decision through unchanged ---
        return {
            "action":    action,
            "reason":    reason,
            "sdt_state": sdt,
            "pdt_state": pdt,
        }

    # ------------------------------------------------------------------
    # A — Act: execute + full audit trail
    # ------------------------------------------------------------------

    async def act(self, plan: dict) -> None:
        action = plan.get("action")

        # Persist full overseer reasoning for human audit
        if self._last_overseer_decision is not None:
            d = self._last_overseer_decision
            overseer_payload = {
                "action":          d.action,
                "risk_level":      d.risk_level,
                "reasoning":       d.reasoning,
                "goals_addressed": d.goals_addressed,
                "agent_queries":   d.agent_queries,
                "executed_action": action,
                "ts_s":            int(time.time()),
            }
            self.doc.log_event("ooda_overseer_decision", overseer_payload, severity="info")
            # Also cache a compact summary in Redis for fast dashboard reads
            try:
                self.cache.set_latest("ooda_overseer_latest", {
                    "action":          d.action,
                    "risk_level":      d.risk_level,
                    "reasoning":       d.reasoning[:400],
                    "goals_addressed": d.goals_addressed,
                    "agent_queries_count": len(d.agent_queries),
                    "overseer_driven": True,
                    "ts_s":            int(time.time()),
                })
            except Exception:
                pass

        if action == "irrigate":
            self._bpdt_reactive.signal_ooda_decision("irrigate")
            self._bpdt_reactive.confirm_valve_open(True)
            self._last_irrigation_time = time.time()

            await self.bus.publish(DomainEvent(
                event_type="bpdt.irrigation_started",
                source_layer="autonomous",
                source_asset=self.config.asset_id,
                payload={
                    "reason":    plan.get("reason"),
                    "sdt_state": plan.get("sdt_state"),
                    "pdt_state": plan.get("pdt_state"),
                    "overseer_driven": self._last_overseer_decision is not None,
                },
                severity="info",
            ))
            self.doc.log_event("irrigation_started", plan, severity="info")
            self.log.info("IRRIGATION STARTED: %s", plan.get("reason"))

        elif action == "emergency_halt":
            self._bpdt_reactive.signal_ooda_decision("no_action")
            self._bpdt_reactive.confirm_valve_open(False)

            await self.bus.publish(DomainEvent(
                event_type="bpdt.emergency",
                source_layer="autonomous",
                source_asset=self.config.asset_id,
                payload=plan, severity="critical",
            ))
            self.doc.log_event("emergency_halt", plan, severity="critical")
            self.log.critical("EMERGENCY HALT: %s", plan.get("reason"))

            if self.notifier:
                await self.notifier.send(
                    f"BPDT Emergency: {plan.get('reason')}", plan
                )

        elif action == "advisory":
            self.doc.log_event("bpdt_advisory", plan, severity="info")
            self.log.info("ADVISORY: %s", plan.get("reason"))

        elif action == "maintenance":
            self._bpdt_reactive.enter_maintenance()
            self.doc.log_event("maintenance_mode_entered", plan, severity="info")
            self.log.info("MAINTENANCE MODE: %s", plan.get("reason"))

        elif action == "rl_control":
            # RL policy optimises irrigation rate during active low-risk irrigation.
            # The overseer decides the strategic gate (start/stop); RL fine-tunes the rate.
            if self.policy:
                await self.policy.step_once()

        else:
            # no_action — check if we should stop an active irrigation
            current_state = self._bpdt_reactive.get_state()
            if current_state == "IRRIGATION_ACTIVE":
                vwc = self.ts.get_latest("vwc_10cm")
                if vwc is not None and vwc >= _PID_SP:
                    self._bpdt_reactive.signal_ooda_decision("no_action")
                    self._bpdt_reactive.confirm_valve_open(False)
                    self.log.info("Irrigation stopped: VWC target reached (%.4f)", vwc)

    # ------------------------------------------------------------------
    # Hot-swap protocol
    # ------------------------------------------------------------------

    async def hot_swap_twin(
        self,
        twin_role: str,
        new_twin,
        drain_seconds: int = 5,
    ) -> None:
        """
        8-step hot-swap: validate → drain → snapshot → deregister →
        register → BoundaryConditions → warm-up → resume.
        """
        self.log.info("Hot-swap requested for role '%s'", twin_role)

        # 1. Validate
        if twin_role not in ("sdt", "pdt"):
            raise ValueError(f"Unknown twin role: {twin_role}")

        # 2. Drain — pause actuation
        self._bpdt_reactive.signal_ooda_decision("no_action")
        self._bpdt_reactive.confirm_valve_open(False)
        await asyncio.sleep(drain_seconds)

        # 3. Snapshot current state
        snapshot = {
            "role":        twin_role,
            "sdt_state":   self._sdt.get_fsm_state() if self._sdt else None,
            "pdt_state":   self._pdt.get_fsm_state() if self._pdt else None,
            "vwc_10cm":    self.ts.get_latest("vwc_10cm"),
        }
        self.doc.log_event("hot_swap_snapshot", snapshot, severity="info")

        # 4. Deregister old twin
        old_twin = self._sdt if twin_role == "sdt" else self._pdt

        # 5. Register new twin
        if twin_role == "sdt":
            self._sdt = new_twin
        else:
            self._pdt = new_twin

        # 6. BoundaryConditions — re-wire soil inputs to PDT
        await self._propagate_boundary_conditions()

        # 7. Warm-up — brief wait for new twin to stabilise
        await asyncio.sleep(10)

        # 8. Resume
        self.log.info("Hot-swap complete: '%s' replaced", twin_role)
        self.doc.log_event("hot_swap_complete", {
            "role": twin_role, "old_id": getattr(old_twin, "config", {}).asset_id if old_twin else None,
        }, severity="info")
