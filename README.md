# Agentic Soil Digital Twin (ASDT)

### Final Year Project — Digital Twin Implementation

---

An eight-layer, agent-based digital twin of a soil parcel, driven by a deployed
ESP32 sensor node. It diagnoses soil problems autonomously and advises through
a scientist and a farmer portal.

Its defining property is **measurement provenance**: every value it reports is
traceable to an instrument, or it is not reported.

---

## Get it running

No hardware required. The captured hardware dataset is in the repository, so a
clone reproduces the evaluation on its own.

```bash
git clone https://github.com/owolabi-victor/merged-asdt.git
cd merged-asdt

python3 -m venv dt_env
source dt_env/bin/activate            # Windows: .\dt_env\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                  # required — see the note below
docker compose up -d                  # infrastructure; give it ~90s the first time

USE_REAL_SENSOR=1 python main.py      # replays the captured node readings
```

Then open the portals:

```bash
streamlit run ui/app_scientist.py --server.port 8501
streamlit run ui/app_farmer.py    --server.port 8502
```

### Why `cp .env.example .env` is not optional

`docker-compose.yml` remaps every published port so this stack can run beside a
second twin — MQTT on **2883**, InfluxDB on **9086**, MongoDB on **28017**,
Redis on **7379**. The code's built-in fallbacks are the container-internal
ports it was remapped away from, so without `.env` the layers start, connect to
nothing, and report no data rather than an error. `.env` is gitignored; it is
yours to hold credentials in.

### The agent needs a model

```bash
ollama pull llama3.2                  # or point OLLAMA_BASE_URL at hosted inference
```

Without it the seven data layers run normally; only the advisory will not
answer.

### Check it is working

```bash
docker compose ps                                  # services up
docker exec merged_asdt_redis redis-cli HGET soil_parcel_001:latest measured_fields
```

That second command is the one that matters: it is the system's own record of
which fields came from an instrument. Every portal reading and every agent
answer is derived from it.

---

## Project Overview

The **Agentic Soil Digital Twin (ASDT)** is an eight-layer, agent-based digital
twin of a soil parcel. It is driven by a deployed ESP32 sensor node, diagnoses
soil problems autonomously, and delivers management advice to a scientist and a
farmer through separate portals.

Its defining property is **measurement provenance**. Every value the system
reports is traceable to an instrument, or it is not reported. Fields with no
sensor behind them were removed from the schema rather than filled with
plausible defaults, because the thresholds, the health score and the depletion
detector all read whatever the schema contains — and a twin that can diagnose a
deficiency from a value nobody measured is worse than one that says nothing.

### What is measured, and what is not

The deployed node instruments three quantities. They are the only three the
system will report:

| Field | Instrument | Reported |
|---|---|---|
| `soil_moisture_pct` | Resistive probe, ADC1 ch6, two-point calibration | yes |
| `air_temperature_c` | DHT11, GPIO18 | yes |
| `relative_humidity_pct` | DHT11, GPIO18 | yes |

Soil pH, nitrogen, phosphorus, potassium, electrical conductivity, organic
matter, microbial biomass and soil respiration have **no instrument on this
node**. They are absent from the ingest schema, the thresholds, the rules, the
portals and the tests. The two atmospheric readings enter instead as *forcing
variables*, driving an evaporation model whose every input is an observation.

> **The moisture reading is a calibrated proxy, not volumetric water content.**
> A resistive probe reports a position between two empirically determined
> endpoints. Converting to true VWC requires gravimetric calibration against
> oven-dried samples, which has not been performed.

---

## Architecture

Eight layers. Data flows upward; control flows downward. No layer imports
another — they coordinate only through MQTT and the shared stores, which is why
any one of them can be restarted or replaced independently.

```
┌──────────────────────────────────────────────────────────────────┐
│  INTELLIGENT     Tool-calling LLM agent · differential diagnosis │
│                  recommendation · escalation decision            │
├──────────────────────────────────────────────────────────────────┤
│  REACTIVE        Threshold monitoring · FSM · alert generation   │
├──────────────────────────────────────────────────────────────────┤
│  SERVICE         Eclipse Ditto Thing · FastAPI REST              │
├──────────────────────────────────────────────────────────────────┤
│  SIMULATION      Water balance · evaporation · residuals         │
├──────────────────────────────────────────────────────────────────┤
│  DATA MGMT       Smoothing · QA/QC flags · soil health score     │
├──────────────────────────────────────────────────────────────────┤
│  DATA            InfluxDB · MongoDB · Redis · MinIO              │
├──────────────────────────────────────────────────────────────────┤
│  NETWORK         MQTT — Mosquitto locally, HiveMQ Cloud          │
├──────────────────────────────────────────────────────────────────┤
│  PHYSICAL        ESP32 node — resistive probe · DHT11            │
└──────────────────────────────────────────────────────────────────┘
```

---

## The flow of a reading

