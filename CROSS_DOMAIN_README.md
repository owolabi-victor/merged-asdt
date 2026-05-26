# ASDT + Cross-Domain Digital Twin — Merged Project

This project merges two systems into one codebase:

1. **ASDT (Agentic Soil Digital Twin)** — your 8-layer IoT soil intelligence system with 11 soil parameters, 8 depletion states, Streamlit dashboards, and LLM-powered diagnostics.
2. **Cross-Domain Framework (DT-Forge)** — your friend's SDT/PDT/BPDT biotic pod system using Richards equation physics, CWSI plant stress, PID irrigation control, and OODA autonomous loops.

Both systems run side-by-side, sharing infrastructure (InfluxDB, MongoDB, Redis, Neo4j, MQTT, MinIO) but using **separate databases/buckets** so they don't interfere with each other.

---

## Architecture After Merge

```
agentic-soil-digital-twin/
│
├── main.py                      ← ASDT orchestrator (unchanged)
├── cross_domain_launcher.py     ← Cross-domain launcher (NEW)
├── docker-compose.yml           ← Merged infrastructure (UPDATED)
│
├── physical/                    ← ASDT Layer 1: simulator
├── dt_network/                  ← ASDT Layer 3: MQTT ingestor
├── data_layer/                  ← ASDT: static data writer
├── data_mgmt/                   ← ASDT Layer 5: ETL pipeline
├── simulation/                  ← ASDT Layer 6: depletion forecasting
├── service_layer/               ← ASDT Layer 7: Ditto sync, FastAPI
├── reactive/                    ← ASDT Layer 8a: rule engine
├── intelligent/                 ← ASDT Layer 8b: LLM agent + diagnostics
├── shared/                      ← ASDT: config, DB helpers
├── ui/                          ← ASDT: Streamlit dashboards + REST API
├── config/                      ← ASDT: Grafana, Mosquitto, Nginx configs
│
└── cross_domain/                ← Friend's DT-Forge code (NEW)
    ├── sdt/                     ← Soil Digital Twin (Richards equation)
    │   ├── sdt.py               ← Twin class (SoilDigitalTwin)
    │   ├── simulator.py         ← Physics sensor simulator
    │   ├── models.py            ← Van Genuchten, Richards ODE, ET surrogate
    │   ├── reactive_layer.py    ← 5-state FSM (OPTIMAL→WILTING_RISK)
    │   ├── intelligent_layer.py ← 4 LLM agents (moisture, sensor, hydraulic, ET)
    │   ├── autonomous_layer.py  ← OODA loop + goal planner
    │   ├── api.py               ← FastAPI REST endpoints (/api/sdt/*)
    │   ├── knowledge_graph.py   ← Neo4j soil ontology seeder
    │   └── sdt.env              ← SDT environment config
    │
    ├── pdt/                     ← Plant Digital Twin (maize drought stress)
    │   ├── pdt.py               ← Twin class (PlantDigitalTwin)
    │   ├── simulator.py         ← Plant physiology simulator
    │   ├── models.py            ← CWSI, P-V curve, stomatal models
    │   ├── reactive_layer.py    ← 6-state plant FSM
    │   ├── intelligent_layer.py ← 4 LLM agents (stress, phenology, etc.)
    │   ├── autonomous_layer.py  ← Plant OODA loop
    │   ├── api.py               ← FastAPI REST endpoints (/api/pdt/*)
    │   ├── knowledge_graph.py   ← Neo4j plant ontology seeder
    │   └── pdt.env              ← PDT environment config
    │
    ├── bpdt/                    ← Biotic Pod Digital Twin (orchestrator)
    │   ├── bpdt.py              ← Twin class (BioticPodDigitalTwin)
    │   ├── models.py            ← Coupled soil-plant + water budget models
    │   ├── reactive_layer.py    ← 7-state FSM + PID irrigation controller
    │   ├── intelligent_layer.py ← 5 system-level LLM agents
    │   ├── autonomous_layer.py  ← OODA overseer + hot-swap protocol
    │   ├── api.py               ← FastAPI REST endpoints (/api/bpdt/*)
    │   ├── knowledge_graph.py   ← Neo4j biotic pod ontology seeder
    │   └── bpdt.env             ← BPDT environment config
    │
    ├── shared/                  ← Shared components
    │   ├── weather_simulator.py ← Physics-grounded arid weather generator
    │   └── actuator_bridge.py   ← MQTT irrigation valve controller bridge
    │
    ├── infra/                   ← Ditto nginx config
    │   ├── mosquitto.conf
    │   └── nginx/
    │
    ├── index.html               ← Cross-domain monitoring dashboard
    └── launcher.py              ← Original friend's launcher (reference only)
```

---

## Data Isolation

| Resource | ASDT | Cross-Domain |
|---|---|---|
| InfluxDB org | `asdt` | `digital_twin` |
| InfluxDB bucket | `soil_telemetry` | `sdt_telemetry`, `pdt_telemetry`, `bpdt_telemetry` |
| MongoDB database | `asdt` | `digital_twin` |
| Redis DB | `0` | `0` (SDT), `1` (PDT), `2` (BPDT) |
| Neo4j | Shared (different node labels) | Shared (different node labels) |
| MQTT topics | `dt/soil_parcel_001/*` | `dt/sdt_*/*`, `dt/pdt_*/*`, `dt/bpdt_*/*` |

---

## Prerequisites

### 1. Docker + Docker Compose
Required for all infrastructure services.

### 2. Python 3.11+
Both systems need Python 3.11 or later.

### 3. DT-Forge Framework (friend's framework)
The cross-domain code depends on `dt_forge`. Install it as an editable package:

```bash
cd /path/to/dt-forge-framework   # the directory with dt_forge's pyproject.toml
pip install -e .
```

