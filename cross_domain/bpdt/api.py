"""BPDT Services Layer — FastAPI REST endpoints.

Endpoints (all under /api/bpdt):
    GET  /status                — pod FSM state + sub-twin states (SVC-F-01)
    GET  /irrigation/history    — recent irrigation events (SVC-F-02)
    POST /irrigation/trigger    — manually trigger irrigation (SVC-F-03)
    POST /irrigation/stop       — stop irrigation (SVC-F-04)
    GET  /water-budget          — rolling water balance (SVC-F-05)
    GET  /ledger                — cumulative water use (SVC-F-06)
    POST /config/swap           — initiate hot-swap (SVC-F-07)
    GET  /config/current        — current PodConfiguration (SVC-F-08)
    GET  /stream                — SSE live system telemetry (SVC-F-09)
    GET  /analytics/wue         — water use efficiency report (SVC-F-10)
    GET  /agents/status         — last status for each MAS agent (SVC-F-11)
    GET  /agents/history        — full persisted MAS decision log (SVC-F-12)
    GET  /ooda/status           — last completed OODA cycle decision (SVC-F-13)
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


class IrrigationTriggerRequest(BaseModel):
    rate_L_hr: float = 5.0
    duration_min: float = 60.0
    reason: str = "manual_api_trigger"


class HotSwapRequest(BaseModel):
    twin_role: str   # "sdt" or "pdt"
    new_config_id: str
    warm_up_steps: int = 6


class AgentChatRequest(BaseModel):
    message: str


def build_bpdt_router(
    config: "TwinConfig",
    ts_store: "TimeSeriesStore",
    doc_store: "DocumentStore",
    cache: "CacheStore",
    reactive=None,     # BioticPodRuleEngine
    ooda=None,         # BioticPodOODALoop
    sdt=None,
    pdt=None,
) -> APIRouter:

    router = APIRouter(prefix="/api/bpdt", tags=["Biotic Pod Digital Twin"])

    def _latest(field: str) -> float | None:
        try:
            return ts_store.get_latest(field)
        except Exception:
            return None

    def _bpdt_state() -> str:
        try:
            return reactive.get_state() if reactive else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def _sdt_state() -> str:
        try:
            return sdt.get_fsm_state() if sdt else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def _pdt_state() -> str:
        try:
            return pdt.get_fsm_state() if pdt else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    @router.get("/status")
    async def pod_status():
        """Full pod status: BPDT FSM + both sub-twin states + key metrics."""
        irr_rate = cache.get_latest_cached("irrigation_rate_L_hr") or 0.0
        return {
            "asset_id":        config.asset_id,
            "bpdt_fsm_state":  _bpdt_state(),
            "sdt_fsm_state":   _sdt_state(),
            "pdt_fsm_state":   _pdt_state(),
            "irrigation_active": _bpdt_state() == "IRRIGATION_ACTIVE",
            "irrigation_rate_L_hr": irr_rate,
            "vwc_10cm":        _latest("vwc_10cm"),
            "cwsi":            _latest("cwsi"),
            "depletion_rate":  _latest("depletion_rate_avg"),
            "et_loss_rate":    _latest("et_loss_rate"),
            "yield_penalty_pct": _latest("yield_penalty_pct"),
            "pid_config": {
                "kp": 15.0, "ki": 0.8, "kd": 2.0,
                "setpoint_m3m3": 0.08, "output_range_L_hr": [0.0, 20.0],
                "active": _bpdt_state() == "IRRIGATION_ACTIVE",
            },
        }

    @router.get("/irrigation/history")
    async def irrigation_history(n: int = 30):
        """Recent irrigation events (start, stop, emergency)."""
        events = doc_store.get_recent_events(n)
        irr_events = [
            e for e in events
            if any(k in e.get("event_type", "") for k in
                   ["irrigation", "emergency", "advisory"])
        ]
        return {
            "asset_id": config.asset_id,
            "count":    len(irr_events),
            "events":   irr_events,
        }

    @router.post("/irrigation/trigger")
    async def trigger_irrigation(req: IrrigationTriggerRequest):
        """Manually start irrigation at specified rate."""
        if reactive is None:
            raise HTTPException(503, "Reactive layer not available")
        if sdt is not None:
            sdt.set_irrigation(req.rate_L_hr)
        reactive.signal_ooda_decision("irrigate")
        reactive.confirm_valve_open(True)
        doc_store.log_event(
            "irrigation_manual_trigger",
            {"rate_L_hr": req.rate_L_hr, "reason": req.reason},
            severity="info",
        )
        return {
            "status": "irrigation_started",
            "rate_L_hr": req.rate_L_hr,
            "duration_min": req.duration_min,
            "reason": req.reason,
        }

    @router.post("/irrigation/stop")
    async def stop_irrigation():
        """Stop irrigation and return to NOMINAL state."""
        if reactive is None:
            raise HTTPException(503, "Reactive layer not available")
        reactive.signal_ooda_decision("no_action")
        reactive.confirm_valve_open(False)
        if sdt is not None:
            sdt.stop_irrigation()
        doc_store.log_event("irrigation_manual_stop", {}, severity="info")
        return {"status": "irrigation_stopped"}

    @router.get("/water-budget")
    async def water_budget():
        """Rolling 7-day water balance summary."""
        return {
            "asset_id": config.asset_id,
            "rolling_7d_balance_mm": cache.get_latest_cached("rolling_7d_balance_mm"),
            "total_irrigation_mm":   cache.get_latest_cached("total_irrigation_mm"),
            "et_loss_rate_mm_day":   _latest("et_loss_rate"),
            "rain_mm_today":         _latest("rain_mm"),
            "irrigation_rate_L_hr":  cache.get_latest_cached("irrigation_rate_L_hr"),
            "field_area_m2": 1.0,
            "method": "FAO-56 water balance: ΔS = Rain + Irr − ET − Drainage",
        }

    @router.get("/ledger")
    async def water_ledger():
        """Cumulative water use ledger."""
        events = doc_store.get_recent_events(100)
        irr_starts = sum(
            1 for e in events if "irrigation_started" in e.get("event_type", "")
        )
        return {
            "asset_id": config.asset_id,
            "irrigation_sessions": irr_starts,
            "total_water_L": cache.get_latest_cached("total_irrigation_L_session") or 0.0,
            "current_rate_L_hr": cache.get_latest_cached("irrigation_rate_L_hr") or 0.0,
            "water_productivity_kg_m3": cache.get_latest_cached("water_productivity_kg_m3"),
        }

    @router.get("/config/current")
    async def config_current():
        """Current PodConfiguration: active SDT/PDT IDs and versions (SVC-F-08)."""
        return {
            "asset_id":    config.asset_id,
            "pod_config": {
                "sdt_id":   sdt.config.asset_id   if sdt  else "unknown",
                "pdt_id":   pdt.config.asset_id   if pdt  else "unknown",
                "sdt_type": sdt.config.asset_type  if sdt  else "unknown",
                "pdt_type": pdt.config.asset_type  if pdt  else "unknown",
                "sdt_state": _sdt_state(),
                "pdt_state": _pdt_state(),
            },
            "soil_type":         "sandy_loam",
            "crop":              "Maize (Zea mays L.) V6-V8",
            "pid_setpoint_m3m3": 0.08,
            "version":           "1.0.0",
            "hot_swap_supported": True,
            "boundary_conditions": ["BC-01", "BC-02", "BC-03", "BC-04", "BC-05"],
        }

    @router.get("/analytics/wue")
    async def water_use_efficiency():
        """Water use efficiency vs rule-based baseline (SVC-F-10)."""
        total_irr_L      = cache.get_latest_cached("total_irrigation_L_session") or 0.0
        yield_penalty    = _latest("yield_penalty_pct") or 0.0
        wpi              = cache.get_latest_cached("water_productivity_kg_m3")
        return {
            "asset_id": config.asset_id,
            "wue": {
                "total_irrigation_applied_L": total_irr_L,
                "yield_penalty_pct":          yield_penalty,
                "water_productivity_kg_m3":   wpi,
                "target_water_saving_pct":    30.0,
                "rule_based_baseline_note": (
                    "Baseline is a fixed-schedule 2.5 mm/day drip equivalent. "
                    "Savings computed post-season from ProvenanceLog."
                ),
            },
            "reference": "AquaCrop Ya/Ymax = 1 - Ky*(1 - ETa/ETc); Hsiao et al. (2009)",
        }

    @router.get("/agents/status")
    async def agents_status():
        """Last observed status for each MAS agent.

        Reads Redis first (fast path — current in-memory state).  If a Redis
        entry is absent (e.g. after a restart), falls back to the most recent
        persisted record from MongoDB so the panel is never blank.

        Each entry has: agent_name, domain, anomaly, action, severity, detail, ts_s.
        """
        if ooda is None or ooda.mas is None:
            return {"asset_id": config.asset_id, "agents": []}
        statuses = []
        for agent in ooda.mas.agents:
            key = f"mas_agent_{agent.agent_name}"
            entry = cache.get_latest_cached(key)
            if not entry:
                # Fall back to most recent persisted MongoDB record
                mongo_events = doc_store.get_events_by_type(key, n=1)
                if mongo_events:
                    entry = mongo_events[0].get("payload")
            statuses.append(entry or {
                "agent_name": agent.agent_name,
                "domain":     getattr(agent, "domain", ""),
                "anomaly":    False,
                "action":     "pending",
                "severity":   "info",
                "detail":     "",
                "ts_s":       None,
            })
        return {"asset_id": config.asset_id, "agents": statuses}

    @router.get("/agents/history")
    async def agents_history(
        n: int = 100,
        agent_name: str | None = None,
    ):
        """Full persisted MAS decision log from MongoDB.

        Query params:
            n          — max records to return (default 100, across all agents)
            agent_name — filter to a single agent (e.g. IrrigationDecisionAgent)

        Each record has the same fields as /agents/status plus a ``timestamp``
        field (ISO-8601 UTC) written by MongoDB.
        """
        if agent_name:
            events = doc_store.get_events_by_type(f"mas_agent_{agent_name}", n=n)
        else:
            agents = ooda.mas.agents if (ooda and ooda.mas) else []
            per_agent = max(n // len(agents), 10) if agents else n
            all_events: list[dict] = []
            for agent in agents:
                all_events.extend(
                    doc_store.get_events_by_type(f"mas_agent_{agent.agent_name}", n=per_agent)
                )
            all_events.sort(
                key=lambda e: str(e.get("timestamp", "")), reverse=True
            )
            events = all_events[:n]

        return {
            "asset_id": config.asset_id,
            "count":    len(events),
            "events":   events,
        }

    @router.get("/ooda/status")
    async def ooda_status():
        """Last completed OODA cycle decision, read from Redis cache."""
        last = cache.get_latest_cached("ooda_last_cycle")
        return {
            "asset_id": config.asset_id,
            "last_cycle": last or {
                "action":     "pending",
                "reason":     "",
                "risk_level": "",
                "ts_s":       None,
            },
        }

    @router.get("/ooda/overseer/latest")
    async def ooda_overseer_latest():
        """Latest AutonomousOverseer decision summary, read from Redis cache.

        Returns the most recent OverseerDecision with a reasoning excerpt,
        goals addressed, agent queries count, and timestamp.
        Falls back to a 'not yet available' response on a fresh start.
        """
        latest = cache.get_latest_cached("ooda_overseer_latest")
        return {
            "asset_id": config.asset_id,
            "overseer_latest": latest or {
                "action":               "pending",
                "risk_level":           "",
                "reasoning":            "No overseer decision recorded yet.",
                "goals_addressed":      [],
                "agent_queries_count":  0,
                "overseer_driven":      False,
                "ts_s":                 None,
            },
        }

    @router.get("/agents/{agent_name}/detail")
    async def agent_detail(agent_name: str):
        """Full detail for a specific MAS agent: observations, findings, tool calls, errors.

        Returns the in-memory snapshot captured after the agent's last MAS cycle.
        agent_name values: system_integration | irrigation_decision | anomaly_detection
                           | water_efficiency | ledger
        """
        if ooda is None or ooda.mas is None:
            raise HTTPException(503, "MAS not available")
        detail = ooda.mas.get_agent_detail(agent_name)
        if not detail:
            raise HTTPException(
                404,
                f"No detail yet for '{agent_name}'. "
                f"Available agents: {[a.agent_name for a in ooda.mas.agents]}"
            )
        return {"asset_id": config.asset_id, **detail}

    @router.post("/agents/{agent_name}/chat")
    async def agent_chat(agent_name: str, req: AgentChatRequest):
        """Ask a specific agent a question without interrupting its autonomous loop.

        The agent uses a separate LangChain invocation so its MAS monitoring
        cycle continues unaffected.  Supports all BPDT agent names.
        """
        if ooda is None or ooda.mas is None:
            raise HTTPException(503, "MAS not available")
        response = await ooda.mas.ask_agent(agent_name, req.message)
        return {
            "agent_name": agent_name,
            "question":   req.message,
            "response":   response,
        }

    @router.post("/config/swap")
    async def config_swap(req: HotSwapRequest):
        """Initiate hot-swap of a sub-twin (SDT or PDT)."""
        if ooda is None:
            raise HTTPException(503, "OODA loop not available")
        if req.twin_role not in ("sdt", "pdt"):
            raise HTTPException(400, "twin_role must be 'sdt' or 'pdt'")
        doc_store.log_event(
            "hot_swap_initiated",
            {"twin_role": req.twin_role, "new_config_id": req.new_config_id},
            severity="warning",
        )
        return {
            "status": "hot_swap_initiated",
            "twin_role": req.twin_role,
            "new_config_id": req.new_config_id,
            "message": "Hot-swap queued. Monitor /status for MAINTENANCE → NOMINAL transition.",
        }

    @router.get("/stream")
    async def system_stream():
        """SSE — streams pod status every 10 seconds."""

        async def _generator() -> AsyncIterator[str]:
            while True:
                irr = cache.get_latest_cached("irrigation_rate_L_hr") or 0.0
                # Read from sub-twin in-memory state (always fresh from startup).
                # InfluxDB is only written after the first simulator step (~15 min),
                # so _latest() would return nulls until then.
                sdt_r = sdt.get_latest_readings() if sdt else {}
                pdt_r = pdt.get_latest_readings() if pdt else {}
                data = {
                    "bpdt_state":   _bpdt_state(),
                    "sdt_state":    _sdt_state(),
                    "pdt_state":    _pdt_state(),
                    "vwc_10cm":     sdt_r.get("vwc_10cm"),
                    "cwsi":         pdt_r.get("cwsi"),
                    "depl_rate":    sdt_r.get("depletion_rate_avg"),
                    "irr_rate_L_hr": irr,
                    "yield_penalty": pdt_r.get("yield_penalty_pct"),
                    "et_rate":      sdt_r.get("et_loss_rate"),
                }
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(10)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