1. **Node.** Eight ADC samples, median-filtered, mapped through the two-point
   calibration. The probe health check runs *before* publication: a reading
   below the fault floor, or with sample scatter above the limit, is rejected
   rather than published. The payload declares `measured_fields`.
2. **Bridge.** `pod_bridge` is the only component that knows both twins'
   vocabularies. It translates and publishes to `dt/soil_parcel_001/telemetry`
   on Mosquitto, and to HiveMQ Cloud over TLS.
3. **Ingest.** `dt_network.ingestor` validates against a Pydantic schema —
   types, ranges, NaN rejection. Failures go to a dead-letter topic, never into
   the store.
4. **Storage.** `data_layer.writer` routes to InfluxDB (series), Redis (latest
   values **and `measured_fields`**) and MongoDB (events).
5. **Processing.** Smoothing and QA/QC flags; the water balance model runs in
   parallel and writes residuals; the rule engine evaluates thresholds and
   drives the FSM.
6. **Service.** FastAPI exposes the summary, including `measured_fields`.
7. **Consumers.** The portals render a provenance chip per field; the agent's
   tools carry the same labels into every answer.

Provenance travels with the reading at every hop. A value that loses it is
treated as unmeasured at the far end.

---

## The sensor node

Firmware lives in a separate repository (`sensors`), built with **ESP-IDF
v5.5.5** for an **ESP32-D0WD-V3**.

| Concern | Implementation |
|---|---|
| Soil probe | Resistive, ADC1 channel 6. ADC2 is unusable once Wi-Fi is up |
| Calibration | 3126 mV dry air → 0 %, 1400 mV in water → 100 % |
| Sampling | 8 samples per reading, median filtered |
| Fault detection | Plausibility floor + sample dispersion, with a 3-reading recovery streak |
| Climate | DHT11 on GPIO18, bit-banged single-wire protocol |
| Provisioning | Captive portal; SSID, password and ingest URL stored in NVS |
| Remote logging | UDP broadcast mirror of the serial console, for untethered use |

The node cannot do TLS — no certificate bundle is attached — so the ingest
endpoint is plain HTTP protected by a shared secret. This is stated rather than
hidden: it stops opportunistic writes, not an observer on the path.

---

## Algorithms

| Stage | Method | Why |
|---|---|---|
| Sampling | Median of 8 | Rejects single-sample spikes outright; a mean would smear them |
| Calibration | Linear two-point, clamped | Endpoints measured empirically, not assumed |
| Fault detection | Plausibility floor + dispersion + hysteresis | Soil cannot conduct better than water; a floating pin scatters |
| Smoothing | Boxcar moving average (`np.convolve`) | Soil changes slowly; the noise is high-frequency |
| Health score | Weighted average over fields with values | Unreported fields are excluded, never assumed healthy |
| Anomaly | Residuals: measured − modelled | Compares against physics rather than a fixed number |
| Evaporation | Drawdown vs vapour pressure deficit | Separates energy-limited from supply-limited drying |
| Diagnosis | Differential ranking with confidence | Escalates below threshold rather than asserting weakly |
| Advisory | Tool-calling loop (ReAct-style) | The model chooses the sequence; it is not a fixed pipeline |

---

## Technology Stack

| Layer                   | Tool               |
| ----------------------- | ------------------ |
| Physical sensing        | ESP32 + ESP-IDF v5.5.5 (C) |
| Sensor simulation       | Python (paho-mqtt) |
| MQTT broker             | Eclipse Mosquitto  |
| Time-series DB          | InfluxDB 2.7       |
| Document DB             | MongoDB 6          |
| Cache                   | Redis 7            |
| File storage            | MinIO              |
| Digital Twin platform   | Eclipse Ditto 3.5  |
| Knowledge graph         | Neo4j 5.13         |
| LLM                     | Ollama — local, or hosted inference |
| Agent framework         | LangChain          |
| Dashboards              | Streamlit portals · Grafana 10 |
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
│   ├── simulator.py            # Soil sensor simulator
│   └── real_sensor_bridge.py   # Replays captured hardware readings into the twin
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
│   ├── model_runner.py         # Soil water balance + residuals
│   └── evaporation.py          # Measured evaporation and the drying-stage split
│
├── service_layer/
│   ├── ditto_client.py         # Eclipse Ditto Thing management
│   └── ditto_sync.py           # Continuous Ditto Thing updater (every 30s)
│
├── reactive/
│   └── rule_engine.py          # Threshold rules + state machine
│
├── ui/                         # Streamlit portals (scientist, farmer) + API layer
├── docker/                     # Dockerfiles and pinned requirements
├── data/captures/              # Hardware telemetry exports, deliberately tracked
├── docs/                       # Implementation notes, deck, defence material
│
└── intelligent/
    ├── neo4j_kg.py             # Soil knowledge graph (symptoms → causes → actions)
    ├── soil_intelligence_agent.py  # Core diagnostic pipeline (6 steps)
    └── agent.py                # LangChain + Ollama agent (natural language Q&A)
