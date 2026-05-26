"""PDT Services Layer — FastAPI REST endpoints.

Endpoints (all under /api/pdt):
    GET  /stress/current        — composite stress state and all indicators
    GET  /history/stress        — recent stress event log (SVC-F-05)
    GET  /transpiration         — sap flow, gs, ETa/ETc analysis
    GET  /wilting/forecast      — time-to-wilting estimate
    GET  /yield/penalty         — cumulative AquaCrop yield penalty
    GET  /state                 — current FSM state
    GET  /stream                — SSE live telemetry
    POST /physiologist/score    — submit manual field observation
    GET  /telemetry/history     — historical multi-field time-series for charts
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


class PhysiologistScore(BaseModel):
    """Manual field observation submitted by an agronomist."""
    observer: str
    visual_stress_level: str    # "none" | "mild" | "moderate" | "severe" | "wilted"
    notes: str = ""
    field_vwc_estimate: float | None = None
    leaf_roll_observed: bool = False


def build_pdt_router(
    config: "TwinConfig",
    ts_store: "TimeSeriesStore",
    doc_store: "DocumentStore",
    cache: "CacheStore",
    simulator=None,
    reactive=None,
    mas=None,          # MultiAgentSystem — for /agents/status
) -> APIRouter:

    router = APIRouter(prefix="/api/pdt", tags=["Plant Digital Twin"])

    def _latest(field: str) -> float | None:
        try:
            return ts_store.get_latest(field)
        except Exception:
            return None

    @router.get("/stress/current")
    async def stress_current():
        """Composite drought stress state and all physiological indicators."""
        cwsi    = _latest("cwsi")
        psi     = _latest("leaf_water_potential_mpa")
        gs      = _latest("stomatal_conductance_mmol")
        rwc     = _latest("relative_water_content")
        sap     = _latest("sap_flow_L_hr")

        # Composite stress score
        scores = []
        if cwsi is not None: scores.append(cwsi)
        if psi  is not None: scores.append(min(1.0, abs(psi) / 2.5))
        if gs   is not None: scores.append(1.0 - min(1.0, gs / 260.0))
        composite = sum(scores) / len(scores) if scores else 0.0

        state = "UNKNOWN"
        try:
            if reactive: state = reactive.get_state()
        except Exception:
            pass

        return {
            "asset_id": config.asset_id,
            "fsm_state": state,
            "composite_stress_score": round(composite, 4),
            "cwsi": cwsi,
            "leaf_water_potential_mpa": psi,
            "stomatal_conductance_mmol": gs,
            "relative_water_content": rwc,
            "sap_flow_L_hr": sap,
            "stress_duration_days": _latest("stress_duration_days"),
            "yield_penalty_pct": _latest("yield_penalty_pct"),
            "thresholds": {
                "cwsi":  {"mild": 0.20, "moderate": 0.45, "severe": 0.70},
                "psi":   {"mild": -0.8, "moderate": -1.2, "severe": -1.8, "wilting": -2.5},
                "gs_fraction_of_nominal": {"mild": 0.75, "severe": 0.40, "nominal_mmol": 260},
            },
        }

    @router.get("/history/stress")
    async def stress_history(n: int = 50):
        """Recent stress events and FSM transitions log (SVC-F-05)."""
        events = doc_store.get_recent_events(n)
        stress_events = [
            e for e in events
            if any(k in e.get("event_type", "")
                   for k in ["stress", "fsm", "wilting", "recovery", "advisory", "observation"])
        ]
        return {
            "asset_id": config.asset_id,
            "count":    len(stress_events),
            "events":   stress_events,
        }

    @router.get("/transpiration")
    async def transpiration():
        """Sap flow, stomatal conductance, and ETa/ETc water-use analysis."""
        return {
            "asset_id": config.asset_id,
            "sap_flow_L_hr_per_plant": _latest("sap_flow_L_hr"),
            "stomatal_conductance_mmol_m2_s": _latest("stomatal_conductance_mmol"),
            "eta_fraction": _latest("eta_fraction"),
            "canopy_temp_c": _latest("canopy_temp_c"),
            "canopy_air_delta_c": _latest("canopy_air_delta_c"),
            "t_air_c": _latest("t_air_c"),
            "vpd_kpa": _latest("vpd_kpa"),
            "model": "SPA-style canopy transpiration (Williams et al. 1996)",
            "crop_stage": "Maize V6-V8",
            "kc": 1.10,
        }

    @router.get("/wilting/forecast")
    async def wilting_forecast():
        """Time-to-wilting estimate from leaf water potential trajectory."""
        psi = _latest("leaf_water_potential_mpa")
        try:
            df = ts_store.query_recent("leaf_water_potential_mpa", minutes=120)
            if df is not None and len(df) >= 2:
                vals = df["value"].tolist() if hasattr(df, "value") else list(df)
                slope_mpa_hr = (vals[-1] - vals[0]) / 2.0
            else:
                slope_mpa_hr = -0.03
        except Exception:
            slope_mpa_hr = -0.03

        ttp = None
        if psi is not None and slope_mpa_hr < -1e-4:
            ttp = max(0.0, (psi - (-2.5)) / abs(slope_mpa_hr))

        return {
            "asset_id": config.asset_id,
            "psi_leaf_current_mpa": psi,
            "psi_decline_rate_mpa_hr": slope_mpa_hr,
            "time_to_wilting_hr": round(ttp, 1) if ttp is not None else None,
            "wilting_threshold_mpa": -2.50,
            "reference": "Westgate & Boyer (1985) Plant Physiol.",
        }

    @router.get("/yield/penalty")
    async def yield_penalty():
        """Cumulative AquaCrop yield penalty estimate."""
        penalty = _latest("yield_penalty_pct")
        eta     = _latest("eta_fraction")
        return {
            "asset_id": config.asset_id,
            "yield_penalty_pct": penalty,
            "eta_fraction_current": eta,
            "ya_ymax_fraction": round(1.0 - (penalty or 0.0) / 100.0, 4),
            "ky": 1.25,
            "model": "AquaCrop Ya/Ymax = 1 - Ky × (1 - ETa/ETc)",
            "reference": "Hsiao et al. (2009) Agron. J.",
            "crop": "Maize (Zea mays L.) V6-V8",
        }

    @router.get("/state")
    async def current_state():
        state = "UNKNOWN"
        try:
            if reactive: state = reactive.get_state()
        except Exception:
            pass
        return {"asset_id": config.asset_id, "fsm_state": state}

    @router.get("/stream")
    async def telemetry_stream():
        """SSE endpoint — streams plant stress readings every 30 seconds."""

        async def _generator() -> AsyncIterator[str]:
            fields = [
                "cwsi", "leaf_water_potential_mpa", "stomatal_conductance_mmol",
                "sap_flow_L_hr", "relative_water_content", "eta_fraction",
                "yield_penalty_pct", "canopy_temp_c",
            ]
            while True:
                # Prefer simulator in-memory state — always populated from startup.
                # InfluxDB is only written after the first 1800 s step.
                if simulator is not None:
                    sim_data = simulator.get_latest()
                    data = {f: sim_data.get(f) for f in fields}
                else:
                    data = {f: _latest(f) for f in fields}
                try:
                    if reactive: data["fsm_state"] = reactive.get_state()
                except Exception:
                    pass
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(10)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/telemetry/history")
    async def telemetry_history(
        minutes: int = 120,
        fields: str = "cwsi,leaf_water_potential_mpa,stomatal_conductance_mmol,yield_penalty_pct",
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

    @router.post("/physiologist/score")
    async def submit_physiologist_score(score: PhysiologistScore):
        """Log a manual agronomist field observation alongside sensor data."""
        doc_store.log_event(
            "physiologist_observation",
            {
                "observer":            score.observer,
                "visual_stress_level": score.visual_stress_level,
                "notes":               score.notes,
                "field_vwc_estimate":  score.field_vwc_estimate,
                "leaf_roll_observed":  score.leaf_roll_observed,
                "sensor_cwsi":         _latest("cwsi"),
                "sensor_psi":          _latest("leaf_water_potential_mpa"),
            },
            severity="info",
        )
        return {"status": "logged", "asset_id": config.asset_id}

    # ------------------------------------------------------------------
    # GET /agents/status
    # ------------------------------------------------------------------
    @router.get("/agents/status")
    async def agents_status():
        """Last observed status for each PDT MAS agent (from Redis / MongoDB)."""
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
                "domain":     getattr(agent, "domain", "plant"),
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
        """Full in-memory detail snapshot for a specific PDT agent.

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
        """Ask a specific PDT agent a question without interrupting its loop."""
        if mas is None:
            raise HTTPException(503, "MAS not available")
        response = await mas.ask_agent(agent_name, req.message)
        return {"agent_name": agent_name, "question": req.message, "response": response}

    return router