This installs: `fastapi`, `uvicorn`, `influxdb-client`, `pymongo`, `redis`,
`simple-pid`, `scipy`, `numpy`, `python-dotenv`, `neo4j`, `transitions`,
`paho-mqtt`, `scikit-learn`, `stable-baselines3`, `langchain`, and more.

### 4. ASDT Python Dependencies
```bash
pip install -r requirements.txt
```

---

## Getting It Running

### Step 1 — Start Infrastructure

```bash
docker compose up -d
```

Wait ~60 seconds for all services to become healthy. Verify:
```bash
docker compose ps
```

You should see 15+ containers running (ASDT infra + Ditto microservices + bucket init).

### Step 2 — Start ASDT (Terminal 1)

```bash
# First time only:
python main.py --setup

# Then:
python main.py
```

This starts your 7 background agents (simulator, ingestor, pipeline, model runner, ditto sync, rule engine, cross-domain sync).

ASDT is now running on:
- **REST API**: http://localhost:8000
- **Scientist Dashboard**: http://localhost:8501
- **Farmer Dashboard**: http://localhost:8502
- **Grafana**: http://localhost:3000

### Step 3 — Start Cross-Domain Twins (Terminal 2)

```bash
python cross_domain_launcher.py
```

Or to wipe cross-domain state first:
```bash
python cross_domain_launcher.py --reset
```

Cross-domain is now running on:
- **Unified API**: http://localhost:8503
- **API Docs**: http://localhost:8503/docs
- **SDT endpoints**: http://localhost:8503/api/sdt/*
- **PDT endpoints**: http://localhost:8503/api/pdt/*
- **BPDT endpoints**: http://localhost:8503/api/bpdt/*
- **System health**: http://localhost:8503/api/system/health
- **Eclipse Ditto**: http://localhost:8080

### Step 4 — Verify Both Systems

```bash
# Check ASDT
curl http://localhost:8000/health

# Check cross-domain
curl http://localhost:8503/api/system/health

# Check SDT soil moisture
curl http://localhost:8503/api/sdt/moisture/profile

# Check PDT plant stress
curl http://localhost:8503/api/pdt/stress/current

# Check BPDT irrigation
curl http://localhost:8503/api/bpdt/status
```

---

## Key Cross-Domain API Endpoints

### SDT (Soil Digital Twin) — `/api/sdt`
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/sdt/moisture/profile` | VWC at 10/30/60 cm + soil potential |
| GET | `/api/sdt/depletion/rate` | Depletion rate + time to PWP |
| GET | `/api/sdt/hydraulic` | Van Genuchten parameters |
| GET | `/api/sdt/forecast` | 24-hr VWC + ET forecast |
| GET | `/api/sdt/state` | Current FSM state |
| GET | `/api/sdt/stream` | SSE live telemetry stream |
| GET | `/api/sdt/thresholds` | Alert thresholds |
| GET | `/api/sdt/alerts/history` | Recent alert events |
| GET | `/api/sdt/agents/status` | MAS agent statuses |
| GET | `/api/sdt/telemetry/history` | Historical time-series |

### PDT (Plant Digital Twin) — `/api/pdt`
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/pdt/stress/current` | CWSI + leaf water potential |
| GET | `/api/pdt/phenology` | Growth stage + thermal time |
| GET | `/api/pdt/forecast` | 24-hr stress forecast |
| GET | `/api/pdt/state` | Current FSM state |
| GET | `/api/pdt/stream` | SSE live telemetry stream |

### BPDT (Biotic Pod Digital Twin) — `/api/bpdt`
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/bpdt/status` | System overview (all 3 FSM states) |
| GET | `/api/bpdt/irrigation` | PID irrigation rate + water budget |
| GET | `/api/bpdt/boundary-conditions` | BC-01 to BC-05 values |
| GET | `/api/bpdt/stream` | SSE aggregated telemetry |
| POST | `/api/bpdt/irrigation/manual` | Manual irrigation override |

---

## How the Two Systems Relate

Your ASDT monitors **soil health** broadly (11 parameters: NPK, pH, EC, organic matter, microbial activity, etc.) and diagnoses **8 depletion states** (nutrient depleted, acidified, salinized, compacted, etc.).

The cross-domain framework focuses on **water dynamics** specifically — it models the Richards equation for 3-layer soil moisture, couples it with a plant stress model (CWSI), and runs a PID controller for irrigation decisions.

Together they form the complete **biotic pod** picture:
- ASDT tells you the soil is nutrient-depleted and needs 150 kg/ha CAN
- Cross-domain tells you the soil is losing water at 0.002 m³/m³/hr and irrigation should start in 6 hours
- The BPDT orchestrates both perspectives

---

## Environment Variables

### ASDT (`.env` or environment)
Same as before — see the migration prompt for the full list.

### Cross-Domain (`cross_domain/sdt/sdt.env`, `pdt/pdt.env`, `bpdt/bpdt.env`)
Key variables:
```
DT_INFLUX__URL=http://localhost:8086
DT_INFLUX__TOKEN=my-super-secret-token
DT_INFLUX__ORG=digital_twin
DT_MONGO__URI=mongodb://admin:password@localhost:27017
DT_MONGO__DB=digital_twin
DT_REDIS__URL=redis://localhost:6379
DT_NEO4J__URI=bolt://localhost:7687
DT_DITTO__URL=http://localhost:8080
DT_LLM__PROVIDER=ollama
DT_LLM__MODEL=qwen3-coder:480b-cloud
```

---

## Stopping Everything

```bash
# Stop ASDT agents: Ctrl+C in terminal 1
# Stop cross-domain twins: Ctrl+C in terminal 2
# Stop infrastructure:
docker compose down

# Full cleanup (remove volumes too):
docker compose down -v
```