```

---

## The situation, step by step

The scenario the running system handles is **water stress**, because moisture is
what the node instruments. The earlier nutrient-deficiency scenario described a
capability this hardware does not have, and was withdrawn along with the fields
behind it.

The farmer reports: _"My maize is wilting in the afternoon and the soil looks
dry."_

### What happens automatically

**Physical layer.** The node reads the probe every second and uploads a batch.
Each payload declares `measured_fields`, so nothing downstream has to guess
which numbers came from hardware:

```json
{ "soil_moisture_pct": 12.4, "air_temperature_c": 31.2,
  "relative_humidity_pct": 46.0,
  "measured_fields": ["soil_moisture_pct","air_temperature_c","relative_humidity_pct"],
  "source": "node-soil-01" }
```

**Network and ingest.** Published to MQTT, validated against the Pydantic
schema. Anything malformed is dead-lettered rather than stored.

**Data management.** Smoothing and QA/QC flags; the health score is computed
over fields that have values, and says how many that was.

**Simulation.** The water balance predicts the next state; residuals record
where the measurement departs from it. The evaporation model uses probe
drawdown as the observation and vapour pressure deficit from the DHT11 as the
atmospheric demand, separating energy-limited from supply-limited drying.

**Reactive layer.** Moisture below the soil-type warning threshold raises an
alert and moves the FSM to `warning`; further decline triggers **S5 —
Water-Stressed**.

**Intelligent layer.** Asked about the parcel, the agent calls its own tools,
reads the current readings and the diagnosis, and answers citing the specific
values it used — labelling each one measured or nominal. With no instrument
reporting it states that the node is not reporting rather than producing a
score.

### Escalation

Confidence is proportional to the deviation from threshold. Above the
autonomous band the agent advises directly; below it, the case is escalated
rather than asserted weakly. A diagnosis is never issued from fields with no
instrument behind them.

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
  |> filter(fn:(r) => r._field == "soil_moisture_pct")

// Soil health score
from(bucket: "soil_telemetry")
  |> range(start: -5m)
  |> filter(fn:(r) => r._measurement == "soil_processed")
  |> filter(fn:(r) => r._field == "soil_health_score")
  |> last()

// Model vs measured moisture (residual anomaly)
from(bucket: "soil_telemetry")
  |> range(start: v.timeRangeStart)
  |> filter(fn:(r) => r._measurement == "soil_residuals")
  |> filter(fn:(r) => r._field == "res_soil_moisture_pct")
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

---

## Setup in detail

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

---

## Running against real hardware

The physical layer is a switch, not a rewrite. Unset, the simulator runs and
nothing else changes.

```bash
# replay the captured dataset (799 readings, 1 Hz, 5 wetting events)
USE_REAL_SENSOR=1 python main.py

# or replay directly, at your own pace
python -m physical.real_sensor_bridge --csv data/soil_dataset.csv --interval 2 --loop
```

For a live node, run the bridge from the firmware repository and point the
node's ingest URL at it. The bridge publishes to both twins and to the cloud
brokers; the node's URL is set through its captive portal and held in NVS, so
moving networks is a re-provision rather than a reflash.

---

## Verifying it is reading live data

```bash
# 1. uploads arriving, with the destinations each was published to
tail -f <bridge log>

# 2. what actually reached the time series, last five minutes
curl -s -X POST "http://localhost:9086/api/v2/query?org=asdt" \
  -H "Authorization: Token $INFLUXDB_TOKEN" \
  -H "Content-Type: application/vnd.flux" -H "Accept: application/csv" \
  --data 'from(bucket:"soil_telemetry") |> range(start:-5m)
          |> filter(fn:(r)=> r._field=="soil_moisture_pct") |> last()'

# 3. provenance — what the hardware actually measured
docker exec merged_asdt_redis redis-cli HGET soil_parcel_001:latest measured_fields
```

The third command is the one that matters. It is the system's own record of
which fields came from an instrument, and every portal and agent answer is
derived from it.

---

## Limitations

Stated here because a twin that hides its boundaries is the problem this project
exists to address.

- **Single instrumented channel.** Moisture, air temperature and humidity are
  measured. Bulk density and soil temperature are reported as null, never
  estimated. Every conclusion the running system draws rests on those three.
- **Not volumetric water content.** The probe reports a calibrated proxy.
  Gravimetric calibration against oven-dried samples has not been performed.
- **Plain-HTTP ingest.** The firmware carries no certificate bundle, so the
  shared secret crosses the network in the clear. It stops strangers who find
  the port; it is not protection against an observer.
- **Evaluation by replay.** Chapter Four's dataset was captured from the
  hardware and replayed, which gives repeatable auditable evaluation but does
  not exercise unattended operation, network interruption or seasonal drift.
- **Hosted dashboards depend on a running host.** The portals reach the API
  through a tunnel that requires the machine to stay online. Adequate for
  evaluation, not for an always-available service.
- **Three-dimensional soil visualisation is illustrative.** It interpolates a
  single point measurement across a volume and does not represent measured
  spatial variation.

---
