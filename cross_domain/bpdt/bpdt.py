"""Biotic Pod Digital Twin (BPDT) — main twin class.

The BPDT is the highest-level decision authority in the soil-plant system.
It extends AbstractDigitalTwin and orchestrates SDT + PDT as subordinates.
BoundaryConditions (BC-01 to BC-05) propagate state between twins.

Full 6-layer architecture:
    L1  TelemetryRouter (aggregated from SDT + PDT)
    L2  SoilPlantCoupledModel + WaterBudgetModel + IrrigationEfficacyModel
    L3  FastAPI REST + SSE
    L4  BioticPodRuleEngine (7-state FSM + PID Kp=15/Ki=0.8/Kd=2.0)
    L5  MultiAgentSystem (5 system-level agents)
    L6  BioticPodOODALoop (10-min OODA, hot-swap support)

BoundaryConditions:
    BC-01: SDT vwc_10cm → PDT vwc_root_zone_10     (direct)
    BC-02: SDT vwc_30cm → PDT vwc_root_zone_30     (direct)
    BC-03: SDT soil_water_potential → PDT root_zone_potential (kPa→MPa /1000)
    BC-04: PDT transpiration_demand → SDT et_demand_input     (direct)
    BC-05: SDT depletion_rate → PDT root_zone_depletion       (direct)

Hot-swap: call hot_swap_twin("sdt"|"pdt", new_twin_instance) for 8-step swap.
"""

from __future__ import annotations

import asyncio
import os
import logging
from dt_forge.core.config import TwinConfig, SensorFieldSpec
from dt_forge.core.base import AbstractDigitalTwin
from dt_forge.data import InfluxAdapter, MongoAdapter, RedisAdapter
from dt_forge.data.writer import TelemetryRouter
from dt_forge.intelligent import MultiAgentSystem
from dt_forge.intelligent.agent import build_llm
from dt_forge.intelligent.knowledge_graph import KnowledgeGraph
from dt_forge.services.ditto.client import DittoClient
from dt_forge.services.ditto.sync import DittoSyncService

from cross_domain.shared.weather_simulator import WeatherSimulator
from cross_domain.shared.actuator_bridge import IrrigationActuatorBridge
from cross_domain.bpdt.reactive_layer import BioticPodRuleEngine
from cross_domain.bpdt.intelligent_layer import (
    SystemIntegrationAgent,
    IrrigationDecisionAgent,
    AnomalyDetectionAgent,
    WaterEfficiencyAgent,
    LedgerAgent,
    bpdt_kg_spec,
)
from cross_domain.bpdt.autonomous_layer import (
    BioticPodOODALoop,
    build_bpdt_planner,
)
from cross_domain.bpdt.api import build_bpdt_router
from cross_domain.sdt.sdt import SoilDigitalTwin, make_sdt_config
from cross_domain.pdt.pdt import PlantDigitalTwin, make_pdt_config

log = logging.getLogger(__name__)

_ENV_FILE = os.path.join(os.path.dirname(__file__), "bpdt.env")

BPDT_SENSOR_FIELDS = [
    SensorFieldSpec(name="vwc_10cm",             unit="m³/m³",    nominal=0.08),
    SensorFieldSpec(name="cwsi",                  unit="",         nominal=0.0),
    SensorFieldSpec(name="leaf_water_potential_mpa", unit="MPa",   nominal=-0.3),
    SensorFieldSpec(name="depletion_rate_avg",    unit="m³/m³/hr", nominal=0.0),
    SensorFieldSpec(name="et_loss_rate",          unit="mm/day",   nominal=2.5),
    SensorFieldSpec(name="irrigation_rate_L_hr",  unit="L/hr",     nominal=0.0),
    SensorFieldSpec(name="yield_penalty_pct",     unit="%",        nominal=0.0),
    SensorFieldSpec(name="sensor_divergence_flag",unit="",         nominal=0.0),
    SensorFieldSpec(name="rain_mm",               unit="mm",       nominal=0.0),
]


def make_bpdt_config() -> TwinConfig:
    return TwinConfig(
        _env_file=_ENV_FILE,
        asset_id="bpdt_component_overseer_001",
        asset_type="biotic_pod",
        asset_name="Biotic Pod Digital Twin — Component Overseer",
        sensor_fields=BPDT_SENSOR_FIELDS,
    )


