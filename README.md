# Agentic Soil Digital Twin (ASDT)

### Final Year Project — Digital Twin Implementation

---

## Project Overview

The **Agentic Soil Digital Twin (ASDT)** is a fully layered, agent-based digital twin system for soil health monitoring and management advisory. It mirrors a physical soil parcel in software, enabling autonomous diagnosis of soil problems and delivery of management recommendations to farmers.

This implementation focuses on **one complete situation**:

> **A farmer reports that their maize crop is showing yellowing leaves and stunted growth. The ASDT must diagnose the soil cause and provide a soil management recommendation.**

The system is built following the **8-layer Digital Twin architecture** from the Implementation Guide, adapted from an industrial pump to an agricultural soil parcel.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  INTELLIGENT LAYER  (Orchestrator + Soil Intelligence Agent)     │
│  • Symptom recognition   • Differential diagnosis               │
│  • Recommendation        • Explanation generation               │
│  • LLM agent (Ollama)    • Escalation decision                  │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  REACTIVE LAYER  (Data Quality Agent)                            │
│  • Threshold monitoring  • State machine (running/warning)       │
│  • QA/QC flagging        • Alert generation via MQTT             │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  SERVICE LAYER  (Eclipse Ditto + FastAPI)                        │
│  • Soil Parcel Thing     • REST API for all layers               │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  SIMULATION & MODEL LAYER  (Soil Process Model)                  │
│  • Water balance model   • Nitrogen cycling                      │
│  • Residuals (anomaly signal)                                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  DATA MANAGEMENT LAYER  (Pipeline)                               │
│  • Smoothing & QC flags  • Soil health score (0–100)            │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  DATA LAYER  (InfluxDB · MongoDB · Redis · MinIO)                │
│  • Live telemetry        • Farmer reports, diagnoses, outcomes   │
│  • Agent interaction log • File storage                          │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls down
┌──────────────────────────▼───────────────────────────────────────┐
│  NETWORK LAYER  (MQTT · Mosquitto)                               │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│  PHYSICAL LAYER  (IoT Sensors / Python Simulator)                │
│  • soil_moisture_pct     • nitrogen_ppm    • soil_ph             │
│  • soil_temp_c           • phosphorus_ppm  • potassium_ppm       │
│  • ec_ds_m               • bulk_density_g_cm3                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Layer                   | Tool               |
| ----------------------- | ------------------ |
| Sensor simulation       | Python (paho-mqtt) |
| MQTT broker             | Eclipse Mosquitto  |
| Time-series DB          | InfluxDB 2.7       |
| Document DB             | MongoDB 6          |
| Cache                   | Redis 7            |
| File storage            | MinIO              |
| Digital Twin platform   | Eclipse Ditto 3.5  |
| Knowledge graph         | Neo4j 5.13         |
| LLM (local, no API key) | Ollama (llama3)    |
| Agent framework         | LangChain          |
| Dashboard               | Grafana 10         |
| Infrastructure          | Docker Compose     |

---

## Project Structure

```
asdt/
├── docker-compose.yml          # All infrastructure services
├── .env                        # Environment variables
├── requirements.txt            # Python dependencies
├── main.py                     # Orchestrator — starts all layers
│
├── shared/                     # Shared helpers (used by all layers)
│   ├── config.py               # ← THE ONE FILE YOU EDIT FOR YOUR ASSET
│   ├── influx_io.py            # InfluxDB read/write helpers
│   ├── mongo_io.py             # MongoDB helpers (reports, diagnoses, etc.)
│   └── redis_io.py             # Redis cache helpers
│
├── physical/
│   └── simulator.py            # Soil sensor simulator (N-deficiency scenario)
│
├── dt_network/
│   └── ingestor.py             # MQTT → Pydantic validation → Data Layer
│
├── data_layer/
│   └── writer.py               # Routes validated data to correct store
│
├── data_mgmt/
│   └── pipeline.py             # Smoothing, quality flags, health score
│
├── simulation/
│   └── model_runner.py         # Soil water balance + N cycling model
│
├── service_layer/
│   ├── ditto_client.py         # Eclipse Ditto Thing management
│   └── ditto_sync.py           # Continuous Ditto Thing updater (every 30s)
│
├── reactive/
│   └── rule_engine.py          # Threshold rules + state machine
│
└── intelligent/
    ├── neo4j_kg.py             # Soil knowledge graph (symptoms → causes → actions)
    ├── soil_intelligence_agent.py  # Core diagnostic pipeline (6 steps)
    └── agent.py                # LangChain + Ollama agent (natural language Q&A)
```

---

## Quick Start

### Prerequisites

- Docker Desktop (≥ 6 GB RAM allocated)
- Python 3.10+

### Step 1: Clone and set up Python environment

