# main.py
"""
ASDT Main Orchestrator — starts all digital twin layers as subprocesses.

Situation: Soil depletion detection and management.
The parcel has been continuously cropped without adequate soil management.

Usage:
    python main.py --setup    # First time only
    python main.py            # Every subsequent run
"""
import subprocess
import sys
import os
import time
import signal
from dotenv import load_dotenv

load_dotenv()


def first_time_setup():
    print("\n[SETUP] ══════════════════════════════════════════")
    print("[SETUP] Agentic Soil Digital Twin — First-Time Setup")
    print("[SETUP] Situation: Soil Depletion Detection & Management")
    print("[SETUP] ══════════════════════════════════════════\n")

    soil_type = os.getenv("ACTIVE_SOIL_TYPE", "loamy")

    # Step 1: Seed MongoDB
    print("[SETUP] Step 1: Seeding static parcel metadata...")
    from data_layer.writer import seed_static_metadata
    from shared.config import SOIL_TYPES
    texture = SOIL_TYPES.get(soil_type, SOIL_TYPES["loamy"])

    seed_static_metadata({
        "name":            "Soil Parcel 001 — Continuous Cropping Field",
        "location":        "Ashanti Region, Ghana — 6.7°N, 1.6°W",
        "area_ha":         2.5,
        "soil_type":       soil_type,
        "soil_texture":    f"{texture['sand_pct']}% sand, {texture['silt_pct']}% silt, {texture['clay_pct']}% clay",
        "sand_pct":        texture["sand_pct"],
        "silt_pct":        texture["silt_pct"],
        "clay_pct":        texture["clay_pct"],
        "parent_material": "Weathered granite",
        "land_use":        "Continuous cropping (multiple seasons, no fallow)",
        "management_history": "No lime, no organic amendments, minimal fertiliser for 3+ seasons",
        "asset_type":      "soil_parcel",
        "depletion_sensitivity": texture.get("depletion_sensitivity", "moderate"),
        "sensor_fields": [
            "soil_moisture_pct", "bulk_density_g_cm3", "soil_temp_c",
            "soil_ph", "nitrogen_ppm", "phosphorus_ppm", "potassium_ppm",
            "ec_ds_m", "organic_matter_pct",
            "microbial_biomass_mg_c_kg", "soil_respiration_mg_co2_kg_day",
        ],
    })

    # Step 2: Wait for Twin API
    print("[SETUP] Step 2: Waiting for Twin API to be ready...")
    ditto_ready = _wait_for_ditto(max_wait=180)

    # Step 3: Create Thing
    print("[SETUP] Step 3: Creating Twin API policy and Soil Parcel Thing...")
    from service_layer.ditto_client import create_policy, create_thing
    create_policy()
    create_thing()

    # Step 4: Build Neo4j knowledge graph
    print("[SETUP] Step 4: Building soil depletion knowledge graph in Neo4j...")
    try:
        from intelligent.neo4j_kg import setup_graph
        setup_graph()
    except Exception as e:
        print(f"[SETUP] ⚠️  Neo4j KG setup failed: {e}")
        print("[SETUP]    Neo4j may still be starting. Run again in 30s if needed.")

    print(f"\n[SETUP] ✅ First-time setup complete! (soil type: {soil_type})")
    print("[SETUP] Now run: python main.py")
    print("[SETUP] UI Dashboards — create your account on first visit:")
    print("[SETUP]   Scientist: http://localhost:8501")
    print("[SETUP]   Farmer:    http://localhost:8502")
    print()


def _wait_for_ditto(max_wait: int = 180) -> bool:
    """Poll Twin API until it responds."""
    import httpx
    from shared.config import DITTO_URL, DITTO_USER, DITTO_PASS

    start = time.time()
    attempt = 0
    while time.time() - start < max_wait:
        attempt += 1
        try:
            r = httpx.get(
                f"{DITTO_URL}/api/2/things",
                auth=(DITTO_USER, DITTO_PASS),
                timeout=8,
            )
            if r.status_code in (200, 404):
                print(f"[SETUP] ✅ Twin API is ready (HTTP {r.status_code}) after {int(time.time()-start)}s")
                return True
            else:
                print(f"[SETUP] Twin API returned HTTP {r.status_code} — still starting (attempt {attempt})...")
        except httpx.ReadError:
            print(f"[SETUP] Twin API connection reset (attempt {attempt})...")
        except httpx.ConnectError:
            print(f"[SETUP] Twin API not accepting connections yet (attempt {attempt})...")
        except Exception as e:
            print(f"[SETUP] Twin API not ready yet: {type(e).__name__} (attempt {attempt})...")
        time.sleep(12)

    print(f"[SETUP] ⚠️  Twin API did not become ready in {max_wait}s — continuing anyway.")
    return False


LAYERS = [
    ("Physical Simulator",     [sys.executable, "-m", "physical.simulator"]),
    ("DT Network Ingestor",    [sys.executable, "-m", "dt_network.ingestor"]),
    ("Data Mgmt Pipeline",     [sys.executable, "-m", "data_mgmt.pipeline"]),
    ("Simulation Runner",      [sys.executable, "-m", "simulation.model_runner"]),
    ("Service Ditto Sync",     [sys.executable, "-m", "service_layer.ditto_sync"]),
    ("Reactive Rule Engine",   [sys.executable, "-m", "reactive.rule_engine"]),
    ("Cross-Domain Sync",      [sys.executable, "-m", "service_layer.cross_domain_sync"]),
]

running = []


def shutdown(sig=None, frame=None):
    print("\n\n[MAIN] Shutting down all ASDT layers...")
    for name, proc in running:
        proc.terminate()
        print(f"[MAIN]   Stopped: {name}")
    sys.exit(0)


signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)


def start_all():
    print("\n[MAIN] ══════════════════════════════════════════")
    print("[MAIN] Agentic Soil Digital Twin — Starting")
    print("[MAIN] Situation: Soil Depletion Detection & Management")
    print("[MAIN] ══════════════════════════════════════════\n")

    for name, cmd in LAYERS:
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
        running.append((name, proc))
        print(f"[MAIN] ▶  Started: {name}  (PID {proc.pid})")
        time.sleep(1.0)

    print(f"\n[MAIN] All {len(LAYERS)} layers running. Press Ctrl+C to stop.\n")
    print("[MAIN] Infrastructure:")
    print("[MAIN]   Grafana:    http://localhost:3000")
    print("[MAIN]   InfluxDB:   http://localhost:8086")
    print("[MAIN]   Twin API:   http://localhost:8080")
    print("[MAIN]   Neo4j:      http://localhost:7474")
    print("[MAIN]   MinIO:      http://localhost:9091")
    print()
    print("[MAIN] UI Dashboards (create your account on first visit):")
    print("[MAIN]   Scientist:  http://localhost:8501")
    print("[MAIN]   Farmer:     http://localhost:8502")
    print()
    print("[MAIN] To run the diagnostic pipeline (in a second terminal):")
    print("[MAIN]   python -m intelligent.soil_intelligence_agent")
    print("[MAIN] To run the LLM agent (in a second terminal):")
    print("[MAIN]   python -m intelligent.agent")
    print()

    for name, proc in running:
        proc.wait()


if __name__ == "__main__":
    if "--setup" in sys.argv:
        first_time_setup()
    else:
        start_all()