class BioticPodDigitalTwin(AbstractDigitalTwin):
    """
    Biotic Pod Digital Twin — orchestrates SDT + PDT.

    Key attributes after build_layers():
        sdt      — SoilDigitalTwin
        pdt      — PlantDigitalTwin
        reactive — BioticPodRuleEngine (FSM + PID)
        ooda     — BioticPodOODALoop

    Key methods:
        hot_swap_twin(role, new_twin) — runtime twin swap
        get_irrigation_rate() -> float [L/hr]
        make_api_router() -> APIRouter
    """

    sdt: SoilDigitalTwin
    pdt: PlantDigitalTwin
    reactive: BioticPodRuleEngine
    ooda: BioticPodOODALoop

    def __init__(
        self,
        config: TwinConfig | None = None,
        sdt: SoilDigitalTwin | None = None,
        pdt: PlantDigitalTwin | None = None,
        weather: WeatherSimulator | None = None,
    ):
        cfg = config or make_bpdt_config()
        super().__init__(cfg)
        self._weather = weather or WeatherSimulator()

        # Sub-twins share a weather instance for consistency
        self.sdt = sdt or SoilDigitalTwin(weather=self._weather)
        self.pdt = pdt or PlantDigitalTwin(weather=self._weather)

    def build_layers(self) -> dict:
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        ditto = DittoClient(self.config)

        # ------------------------------------------------------------------
        # L4 — Reactive FSM + PID
        # ------------------------------------------------------------------
        reactive = BioticPodRuleEngine(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            eval_interval=30,   # re-evaluate every 30 s (matches aggregate feed)
        )
        self.reactive = reactive

        # ------------------------------------------------------------------
        # L5 — Multi-Agent System (LLM agents)
        # ------------------------------------------------------------------
        llm = build_llm(self.config)
        kg  = KnowledgeGraph(self.config, bpdt_kg_spec)
        agent_kwargs = dict(
            config=self.config, llm=llm, ditto_client=ditto,
            ts_store=ts, doc_store=doc, knowledge_graph=kg,
            cache=cache,
        )
        mas = MultiAgentSystem(
            self.config, self.bus,
            agents=[
                SystemIntegrationAgent(**agent_kwargs),
                IrrigationDecisionAgent(**agent_kwargs),
                AnomalyDetectionAgent(**agent_kwargs),
                WaterEfficiencyAgent(**agent_kwargs),
                LedgerAgent(**agent_kwargs),
            ],
            monitor_interval=30,    # 30-s cycle — matches sim cadence
            cache=cache,
            doc_store=doc,
        )

        # ------------------------------------------------------------------
        # L6 — Autonomous OODA
        # ------------------------------------------------------------------
        planner = build_bpdt_planner()
        ooda = BioticPodOODALoop(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            ditto_client=ditto,
            models={}, reactive=reactive, mas=mas, connectors=[],
            planner=planner, llm=llm, sdt=self.sdt, pdt=self.pdt, loop_interval=60,
        )
        self.ooda = ooda

        # ------------------------------------------------------------------
        # L1 — Telemetry router (aggregates from sub-twins into BPDT storage)
        # ------------------------------------------------------------------
        router = TelemetryRouter(self.config, self.bus,
                                  ts_store=ts, doc_store=doc, cache=cache)
        self.router = router

        actuator_bridge = IrrigationActuatorBridge(self.config)

        async def _aggregate_feed():
            """Periodically pull from SDT + PDT, propagate BoundaryConditions, and write to BPDT storage.

            Runs every 30 s so that:
              - BC-01..BC-05 (SDT→PDT soil moisture) propagate promptly,
              - BPDT FSM (which reads the cache) sees fresh sub-twin states,
              - BPDT InfluxDB bucket receives aggregated telemetry,
              - PID irrigation output is fed back to the soil simulator,
              - Physical actuator bridge publishes to MQTT if enabled.
            """
            while True:
                try:
                    sdt_r = self.sdt.get_latest_readings()
                    pdt_r = self.pdt.get_latest_readings()

                    # BC-01..BC-05: propagate soil state from SDT → PDT immediately
                    self.pdt.set_soil_inputs(
                        vwc_10cm=sdt_r.get("vwc_10cm"),
                        vwc_30cm=sdt_r.get("vwc_30cm"),
                        root_zone_potential_kpa=sdt_r.get("soil_water_potential_10"),
                    )

                    # Cache sub-twin FSM states so BPDT reactive FSM sees them
                    sdt_state = self.sdt.get_fsm_state()
                    pdt_state = self.pdt.get_fsm_state()
                    cache.set_latest("sdt_fsm_state", sdt_state)
                    cache.set_latest("pdt_fsm_state", pdt_state)
                    reactive.update_sub_twin_states(sdt_state, pdt_state)

                    # Close the irrigation loop: pass PID output to the soil simulator
                    # so that irrigation actually affects the moisture model.
                    irr_rate = reactive.irrigation_rate_L_hr
                    if irr_rate > 0:
                        self.sdt.set_irrigation(irr_rate)
                    else:
                        self.sdt.stop_irrigation()
                    # Physical actuator: publish to MQTT valve controller if enabled
                    actuator_bridge.publish(irr_rate)

                    # Aggregate telemetry into BPDT bucket
                    merged = {}
                    merged.update({k: v for k, v in sdt_r.items()
                                   if k in self.config.field_names})
                    merged.update({k: v for k, v in pdt_r.items()
                                   if k in self.config.field_names})
                    if merged:
                        await router.route(merged)

                except Exception as e:
                    log.debug("BPDT aggregate feed error: %s", e)
                await asyncio.sleep(30)

        self._aggregate_feed = _aggregate_feed

        return {
            "data":        router,
            "services":    DittoSyncService(self.config, self.bus, ts_store=ts,
                                             cache=cache, ditto_client=ditto),
            "reactive":    reactive,
            "intelligent": mas,
            "autonomous":  ooda,
        }

    async def initialise(self) -> None:
        """Initialise BPDT and both sub-twins."""
        await self.sdt.initialise()
        await self.pdt.initialise()
        await super().initialise()

    async def start(self) -> None:
        """Start all layers + both sub-twins + aggregation feed."""
        bpdt_tasks = [
            asyncio.create_task(layer.start(), name=f"bpdt.{name}")
            for name, layer in self.layers.items()
        ]
        sdt_task  = asyncio.create_task(self.sdt.start(), name="sdt.start")
        pdt_task  = asyncio.create_task(self.pdt.start(), name="pdt.start")
        agg_task  = asyncio.create_task(self._aggregate_feed(), name="bpdt.aggregate")

        await asyncio.gather(*bpdt_tasks, sdt_task, pdt_task, agg_task)

    async def stop(self) -> None:
        await self.sdt.stop()
        await self.pdt.stop()
        await super().stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_irrigation_rate(self) -> float:
        """Current PID-computed irrigation rate [L/hr]."""
        return self.reactive.irrigation_rate_L_hr

    def get_system_state(self) -> dict:
        """Combined state snapshot of all three twins."""
        return {
            "bpdt": self.reactive.get_state(),
            "sdt":  self.sdt.get_fsm_state(),
            "pdt":  self.pdt.get_fsm_state(),
            "irrigation_rate_L_hr": self.get_irrigation_rate(),
        }

    async def hot_swap_twin(self, twin_role: str, new_twin) -> bool:
        """Delegate hot-swap to OODA loop (8-step protocol)."""
        return await self.ooda.hot_swap_twin(twin_role, new_twin)

    def make_api_router(self):
        """Return the FastAPI router for all BPDT endpoints."""
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        return build_bpdt_router(
            self.config, ts, doc, cache,
            reactive=self.reactive, ooda=self.ooda,
            sdt=self.sdt, pdt=self.pdt,
        )


# ---------------------------------------------------------------------------
# Entry point (standalone run of all three twins)
# ---------------------------------------------------------------------------

async def main():
    import uvicorn
    from fastapi import FastAPI

    # Single weather instance shared by all three twins
    weather = WeatherSimulator()

    pod = BioticPodDigitalTwin(weather=weather)
    await pod.initialise()

    app = FastAPI(title="Biotic Pod Digital Twin — Component Overseer")

    # Mount all three API routers
    app.include_router(pod.sdt.make_api_router())
    app.include_router(pod.pdt.make_api_router())
    app.include_router(pod.make_api_router())

    server = uvicorn.Server(uvicorn.Config(
        app, host=pod.config.api_host, port=pod.config.api_port, log_level="info"
    ))
    await asyncio.gather(pod.start(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
