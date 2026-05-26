"""SDT Intelligent Layer — 4 LLM agents for agronomic soil analysis.

Agents (by priority):
    1. MoistureDepletionAgent  (10) — hydrology: depletion trajectory analysis
    2. SensorValidationAgent   (9)  — sensor health, cross-depth consistency
    3. HydraulicPropertyAgent  (8)  — van Genuchten parameter drift detection
    4. ETForecastAgent         (7)  — agro-meteorological ET prediction

Each agent extends DiagnosticAgent (LangChain tool-calling) and uses the
configured Ollama/OpenAI/Anthropic LLM for agronomic reasoning. The observe()
methods gather structured sensor data and compute physics-based indicators;
reason() submits those observations to the LLM for natural-language analysis.

References:
    Allen et al. (1998) FAO-56 Chapter 6 — ET guidelines
    van Genuchten (1980) — soil retention curve
    Shock & Wang (2011) — soil moisture sensor cross-validation
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

_PWP = 0.050    # permanent wilting point [m³/m³]
_FC  = 0.100    # field capacity

# ---------------------------------------------------------------------------
# Domain knowledge graph spec
# ---------------------------------------------------------------------------

sdt_kg_spec = KnowledgeGraphSpec(
    components=[
        "vwc_sensor_10cm", "vwc_sensor_30cm", "vwc_sensor_60cm",
        "tensiometer_10cm", "tensiometer_30cm", "soil_temp_sensor",
    ],
    failure_modes=[
        FailureMode(
            name="sensor_drift",
            severity="high",
            maintenance_actions=["recalibrate_sensor", "cross_validate_depths"],
            affected_components=["vwc_sensor_10cm", "vwc_sensor_30cm", "vwc_sensor_60cm"],
        ),
        FailureMode(
            name="rapid_soil_depletion",
            severity="critical",
            maintenance_actions=["trigger_irrigation", "check_drainage"],
            affected_components=["vwc_sensor_10cm", "vwc_sensor_30cm"],
        ),
        FailureMode(
            name="profile_inversion",
            severity="medium",
            maintenance_actions=["inspect_drainage", "check_sensor_placement"],
            affected_components=["vwc_sensor_10cm", "vwc_sensor_60cm"],
        ),
        FailureMode(
            name="wilting_threshold_breach",
            severity="critical",
            maintenance_actions=["immediate_irrigation", "alert_agronomist"],
            affected_components=["vwc_sensor_10cm", "tensiometer_10cm"],
        ),
        FailureMode(
            name="high_evapotranspiration",
            severity="medium",
            maintenance_actions=["review_irrigation_schedule", "check_weather_forecast"],
            affected_components=["vwc_sensor_10cm"],
        ),
    ],
    symptom_mappings=[
        SymptomMapping("low_vwc_10cm",    "vwc_10cm",             0.045, ["wilting_threshold_breach", "rapid_soil_depletion"], "low"),
        SymptomMapping("critical_vwc",    "vwc_10cm",             0.030, ["wilting_threshold_breach"],                          "low"),
        SymptomMapping("high_depletion",  "depletion_rate_avg",   0.002, ["rapid_soil_depletion"],                              "high"),
        SymptomMapping("sensor_fault",    "sensor_divergence_flag", 0.04, ["sensor_drift"],                                     "high"),
        SymptomMapping("high_et",         "et_loss_rate",         5.0,   ["high_evapotranspiration"],                          "high"),
    ],
)


# ---------------------------------------------------------------------------
# Shared constructor helper
# ---------------------------------------------------------------------------

def _agent_kwargs(config, ts_store, doc_store, llm, ditto_client, kg):
    return dict(
        config=config, llm=llm, ditto_client=ditto_client,
        ts_store=ts_store, doc_store=doc_store, knowledge_graph=kg,
    )


# ---------------------------------------------------------------------------
# Agent 1: MoistureDepletionAgent (priority 10)
# ---------------------------------------------------------------------------

class MoistureDepletionAgent(DiagnosticAgent):
    """Hydrology agent — tracks VWC depletion trajectory across all depths.

    Uses the LLM to synthesise multi-depth moisture data, depletion rates,
    and time-to-PWP estimates into agronomic irrigation recommendations.
    """

    agent_name = "moisture_depletion"
    domain     = "hydrology"
    priority   = 10

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_soil_moisture_profile() -> str:
            """Return VWC at all three depths plus computed deficit and depletion rate."""
            vwc_10 = ts.get_latest("vwc_10cm")
            vwc_30 = ts.get_latest("vwc_30cm")
            vwc_60 = ts.get_latest("vwc_60cm")
            depl   = ts.get_latest("depletion_rate_avg")
            deficit_mm = round((_FC - (vwc_10 or _FC)) * 200.0, 1) if vwc_10 else None
            ttp = None
            if vwc_10 and depl and depl > 1e-6:
                ttp = round((vwc_10 - _PWP) / depl, 1)
            return (
                f"VWC: 10cm={vwc_10}, 30cm={vwc_30}, 60cm={vwc_60} m³/m³ | "
                f"FC={_FC}, PWP={_PWP} | "
                f"Depletion rate={depl} m³/m³/hr | "
                f"Soil water deficit={deficit_mm} mm | "
                f"Time to PWP={ttp} hr"
            )

        @lc_tool
        def get_et_demand() -> str:
            """Return current evapotranspiration demand and weather drivers."""
            et   = ts.get_latest("et_loss_rate")
            vpd  = ts.get_latest("vpd_kpa")
            temp = ts.get_latest("t_air_c")
            rain = ts.get_latest("rain_mm")
            return (
                f"ETc={et} mm/day (FAO-56, Kc=1.10 maize V6-V8) | "
                f"VPD={vpd} kPa | T_air={temp} °C | Rain={rain} mm"
            )

        return [get_soil_moisture_profile, get_et_demand]

    async def observe(self) -> dict:
        vwc_10  = self._ts_store.get_latest("vwc_10cm") if self._ts_store else None
        vwc_30  = self._ts_store.get_latest("vwc_30cm") if self._ts_store else None
        vwc_60  = self._ts_store.get_latest("vwc_60cm") if self._ts_store else None
        depl    = self._ts_store.get_latest("depletion_rate_avg") if self._ts_store else None
        et_rate = self._ts_store.get_latest("et_loss_rate") if self._ts_store else None

        try:
            vwc_10 = self._ts_store.get_latest("vwc_10cm")
            vwc_30 = self._ts_store.get_latest("vwc_30cm")
            vwc_60 = self._ts_store.get_latest("vwc_60cm")
            depl   = self._ts_store.get_latest("depletion_rate_avg")
            et_rate = self._ts_store.get_latest("et_loss_rate")
        except Exception:
            pass

        deficit_mm = ((_FC - vwc_10) * 200.0) if vwc_10 is not None else None
        time_to_pwp_hr = None
        if vwc_10 is not None and depl is not None and depl > 1e-6:
            time_to_pwp_hr = (vwc_10 - _PWP) / depl

        anomaly_detected = (
            depl is not None and depl > 0.002 and
            et_rate is not None and et_rate > 4.0
        ) or (vwc_10 is not None and vwc_10 < 0.045)

        return {
            "vwc_10": vwc_10, "vwc_30": vwc_30, "vwc_60": vwc_60,
            "depletion_rate": depl,
            "et_loss_rate": et_rate,
            "soil_water_deficit_mm": deficit_mm,
            "estimated_time_to_pwp_hr": time_to_pwp_hr,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        vwc   = observations.get("vwc_10")
        depl  = observations.get("depletion_rate")
        ttp   = observations.get("estimated_time_to_pwp_hr")
        swd   = observations.get("soil_water_deficit_mm")
        et    = observations.get("et_loss_rate")

        if ttp is not None and ttp < 6:
            severity, action = "critical", "publish_critical_alert"
        elif (ttp is not None and ttp < 24) or (vwc is not None and vwc < 0.045):
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are an agronomic AI analyst for a maize field (V6-V8, sandy loam soil, "
            f"field capacity=0.100 m³/m³, permanent wilting point=0.050 m³/m³).\n\n"
            f"Current soil moisture data:\n"
            f"- VWC at 10 cm: {vwc} m³/m³\n"
            f"- VWC at 30 cm: {observations.get('vwc_30')} m³/m³\n"
            f"- VWC at 60 cm: {observations.get('vwc_60')} m³/m³\n"
            f"- Depletion rate: {depl} m³/m³/hr\n"
            f"- Estimated hours to permanent wilting point: {ttp}\n"
            f"- Soil water deficit: {swd} mm\n"
            f"- Evapotranspiration demand: {et} mm/day\n\n"
            f"Provide a concise agronomic assessment (2-3 sentences). Cite the specific "
            f"sensor values. State whether irrigation is required and when."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "time_to_pwp_hr": ttp, "soil_water_deficit_mm": swd,
        }


# ---------------------------------------------------------------------------
# Agent 2: SensorValidationAgent (priority 9)
# ---------------------------------------------------------------------------

class SensorValidationAgent(DiagnosticAgent):
    """Sensor health agent — cross-validates VWC readings at all depths.

    The LLM analyses cross-depth consistency, profile inversions, and
    divergence flags to produce a calibration recommendation.
    """

    agent_name = "sensor_validation"
    domain     = "sensor_health"
    priority   = 9

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_sensor_health_data() -> str:
            """Return VWC at all depths with divergence flag and range check results."""
            vwc_10 = ts.get_latest("vwc_10cm")
            vwc_30 = ts.get_latest("vwc_30cm")
            vwc_60 = ts.get_latest("vwc_60cm")
            div    = ts.get_latest("sensor_divergence_flag")
            inversion = (
                vwc_10 is not None and vwc_60 is not None and
                (vwc_60 - vwc_10) > 0.015
            )
            oor = [
                d for d, v in [("10cm", vwc_10), ("30cm", vwc_30), ("60cm", vwc_60)]
                if v is not None and (v < 0.01 or v > 0.45)
            ]
            return (
                f"VWC: 10cm={vwc_10}, 30cm={vwc_30}, 60cm={vwc_60} m³/m³ | "
                f"Divergence flag={div} | "
                f"Profile inversion (60>10+1.5%)={inversion} | "
                f"Out-of-range depths={oor or 'none'}"
            )

        return [get_sensor_health_data]

    async def observe(self) -> dict:
        try:
            vwc_10 = self._ts_store.get_latest("vwc_10cm")
            vwc_30 = self._ts_store.get_latest("vwc_30cm")
            vwc_60 = self._ts_store.get_latest("vwc_60cm")
            div_flg = self._ts_store.get_latest("sensor_divergence_flag")
        except Exception:
            vwc_10 = vwc_30 = vwc_60 = div_flg = None

        def oor(v):
            return v is not None and (v < 0.01 or v > 0.45)

        inversion = (
            vwc_10 is not None and vwc_60 is not None and
            (vwc_60 - vwc_10) > 0.015
        )
        anomaly_detected = (
            oor(vwc_10) or oor(vwc_30) or oor(vwc_60) or
            inversion or (div_flg is not None and div_flg > 0.03)
        )

        return {
            "vwc_10": vwc_10, "vwc_30": vwc_30, "vwc_60": vwc_60,
            "sensor_divergence_flag": div_flg,
            "out_of_range_10cm": oor(vwc_10),
            "out_of_range_30cm": oor(vwc_30),
            "out_of_range_60cm": oor(vwc_60),
            "profile_inversion": inversion,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        issues = [
            desc for flag, desc in [
                (observations.get("out_of_range_10cm"), "10 cm VWC out of physical range (0.01–0.45)"),
                (observations.get("out_of_range_30cm"), "30 cm VWC out of physical range"),
                (observations.get("out_of_range_60cm"), "60 cm VWC out of physical range"),
                (observations.get("profile_inversion"),  "depth profile inversion: 60 cm wetter than 10 cm by >1.5%"),
            ] if flag
        ]
        div = observations.get("sensor_divergence_flag")
        if div and div > 0.03:
            issues.append(f"inter-sensor divergence={div:.4f} exceeds 0.03 threshold")

        severity = "critical" if issues else "info"
        action   = "trigger_recalibration" if issues else "no_action"

        prompt = (
            f"You are a precision agriculture sensor specialist. Analyse the following "
            f"soil VWC sensor health data for a maize field:\n"
            f"- VWC 10cm={observations.get('vwc_10')}, 30cm={observations.get('vwc_30')}, "
            f"60cm={observations.get('vwc_60')} m³/m³ (valid range: 0.01–0.45)\n"
            f"- Inter-sensor divergence flag={observations.get('sensor_divergence_flag')} "
            f"(threshold: 0.03)\n"
            f"- Profile inversion (60 cm > 10 cm + 1.5%)={observations.get('profile_inversion')}\n"
            f"- Detected issues: {issues if issues else 'none'}\n\n"
            f"Diagnose the sensor status in 2–3 sentences. If issues exist, recommend "
            f"specific calibration or maintenance actions."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "issues": issues,
        }


# ---------------------------------------------------------------------------
# Agent 3: HydraulicPropertyAgent (priority 8)
# ---------------------------------------------------------------------------

class HydraulicPropertyAgent(DiagnosticAgent):
    """Soil physics agent — monitors van Genuchten parameter drift.

    The LLM interprets deviations between observed VWC and the calibrated
    retention curve (van Genuchten 1980) as soil compaction or sensor offset.
    """

    agent_name = "hydraulic_property"
    domain     = "soil_physics"
    priority   = 8

    # Sandy loam van Genuchten parameters (calibrated to FC=0.10, PWP=0.050)
    _THETA_R = 0.050
    _THETA_S = 0.430
    _ALPHA   = 0.022   # cm⁻¹
    _N       = 2.00
    _M       = 0.50

    def _vg_theta(self, psi_kpa: float) -> float:
        psi_cm = abs(psi_kpa) * 10.197
        denom  = 1.0 + (self._ALPHA * psi_cm) ** self._N
        se     = denom ** (-self._M)
        return self._THETA_R + se * (self._THETA_S - self._THETA_R)

    def _build_extra_tools(self) -> list:
        ts = self._ts_store
        vg = self._vg_theta

        @lc_tool
        def get_hydraulic_analysis() -> str:
            """Return measured vs van Genuchten predicted VWC and the residual."""
            vwc_10 = ts.get_latest("vwc_10cm")
            psi_10 = ts.get_latest("soil_water_potential_10")
            if psi_10 is not None and vwc_10 is not None:
                model_theta = vg(psi_10)
                residual    = abs(vwc_10 - model_theta)
                return (
                    f"VWC observed={vwc_10:.4f}, model (vG)={model_theta:.4f}, "
                    f"residual={residual:.4f} m³/m³ | "
                    f"Matric potential ψ={psi_10} kPa | "
                    f"vG params: θr=0.050, θs=0.430, α=0.022 cm⁻¹, n=2.0"
                )
            return "Insufficient data for van Genuchten comparison."

        return [get_hydraulic_analysis]

    async def observe(self) -> dict:
        try:
            vwc_10 = self._ts_store.get_latest("vwc_10cm")
            psi_10 = self._ts_store.get_latest("soil_water_potential_10")
        except Exception:
            vwc_10 = psi_10 = None

        model_theta = residual = None
        if psi_10 is not None and vwc_10 is not None:
            model_theta = self._vg_theta(psi_10)
            residual    = abs(vwc_10 - model_theta)

        anomaly_detected = residual is not None and residual > 0.015

        return {
            "vwc_10cm_observed": vwc_10,
            "vwc_10cm_model":    model_theta,
            "retention_residual": residual,
            "psi_10_kpa": psi_10,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        res = observations.get("retention_residual")
        obs = observations.get("vwc_10cm_observed")
        mod = observations.get("vwc_10cm_model")
        psi = observations.get("psi_10_kpa")

        if res is not None and res > 0.020:
            severity, action = "critical", "notify_agronomist"
        elif res is not None and res > 0.015:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are a soil scientist specialising in vadose zone hydraulics. "
            f"Analyse the following soil retention curve data for a sandy loam field:\n"
            f"- Observed VWC at 10 cm: {obs} m³/m³\n"
            f"- Van Genuchten predicted VWC at ψ={psi} kPa: {mod} m³/m³\n"
            f"- Retention curve residual |observed - model|: {res} m³/m³\n"
            f"  (vG parameters: θr=0.050, θs=0.430, α=0.022 cm⁻¹, n=2.0)\n"
            f"  Acceptable residual: <0.015; warning >0.015; critical >0.020\n\n"
            f"Interpret the residual in 2–3 sentences. If anomalous, identify "
            f"likely causes (sensor offset, soil compaction, structural change) "
            f"and recommend follow-up actions."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "retention_residual": res,
        }


# ---------------------------------------------------------------------------
# Agent 4: ETForecastAgent (priority 7)
# ---------------------------------------------------------------------------

class ETForecastAgent(DiagnosticAgent):
    """Agro-meteorological agent — forecasts ET demand and 24-hr soil moisture change.

    The LLM interprets FAO-56 ETc demand, VPD, and rainfall to produce
    a water-balance irrigation pre-planning recommendation.
    """

    agent_name = "et_forecast"
    domain     = "agro_meteorology"
    priority   = 7

    _KC         = 1.10    # maize mid-season crop coefficient
    _ROOT_DEPTH = 0.50    # effective root zone depth [m] at V6-V8
    _FC         = 0.100
    _PWP        = 0.050

    def _build_extra_tools(self) -> list:
        ts = self._ts_store

        @lc_tool
        def get_weather_and_et() -> str:
            """Return current meteorological drivers and ETc demand."""
            et   = ts.get_latest("et_loss_rate")
            vpd  = ts.get_latest("vpd_kpa")
            temp = ts.get_latest("t_air_c")
            rain = ts.get_latest("rain_mm")
            vwc  = ts.get_latest("vwc_10cm")
            etc_m = (et or 0) / 1000.0
            depl  = round(etc_m / 0.50, 6) if et else None
            fvwc  = round(max(0.050, (vwc or 0.08) - (depl or 0)), 4)
            return (
                f"ETc={et} mm/day (FAO-56, Kc=1.10) | VPD={vpd} kPa | "
                f"T_air={temp} °C | Rain={rain} mm | "
                f"VWC_10cm={vwc} | 24hr forecast VWC={fvwc}"
            )

        return [get_weather_and_et]

    async def observe(self) -> dict:
        try:
            et_rate = self._ts_store.get_latest("et_loss_rate")
            vwc_10  = self._ts_store.get_latest("vwc_10cm")
            vpd     = self._ts_store.get_latest("vpd_kpa")
            t_air   = self._ts_store.get_latest("t_air_c")
            rain    = self._ts_store.get_latest("rain_mm")
        except Exception:
            et_rate = vwc_10 = vpd = t_air = rain = None

        forecast_depl = forecast_vwc = None
        if et_rate is not None and vwc_10 is not None:
            etc_m = et_rate / 1000.0
            depl_per_m = etc_m / self._ROOT_DEPTH
            forecast_depl = depl_per_m
            forecast_vwc  = max(self._PWP, vwc_10 - depl_per_m)

        taw = (self._FC - self._PWP) * self._ROOT_DEPTH * 1000

        anomaly_detected = et_rate is not None and et_rate > 5.0

        return {
            "et_loss_rate_mm_day": et_rate,
            "vpd_kpa": vpd, "t_air_c": t_air, "rain_mm": rain,
            "vwc_10cm_current": vwc_10,
            "forecast_vwc_24hr": forecast_vwc,
            "forecast_depletion_per_day": forecast_depl,
            "taw_mm": taw,
            "anomaly_detected": anomaly_detected,
        }

    async def reason(self, observations: dict) -> dict:
        et   = observations.get("et_loss_rate_mm_day")
        fvwc = observations.get("forecast_vwc_24hr")
        vpd  = observations.get("vpd_kpa")
        rain = observations.get("rain_mm", 0)
        taw  = observations.get("taw_mm")

        if et is not None and et > 5.0:
            severity, action = "warning", "publish_advisory"
        elif fvwc is not None and fvwc < 0.055:
            severity, action = "warning", "publish_advisory"
        else:
            severity, action = "info", "no_action"

        prompt = (
            f"You are an agro-meteorologist advising on irrigation scheduling for "
            f"maize (V6-V8, Kc=1.10, root depth=0.50 m, TAW={taw:.0f} mm).\n\n"
            f"Current conditions:\n"
            f"- ETc demand: {et} mm/day\n"
            f"- VPD: {vpd} kPa\n"
            f"- Air temperature: {observations.get('t_air_c')} °C\n"
            f"- Rainfall: {rain} mm\n"
            f"- Current VWC at 10 cm: {observations.get('vwc_10cm_current')} m³/m³\n"
            f"- Forecast VWC in 24 hr: {fvwc} m³/m³ "
            f"(PWP threshold=0.050, FC=0.100)\n\n"
            f"Assess the ET demand and water balance outlook in 2–3 sentences. "
            f"State whether pre-emptive irrigation is warranted and the recommended timing."
        )

        summary = await self.ask(prompt)

        return {
            "agent": self.agent_name, "summary": summary,
            "action": action, "severity": severity,
            "et_loss_rate_mm_day": et,
            "forecast_vwc_24hr": fvwc,
        }
