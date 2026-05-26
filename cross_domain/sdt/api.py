"""SDT Services Layer — FastAPI REST endpoints.

Endpoints (all under /api/sdt):
    GET  /moisture/profile          — current VWC at all 3 depths + soil potential
    GET  /depletion/rate            — depletion rate, time-to-PWP estimate
    GET  /hydraulic                 — van Genuchten retention, hydraulic conductivity
    GET  /forecast                  — 24-hr VWC and ET forecast
    GET  /thresholds                — configured alert thresholds
    GET  /stream                    — SSE live telemetry stream
    POST /calibrate                 — trigger sensor recalibration
    GET  /alerts/history            — recent FSM state change events
    GET  /state                     — current FSM state
    GET  /telemetry/history         — historical multi-field time-series for charts

Each endpoint reads live data from InfluxDB (TimeSeriesStore) and Redis (CacheStore).
Forecast data comes from the ProphetMoistureForecast model stored in the simulator.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from dt_forge.data.storage.base import TimeSeriesStore, DocumentStore, CacheStore

log = logging.getLogger(__name__)


class AgentChatRequest(BaseModel):
    message: str


def build_sdt_router(
    config: "TwinConfig",
    ts_store: "TimeSeriesStore",
    doc_store: "DocumentStore",
    cache: "CacheStore",
    simulator=None,    # SoilSensorSimulator — optional
    reactive=None,     # SoilRuleEngine — for FSM state
    mas=None,          # MultiAgentSystem — for /agents/status
) -> APIRouter:
    """Return the APIRouter for all SDT endpoints."""

    router = APIRouter(prefix="/api/sdt", tags=["Soil Digital Twin"])

    def _latest(field: str) -> float | None:
        try:
            return ts_store.get_latest(field)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # GET /moisture/profile
    # ------------------------------------------------------------------
    @router.get("/moisture/profile")
    async def moisture_profile():
        """Current volumetric water content at 10, 30, 60 cm depths + soil potential."""
        return {
            "asset_id": config.asset_id,
            "vwc_10cm":  _latest("vwc_10cm"),
            "vwc_30cm":  _latest("vwc_30cm"),
            "vwc_60cm":  _latest("vwc_60cm"),
            "soil_water_potential_10_kpa": _latest("soil_water_potential_10"),
            "soil_water_potential_30_kpa": _latest("soil_water_potential_30"),
            "soil_temp_10cm_c":  _latest("soil_temp_10cm"),
            "soil_temp_30cm_c":  _latest("soil_temp_30cm"),
            "sensor_divergence_flag": _latest("sensor_divergence_flag"),
            "units": {
                "vwc":           "m³/m³",
                "soil_potential": "kPa",
                "temp":          "°C",
            },
        }

    # ------------------------------------------------------------------
    # GET /depletion/rate
    # ------------------------------------------------------------------
    @router.get("/depletion/rate")
    async def depletion_rate():
        """Depletion rate and estimated time to permanent wilting point."""
        vwc_10 = _latest("vwc_10cm")
        depl   = _latest("depletion_rate_avg")
        _PWP   = 0.050

        time_to_pwp_hr = None
        if vwc_10 is not None and depl is not None and depl > 1e-6:
            time_to_pwp_hr = round((vwc_10 - _PWP) / depl, 1)

        return {
            "asset_id": config.asset_id,
            "depletion_rate_m3m3_per_hr": depl,
            "et_loss_rate_mm_per_day":    _latest("et_loss_rate"),
            "vwc_10cm": vwc_10,
            "time_to_pwp_hr": time_to_pwp_hr,
            "pwp_m3m3": _PWP,
            "fc_m3m3": 0.100,
        }

    # ------------------------------------------------------------------
    # GET /hydraulic
    # ------------------------------------------------------------------
    @router.get("/hydraulic")
    async def hydraulic_properties():
        """Van Genuchten soil hydraulic parameters and current matric potential."""
        return {
            "asset_id": config.asset_id,
            "van_genuchten": {
                "theta_r": 0.050,
                "theta_s": 0.430,
                "alpha_cm_inv": 0.022,
                "n": 2.00,
                "m": 0.50,
                "ks_m_per_day": 0.44,
                "reference": "van Genuchten (1980), calibrated to FC=0.10 @ -33 kPa",
            },
            "soil_water_potential_10_kpa": _latest("soil_water_potential_10"),
            "soil_water_potential_30_kpa": _latest("soil_water_potential_30"),
            "soil_type": "sandy_loam",
        }

    # ------------------------------------------------------------------
    # GET /forecast
    # ------------------------------------------------------------------
    @router.get("/forecast")
    async def moisture_forecast():
        """24-hour VWC forecast from ET-based depletion extrapolation."""
        vwc_10  = _latest("vwc_10cm") or 0.06
        et_rate = _latest("et_loss_rate") or 1.5
        rain    = _latest("rain_mm") or 0.0

        root_depth_m = 0.50
        etc_m_per_day = et_rate / 1000.0
        depl_m3m3    = etc_m_per_day / root_depth_m
        net_depl     = max(0, depl_m3m3 - (rain / 1000.0 / root_depth_m))

        forecast_24hr = round(max(0.050, vwc_10 - net_depl), 4)
        deficit_mm    = round((0.100 - vwc_10) * root_depth_m * 1000, 1)

        return {
            "asset_id": config.asset_id,
            "current_vwc_10cm": vwc_10,
            "forecast_vwc_10cm_24hr": forecast_24hr,
            "net_depletion_m3m3_24hr": round(net_depl, 5),
            "soil_water_deficit_mm": deficit_mm,
            "et_loss_rate_mm_day": et_rate,
            "rain_mm": rain,
            "method": "FAO-56 ET×Kc linear extrapolation",
            "kc": 1.10,
        }

    # ------------------------------------------------------------------
    # GET /thresholds
    # ------------------------------------------------------------------
    @router.get("/thresholds")
    async def thresholds():
        """Alert thresholds for all monitored fields."""
        return {
            "asset_id": config.asset_id,
            "thresholds": {
                "vwc_10cm":   {"warn": 0.045, "crit": 0.030, "unit": "m³/m³"},
                "vwc_30cm":   {"warn": 0.042, "crit": 0.028, "unit": "m³/m³"},
                "vwc_60cm":   {"warn": 0.038, "crit": 0.025, "unit": "m³/m³"},
                "depletion_rate_avg": {"warn": 0.0015, "crit": 0.002, "unit": "m³/m³/hr"},
                "soil_water_potential_10": {"warn": -50.0, "crit": -80.0, "unit": "kPa"},
                "sensor_divergence_flag":  {"warn": 0.03,  "crit": 0.05,  "unit": "dimensionless"},
                "et_loss_rate": {"warn": 3.5, "crit": 5.0, "unit": "mm/day"},
            },
            "fsm_states": ["OPTIMAL", "DEPLETING", "CRITICAL", "WILTING_RISK", "SENSOR_FAULT"],
            "fc_m3m3": 0.100,
            "pwp_m3m3": 0.050,
        }

    # ------------------------------------------------------------------
    # GET /state
    # ------------------------------------------------------------------
    @router.get("/state")
    async def current_state():
        """Current FSM state from the reactive layer."""
        state = cache.get_state() if cache else "UNKNOWN"
        try:
            if reactive is not None:
                state = reactive.get_state()
        except Exception:
            pass
        return {"asset_id": config.asset_id, "fsm_state": state}

    # ------------------------------------------------------------------
    # GET /stream  (Server-Sent Events)
    # ------------------------------------------------------------------
    @router.get("/stream")
    async def telemetry_stream():
        """SSE endpoint — streams latest telemetry every 15 seconds."""

        async def _event_generator() -> AsyncIterator[str]:
            fields = [
                "vwc_10cm", "vwc_30cm", "vwc_60cm",
                "soil_water_potential_10", "depletion_rate_avg",
                "et_loss_rate", "soil_temp_10cm", "sensor_divergence_flag",
            ]
            while True:
                # Prefer simulator in-memory state — always populated from startup.
                # InfluxDB is only written after the first 900 s step, so reading
                # from it would produce nulls for the first ~15 minutes.
                if simulator is not None:
                    sim_data = simulator.get_latest()
                    data = {f: sim_data.get(f) for f in fields}
                else:
                    data = {f: _latest(f) for f in fields}
                try:
                    if reactive:
                        data["fsm_state"] = reactive.get_state()
                except Exception:
                    pass
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(10)

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # GET /telemetry/history
    # ------------------------------------------------------------------
    @router.get("/telemetry/history")
    async def telemetry_history(
        minutes: int = 120,
        fields: str = "vwc_10cm,vwc_30cm,depletion_rate_avg,et_loss_rate",
    ):
        """Historical time-series for multiple fields — used to pre-populate dashboard charts.

        Query params:
            minutes  — lookback window (default 120 min, max 1440)
            fields   — comma-separated field names
        """
        minutes = min(max(minutes, 1), 1440)
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        data = ts_store.query_recent_fields(field_list, minutes=minutes)
        return {
            "asset_id": config.asset_id,
            "minutes":  minutes,
            "fields":   data,
        }

    # ------------------------------------------------------------------
    # POST /calibrate
    # ------------------------------------------------------------------
    @router.post("/calibrate")
    async def trigger_calibration():
        """Request an in-situ sensor recalibration cycle."""
        doc_store.log_event(
            "calibration_requested",
            {"requested_by": "api", "asset_id": config.asset_id},
            severity="info",
        )
        return {
            "asset_id": config.asset_id,
            "status": "calibration_scheduled",
            "message": "Calibration request logged. Sensor readings will be flagged during recalibration.",
        }

    # ------------------------------------------------------------------
    # GET /alerts/history
    # ------------------------------------------------------------------
    @router.get("/alerts/history")
    async def alerts_history(n: int = 20):
        """Recent FSM state-change events and alert history."""
        events = doc_store.get_recent_events(n)
        return {
            "asset_id": config.asset_id,
            "count":    len(events),
            "events":   events,
        }

    # ------------------------------------------------------------------
    # GET /agents/status
    # ------------------------------------------------------------------
    @router.get("/agents/status")
    async def agents_status():
        """Last observed status for each SDT MAS agent (from Redis / MongoDB)."""
        if mas is None:
            return {"asset_id": config.asset_id, "agents": []}
        statuses = []
        for agent in mas.agents:
            key = f"mas_agent_{agent.agent_name}"
            entry = cache.get_latest_cached(key)
            if not entry:
                mongo_events = doc_store.get_events_by_type(key, n=1)
                if mongo_events:
                    entry = mongo_events[0].get("payload")
            statuses.append(entry or {
                "agent_name": agent.agent_name,
                "domain":     getattr(agent, "domain", "soil"),
                "anomaly":    False,
                "action":     "pending",
                "severity":   "info",
                "detail":     "",
                "ts_s":       None,
            })
        return {"asset_id": config.asset_id, "agents": statuses}

    @router.get("/agents/history")
    async def agents_history(n: int = 60, agent_name: str | None = None):
        """Persisted MAS decision log from MongoDB."""
        agents_list = mas.agents if mas else []
        if agent_name:
            events = doc_store.get_events_by_type(f"mas_agent_{agent_name}", n=n)
        else:
            per = max(n // len(agents_list), 10) if agents_list else n
            all_events: list[dict] = []
            for ag in agents_list:
                all_events.extend(doc_store.get_events_by_type(f"mas_agent_{ag.agent_name}", n=per))
            all_events.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
            events = all_events[:n]
        return {"asset_id": config.asset_id, "count": len(events), "events": events}

    @router.get("/agents/{agent_name}/detail")
    async def agent_detail(agent_name: str):
        """Full in-memory detail snapshot for a specific SDT agent.

        Returns observations, findings, tool calls, and any error from the
        agent's last MAS cycle. Also queryable from MongoDB via /agents/history.
        """
        if mas is None:
            raise HTTPException(503, "MAS not available")
        detail = mas.get_agent_detail(agent_name)
        if not detail:
            raise HTTPException(
                404,
                f"No detail yet for '{agent_name}'. "
                f"Available: {[a.agent_name for a in mas.agents]}"
            )
        return {"asset_id": config.asset_id, **detail}

    @router.post("/agents/{agent_name}/chat")
    async def agent_chat(agent_name: str, req: AgentChatRequest):
        """Ask a specific SDT agent a question without interrupting its loop."""
        if mas is None:
            raise HTTPException(503, "MAS not available")
        response = await mas.ask_agent(agent_name, req.message)
        return {"agent_name": agent_name, "question": req.message, "response": response}

    return router
