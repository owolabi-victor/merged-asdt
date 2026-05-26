"""Cross-Domain Digital Twin Launcher.

Starts the friend's DT-Forge-based SDT/PDT/BPDT framework alongside
the existing ASDT system. This launcher runs the Biotic Pod Digital Twin
(which internally creates and manages SDT + PDT sub-twins) as a FastAPI
server on port 8503.

The ASDT system (main.py) runs independently on its own ports (8000, 8501, 8502).
Both systems share the same infrastructure (InfluxDB, MongoDB, Redis, Neo4j, etc.)
but use separate databases/buckets to avoid collisions.

Usage:
    # 1. Start infrastructure
    docker compose up -d

    # 2. Start ASDT (your project) — in terminal 1
    python main.py --setup   # first time only
    python main.py

    # 3. Start cross-domain framework (friend's project) — in terminal 2
    python cross_domain_launcher.py
    python cross_domain_launcher.py --reset   # wipe cross-domain state first

Requirements:
    - dt_forge must be installed: pip install -e /path/to/dt-forge-framework
    - All Docker infrastructure must be running (docker compose up -d)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import os

# Ensure the project root is on sys.path so `cross_domain.*` imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cross_domain.launcher")


def check_dt_forge():
    """Check if dt_forge is installed and give a helpful error if not."""
    try:
        import dt_forge  # noqa: F401
        return True
    except ImportError:
        log.error(
            "=" * 60 + "\n"
            "  dt_forge framework is NOT installed.\n"
            "  This is your friend's framework — install it with:\n"
            "    pip install -e /path/to/dt-forge-framework\n"
            "  (the directory containing dt_forge's pyproject.toml)\n"
            + "=" * 60
        )
        return False


def reset_state():
    """Wipe Redis, InfluxDB, and MongoDB for cross-domain twins."""
    from dotenv import dotenv_values

    _HERE = os.path.dirname(os.path.abspath(__file__))
    cd = os.path.join(_HERE, "cross_domain")

    # Load main project .env so fallbacks match the actual infrastructure ports
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))

    env = {
        **dotenv_values(os.path.join(cd, "sdt", "sdt.env")),
        **dotenv_values(os.path.join(cd, "pdt", "pdt.env")),
        **dotenv_values(os.path.join(cd, "bpdt", "bpdt.env")),
    }

    influx_url   = env.get("DT_INFLUX__URL",   os.getenv("INFLUXDB_URL",    "http://localhost:9086"))
    influx_token = env.get("DT_INFLUX__TOKEN",  os.getenv("INFLUXDB_TOKEN",  "my-super-secret-token"))
    influx_org   = env.get("DT_INFLUX__ORG",    os.getenv("INFLUXDB_ORG",    "asdt"))
    mongo_uri    = env.get("DT_MONGO__URI",     os.getenv("MONGO_URI",       "mongodb://admin:password@localhost:28017"))
    mongo_db     = env.get("DT_MONGO__DB",      os.getenv("MONGO_DB",        "asdt"))
    redis_url    = env.get("DT_REDIS__URL",     os.getenv("REDIS_URL",       "redis://localhost:7379"))

    log.info("=" * 60)
    log.info("RESET: wiping cross-domain state")
    log.info("=" * 60)

    try:
        import redis as _redis
        for db in (0, 1, 2):
            r = _redis.from_url(redis_url, db=db)
            r.flushdb()
            r.close()
        log.info("Redis: DBs 0, 1, 2 flushed")
    except Exception as exc:
        log.warning("Redis reset failed: %s", exc)

    try:
        from influxdb_client import InfluxDBClient
        buckets = ["sdt_telemetry", "pdt_telemetry", "bpdt_telemetry"]
        with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
            api = client.buckets_api()
            for name in buckets:
                existing = api.find_bucket_by_name(name)
                if existing:
                    api.delete_bucket(existing)
                api.create_bucket(bucket_name=name, org=influx_org)
                log.info("InfluxDB: bucket '%s' reset", name)
    except Exception as exc:
        log.warning("InfluxDB reset failed: %s", exc)

    try:
        import pymongo
        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        client.drop_database(mongo_db)
        client.close()
        log.info("MongoDB: database '%s' dropped", mongo_db)
    except Exception as exc:
        log.warning("MongoDB reset failed: %s", exc)

    log.info("RESET complete")


async def main(reset: bool = False):
    if not check_dt_forge():
        sys.exit(1)

    if reset:
        reset_state()

    import uvicorn
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from cross_domain.shared.weather_simulator import WeatherSimulator
    from cross_domain.bpdt.bpdt import BioticPodDigitalTwin

    log.info("=" * 60)
    log.info("Cross-Domain Digital Twin System (DT-Forge)")
    log.info("  SDT → Soil water depletion (Richards equation)")
    log.info("  PDT → Plant drought stress (CWSI)")
    log.info("  BPDT → Biotic Pod overseer (PID irrigation)")
    log.info("=" * 60)

    weather = WeatherSimulator()
    pod = BioticPodDigitalTwin(weather=weather)
    await pod.initialise()

    log.info("SDT asset_id : %s", pod.sdt.config.asset_id)
    log.info("PDT asset_id : %s", pod.pdt.config.asset_id)
    log.info("BPDT asset_id: %s", pod.config.asset_id)

    app = FastAPI(
        title="ASDT Cross-Domain Digital Twin System",
        description=(
            "Integrated soil-plant-biotic digital twin system.\n"
            "  - SDT (Soil DT): /api/sdt/*\n"
            "  - PDT (Plant DT): /api/pdt/*\n"
            "  - BPDT (Biotic Pod DT): /api/bpdt/*\n\n"
            "Part of the Agentic Soil Digital Twin (ASDT) project.\n"
            "Run alongside main.py for the full ASDT experience."
        ),
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    app.include_router(pod.sdt.make_api_router())
    app.include_router(pod.pdt.make_api_router())
    app.include_router(pod.make_api_router())

    @app.get("/api/system/health")
    async def system_health():
        return {
            "status": "running",
            "twins": pod.get_system_state(),
            "endpoints": {
                "sdt":  "http://0.0.0.0:8503/api/sdt",
                "pdt":  "http://0.0.0.0:8503/api/pdt",
                "bpdt": "http://0.0.0.0:8503/api/bpdt",
            },
            "asdt_endpoints": {
                "twin_api":     "http://0.0.0.0:8000",
                "scientist_ui": "http://0.0.0.0:8501",
                "farmer_ui":    "http://0.0.0.0:8502",
            },
        }

    host = pod.config.api_host
    port = pod.config.api_port

    log.info("Starting unified cross-domain API on %s:%d", host, port)
    log.info("  SDT endpoints  → http://%s:%d/api/sdt/moisture/profile", host, port)
    log.info("  PDT endpoints  → http://%s:%d/api/pdt/stress/current", host, port)
    log.info("  BPDT endpoints → http://%s:%d/api/bpdt/status", host, port)
    log.info("  System health  → http://%s:%d/api/system/health", host, port)
    log.info("  API docs       → http://%s:%d/docs", host, port)

    server_cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(server_cfg)
    server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    pod_task    = asyncio.create_task(pod.start(),    name="pod")
    server_task = asyncio.create_task(server.serve(), name="api")

    stop_task = asyncio.create_task(stop.wait(), name="stop_signal")
    await asyncio.wait(
        {pod_task, server_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    stop_task.cancel()

    log.info("Shutting down cross-domain twins…")
    server_task.cancel()
    pod_task.cancel()
    await asyncio.gather(pod_task, server_task, return_exceptions=True)
    log.info("Stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cross-Domain Digital Twin System (DT-Forge based)"
    )
    parser.add_argument(
        "--reset", "-r",
        action="store_true",
        help="Wipe cross-domain Redis/InfluxDB/MongoDB before starting",
    )
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))
