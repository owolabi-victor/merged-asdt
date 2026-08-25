# Real sensor integration

How ESP32 soil readings enter the 8-layer twin, what was changed to make that
work, and what is still needed.

Last updated: 2026-08-25

---

## 1. What was wrong

`physical/simulator.py` invents every reading with `random.gauss()` and
`random.uniform()`. The whole stack — reactive rules, the FSM, the LLM
diagnosis — reasoned over numbers a random number generator produced. There was
no path for hardware data.

## 2. The data

Captured from an ESP32 with a resistive soil probe, calibrated on the bench
(3126 mV dry air → 0 %, 1400 mV in water → 100 %).

| | |
|---|---|
| Readings | 799 |
| Rate | 1 Hz |
| Moisture range | 0.0 % – 63.9 % |
| Wetting events | 5 |

Stored at `data/soil_dataset.csv`.

## 3. What was added

**`physical/real_sensor_bridge.py`** — publishes real readings to
`TOPIC_TELEMETRY` using the same contract as the simulator, so every layer above
is unchanged.

```bash
# run the twin on real readings instead of the simulator
USE_REAL_SENSOR=1 python main.py

# or replay directly
python -m physical.real_sensor_bridge --csv data/soil_dataset.csv --interval 2 --loop
python -m physical.real_sensor_bridge --csv data/soil_dataset.csv --tail 200   # wet events only
```

`main.py` gained a `USE_REAL_SENSOR` switch. Unset, nothing changes: the
simulator runs exactly as before.

## 4. Three defects this exposed

**Timestamps were the wrong type.** `SoilTelemetryMessage` declares
`timestamp: float` (Unix epoch); the simulator sends `time.time()`. Publishing an
ISO-8601 string means every message is dead-lettered. The bridge converts, and
keeps the real capture time rather than stamping "now" — the twin's time series
depends on when the soil was actually measured.

**Provenance was silently discarded, twice.** Pydantic drops undeclared fields,
so `source` and `measured_fields` vanished at the ingestor; `route_telemetry`
then filters to `SENSOR_FIELDS`, which would have dropped them again. Both were
fixed, so a measured reading is now distinguishable from an invented one:

- `dt_network/ingestor.py` — schema carries `source` and `measured_fields`
- `data_layer/writer.py` — caches both to Redis

**MQTT delivery is not ingestion.** An early test confirmed messages reached the
broker and was taken as success. The ingestor was not running, so nothing was
validating them. Verification now checks the dead-letter topic and Redis.

## 5. Verified

With `USE_REAL_SENSOR=1 python main.py`, all 7 layers running:

```
soil_moisture_pct          = 60.5           measured
soil_moisture_pct_smooth   = 46.3           Data Mgmt Pipeline
soil_moisture_pct_quality  = 3.0            QA/QC flag
data_source                = "esp32_node"   provenance preserved
active_alert_count         = 1              Reactive layer
state                      = warning        FSM left normal
```

Ingestor rejections: 0 of 120. The reactive rule engine moved the twin to
`warning` from real readings.

## 6. Honest limitations

**Only moisture is measured.** The node carries one resistive probe. Every other
field in the payload is the soil-type nominal, which is why each reading declares
`measured_fields: ["soil_moisture_pct"]`. Nothing downstream should treat
nitrogen, pH or organic matter from this node as observed.

**The probe reports a 0–100 % resistive scale, not m³/m³ VWC.** `SENSOR_FIELDS`
documents `soil_moisture_pct` as volumetric water content. Converting requires
gravimetric calibration against oven-dried samples of the same soil.

**This is a replay, not a live link.** Readings were captured to CSV and are
replayed. The node is not currently publishing to this broker.

## 7. The better path, not yet taken

`ui/sensor_connection.py` is a production-grade onboarding module — per-user MQTT
credentials, payload validation, field mapping, heartbeat monitoring, automatic
reconnection with backoff, TLS. It is the mechanism this project already designed
for real sensors, and it is a better integration point than the bridge.

Using it needs two things:

1. **Firmware change.** The node currently POSTs over HTTP. It would publish MQTT
   instead (ESP-IDF ships `esp-mqtt`). Requires physical access to reflash.
2. **A configured data source.** `set_user_data_source(user_id, parcel_id,
   mode="sensor", ...)` with a `field_mapping` translating the node's field names
   to canonical ones.

The bridge is the pragmatic path while the hardware is not to hand. The
`sensor_connection.py` route is the one to describe as the production design.

## 8. Running it

```bash
cd ~/Downloads/merged-asdt

# infrastructure (already up: mongo, mosquitto, influx, redis, neo4j, grafana)
docker compose up -d

# the twin, on real readings
USE_REAL_SENSOR=1 .venv/bin/python main.py

# the two portals (both are login-gated)
.venv/bin/streamlit run ui/app_scientist.py --server.port 8501
.venv/bin/streamlit run ui/app_farmer.py    --server.port 8502
```

Note `.env` points `OLLAMA_BASE_URL` at `https://ollama.com` with model
`gemma4:31b-cloud`. Locally only `llama3.2` and `llama3` are pulled, so for a
local run override:

```bash
OLLAMA_BASE_URL=http://localhost:11434 OLLAMA_MODEL=llama3.2
```
