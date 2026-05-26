"""Soil Digital Twin (SDT) — main twin class.

Full 6-layer architecture:
    L1  TelemetryRouter (sensor → InfluxDB/Redis/Mongo)
    L2  SoilSensorSimulator (Richards + ET physics)
    L3  FastAPI REST + SSE
    L4  SoilRuleEngine (5-state FSM)
    L5  MultiAgentSystem (4 agents: moisture/validation/hydraulic/ET)
    L6  SoilOODALoop (15-min OODA, soil-specific decision policy)

Physical twin connection:
    Set DT_USE_PHYSICAL_SENSORS=true to route data from real capacitive
    sensors (e.g. TEROS-12) via MQTT instead of the Richards equation simulator.

References:
    van Genuchten (1980) — soil retention
    Allen et al. (1998) FAO-56 — ET
    Richards (1931) — soil water dynamics
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
from cross_domain.sdt.simulator import SoilSensorSimulator
from cross_domain.sdt.reactive_layer import SoilRuleEngine
from cross_domain.sdt.intelligent_layer import (
    MoistureDepletionAgent,
    SensorValidationAgent,
    HydraulicPropertyAgent,
    ETForecastAgent,
    sdt_kg_spec,
)
from cross_domain.sdt.autonomous_layer import (
    SoilOODALoop,
    build_soil_planner,
)
from cross_domain.sdt.api import build_sdt_router

log = logging.getLogger(__name__)

_ENV_FILE = os.path.join(os.path.dirname(__file__), "sdt.env")

# ---------------------------------------------------------------------------
# Sensor field specifications
# ---------------------------------------------------------------------------

SDT_SENSOR_FIELDS = [
    SensorFieldSpec(name="vwc_10cm",  unit="m³/m³", nominal=0.08,
                    warn_threshold=0.045, crit_threshold=0.030,
                    threshold_direction="low"),
    SensorFieldSpec(name="vwc_30cm",  unit="m³/m³", nominal=0.07,
                    warn_threshold=0.042, crit_threshold=0.028,
                    threshold_direction="low"),
    SensorFieldSpec(name="vwc_60cm",  unit="m³/m³", nominal=0.07,
                    warn_threshold=0.038, crit_threshold=0.025,
                    threshold_direction="low"),
    SensorFieldSpec(name="soil_temp_10cm", unit="°C",    nominal=25.0),
    SensorFieldSpec(name="soil_temp_30cm", unit="°C",    nominal=22.0),
    SensorFieldSpec(name="soil_water_potential_10", unit="kPa", nominal=-30.0,
                    warn_threshold=-50.0, crit_threshold=-80.0,
                    threshold_direction="low"),
    SensorFieldSpec(name="soil_water_potential_30", unit="kPa", nominal=-25.0),
    SensorFieldSpec(name="depletion_rate_avg", unit="m³/m³/hr", nominal=0.0,
                    warn_threshold=0.0015, crit_threshold=0.002,
                    threshold_direction="high"),
    SensorFieldSpec(name="et_loss_rate",   unit="mm/day", nominal=2.5,
                    warn_threshold=3.5, crit_threshold=5.0,
                    threshold_direction="high"),
    SensorFieldSpec(name="t_air_c",        unit="°C",    nominal=28.0),
    SensorFieldSpec(name="vpd_kpa",        unit="kPa",   nominal=1.5),
    SensorFieldSpec(name="rain_mm",        unit="mm",    nominal=0.0),
    SensorFieldSpec(name="sensor_divergence_flag", unit="dimensionless", nominal=0.0,
                    warn_threshold=0.03, crit_threshold=0.05,
                    threshold_direction="high"),
]


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------

def make_sdt_config() -> TwinConfig:
    return TwinConfig(
        _env_file=_ENV_FILE,
        asset_id="sdt_water_depletion_001",
        asset_type="soil",
        asset_name="Soil Digital Twin — Water Depletion",
        sensor_fields=SDT_SENSOR_FIELDS,
    )


# ---------------------------------------------------------------------------
# SoilDigitalTwin
# ---------------------------------------------------------------------------

class SoilDigitalTwin(AbstractDigitalTwin):
    """
    Soil Digital Twin — physics-grounded moisture monitoring and irrigation advisory.

    Key public attributes set after build_layers():
        simulator    — SoilSensorSimulator for live readings
        reactive     — SoilRuleEngine for FSM state queries
        ooda         — SoilOODALoop for autonomous decisions
        router       — TelemetryRouter for data ingestion

    To enable physical sensors: set DT_USE_PHYSICAL_SENSORS=true in sdt.env.
    To set irrigation rate externally: call set_irrigation(rate_L_hr).
    """

    # Exposed after build_layers
    simulator: SoilSensorSimulator
    reactive: SoilRuleEngine
    ooda: SoilOODALoop
    router: TelemetryRouter

    def __init__(
        self,
        config: TwinConfig | None = None,
        weather: WeatherSimulator | None = None,
    ):
        cfg = config or make_sdt_config()
        super().__init__(cfg)
        self._weather = weather or WeatherSimulator()

    def build_layers(self) -> dict:
        # ------------------------------------------------------------------
        # Infrastructure adapters
        # ------------------------------------------------------------------
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        ditto = DittoClient(self.config)

        # ------------------------------------------------------------------
        # L2 — Physics simulator (Richards equation)
        # ------------------------------------------------------------------
        use_physical = os.getenv("DT_USE_PHYSICAL_SENSORS", "false").lower() == "true"
        simulator = SoilSensorSimulator(
            self.config,
            publish_interval_s=900.0,
            use_physical_sensors=use_physical,
            weather=self._weather,
        )
        self.simulator = simulator

        # ------------------------------------------------------------------
        # L4 — Reactive FSM
        # ------------------------------------------------------------------
        reactive = SoilRuleEngine(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            eval_interval=30,   # matches sim step cadence (30 s wall = 900 s physics)
        )
        self.reactive = reactive

        # ------------------------------------------------------------------
        # L5 — Multi-Agent System (LLM agents)
        # ------------------------------------------------------------------
        llm = build_llm(self.config)
        kg  = KnowledgeGraph(self.config, sdt_kg_spec)
        agent_kwargs = dict(
            config=self.config, llm=llm, ditto_client=ditto,
            ts_store=ts, doc_store=doc, knowledge_graph=kg,
        )
        mas = MultiAgentSystem(
            self.config, self.bus,
            agents=[
                MoistureDepletionAgent(**agent_kwargs),
                SensorValidationAgent(**agent_kwargs),
                HydraulicPropertyAgent(**agent_kwargs),
                ETForecastAgent(**agent_kwargs),
            ],
            monitor_interval=60,    # 1-min MAS cycle
            cache=cache,
            doc_store=doc,
        )
        self.mas = mas

        # ------------------------------------------------------------------
        # L6 — Autonomous OODA loop
        # ------------------------------------------------------------------
        planner = build_soil_planner()
        ooda = SoilOODALoop(
            self.config, self.bus,
            ts_store=ts, cache=cache, doc_store=doc,
            ditto_client=ditto,
            models={},
            reactive=reactive, mas=mas,
            connectors=[],
            planner=planner,
            simulator=simulator,
            loop_interval=60,   # 1-min OODA cycle
        )
        self.ooda = ooda

        # ------------------------------------------------------------------
        # L1 — Data router (includes simulator data feed as inner task)
        # ------------------------------------------------------------------
        router = TelemetryRouter(self.config, self.bus,
                                  ts_store=ts, doc_store=doc, cache=cache)
        self.router = router

        # Subscribe: when simulator publishes a step, route it to storage
        async def _sim_feed_loop():
            while True:
                # _step_sync() always runs the Richards equation forward in time.
                # step_once() returns cached readings when called from an async
                # context, which would freeze the simulation after the first step.
                # Sleep is 30 s wall time; physics dt stays 900 s → 30× time compression.
                readings = simulator._step_sync()
                await router.route(readings)
                await asyncio.sleep(30)

        # Store the feed coroutine so start() can schedule it
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
        """Start all layers and the simulator feed."""
        layer_tasks = [
            asyncio.create_task(layer.start(), name=name)
            for name, layer in self.layers.items()
        ]
        sim_task = asyncio.create_task(self._sim_feed(), name="sim_feed")
        await asyncio.gather(*layer_tasks, sim_task)

    # ------------------------------------------------------------------
    # Public convenience methods
    # ------------------------------------------------------------------

    def set_irrigation(self, rate_L_hr: float, field_area_m2: float = 1.0) -> None:
        """Set irrigation rate [L/hr] on the physics simulator."""
        self.simulator.set_irrigation(rate_L_hr, field_area_m2)

    def stop_irrigation(self) -> None:
        """Stop irrigation in the physics simulator."""
        self.simulator.stop_irrigation()

    def get_latest_readings(self) -> dict:
        """Return the most recent sensor snapshot."""
        return self.simulator.get_latest()

    def get_fsm_state(self) -> str:
        """Return the current reactive FSM state."""
        return self.reactive.get_state()

    def make_api_router(self):
        """Return the FastAPI router for this twin's REST endpoints."""
        ts    = InfluxAdapter(self.config)
        doc   = MongoAdapter(self.config)
        cache = RedisAdapter(self.config)
        return build_sdt_router(
            self.config, ts, doc, cache,
            simulator=self.simulator,
            reactive=self.reactive,
            mas=self.mas,
        )


# ---------------------------------------------------------------------------
# Entry point (standalone run)
# ---------------------------------------------------------------------------

async def main():
    import uvicorn
    from fastapi import FastAPI

    twin = SoilDigitalTwin()
    await twin.initialise()

    app = FastAPI(title=twin.config.asset_name)
    app.include_router(twin.make_api_router())

    server = uvicorn.Server(uvicorn.Config(
        app, host=twin.config.api_host, port=twin.config.api_port, log_level="info"
    ))
    await asyncio.gather(twin.start(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
