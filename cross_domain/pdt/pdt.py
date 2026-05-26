"""Plant Digital Twin (PDT) — main twin class.

Full 6-layer architecture:
    L1  TelemetryRouter (plant sensor → InfluxDB/Redis/Mongo)
    L2  PlantSensorSimulator (P-V + CWSI + SPA physics)
    L3  FastAPI REST + SSE
    L4  PlantRuleEngine (6-state FSM)
    L5  MultiAgentSystem (5 agents)
    L6  PlantOODALoop (30-min OODA, plant-specific decision policy)

Receives soil inputs from SDT via BPDT BoundaryConditions:
    set_soil_inputs(vwc_10cm, vwc_30cm, root_zone_potential_kpa)

Physical sensor connection:
    Set DT_USE_PHYSICAL_SENSORS=true to switch from physics to real MQTT sensors.
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
from cross_domain.pdt.simulator import PlantSensorSimulator
from cross_domain.pdt.reactive_layer import PlantRuleEngine
from cross_domain.pdt.intelligent_layer import (
    StressClassificationAgent,
    WiltingPredictionAgent,
    TranspirationAgent,
    PhotosynthesisAgent,
    YieldPenaltyAgent,
    pdt_kg_spec,
)
from cross_domain.pdt.autonomous_layer import (
    PlantOODALoop,
    build_plant_planner,
)
from cross_domain.pdt.api import build_pdt_router

log = logging.getLogger(__name__)

_ENV_FILE = os.path.join(os.path.dirname(__file__), "pdt.env")

PDT_SENSOR_FIELDS = [
    SensorFieldSpec(name="cwsi",                      unit="",         nominal=0.0,
                    warn_threshold=0.20, crit_threshold=0.70, threshold_direction="high"),
    SensorFieldSpec(name="leaf_water_potential_mpa",   unit="MPa",      nominal=-0.3,
                    warn_threshold=-1.2, crit_threshold=-2.5, threshold_direction="low"),
    SensorFieldSpec(name="stomatal_conductance_mmol",  unit="mmol/m²/s",nominal=260.0,
                    warn_threshold=195.0, crit_threshold=104.0, threshold_direction="low"),
    SensorFieldSpec(name="canopy_temp_c",              unit="°C",       nominal=28.0),
    SensorFieldSpec(name="canopy_air_delta_c",         unit="°C",       nominal=-0.2),
    SensorFieldSpec(name="sap_flow_L_hr",              unit="L/hr",     nominal=0.25),
    SensorFieldSpec(name="relative_water_content",     unit="",         nominal=0.95,
                    warn_threshold=0.90, crit_threshold=0.80, threshold_direction="low"),
    SensorFieldSpec(name="yield_penalty_pct",          unit="%",        nominal=0.0,
                    warn_threshold=10.0, crit_threshold=25.0, threshold_direction="high"),
    SensorFieldSpec(name="stress_duration_days",       unit="days",     nominal=0.0),
    SensorFieldSpec(name="time_to_wilting_h",          unit="h",        nominal=None,
                    warn_threshold=12.0, crit_threshold=4.0, threshold_direction="low"),
    SensorFieldSpec(name="eta_fraction",               unit="",         nominal=1.0,
                    warn_threshold=0.70, crit_threshold=0.40, threshold_direction="low"),
    SensorFieldSpec(name="t_air_c",                    unit="°C",       nominal=28.0),
    SensorFieldSpec(name="vpd_kpa",                    unit="kPa",      nominal=1.5),
    SensorFieldSpec(name="vwc_root_zone",              unit="m³/m³",    nominal=0.07),
]


def make_pdt_config() -> TwinConfig:
    return TwinConfig(
        _env_file=_ENV_FILE,
        asset_id="pdt_maize_drought_001",
        asset_type="plant",
        asset_name="Plant Digital Twin — Maize Drought Stress",
        sensor_fields=PDT_SENSOR_FIELDS,
    )


class PlantDigitalTwin(AbstractDigitalTwin):
    """
    Plant Digital Twin — drought stress monitoring for maize V6-V8.

    Key public attributes (set after build_layers):
        simulator — PlantSensorSimulator
        reactive  — PlantRuleEngine (FSM state)
        ooda      — PlantOODALoop

    External API:
        set_soil_inputs(vwc_10cm, vwc_30cm, potential_kpa) — from BPDT BC
        get_latest_readings() -> dict
        get_fsm_state() -> str
    """

    simulator: PlantSensorSimulator
    reactive: PlantRuleEngine
    ooda: PlantOODALoop

    def __init__(
        self,
        config: TwinConfig | None = None,
        weather: WeatherSimulator | None = None,
    ):
        cfg = config or make_pdt_config()
        super().__init__(cfg)
        self._weather = weather or WeatherSimulator()

    def build_layers(self) -> dict:
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        ditto = DittoClient(self.config)

        use_physical = os.getenv("DT_USE_PHYSICAL_SENSORS", "false").lower() == "true"
        simulator = PlantSensorSimulator(
            self.config,
            publish_interval_s=1800.0,
            use_physical_sensors=use_physical,
            weather=self._weather,
        )
        self.simulator = simulator

        reactive = PlantRuleEngine(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            eval_interval=30,   # matches sim step cadence (30 s wall = 1800 s physics)
        )
        self.reactive = reactive

        llm = build_llm(self.config)
        kg  = KnowledgeGraph(self.config, pdt_kg_spec)
        agent_kwargs = dict(
            config=self.config, llm=llm, ditto_client=ditto,
            ts_store=ts, doc_store=doc, knowledge_graph=kg,
        )
        mas = MultiAgentSystem(
            self.config, self.bus,
            agents=[
                StressClassificationAgent(**agent_kwargs),
                WiltingPredictionAgent(**agent_kwargs),
                TranspirationAgent(**agent_kwargs),
                PhotosynthesisAgent(**agent_kwargs),
                YieldPenaltyAgent(**agent_kwargs),
            ],
            monitor_interval=60,    # 1-min MAS cycle
            cache=cache,
            doc_store=doc,
        )
        self.mas = mas

        planner = build_plant_planner()
        ooda = PlantOODALoop(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            ditto_client=ditto,
            models={}, reactive=reactive, mas=mas, connectors=[],
            planner=planner, simulator=simulator, loop_interval=60,   # 1-min OODA cycle
        )
        self.ooda = ooda

        router = TelemetryRouter(self.config, self.bus,
                                  ts_store=ts, doc_store=doc, cache=cache)
        self.router = router

        async def _sim_feed_loop():
            while True:
                # Physics dt stays 1800 s; wall-clock sleep is 30 s → 60× time compression.
                readings = simulator.step_once()
                await router.route(readings)
                await asyncio.sleep(30)

        self._sim_feed = _sim_feed_loop

        return {
            "data":        router,
            "services":    DittoSyncService(self.config, self.bus, ts_store=ts,
                                             cache=cache, ditto_client=ditto),
            "reactive":    reactive,
            "intelligent": mas,
            "autonomous":  ooda,
        }

    async def start(self) -> None:
        tasks = [
            asyncio.create_task(layer.start(), name=name)
            for name, layer in self.layers.items()
        ]
        sim_task = asyncio.create_task(self._sim_feed(), name="sim_feed")
        await asyncio.gather(*tasks, sim_task)

    def set_soil_inputs(
        self,
        vwc_10cm: float | None = None,
        vwc_30cm: float | None = None,
        root_zone_potential_kpa: float | None = None,
    ) -> None:
        """Inject soil boundary conditions from SDT (via BPDT)."""
        self.simulator.set_soil_inputs(vwc_10cm, vwc_30cm, root_zone_potential_kpa)

    def get_latest_readings(self) -> dict:
        return self.simulator.get_latest()

    def get_fsm_state(self) -> str:
        return self.reactive.get_state()

    def make_api_router(self):
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        return build_pdt_router(
            self.config, ts, doc, cache,
            simulator=self.simulator, reactive=self.reactive,
            mas=self.mas,
        )


async def main():
    import uvicorn
    from fastapi import FastAPI

    twin = PlantDigitalTwin()
    await twin.initialise()

    app = FastAPI(title=twin.config.asset_name)
    app.include_router(twin.make_api_router())

    server = uvicorn.Server(uvicorn.Config(
        app, host=twin.config.api_host, port=twin.config.api_port, log_level="info"
    ))
    await asyncio.gather(twin.start(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
