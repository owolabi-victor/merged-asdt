"""BPDT Intelligent Layer — 5 LLM agents for system-level soil-plant oversight.

Agents (by priority):
    1. SystemIntegrationAgent    (10) — cross-twin state consistency and coupled stress
    2. IrrigationDecisionAgent   (9)  — FAO-56 irrigation need and optimal depth
    3. AnomalyDetectionAgent     (8)  — cross-domain statistical anomaly synthesis
    4. WaterEfficiencyAgent      (6)  — irrigation WUE and water productivity
    5. LedgerAgent               (5)  — water use accounting and session reporting

Each agent extends DiagnosticAgent. observe() reads from both SDT and PDT data
stores and computes coupled physics indicators; reason() uses the LLM to
synthesise cross-domain evidence into system-level management recommendations.

References:
    English & Raja (1996) — deficit irrigation optimisation
    Zwart & Bastiaanssen (2004) — water productivity benchmarks
    FAO-56 Allen et al. (1998) — irrigation scheduling
    Hsiao et al. (2009) — AquaCrop coupled soil-plant model
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.tools import tool as lc_tool

from dt_forge.intelligent.agent import DiagnosticAgent
from dt_forge.intelligent.knowledge_graph import (
    KnowledgeGraph, KnowledgeGraphSpec, FailureMode, SymptomMapping,
)

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.data.storage.base import TimeSeriesStore, DocumentStore, CacheStore
    from dt_forge.services.ditto.client import DittoClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain knowledge graph spec
# ---------------------------------------------------------------------------

bpdt_kg_spec = KnowledgeGraphSpec(
    components=[
        "sdt_twin", "pdt_twin", "irrigation_controller",
        "weather_station", "crop_model",
    ],
    failure_modes=[
        FailureMode(
            name="state_inconsistency",
            severity="high",
            maintenance_actions=["inspect_sensor_coupling", "verify_root_soil_contact", "cross_validate_data"],
            affected_components=["sdt_twin", "pdt_twin"],
        ),
        FailureMode(
            name="irrigation_failure",
            severity="critical",
            maintenance_actions=["manual_irrigation", "check_valve", "inspect_dripper_lines"],
            affected_components=["irrigation_controller"],
        ),
        FailureMode(
            name="sensor_decoupling",
            severity="high",
            maintenance_actions=["recalibrate_sensors", "inspect_installation", "cross_check_with_manual_reading"],
            affected_components=["sdt_twin"],
        ),
        FailureMode(
            name="low_water_productivity",
            severity="medium",
            maintenance_actions=["optimise_irrigation_schedule", "review_soil_amendment", "check_drainage"],
            affected_components=["irrigation_controller", "crop_model"],
        ),
        FailureMode(
            name="coupled_water_stress",
            severity="critical",
            maintenance_actions=["immediate_irrigation", "alert_agronomist", "shade_canopy"],
            affected_components=["sdt_twin", "pdt_twin", "irrigation_controller"],
        ),
    ],
    symptom_mappings=[
        SymptomMapping("high_coupled_cwsi",       "cwsi",                   0.65, ["coupled_water_stress"],    "high"),
        SymptomMapping("irrigation_deficit",      "depletion_rate_avg",     0.001, ["irrigation_failure"],     "high"),
        SymptomMapping("poor_water_productivity", "yield_penalty_pct",      10.0,  ["low_water_productivity"], "high"),
        SymptomMapping("cross_sensor_fault",      "sensor_divergence_flag", 0.04,  ["sensor_decoupling"],      "high"),
    ],
)


# ---------------------------------------------------------------------------
# Shared base — adds cache access for sub-twin FSM state retrieval
# ---------------------------------------------------------------------------

class _BpdtLLMAgentBase(DiagnosticAgent):
    """BPDT LLM agent base — stores cache BEFORE super().__init__() so that
    _build_extra_tools() can use it via closures."""

    def __init__(self, config, *, llm, ditto_client, ts_store, doc_store,
                 knowledge_graph, cache=None):
        self._cache = cache  # must be set before super().__init__ calls _build_extra_tools()
        super().__init__(config, llm=llm, ditto_client=ditto_client,
                         ts_store=ts_store, doc_store=doc_store,
                         knowledge_graph=knowledge_graph)

    def _cache_get(self, key: str):
        try:
            return self._cache.get_latest_cached(key) if self._cache else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Agent 1: SystemIntegrationAgent (priority 10)
# ---------------------------------------------------------------------------

class SystemIntegrationAgent(_BpdtLLMAgentBase):
    """Cross-twin consistency agent.

    The LLM analyses disagreements between SDT (soil) and PDT (plant) FSM
    states, interprets coupled CWSI, and recommends diagnostic follow-up
    when state divergence exceeds physical explanation.
    """

    agent_name = "system_integration"
    domain     = "system_oversight"
    priority   = 10

    def _build_extra_tools(self) -> list:
        ts    = self._ts_store
        cache = self._cache

        @lc_tool
        def get_system_state() -> str:
            """Return both sub-twin FSM states, coupled CWSI, and cross-domain indicators."""
            sdt_state = (cache.get_latest_cached("sdt_fsm_state") if cache else None) or "UNKNOWN"
            pdt_state = (cache.get_latest_cached("pdt_fsm_state") if cache else None) or "UNKNOWN"
            vwc_rz    = ts.get_latest("vwc_10cm")
            psi_leaf  = ts.get_latest("leaf_water_potential_mpa")
            cwsi      = ts.get_latest("cwsi")
            sap       = ts.get_latest("sap_flow_L_hr")
            inconsistent = (
                sdt_state in ("OPTIMAL", "DEPLETING") and
                pdt_state in ("SEVERE_STRESS", "WILTING")
            )
            return (
                f"SDT FSM state={sdt_state} | PDT FSM state={pdt_state} | "
                f"State inconsistency={inconsistent} | "
                f"VWC root-zone={vwc_rz} m³/m³ | ψ_leaf={psi_leaf} MPa | "
                f"CWSI={cwsi} | Sap flow={sap} L/hr/plant"
            )

        @lc_tool
        def get_coupled_stress_model() -> str:
            """Return coupled CWSI and soil-plant conductance estimate."""
            vwc_rz   = ts.get_latest("vwc_10cm") or 0.06
            psi_leaf = ts.get_latest("leaf_water_potential_mpa") or -0.3
            cwsi     = ts.get_latest("cwsi") or 0.0
            soil_stress  = max(0.0, (0.10 - vwc_rz) / (0.10 - 0.05))
            plant_stress = min(1.0, abs(psi_leaf) / 2.5)
            coupled = (cwsi + soil_stress + plant_stress) / 3.0
            return (
                f"Soil stress index={soil_stress:.3f} | "
                f"Plant stress index={plant_stress:.3f} | "
                f"Coupled CWSI={coupled:.3f} | "
                f"Interpretation: <0.30=low, 0.30–0.60=moderate, >0.60=high"
            )

        return [get_system_state, get_coupled_stress_model]

    async def observe(self) -> dict:
        try:
            vwc_rz   = self._ts_store.get_latest("vwc_10cm") or 0.06
            psi_leaf = self._ts_store.get_latest("leaf_water_potential_mpa") or -0.3
            cwsi     = self._ts_store.get_latest("cwsi") or 0.0
        except Exception:
            vwc_rz = 0.06; psi_leaf = -0.3; cwsi = 0.0

        sdt_state = self._cache_get("sdt_fsm_state") or "OPTIMAL"
        pdt_state = self._cache_get("pdt_fsm_state") or "UNSTRESSED"

        soil_stress  = max(0.0, (0.10 - vwc_rz) / (0.10 - 0.05))
        plant_stress = min(1.0, abs(psi_leaf) / 2.5)
        cwsi_coupled = (cwsi + soil_stress + plant_stress) / 3.0

        inconsistency = (
            sdt_state in ("OPTIMAL", "DEPLETING") and
            pdt_state in ("SEVERE_STRESS", "WILTING")
        )
        anomaly_detected = inconsistency or cwsi_coupled > 0.65

        return {
            "cwsi_coupled": round(cwsi_coupled, 3),
            "sdt_state": sdt_state, "pdt_state": pdt_state,
            "cwsi_plant": cwsi,
            "vwc_root_zone": vwc_rz,
            "psi_leaf": psi_leaf,
            "state_inconsistency": inconsistency,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        cwsi_c = observations.get("cwsi_coupled", 0.0)
        incons = observations.get("state_inconsistency")
        sdt    = observations.get("sdt_state")
        pdt    = observations.get("pdt_state")
        vwc    = observations.get("vwc_root_zone")
        psi    = observations.get("psi_leaf")

        if incons:
            severity, action = "warning", "publish_advisory"
        elif cwsi_c > 0.70:
            severity, action = "warning", "publish_alert"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a precision irrigation systems analyst overseeing a coupled "
            f"soil-plant digital twin for a maize field.\n\n"
            f"System state summary:\n"
            f"- Soil Digital Twin FSM state: {sdt}\n"
            f"- Plant Digital Twin FSM state: {pdt}\n"
            f"- State inconsistency (soil OK but plant severely stressed): {incons}\n"
            f"- Root-zone VWC: {vwc} m³/m³\n"
            f"- Leaf water potential ψ: {psi} MPa\n"
            f"- Coupled CWSI (combined soil-plant stress index): {cwsi_c:.3f} "
            f"(<0.30=low, 0.30–0.60=moderate, >0.60=high)\n\n"
            f"Analyse the coupled soil-plant system state in 2–3 sentences. "
            f"If state inconsistency exists, explain the most likely physical cause "
            f"(sensor error, soil-root hydraulic limitation, delayed plant response). "
            f"Recommend a diagnostic or management action."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "cwsi_coupled": cwsi_c, "state_inconsistency": incons,
        }


# ---------------------------------------------------------------------------
# Agent 2: IrrigationDecisionAgent (priority 9)
# ---------------------------------------------------------------------------

class IrrigationDecisionAgent(_BpdtLLMAgentBase):
    """FAO-56 irrigation scheduling and efficacy agent.

    The LLM weighs soil depletion against RAW, current irrigation rate,
    and efficiency to recommend when and how much to irrigate.
    """

    agent_name = "irrigation_decision"
    domain     = "irrigation_management"
    priority   = 9

    _FC = 0.100; _PWP = 0.050; _ROOT_DEPTH_M = 0.50; _P = 0.55

    def _build_extra_tools(self) -> list:
        ts    = self._ts_store
        cache = self._cache
        fc = self._FC; pwp = self._PWP; rd = self._ROOT_DEPTH_M; p = self._P

        @lc_tool
        def get_irrigation_schedule_data() -> str:
            """Return FAO-56 depletion, RAW, recommended depth, and current irrigation rate."""
            vwc_10 = ts.get_latest("vwc_10cm") or 0.06
            vwc_30 = ts.get_latest("vwc_30cm") or 0.06
            vwc_avg = (vwc_10 + vwc_30) / 2.0
            taw = (fc - pwp) * rd * 1000
            raw = p * taw
            depl = max(0.0, (fc - vwc_avg) * rd * 1000)
            irrigate = depl >= raw
            depth = round(depl * 1.10, 1) if irrigate else 0.0
            irr_now = cache.get_latest_cached("irrigation_rate_L_hr") if cache else 0.0
            return (
                f"VWC avg (10+30cm)={vwc_avg:.4f} | TAW={taw:.1f} mm | "
                f"RAW={raw:.1f} mm (p={p}) | Depletion={depl:.1f} mm | "
                f"Irrigate now={irrigate} | Recommended depth={depth} mm | "
                f"Current rate={irr_now} L/hr"
            )

        return [get_irrigation_schedule_data]

    async def observe(self) -> dict:
        try:
            vwc_10 = self._ts_store.get_latest("vwc_10cm") or 0.06
            vwc_30 = self._ts_store.get_latest("vwc_30cm") or 0.06
        except Exception:
            vwc_10 = vwc_30 = 0.06

        irr_now = self._cache_get("irrigation_rate_L_hr") or 0.0

        vwc_avg = (vwc_10 + vwc_30) / 2.0
        taw  = (self._FC - self._PWP) * self._ROOT_DEPTH_M * 1000
        raw  = self._P * taw
        depl = max(0.0, (self._FC - vwc_avg) * self._ROOT_DEPTH_M * 1000)
        irrigate = depl >= raw
        depth    = round(depl * 1.10, 1) if irrigate else 0.0
        eff      = max(0.0, 1.0 - max(0.0, depl / taw - 0.5))

        return {
            "vwc_10": vwc_10, "vwc_30": vwc_30,
            "taw_mm": taw, "raw_mm": raw,
            "current_depletion_mm": depl,
            "irrigate_now": irrigate,
            "recommended_depth_mm": depth,
            "efficacy_fraction": eff,
            "current_irrigation_rate_L_hr": irr_now,
            "anomaly_detected": irrigate,
        }

    async def reason(self, observations: dict) -> dict:
        irrigate = observations.get("irrigate_now")
        depth    = observations.get("recommended_depth_mm", 0.0)
        depl     = observations.get("current_depletion_mm", 0.0)
        raw      = observations.get("raw_mm", 0.0)
        eff      = observations.get("efficacy_fraction", 1.0)
        irr_rate = observations.get("current_irrigation_rate_L_hr", 0.0)

        if irrigate and depl > raw:
            severity, action = "warning", "request_irrigation"
        elif irrigate:
            severity, action = "info", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are an irrigation engineer applying FAO-56 soil water balance "
            f"for maize (V6-V8, root depth=0.50 m, p=0.55, Kc=1.10).\n\n"
            f"Soil water balance:\n"
            f"- VWC at 10 cm: {observations.get('vwc_10')} m³/m³\n"
            f"- VWC at 30 cm: {observations.get('vwc_30')} m³/m³\n"
            f"- TAW: {observations.get('taw_mm'):.1f} mm | RAW: {raw:.1f} mm\n"
            f"- Current depletion: {depl:.1f} mm\n"
            f"- Irrigation trigger exceeded: {irrigate}\n"
            f"- Recommended depth: {depth} mm (10% refill overshoot)\n"
            f"- Current PID rate: {irr_rate} L/hr | Efficacy: {eff:.0%}\n\n"
            f"Provide an irrigation scheduling recommendation in 2–3 sentences. "
            f"State whether to irrigate, how much, and the agronomic rationale."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "recommended_depth_mm": depth, "depletion_mm": depl,
        }


# ---------------------------------------------------------------------------
# Agent 3: AnomalyDetectionAgent (priority 8)
# ---------------------------------------------------------------------------

class AnomalyDetectionAgent(_BpdtLLMAgentBase):
    """Cross-domain anomaly synthesis agent.

    The LLM distinguishes genuine agronomic crises from sensor artifacts
    by reasoning about patterns that span the soil and plant domains.
    """

    agent_name = "anomaly_detection"
    domain     = "system_diagnostics"
    priority   = 8

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_anomaly_indicators() -> str:
            """Return cross-domain anomaly signals from soil and plant domains."""
            vwc_10    = ts.get_latest("vwc_10cm") or 0.06
            cwsi      = ts.get_latest("cwsi") or 0.0
            div_flg   = ts.get_latest("sensor_divergence_flag") or 0.0
            depl_rate = ts.get_latest("depletion_rate_avg") or 0.0
            rain      = ts.get_latest("rain_mm") or 0.0
            spd  = vwc_10 < 0.040 and cwsi < 0.15
            rapid = depl_rate > 0.003 and rain < 0.1
            fault = div_flg > 0.04
            return (
                f"VWC_10={vwc_10}, CWSI={cwsi}, depl_rate={depl_rate}, "
                f"rain={rain} mm, sensor_div={div_flg} | "
                f"Soil-plant decoupling={spd} | Rapid depletion w/o rain={rapid} | "
                f"Sensor fault (div>0.04)={fault}"
            )

        return [get_anomaly_indicators]

    async def observe(self) -> dict:
        try:
            vwc_10    = self._ts_store.get_latest("vwc_10cm") or 0.06
            cwsi      = self._ts_store.get_latest("cwsi") or 0.0
            div_flg   = self._ts_store.get_latest("sensor_divergence_flag") or 0.0
            depl_rate = self._ts_store.get_latest("depletion_rate_avg") or 0.0
            rain      = self._ts_store.get_latest("rain_mm") or 0.0
        except Exception:
            vwc_10 = 0.06; cwsi = div_flg = depl_rate = rain = 0.0

        soil_plant_decoupling = vwc_10 < 0.040 and cwsi < 0.15
        rapid_depl   = depl_rate > 0.003 and rain < 0.1
        sensor_fault = div_flg > 0.04

        return {
            "vwc_10": vwc_10, "cwsi": cwsi, "depletion_rate": depl_rate,
            "sensor_divergence": div_flg, "rain_mm": rain,
            "soil_plant_decoupling": soil_plant_decoupling,
            "rapid_depletion": rapid_depl,
            "sensor_fault": sensor_fault,
            "anomaly_detected": soil_plant_decoupling or rapid_depl or sensor_fault,
        }

    async def reason(self, observations: dict) -> dict:
        sf   = observations.get("sensor_fault")
        spd  = observations.get("soil_plant_decoupling")
        rd   = observations.get("rapid_depletion")
        div  = observations.get("sensor_divergence")
        depl = observations.get("depletion_rate")
        rain = observations.get("rain_mm", 0)

        if sf:
            severity, action = "critical", "trigger_maintenance"
        elif spd or rd:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        anomalies = []
        if sf:  anomalies.append(f"sensor divergence={div:.4f} (>0.04)")
        if spd: anomalies.append("soil dry (VWC<0.040) but CWSI<0.15 — soil-plant decoupling")
        if rd:  anomalies.append(f"rapid depletion={depl:.4f} m³/m³/hr without rain ({rain} mm)")

        prompt = (
            f"You are a precision agriculture data analyst detecting cross-domain "
            f"anomalies in a coupled soil-plant digital twin for maize.\n\n"
            f"Cross-domain signals:\n"
            f"- VWC at 10 cm: {observations.get('vwc_10')} m³/m³\n"
            f"- CWSI (plant stress): {observations.get('cwsi')}\n"
            f"- Soil depletion rate: {depl} m³/m³/hr\n"
            f"- Rainfall: {rain} mm\n"
            f"- Inter-sensor divergence: {div}\n"
            f"- Detected anomalies: {anomalies if anomalies else 'none'}\n\n"
            f"Analyse the anomaly pattern in 2–3 sentences. For each anomaly, "
            f"distinguish genuine agronomic crisis from instrument artifact, "
            f"and recommend a specific diagnostic or corrective action."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
        }


# ---------------------------------------------------------------------------
# Agent 4: WaterEfficiencyAgent (priority 6)
# ---------------------------------------------------------------------------

class WaterEfficiencyAgent(_BpdtLLMAgentBase):
    """Irrigation water productivity benchmarking agent.

    The LLM compares estimated WP against Zwart & Bastiaanssen (2004)
    maize standards and recommends scheduling improvements.
    """

    agent_name = "water_efficiency"
    domain     = "resource_management"
    priority   = 6

    def _build_extra_tools(self) -> list:
        ts    = self._ts_store
        cache = self._cache

        @lc_tool
        def get_water_productivity() -> str:
            """Return WUE, ETa/ETc, daily balance, and yield penalty."""
            eta_frac = ts.get_latest("eta_fraction") or 1.0
            et_rate  = ts.get_latest("et_loss_rate") or 2.0
            rain_mm  = ts.get_latest("rain_mm") or 0.0
            penalty  = ts.get_latest("yield_penalty_pct") or 0.0
            irr_rate = (cache.get_latest_cached("irrigation_rate_L_hr") if cache else 0.0) or 0.0
            irr_mm   = irr_rate * 24.0
            ya_frac  = max(0.0, 1.0 - 1.25 * (1.0 - eta_frac))
            wp       = round((12.0 * ya_frac * 1000) / max(1.0, et_rate * 30), 3)
            balance  = round(rain_mm + irr_mm - et_rate, 2)
            return (
                f"ETa/ETc={eta_frac} | ET={et_rate} mm/day | Rain={rain_mm} mm | "
                f"Irrigation={irr_mm:.1f} mm/day | Balance={balance} mm | "
                f"WP≈{wp} kg/m³ (benchmark 1.0–1.8) | Ya/Ymax={ya_frac:.3f} | "
                f"Yield penalty={penalty:.1f}%"
            )

        return [get_water_productivity]

    async def observe(self) -> dict:
        try:
            eta_frac = self._ts_store.get_latest("eta_fraction") or 1.0
            et_rate  = self._ts_store.get_latest("et_loss_rate") or 2.0
            rain_mm  = self._ts_store.get_latest("rain_mm") or 0.0
        except Exception:
            eta_frac = 1.0; et_rate = 2.0; rain_mm = 0.0

        irr_rate   = self._cache_get("irrigation_rate_L_hr") or 0.0
        irr_mm_day = irr_rate * 24.0

        ya_frac  = max(0.0, 1.0 - 1.25 * (1.0 - eta_frac))
        wp       = (12.0 * ya_frac * 1000) / max(1.0, et_rate * 30)
        balance  = round(rain_mm + irr_mm_day - et_rate, 2)

        return {
            "eta_fraction": eta_frac,
            "et_loss_rate": et_rate,
            "rain_mm": rain_mm,
            "irrigation_rate_L_hr": irr_rate,
            "daily_balance_mm": balance,
            "water_productivity_kg_m3": round(wp, 3),
            "ya_fraction": round(ya_frac, 3),
            "anomaly_detected": wp < 0.8,
        }

    async def reason(self, observations: dict) -> dict:
        wp  = observations.get("water_productivity_kg_m3", 0.0)
        eta = observations.get("eta_fraction", 1.0)
        bal = observations.get("daily_balance_mm", 0.0)
        ya  = observations.get("ya_fraction", 1.0)

        severity = "warning" if wp < 0.8 else "info"
        action   = "publish_advisory" if wp < 0.8 else "no_action"

        prompt = (
            f"You are an irrigation efficiency specialist benchmarking water "
            f"productivity for irrigated maize (Zea mays, V6-V8).\n\n"
            f"Water use data:\n"
            f"- ETa/ETc: {eta}\n"
            f"- ET demand: {observations.get('et_loss_rate')} mm/day\n"
            f"- Rainfall: {observations.get('rain_mm')} mm\n"
            f"- Applied irrigation: {observations.get('irrigation_rate_L_hr', 0) * 24:.1f} mm/day\n"
            f"- Daily balance: {bal:.2f} mm\n"
            f"- Estimated WP: {wp:.3f} kg/m³ "
            f"(benchmark 1.0–1.8 kg/m³, Zwart & Bastiaanssen 2004)\n"
            f"- Ya/Ymax: {ya:.3f}\n\n"
            f"Assess water use efficiency in 2–3 sentences. If WP is below "
            f"benchmark, identify the limiting factor and recommend an improvement."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "water_productivity_kg_m3": wp,
        }


# ---------------------------------------------------------------------------
# Agent 5: LedgerAgent (priority 5)
# ---------------------------------------------------------------------------

class LedgerAgent(_BpdtLLMAgentBase):
    """Water use ledger — accumulates session totals and generates LLM reports."""

    agent_name = "ledger"
    domain     = "reporting"
    priority   = 5

    def __init__(self, *args, **kwargs):
        self._total_irrigation_L: float = 0.0
        self._session_count: int = 0
        super().__init__(*args, **kwargs)

    def _build_extra_tools(self) -> list:
        ts    = self._ts_store
        cache = self._cache

        @lc_tool
        def get_water_ledger() -> str:
            """Return current session water use totals and demand vs supply."""
            irr_rate = (cache.get_latest_cached("irrigation_rate_L_hr") if cache else 0.0) or 0.0
            et_rate  = ts.get_latest("et_loss_rate") or 0.0
            rain_mm  = ts.get_latest("rain_mm") or 0.0
            return (
                f"Current rate={irr_rate} L/hr | ET={et_rate} mm/day | Rain={rain_mm} mm | "
                f"Session total≈{self._total_irrigation_L:.1f} L | "
                f"Irrigation events={self._session_count}"
            )

        return [get_water_ledger]

    async def observe(self) -> dict:
        irr_rate = self._cache_get("irrigation_rate_L_hr") or 0.0
        self._total_irrigation_L += irr_rate * (30.0 / 3600.0)

        try:
            et_rate = self._ts_store.get_latest("et_loss_rate") or 0.0
            rain_mm = self._ts_store.get_latest("rain_mm") or 0.0
        except Exception:
            et_rate = rain_mm = 0.0

        if irr_rate > 0:
            self._session_count += 1

        return {
            "current_irrigation_rate_L_hr": irr_rate,
            "total_irrigation_L_session": round(self._total_irrigation_L, 2),
            "session_count": self._session_count,
            "et_rate": et_rate,
            "rain_mm": rain_mm,
            "anomaly_detected": False,
        }

    async def reason(self, observations: dict) -> dict:
        total = observations.get("total_irrigation_L_session", 0.0)
        rate  = observations.get("current_irrigation_rate_L_hr", 0.0)
        et    = observations.get("et_rate", 0.0)
        rain  = observations.get("rain_mm", 0.0)

        prompt = (
            f"You are a water resources accountant summarising irrigation use "
            f"for a precision-irrigated maize field.\n\n"
            f"Session ledger:\n"
            f"- Total applied this session: {total:.1f} L\n"
            f"- Current rate: {rate:.1f} L/hr\n"
            f"- ET demand: {et:.2f} mm/day\n"
            f"- Rainfall: {rain:.1f} mm\n\n"
            f"Give a 1–2 sentence water accounting summary. Comment on whether "
            f"irrigation is tracking ET demand and note any over- or under-application concern."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": "no_action", "severity": "info",
            "total_irrigation_L": total,
        }