```bash
cd asdt
python3 -m venv dt_env
source dt_env/bin/activate          # Linux/macOS
# .\dt_env\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Step 2: Create the configuration file

```bash
cp .env.example .env
```

Required, not optional. `docker-compose.yml` remaps every service port so this
stack can run alongside a second twin — MQTT on 2883, InfluxDB on 9086, MongoDB
on 28017, Redis on 7379. The code's built-in fallbacks are the container-internal
ports, so without `.env` the layers start, connect to nothing, and report no
data rather than an error.

`.env` is gitignored: it is yours to hold credentials in, and it is never
committed.

### Step 3: Start all infrastructure

```bash
docker compose up -d
docker compose ps    # all containers should show "running"
```

Wait ~90 seconds for Eclipse Ditto to fully initialise.

### Step 4: Pull the Ollama LLM model (one-time, ~4 GB download)

```bash
docker exec asdt_ollama ollama pull llama3
```

### Step 5: First-time setup

```bash
python main.py --setup
```

This seeds MongoDB, creates the Eclipse Ditto Soil Parcel Thing, and builds the Neo4j knowledge graph.

### Step 6: Start all digital twin layers

```bash
python main.py
```

### Step 7: Run the diagnostic agent

In a second terminal:

```bash
source dt_env/bin/activate
python -m intelligent.agent
```

---

## The Situation — Step by Step

The farmer reports: _"My maize is showing yellowing leaves and stunted growth."_

### What happens automatically:

**Physical Layer**: Simulator publishes soil readings every 30 seconds:

- `nitrogen_ppm = 8.0` (critical: should be ≥ 15)
- `soil_ph = 5.6` (marginal: maize minimum is 5.5)
- `soil_moisture_pct = 52.0` (adequate — rules out water stress)

**Reactive Layer**: Rule engine detects nitrogen below warning threshold → raises alert, sets state to `warning`.

**Intelligent Layer** (when farmer reports symptoms):

1. **Symptom Recognition**: "yellowing" + "stunted" → `[yellowing_leaves, stunted_growth]`
2. **Soil Data Retrieval**: reads all sensor fields from InfluxDB
3. **Differential Diagnosis**:
   - `nitrogen_deficiency`: 85% confidence (symptoms + sensor confirm)
   - `water_stress`: 10% (ruled out — moisture adequate)
   - `soil_acidity`: 30% (secondary — pH marginal but above critical)
4. **Recommendation**: Apply 200 kg/ha CAN immediately
5. **Explanation**: Plain-language report for farmer
6. **Outcome Tracking**: Saves to MongoDB; updates Ditto Thing

### Escalation logic:

- Confidence ≥ 85% → Autonomous: delivered directly to farmer
- Confidence 70–85% → Intelligent: multi-agent review, then delivered
- Confidence < 70% → Escalated to Soil Scientist for review

---

## Verification Checklist

After `python main.py` is running:

- [ ] `docker compose ps` → all containers `running`
- [ ] InfluxDB UI `http://localhost:8086` → data arriving in `soil_telemetry` bucket
- [ ] Grafana `http://localhost:3000` → live dashboard panels updating
- [ ] Ditto REST: `curl http://localhost:8080/api/2/things -u ditto:ditto` → Thing JSON
- [ ] Neo4j `http://localhost:7474` → `MATCH (n) RETURN n` shows knowledge graph
- [ ] Redis: `redis-cli hgetall soil_parcel_001:latest` → current sensor values
- [ ] MongoDB: `db.farmer_reports.find({})` → reports being saved
- [ ] Agent: `python -m intelligent.agent` → diagnosis printed to terminal

---

## Key Design Decisions

| Decision                                  | Rationale                                                                                          |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Ollama (local LLM) instead of OpenAI      | No API key or internet dependency — runs fully offline                                             |
| 30-second sensor cycle                    | Soil properties change slowly; 1-second cycle would be wasteful                                    |
| CAN preferred over Urea in recommendation | CAN does not volatilise without rain — agronomically correct for dry conditions                    |
| Confidence thresholds (70% / 85%)         | Matches the situation document: autonomous at high confidence, escalate at low                     |
| Neo4j knowledge graph                     | Separates agricultural expertise from code — agronomists can update the KG without touching Python |
| MongoDB for farmer reports and outcomes   | Document model fits variable-structure agronomic data better than relational tables                |

---

## Grafana Dashboard Setup

1. Open `http://localhost:3000` → login `admin / admin`
2. **Connections → Data Sources → Add → InfluxDB**
   - Query Language: `Flux`
   - URL: `http://influxdb:8086`
   - Org: `asdt` | Bucket: `soil_telemetry` | Token: `my-super-secret-token`
3. **Create panels** using these Flux queries:

```flux
// Live nitrogen reading
from(bucket: "soil_telemetry")
  |> range(start: v.timeRangeStart)
  |> filter(fn:(r) => r._measurement == "soil_telemetry")
  |> filter(fn:(r) => r._field == "nitrogen_ppm")

// Soil health score
from(bucket: "soil_telemetry")
  |> range(start: -5m)
  |> filter(fn:(r) => r._measurement == "soil_processed")
  |> filter(fn:(r) => r._field == "soil_health_score")
  |> last()

// Model vs real nitrogen (residual anomaly)
from(bucket: "soil_telemetry")
  |> range(start: v.timeRangeStart)
  |> filter(fn:(r) => r._measurement == "soil_residuals")
  |> filter(fn:(r) => r._field == "res_nitrogen_ppm")
```

---

## Troubleshooting

| Symptom               | Fix                                                                    |
| --------------------- | ---------------------------------------------------------------------- |
| Ditto returns 401     | Wait 90s after `docker compose up`. Use `ditto:ditto` credentials      |
| InfluxDB write fails  | Token must be `my-super-secret-token` exactly as in docker-compose.yml |
| Grafana "No data"     | Set datasource URL to `http://influxdb:8086` not `localhost`           |
| Neo4j ConnectionError | Run `docker logs asdt_neo4j \| tail -20` and wait for "Started"        |
| Ollama agent hangs    | Ensure model is pulled: `docker exec asdt_ollama ollama pull llama3`   |
| `ModuleNotFoundError` | Always run from project root: `python -m module.submodule`             |
