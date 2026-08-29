# Deploying to a DigitalOcean droplet

What this covers: getting the twins, the dashboards and the ESP32's ingest
endpoint running on a server instead of a laptop. Written 29 August 2026.

## Why bother

Everything worked locally, but four things only ever ran by hand — `pod_bridge`,
the ASDT ingestor, a LAN port-forwarder and a Cloudflare tunnel — and died with
the terminal that started them. Worse, the node's ingest URL pointed at
`172.20.10.2`, a DHCP address handed out by a phone hotspot. When it changed,
uploads stopped. A droplet has a static IP, which removes that whole class of
problem.

## Sizing

Measured, not guessed:

| What | Memory |
|---|---|
| Brokers, storage, both dashboards (10 containers) | 854 MiB |
| \+ ingestor, pod-bridge, webapp | ~1.5 GB |
| Everything incl. Ditto ×6 and Neo4j ×2 | 5.5 GB |

**4 GB droplet.** Skip Ditto and Neo4j at first: they are ~4 GB of that last
figure and nothing in the sensor path needs them. Running the full set on an
8 GB Docker VM locally killed the daemon twice.

## Before you start

1. **Rotate the Anthropic key.** `.env` is tracked in git and contains
   `DT_LLM__API_KEY=sk-ant-…`. A gitignore rule cannot untrack it and deleting
   the line leaves it in history. Revoke the key, `git rm --cached .env`, then
   purge history with `git filter-repo`. The project now uses Ollama, so the
   key is dead weight anyway.
2. **Keep `.env.cloud` out of git.** Both repos ignore it. It carries the
   HiveMQ password, the Mongo Atlas URI with its password inline, the InfluxDB
   Cloud token and the Ollama key. Copy it to the droplet with `scp`, never a
   commit.
3. **Decide how ingest is authenticated** — see Security below.

## Droplet setup

```bash
# Ubuntu 24.04 + Docker (marketplace image, or install docker.io + compose v2)
ssh root@<droplet-ip>

git clone <your-remote>/merged-asdt.git
git clone <your-remote>/a-opdt.git
git clone <your-remote>/sensors.git        # firmware repo; pod_bridge lives here

# Secrets, copied not committed
scp .env.cloud root@<droplet-ip>:~/merged-asdt/.env.cloud
scp .env.cloud root@<droplet-ip>:~/a-opdt/.env.cloud
```

## Build and run

`pod-bridge` builds from the firmware repo, so it is built separately — compose
cannot reach outside its own build context.

```bash
cd ~/sensors
docker build -f docker/pod-bridge.Dockerfile -t asdt/pod-bridge:local .

cd ~/merged-asdt
export AOPDT_ENV_CLOUD=~/a-opdt/.env.cloud
docker compose -f docker-compose.yml \
               -f docker-compose.build.yml \
               -f docker-compose.deploy.yml \
               up -d --build mosquitto influxdb redis mongodb \
                             twin-api scientist-ui farmer-ui \
                             ingestor pod-bridge

cd ~/a-opdt
PUBLIC_API_URL=http://<droplet-ip>:8500 \
PUBLIC_WEB_ORIGIN=http://<droplet-ip>:5173 \
  docker compose -f docker-compose.yml -f docker-compose.webapp.yml \
                 up -d --build
```

`PUBLIC_API_URL` is baked into the frontend bundle at build time — changing it
needs a rebuild, not a restart. It must be the address a **browser** uses, not
`localhost`, or every viewer's browser will call its own machine.

## Ports

| Port | Service |
|---|---|
| 8000 | pod-bridge — the node POSTs here |
| 5173 | A-OPDT webapp (nginx) |
| 8500 | A-OPDT API |
| 9501 / 9502 | ASDT scientist / farmer portals |
| 9500 | ASDT twin API |

Open only what you need in the DO firewall. 2883/1883 (MQTT) should stay closed
— nothing outside the droplet needs them.

## Point the node at the droplet

The URL lives in NVS, so this is a re-provision, not a reflash:

1. Hold **BOOT**, tap **EN/RST**, keep BOOT held ~2 s. The check is a single pin
   read about 600 ms into boot, so tapping is not enough. This erases the stored
   Wi-Fi credentials.
2. Join the `soil-node-setup` access point, browse to `192.168.4.1`.
3. Fill in: network name, password, and
   `http://<droplet-ip>:8000/sync/upload`.
4. **Keep it `http://`.** The firmware cannot do TLS — `cloud.c` sets
   `crt_bundle_attach = NULL` — so an `https://` URL fails outright.

## Security

`pod_bridge` requires a shared secret when `INGEST_TOKEN` is set. Unset, the
endpoint is open and says so at startup — never run that on a public address.

Generate one and pass it in:

```bash
export INGEST_TOKEN=$(openssl rand -hex 24)
echo "$INGEST_TOKEN"
```

The node supplies it as a query parameter, so **no reflash is needed** — the
ingest URL lives in NVS and is set through the setup portal:

```
http://<droplet-ip>:8000/sync/upload?key=<token>
```

`X-Ingest-Key` and `Authorization: Bearer` are accepted too, for callers that
are not the node.

**The node stops working the moment you enable this**, until it is
re-provisioned with the new URL. Expect `401` and `rejected: bad or missing
ingest token` in the bridge log until then — that is the check working.

### What this does not protect against

It is a shared secret over plain HTTP. The firmware cannot do TLS — `cloud.c`
sets `crt_bundle_attach = NULL` — so the token crosses the network in the clear
and anyone able to observe the traffic can replay it. It stops opportunistic
writes from strangers who find the port. It is not protection against someone
on the path, and putting TLS on a reverse proxy would not change that while the
node itself speaks only HTTP. Closing that properly means firmware work: attach
the certificate bundle (already enabled in sdkconfig) and move to HTTPS.

## Verifying

```bash
docker compose ps                          # everything Up
docker logs -f asdt_pod_bridge             # one line per upload, four destinations
docker logs --tail 20 merged_asdt_ingestor # "Connected. Listening on ..."
curl -s http://<droplet-ip>:8500/docs      # A-OPDT API
```

A good pod-bridge line looks like:

```
172.20.10.4  ...Z  moisture=53.7%  air=29.5C  rh=83.0%  SDT=ok PDT=ok PDT☁=ok SDT☁=ok
```

`MISSED` on every destination at once usually means two `pod_bridge` instances
are running: the MQTT client ID is `pod-bridge-{name}` with no uniqueness, so a
second instance evicts the first about once a second. Run one.

## Not covered

- **Ditto and Neo4j** are omitted deliberately. Add them only with an 8 GB droplet.
- **TLS for the dashboards.** Everything above is plain HTTP. Put Caddy or nginx
  with Let's Encrypt in front before anyone outside the team uses it.
- **a-opdt's `twin.py`** is not containerized. Without it, and without a-opdt's
  InfluxDB, that webapp shows stage nominals rather than measurements — check
  `measured_count` on the dashboard, not the numbers.
- **Backups.** InfluxDB and Mongo write to Docker volumes on the droplet. Nothing
  snapshots them.
