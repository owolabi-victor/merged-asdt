# Running the hosted scientist dashboard

https://asdt-scientist.streamlit.app reads live sensor data. This is what makes
that true, and what to do when it stops.

Last verified: 30 August 2026.

## The path a reading takes

```
ESP32  --HTTP-->  pod_bridge  --MQTT/TLS-->  HiveMQ Cloud
                                                  |
                                            cloud ingestor
                                                  |
                                            InfluxDB Cloud
                                                  |
Streamlit Cloud  <--HTTPS--  cloudflared  <--  twin_api (+ agent -> Ollama Cloud)
```

Nothing in the hosted app talks to your laptop directly. It calls one URL —
`ASDT_API_URL` in its Streamlit secrets — and everything behind that is the
tunnel into `twin_api`.

## What must be running on the laptop

Four processes. If the dashboard goes blank, one of these has died.

| What | Check | Purpose |
|---|---|---|
| `pod_bridge` on :8000 | `pgrep -f "pod_bridge.py 8000"` | the node's ingest endpoint |
| cloud ingestor | `pgrep -f dt_network.ingestor` | HiveMQ → InfluxDB Cloud |
| `twin_api` on :8090 | `curl -s -o /dev/null -w '%{http_code}' localhost:8090/` | the API the dashboard calls |
| `cloudflared` | `pgrep -f "cloudflared tunnel"` | makes :8090 public |

The Mac must also stay awake. Sleep kills the tunnel, and with it the
dashboard. `caffeinate -s` in a spare terminal prevents that.

### Starting them

```bash
cd ~/Downloads/merged-asdt
set -a; source .env.cloud; set +a

# 1. ingest endpoint for the node (local brokers)
cd ~/sensors && SDT_BROKER=localhost SDT_PORT=2883 PDT_BROKER=localhost PDT_PORT=1883 \
  ~/Downloads/merged-asdt/.venv/bin/python -u tools/pod_bridge.py 8000 &

# 2. HiveMQ -> InfluxDB Cloud
cd ~/Downloads/merged-asdt && ./.venv/bin/python -u -m dt_network.ingestor &

# 3. the API the hosted dashboard calls
./.venv/bin/python -c "import uvicorn; uvicorn.run('service_layer.twin_api:app', host='0.0.0.0', port=8090)" &

# 4. make it public
cloudflared tunnel --url http://localhost:8090
```

`cloudflared` prints the public hostname. **It changes every restart.** Put the
new one in the Streamlit secret each time:

```toml
ASDT_API_URL = "https://<whatever-it-printed>.trycloudflare.com"
```

That is the whole reason this arrangement is temporary.

## Streamlit secrets

The scientist app reads exactly one variable — `ASDT_API_URL`. Everything else
that may be in there (MQTT, Influx, Mongo, Neo4j, Ollama keys) is unused by the
app: the backend holds those, and the backend runs here. They can be deleted.

## Moving to a droplet

This removes the tunnel, the laptop dependency, and the URL that changes.

### 1. Create it

DigitalOcean → Create → Droplet.

- **Image:** Marketplace → Docker on Ubuntu 24.04
- **Size:** Basic → Regular → **4 GB / 2 vCPU** ($24/mo). Measured need is
  ~1.5 GB for the working set; 2 GB leaves nothing for builds.
- **Auth:** SSH key — paste `~/.ssh/id_ed25519.pub`
- Note the IP.

### 2. Get the code on it

```bash
ssh root@<droplet-ip>
git clone https://github.com/owolabi-victor/merged-asdt.git
git clone https://github.com/afroBalogun/sensors.git
```

Secrets are gitignored by design, so copy them separately from your laptop:

```bash
scp ~/Downloads/merged-asdt/.env.cloud root@<droplet-ip>:~/merged-asdt/.env.cloud
```

### 3. Run it

```bash
cd ~/sensors
docker build -f docker/pod-bridge.Dockerfile -t asdt/pod-bridge:local .

cd ~/merged-asdt
export INGEST_TOKEN=$(openssl rand -hex 24); echo "$INGEST_TOKEN"   # write this down
docker compose -f docker-compose.yml \
               -f docker-compose.build.yml \
               -f docker-compose.deploy.yml \
               up -d --build mosquitto influxdb redis mongodb \
                             twin-api ingestor pod-bridge
```

Skip Ditto and Neo4j. They are ~4 GB of the full stack and nothing in the
sensor path needs them.

### 4. Repoint the two clients

**Streamlit secret:**

```toml
ASDT_API_URL = "http://<droplet-ip>:9500"
```

**The node**, through its setup portal (hold BOOT, tap EN/RST, keep BOOT held
~2s; join `soil-node-setup`; browse to 192.168.4.1):

```
http://<droplet-ip>:8000/sync/upload?key=<the INGEST_TOKEN from step 3>
```

Keep it `http://` — the firmware cannot do TLS.

### 5. Firewall

Open only 9500 (API) and 8000 (ingest). Leave 1883/2883 closed; nothing
outside the droplet needs the brokers.

## Known rough edges

- **No TLS on the dashboard or ingest.** Fine for a demo, not for anything
  public. Caddy in front of the API would fix the first.
- **The ingest token crosses the network in clear text**, because the firmware
  cannot do HTTPS. It stops strangers who find the port, not an observer.
- **Nothing backs up the droplet's Docker volumes.**
- **a-opdt's dashboard reports every field as measured** once its twin is
  running, because it infers provenance from "is there a value in the
  database". ASDT does not have this bug — it reads `measured_fields`.
