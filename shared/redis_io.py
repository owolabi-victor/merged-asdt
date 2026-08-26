# shared/redis_io.py
"""
Redis helper for fast in-memory caching.
Used for:
  - Latest sensor values (avoid InfluxDB round-trip on every read)
  - Operational state string (running / warning / shutdown)
  - Diagnosis cache (current active diagnosis for the parcel)
  - Agent pub/sub for real-time MAS coordination
"""
import redis
import json
from shared.config import REDIS_URL, ASSET_ID

# Managed Redis sits across the internet here, not on loopback, so a slow link
# has to be tolerated rather than treated as an error. Retry the round-trip and
# keep the socket alive between the infrequent telemetry writes.
_r = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_timeout=15,
    socket_connect_timeout=15,
    socket_keepalive=True,
    retry_on_timeout=True,
    health_check_interval=30,
)


# ── Latest sensor value cache ─────────────────────────────────────────────────

def set_latest(field: str, value):
    """
    Cache the most recent value of any field under the asset hash.

    The cache is an optimisation over InfluxDB, so losing a write costs a
    round-trip later, not the reading. Never let it break the ingest path.
    """
    try:
        _r.hset(f"{ASSET_ID}:latest", field, json.dumps(value))
        return True
    except redis.RedisError as exc:
        print(f"[redis] cache write dropped ({type(exc).__name__}: {exc})", flush=True)
        return False

def get_latest_cached(field: str):
    """
    Retrieve the cached value of a field.
    Returns None if not yet set — callers must handle None.
    """
    try:
        raw = _r.hget(f"{ASSET_ID}:latest", field)
    except redis.RedisError as exc:
        # Indistinguishable from "not cached" to every caller, and they all fall
        # through to InfluxDB, which is the authoritative store anyway.
        print(f"[redis] cache read failed ({type(exc).__name__}: {exc})", flush=True)
        return None
    return json.loads(raw) if raw else None


# ── Operational state ─────────────────────────────────────────────────────────

def set_state(state: str):
    """Set the current operational state: 'running', 'warning', or 'shutdown'."""
    _r.set(f"{ASSET_ID}:state", state)

def get_state() -> str:
    return _r.get(f"{ASSET_ID}:state") or "unknown"


# ── Active diagnosis cache ────────────────────────────────────────────────────

def set_active_diagnosis(diagnosis: dict):
    """Cache the most recent diagnosis for fast access by the advisory interface."""
    _r.set(f"{ASSET_ID}:active_diagnosis", json.dumps(diagnosis))

def get_active_diagnosis() -> dict:
    raw = _r.get(f"{ASSET_ID}:active_diagnosis")
    return json.loads(raw) if raw else {}


# ── Pub/Sub for agent coordination ───────────────────────────────────────────

def publish(channel: str, message: dict):
    """Publish an inter-agent message on a Redis channel."""
    _r.publish(channel, json.dumps(message))

def subscribe(channel: str):
    """Return a PubSub object subscribed to the given channel."""
    ps = _r.pubsub()
    ps.subscribe(channel)
    return ps
