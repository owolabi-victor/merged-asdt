"""PDT Intelligent Layer — 5 LLM agents for plant drought stress analysis.

Agents (by priority):
    1. StressClassificationAgent  (10) — multi-indicator composite stress scoring
    2. WiltingPredictionAgent     (9)  — time-to-wilting from P-V trajectory
    3. TranspirationAgent         (8)  — water-use hydraulic balance analysis
    4. PhotosynthesisAgent        (6)  — stomatal closure and C4 photosynthesis
    5. YieldPenaltyAgent          (5)  — cumulative AquaCrop yield impact

Each agent extends DiagnosticAgent. observe() computes biophysical indicators
from sensor data; reason() submits them to the LLM for plant physiological
analysis and natural-language recommendations.

References:
    Lecoeur & Sinclair (1996) — maize stress physiology
    Hsiao et al. (2009) — AquaCrop yield model
    Jackson et al. (1981) — CWSI derivation
    Flexas & Medrano (2002) — stomatal limitation of photosynthesis
    Westgate & Boyer (1985) — leaf water potential and wilting
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

_GS_NOMINAL = 260.0   # mmol/m²/s at full turgor (maize V6-V8)
_PSI_TLP    = -1.20   # turgor loss point [MPa]
_PSI_WILT   = -2.50   # wilting threshold [MPa]

# ---------------------------------------------------------------------------
# Domain knowledge graph spec
# ---------------------------------------------------------------------------

pdt_kg_spec = KnowledgeGraphSpec(
    components=[
        "cwsi_sensor", "infrared_thermometer", "sap_flow_meter",
        "porometer", "pressure_bomb", "canopy_thermometer",
    ],
    failure_modes=[
        FailureMode(
            name="mild_drought_stress",
            severity="medium",
            maintenance_actions=["irrigate_within_24hr", "monitor_cwsi"],
            affected_components=["cwsi_sensor", "sap_flow_meter"],
        ),
        FailureMode(
            name="moderate_drought_stress",
            severity="high",
            maintenance_actions=["irrigate_urgently", "reduce_canopy_temperature"],
            affected_components=["cwsi_sensor", "porometer", "sap_flow_meter"],
        ),
        FailureMode(
            name="severe_drought_stress",
            severity="critical",
            maintenance_actions=["irrigate_immediately", "alert_agronomist"],
            affected_components=["cwsi_sensor", "pressure_bomb", "sap_flow_meter"],
        ),
        FailureMode(
            name="wilting",
            severity="critical",
            maintenance_actions=["emergency_irrigation", "shade_canopy"],
            affected_components=["pressure_bomb", "sap_flow_meter"],
        ),
        FailureMode(
            name="hydraulic_failure",
            severity="high",
            maintenance_actions=["check_root_zone", "irrigate", "inspect_xylem"],
            affected_components=["sap_flow_meter", "porometer"],
        ),
        FailureMode(
            name="yield_loss",
            severity="high",
            maintenance_actions=["irrigation_and_fertilizer_review", "adjust_planting_density"],
            affected_components=["cwsi_sensor"],
        ),
    ],
    symptom_mappings=[
        SymptomMapping("elevated_cwsi",       "cwsi",                       0.45,  ["moderate_drought_stress"],  "high"),
        SymptomMapping("severe_cwsi",         "cwsi",                       0.70,  ["severe_drought_stress"],    "high"),
        SymptomMapping("low_water_potential", "leaf_water_potential_mpa",   -1.20, ["mild_drought_stress"],      "low"),
        SymptomMapping("critical_psi",        "leaf_water_potential_mpa",   -2.00, ["severe_drought_stress", "wilting"], "low"),
        SymptomMapping("significant_yield_loss", "yield_penalty_pct",       10.0,  ["yield_loss"],              "high"),
    ],
)


# ---------------------------------------------------------------------------
# Agent 1: StressClassificationAgent (priority 10)
# ---------------------------------------------------------------------------

class StressClassificationAgent(DiagnosticAgent):
    """Multi-indicator plant stress classifier.

    The LLM synthesises CWSI, ψ_leaf, gs, RWC, and sap flow into a holistic
    physiological stress narrative with irrigation urgency assessment.
    """

    agent_name = "stress_classification"
    domain     = "plant_physiology"
    priority   = 10

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_plant_stress_indicators() -> str:
            """Return all primary plant water stress indicators."""
            cwsi  = ts.get_latest("cwsi")
            psi   = ts.get_latest("leaf_water_potential_mpa")
            gs    = ts.get_latest("stomatal_conductance_mmol")
            rwc   = ts.get_latest("relative_water_content")
            sap   = ts.get_latest("sap_flow_L_hr")
            days  = ts.get_latest("stress_duration_days")
            scores = [v for v in [
                cwsi,
                min(1.0, abs(psi) / 2.5) if psi else None,
                (1.0 - min(1.0, gs / _GS_NOMINAL)) if gs else None,
                (1.0 - min(1.0, rwc)) if rwc else None,
            ] if v is not None]
            composite = sum(scores) / len(scores) if scores else 0.0
            return (
                f"CWSI={cwsi} (mild≥0.20, moderate≥0.45, severe≥0.70) | "
                f"ψ_leaf={psi} MPa (TLP={_PSI_TLP}, wilting={_PSI_WILT}) | "
                f"gs={gs} mmol/m²/s (nominal={_GS_NOMINAL}) | "
                f"RWC={rwc} | sap_flow={sap} L/hr/plant | "
                f"stress_duration={days} days | composite_score={composite:.3f}"
            )

        return [get_plant_stress_indicators]

    async def observe(self) -> dict:
        try:
            cwsi    = self._ts_store.get_latest("cwsi")
            psi     = self._ts_store.get_latest("leaf_water_potential_mpa")
            gs      = self._ts_store.get_latest("stomatal_conductance_mmol")
            rwc     = self._ts_store.get_latest("relative_water_content")
            sap     = self._ts_store.get_latest("sap_flow_L_hr")
            stress_days = self._ts_store.get_latest("stress_duration_days")
        except Exception:
            cwsi = psi = gs = rwc = sap = stress_days = None

        scores = [v for v in [
            cwsi,
            min(1.0, abs(psi) / abs(_PSI_WILT)) if psi else None,
            (1.0 - min(1.0, gs / _GS_NOMINAL)) if gs else None,
            (1.0 - min(1.0, rwc)) if rwc else None,
        ] if v is not None]
        composite = sum(scores) / len(scores) if scores else 0.0

        anomaly_detected = composite > 0.35 or (psi is not None and psi < _PSI_TLP)

        return {
            "cwsi": cwsi, "psi_mpa": psi, "gs_mmol": gs,
            "rwc": rwc, "sap_L_hr": sap, "stress_days": stress_days,
            "composite_stress_score": round(composite, 4),
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        score = observations.get("composite_stress_score", 0.0)
        psi   = observations.get("psi_mpa")
        cwsi  = observations.get("cwsi")
        days  = observations.get("stress_days", 0.0)

        if psi is not None and psi < _PSI_WILT:
            severity, action = "critical", "publish_critical_alert"
        elif score > 0.65:
            severity, action = "critical", "publish_alert"
        elif score > 0.35:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a plant physiologist monitoring a maize crop (Zea mays, V6-V8 stage, "
            f"C4 photosynthesis pathway) under field conditions.\n\n"
            f"Current plant water status:\n"
            f"- CWSI: {cwsi} (mild stress: 0.20–0.44, moderate: 0.45–0.69, severe: ≥0.70)\n"
            f"- Leaf water potential ψ: {psi} MPa "
            f"(turgor loss point: {_PSI_TLP} MPa, wilting: {_PSI_WILT} MPa)\n"
            f"- Stomatal conductance gs: {observations.get('gs_mmol')} mmol/m²/s "
            f"(nominal fully-hydrated: {_GS_NOMINAL})\n"
            f"- Relative water content: {observations.get('rwc')}\n"
            f"- Sap flow: {observations.get('sap_L_hr')} L/hr/plant\n"
            f"- Composite stress score: {score:.3f} (0=no stress, 1=wilting)\n"
            f"- Continuous stress duration: {days} days\n\n"
            f"Provide a concise plant physiological assessment (2–3 sentences). "
            f"Reference specific indicator values. State urgency of irrigation and "
            f"expected yield impact if stress continues."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "composite_stress_score": score,
        }


# ---------------------------------------------------------------------------
# Agent 2: WiltingPredictionAgent (priority 9)
# ---------------------------------------------------------------------------

class WiltingPredictionAgent(DiagnosticAgent):
    """Time-to-wilting predictor using ψ_leaf decline trajectory.

    The LLM interprets the rate of water potential decline relative to
    the wilting threshold to assess irrigation urgency.
    """

    agent_name = "wilting_prediction"
    domain     = "plant_physiology"
    priority   = 9

    _WINDOW_MIN = 120

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_wilting_trajectory() -> str:
            """Return current ψ_leaf, decline rate, and estimated time to wilting."""
            psi_now = ts.get_latest("leaf_water_potential_mpa")
            rwc     = ts.get_latest("relative_water_content")
            try:
                df = ts.query_recent("leaf_water_potential_mpa", minutes=120)
                if df is not None and len(df) >= 2:
                    vals  = df["value"].tolist() if hasattr(df, "value") else list(df)
                    slope = (vals[-1] - vals[0]) / 2.0
                else:
                    slope = -0.05
            except Exception:
                slope = -0.05
            ttp = None
            if psi_now is not None and slope < -1e-3:
                ttp = round(max(0.0, (_PSI_WILT - psi_now) / slope), 1)
            return (
                f"ψ_leaf={psi_now} MPa | decline rate={slope:.3f} MPa/hr | "
                f"time-to-wilting={ttp} hr | RWC={rwc} | "
                f"wilting threshold={_PSI_WILT} MPa"
            )

        return [get_wilting_trajectory]

    async def observe(self) -> dict:
        try:
            psi_now = self._ts_store.get_latest("leaf_water_potential_mpa")
            rwc     = self._ts_store.get_latest("relative_water_content")
        except Exception:
            psi_now = rwc = None

        try:
            df = self._ts_store.query_recent("leaf_water_potential_mpa", minutes=self._WINDOW_MIN)
            if df is not None and len(df) >= 2:
                vals  = df["value"].tolist() if hasattr(df, "value") else list(df)
                slope = (vals[-1] - vals[0]) / (self._WINDOW_MIN / 60.0)
            else:
                slope = -0.05
        except Exception:
            slope = -0.05

        time_to_wilting_hr = None
        if psi_now is not None and slope < -1e-3:
            time_to_wilting_hr = max(0.0, (_PSI_WILT - psi_now) / slope)

        anomaly_detected = (
            (time_to_wilting_hr is not None and time_to_wilting_hr < 6) or
            (psi_now is not None and psi_now < -2.0)
        )

        return {
            "psi_leaf_mpa": psi_now,
            "psi_decline_rate_mpa_hr": slope,
            "time_to_wilting_hr": time_to_wilting_hr,
            "rwc": rwc,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        ttp  = observations.get("time_to_wilting_hr")
        psi  = observations.get("psi_leaf_mpa")
        rate = observations.get("psi_decline_rate_mpa_hr")
        rwc  = observations.get("rwc")

        if ttp is not None and ttp < 3:
            severity, action = "critical", "publish_critical_alert"
        elif ttp is not None and ttp < 12:
            severity, action = "warning", "publish_alert"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a plant water relations specialist assessing wilting risk "
            f"in a maize field.\n\n"
            f"Current leaf water status:\n"
            f"- Leaf water potential ψ: {psi} MPa\n"
            f"- ψ decline rate: {rate:.3f} MPa/hr (negative = drying)\n"
            f"- Estimated time to wilting threshold ({_PSI_WILT} MPa): {ttp} hr\n"
            f"- Relative water content: {rwc}\n\n"
            f"Assess wilting risk in 2–3 sentences using Pressure-Volume curve "
            f"terminology where relevant. State whether emergency, urgent, or "
            f"scheduled irrigation is needed."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "time_to_wilting_hr": ttp,
        }


# ---------------------------------------------------------------------------
# Agent 3: TranspirationAgent (priority 8)
# ---------------------------------------------------------------------------

class TranspirationAgent(DiagnosticAgent):
    """Plant hydraulics agent — transpiration demand vs soil supply balance.

    The LLM identifies hydraulic failure (stomatal closure despite adequate
    soil moisture) and translates it into root-zone health recommendations.
    """

    agent_name = "transpiration"
    domain     = "plant_hydraulics"
    priority   = 8

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_transpiration_data() -> str:
            """Return sap flow, stomatal conductance, ETa/ETc, and root-zone VWC."""
            sap      = ts.get_latest("sap_flow_L_hr")
            gs       = ts.get_latest("stomatal_conductance_mmol")
            eta_frac = ts.get_latest("eta_fraction")
            vpd      = ts.get_latest("vpd_kpa")
            vwc_rz   = ts.get_latest("vwc_root_zone") or ts.get_latest("vwc_10cm")
            hf = gs is not None and gs < _GS_NOMINAL * 0.30 and vwc_rz is not None and vwc_rz > 0.035
            return (
                f"Sap flow={sap} L/hr/plant | "
                f"gs={gs} mmol/m²/s (nominal={_GS_NOMINAL}, hydraulic failure threshold=30%) | "
                f"ETa/ETc={eta_frac} | VPD={vpd} kPa | "
                f"Root-zone VWC={vwc_rz} m³/m³ | "
                f"Hydraulic failure signal={'YES' if hf else 'no'}"
            )

        return [get_transpiration_data]

    async def observe(self) -> dict:
        try:
            sap      = self._ts_store.get_latest("sap_flow_L_hr")
            gs       = self._ts_store.get_latest("stomatal_conductance_mmol")
            eta_frac = self._ts_store.get_latest("eta_fraction")
            vpd      = self._ts_store.get_latest("vpd_kpa")
            t_air    = self._ts_store.get_latest("t_air_c")
            vwc_rz   = (self._ts_store.get_latest("vwc_root_zone") or
                        self._ts_store.get_latest("vwc_10cm"))
        except Exception:
            sap = gs = eta_frac = vpd = t_air = vwc_rz = None

        hydraulic_failure = (
            gs is not None and gs < _GS_NOMINAL * 0.30 and
            vwc_rz is not None and vwc_rz > 0.035
        )
        anomaly_detected = (
            (eta_frac is not None and eta_frac < 0.50) or hydraulic_failure
        )

        return {
            "sap_flow_L_hr": sap, "stomatal_conductance_mmol": gs,
            "eta_fraction": eta_frac, "vpd_kpa": vpd, "t_air_c": t_air,
            "vwc_root_zone": vwc_rz,
            "hydraulic_failure": hydraulic_failure,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        eta = observations.get("eta_fraction", 1.0)
        sap = observations.get("sap_flow_L_hr")
        hf  = observations.get("hydraulic_failure")
        gs  = observations.get("stomatal_conductance_mmol")
        vwc = observations.get("vwc_root_zone")

        if hf:
            severity, action = "warning", "publish_advisory"
        elif eta is not None and eta < 0.40:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a plant hydraulics specialist analysing transpiration "
            f"and stomatal behaviour in maize (V6-V8).\n\n"
            f"Hydraulic data:\n"
            f"- Sap flow: {sap} L/hr/plant\n"
            f"- Stomatal conductance gs: {gs} mmol/m²/s "
            f"(nominal={_GS_NOMINAL}; hydraulic failure if gs<{_GS_NOMINAL*0.30:.0f} with VWC>0.035)\n"
            f"- ETa/ETc fraction: {eta} (1.0=full demand met, 0=no transpiration)\n"
            f"- VPD: {observations.get('vpd_kpa')} kPa\n"
            f"- Root-zone VWC: {vwc} m³/m³\n"
            f"- Hydraulic failure detected: {hf}\n\n"
            f"Interpret the plant water use status in 2–3 sentences. "
            f"If hydraulic failure is detected, explain the mechanism and "
            f"recommend root-zone or management intervention."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "eta_fraction": eta,
        }


# ---------------------------------------------------------------------------
# Agent 4: PhotosynthesisAgent (priority 6)
# ---------------------------------------------------------------------------

class PhotosynthesisAgent(DiagnosticAgent):
    """Photosynthetic downregulation estimator from stomatal closure.

    The LLM interprets the Ball-Berry gs–A relationship for maize C4
    biochemistry under drought-induced stomatal limitation.
    """

    agent_name = "photosynthesis"
    domain     = "plant_physiology"
    priority   = 6

    _AN_MAX = 40.0   # maize C4 An at saturating light [µmol/m²/s]

    def _build_extra_tools(self) -> list:
        ts      = self._ts_store
        an_max  = self._AN_MAX

        @lc_tool
        def get_photosynthesis_status() -> str:
            """Return estimated net assimilation rate and stomatal limitation index."""
            gs   = ts.get_latest("stomatal_conductance_mmol")
            cwsi = ts.get_latest("cwsi")
            if gs is not None:
                gs_frac = min(1.0, gs / _GS_NOMINAL)
                an_est  = round(an_max * (gs_frac ** 0.6), 2)
                an_frac = round(an_est / an_max, 3)
            else:
                an_est = an_frac = None
            return (
                f"gs={gs} mmol/m²/s | "
                f"Estimated An≈{an_est} µmol/m²/s (An_max={an_max}, Ball-Berry C4) | "
                f"An fraction={an_frac} | CWSI={cwsi}"
            )

        return [get_photosynthesis_status]

    async def observe(self) -> dict:
        try:
            gs   = self._ts_store.get_latest("stomatal_conductance_mmol")
            cwsi = self._ts_store.get_latest("cwsi")
        except Exception:
            gs = cwsi = None

        an_estimated = None
        if gs is not None:
            gs_frac     = min(1.0, gs / _GS_NOMINAL)
            an_estimated = self._AN_MAX * (gs_frac ** 0.6)

        anomaly_detected = (
            an_estimated is not None and an_estimated < self._AN_MAX * 0.60
        )

        return {
            "gs_mmol": gs, "cwsi": cwsi,
            "an_estimated_umol_m2_s": an_estimated,
            "an_fraction": round(an_estimated / self._AN_MAX, 3) if an_estimated else None,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        an   = observations.get("an_estimated_umol_m2_s")
        frac = observations.get("an_fraction", 1.0)
        gs   = observations.get("gs_mmol")
        cwsi = observations.get("cwsi")

        if frac is not None and frac < 0.40:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a plant biochemist assessing photosynthetic limitation "
            f"in maize (Zea mays, C4 pathway, V6-V8 growth stage).\n\n"
            f"Photosynthesis data (Ball-Berry stomatal model):\n"
            f"- Stomatal conductance gs: {gs} mmol/m²/s (nominal={_GS_NOMINAL})\n"
            f"- Estimated net assimilation rate An ≈ {an:.1f if an else 'N/A'} µmol/m²/s "
            f"(An_max={self._AN_MAX}, using An ≈ An_max × (gs/gs_max)^0.6)\n"
            f"- An as fraction of maximum: {frac}\n"
            f"- CWSI: {cwsi}\n\n"
            f"Interpret the degree of stomatal limitation on C4 photosynthesis in "
            f"2–3 sentences. State the likely mechanism (ABA signalling, hydraulic "
            f"signal) and the expected impact on biomass accumulation."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "an_fraction": frac,
        }


# ---------------------------------------------------------------------------
# Agent 5: YieldPenaltyAgent (priority 5)
# ---------------------------------------------------------------------------

class YieldPenaltyAgent(DiagnosticAgent):
    """Cumulative yield impact via FAO AquaCrop model.

    The LLM contextualises the yield penalty using Ky=1.25 (Doorenbos &
    Kassam 1979) and recommends season-recovery irrigation strategies.
    """

    agent_name = "yield_penalty"
    domain     = "agronomy"
    priority   = 5

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_yield_status() -> str:
            """Return cumulative yield penalty and AquaCrop model parameters."""
            penalty     = ts.get_latest("yield_penalty_pct")
            eta         = ts.get_latest("eta_fraction")
            stress_days = ts.get_latest("stress_duration_days")
            ya_frac     = round(1.0 - (penalty or 0.0) / 100.0, 4) if penalty else None
            return (
                f"Yield penalty={penalty}% | Ya/Ymax={ya_frac} | "
                f"ETa/ETc={eta} | Stress duration={stress_days} days | "
                f"Model: AquaCrop Ya/Ymax=1-Ky×(1-ETa/ETc) | Ky=1.25 (maize)"
            )

        return [get_yield_status]

    async def observe(self) -> dict:
        try:
            penalty     = self._ts_store.get_latest("yield_penalty_pct")
            eta         = self._ts_store.get_latest("eta_fraction")
            stress_days = self._ts_store.get_latest("stress_duration_days")
        except Exception:
            penalty = eta = stress_days = None

        anomaly_detected = penalty is not None and penalty > 10.0

        return {
            "yield_penalty_pct": penalty,
            "eta_fraction": eta,
            "stress_duration_days": stress_days,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        penalty     = observations.get("yield_penalty_pct", 0.0)
        stress_days = observations.get("stress_duration_days", 0.0)
        eta         = observations.get("eta_fraction")

        if penalty > 25.0:
            severity, action = "critical", "publish_alert"
        elif penalty > 10.0:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        ya_frac = round(1.0 - penalty / 100.0, 3) if penalty else None

        prompt = (
            f"You are an agronomist assessing yield impact in a maize crop "
            f"(Zea mays, V6-V8, high-yield potential environment) using the "
            f"FAO AquaCrop model.\n\n"
            f"Yield model output:\n"
            f"- Cumulative yield penalty: {penalty:.1f}%\n"
            f"- Ya/Ymax ratio: {ya_frac} (1.0=no loss)\n"
            f"- ETa/ETc: {eta} (current actual vs potential ET ratio)\n"
            f"- Continuous stress duration: {stress_days:.0f} days\n"
            f"- AquaCrop: Ya/Ymax = 1 – Ky × (1 – ETa/ETc), Ky=1.25 (Doorenbos & Kassam)\n\n"
            f"Interpret the yield penalty in 2–3 sentences. At this growth stage, "
            f"assess whether the loss is recoverable with immediate irrigation, "
            f"and quantify the approximate remaining season impact if stress continues."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "yield_penalty_pct": penalty,
        }
