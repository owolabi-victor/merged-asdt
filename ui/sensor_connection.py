# ui/sensor_connection.py
"""
Production-grade IoT Sensor Connection Module.

Handles real sensor onboarding for ASDT users:
  - Validate connection credentials before saving
  - Subscribe to user's MQTT topic with auth
  - Validate incoming payloads (schema, ranges)
  - Persist to InfluxDB + per-user manual_readings collection
  - Heartbeat monitoring (alert if sensor stops sending)
  - Automatic reconnection with exponential backoff
  - TLS support for production deployments

Each user-parcel can connect their own sensor by providing:
  - MQTT broker host:port
  - Username/password (or X.509 cert path)
  - Topic to subscribe to
  - Optional: payload schema mapping if they use different field names
"""
import json
import time
import ssl
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Callable
import paho.mqtt.client as mqtt
from pymongo import MongoClient

from shared.config import MONGO_URI, MONGO_DB, SENSOR_FIELDS
from shared.influx_io import write_point
from ui.data_sources import save_manual_reading, get_user_data_source

_client = MongoClient(MONGO_URI)
_db = _client[MONGO_DB]

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")


# ── Field validation ranges (sane physical limits) ──────────────────────────

VALIDATION_RANGES = {
    "soil_moisture_pct": (0.0, 100.0),
    "bulk_density_g_cm3": (0.5, 3.0),
    "soil_temp_c": (-30.0, 70.0),
}


def validate_payload(payload: dict, field_mapping: dict = None) -> tuple[dict, list]:
    """
    Validate an incoming sensor payload.
    Returns (clean_data, errors).

    field_mapping: optional dict to translate user's field names to canonical.
                   e.g., {"moisture": "soil_moisture_pct", "temp": "soil_temp_c"}
    """
    if not isinstance(payload, dict):
        return {}, ["Payload is not a JSON object"]

    field_mapping = field_mapping or {}
    clean = {}
    errors = []

    for raw_key, raw_val in payload.items():
        # Translate field name if mapping provided
        canonical = field_mapping.get(raw_key, raw_key)

        # Skip non-sensor fields silently (timestamp, deviceId, etc.)
        if canonical not in SENSOR_FIELDS:
            continue

        # Validate type
        try:
            val = float(raw_val)
        except (ValueError, TypeError):
            errors.append(f"{canonical}: not a number ({raw_val!r})")
            continue

        # Validate range
        lo, hi = VALIDATION_RANGES.get(canonical, (None, None))
        if lo is not None and (val < lo or val > hi):
            errors.append(f"{canonical}: {val} out of range [{lo}, {hi}]")
            continue

        clean[canonical] = val

    return clean, errors


# ── Connection validator (used before saving config) ────────────────────────

def test_mqtt_connection(broker: str, port: int, username: str = "",
                         password: str = "", topic: str = "",
                         use_tls: bool = False, timeout: int = 10) -> dict:
    """
    Test MQTT connection without subscribing permanently.
    Returns {success: bool, message: str, latency_ms: float}.
    """
    result = {"success": False, "message": "", "latency_ms": 0}
    start = time.time()
    connected = threading.Event()
    error_msg = []

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            connected.set()
        else:
            codes = {
                1: "Incorrect protocol version",
                2: "Invalid client identifier",
                3: "Server unavailable",
                4: "Bad username or password",
                5: "Not authorized",
            }
            error_msg.append(codes.get(rc, f"Connection refused (code {rc})"))

    client = mqtt.Client(client_id=f"asdt-test-{int(time.time())}", clean_session=True)
    if username:
        client.username_pw_set(username, password)
    if use_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect

    try:
        client.connect(broker, port, keepalive=timeout)
        client.loop_start()

        if connected.wait(timeout):
            latency = (time.time() - start) * 1000

            # Optionally test subscription
            if topic:
                msg_received = threading.Event()

                def on_msg(c, u, m):
                    msg_received.set()

                client.on_message = on_msg
                client.subscribe(topic)
                # Don't wait for message — just confirm subscription doesn't fail
                time.sleep(0.5)

            result["success"] = True
            result["message"] = f"Connected to {broker}:{port}"
            result["latency_ms"] = round(latency, 1)
        else:
            result["message"] = error_msg[0] if error_msg else f"Connection timeout after {timeout}s"
    except (OSError, ConnectionRefusedError) as e:
        result["message"] = f"Connection error: {e}"
    except Exception as e:
        result["message"] = f"Unexpected error: {type(e).__name__}: {e}"
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    return result


# ── Persistent sensor subscriber (one per user) ─────────────────────────────

class UserSensorClient:
    """
    Long-running MQTT subscriber for a single user-parcel.
    Persists incoming data to InfluxDB and manual_readings.
    Auto-reconnects on disconnect.
    """

    def __init__(self, user_id: str, parcel_id: str, config: dict):
        self.user_id = user_id
        self.parcel_id = parcel_id
        self.config = config
        self.field_mapping = config.get("field_mapping", {})
        self.client = None
        self.running = False
        self.message_count = 0
        self.error_count = 0
        self.last_message_at = None
        self.thread = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info(f"[Sensor:{self.user_id[:8]}] Connected to broker, subscribing to {self.config['topic']}")
            client.subscribe(self.config["topic"], qos=1)
            self._update_status("connected")
        else:
            logger.error(f"[Sensor:{self.user_id[:8]}] Connection failed: code {rc}")
            self._update_status("error", f"Connection refused (code {rc})")

    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"[Sensor:{self.user_id[:8]}] Disconnected (code {rc}), will auto-reconnect")
        self._update_status("disconnected")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            self.error_count += 1
            logger.warning(f"[Sensor:{self.user_id[:8]}] Invalid JSON: {msg.payload[:100]}")
            return

        clean, errors = validate_payload(payload, self.field_mapping)

        if errors:
            self.error_count += 1
            logger.warning(f"[Sensor:{self.user_id[:8]}] Validation errors: {errors}")
            self._log_validation_errors(errors, payload)

        if not clean:
            return

        # Persist to InfluxDB (with user tag)
        try:
            write_point("soil_telemetry", clean, tags={
                "user_id": self.user_id,
                "parcel_id": self.parcel_id,
                "source": "iot_sensor",
            })
        except Exception as e:
            logger.error(f"[Sensor:{self.user_id[:8]}] InfluxDB write failed: {e}")
            self.error_count += 1

        # Persist to per-user manual_readings (keeps user data isolated)
        for field, val in clean.items():
            try:
                save_manual_reading(self.user_id, self.parcel_id, field, val,
                                    source="iot_sensor")
            except Exception as e:
                logger.error(f"[Sensor:{self.user_id[:8]}] Manual reading save failed: {e}")

        self.message_count += 1
        self.last_message_at = datetime.now(timezone.utc)
        self._update_heartbeat()

    def _update_status(self, status: str, error: str = None):
        update = {
            "user_id": self.user_id,
            "parcel_id": self.parcel_id,
            "status": status,
            "updated_at": datetime.now(timezone.utc),
            "message_count": self.message_count,
            "error_count": self.error_count,
        }
        if error:
            update["last_error"] = error
        if self.last_message_at:
            update["last_message_at"] = self.last_message_at

        _db.sensor_connections.update_one(
            {"user_id": self.user_id, "parcel_id": self.parcel_id},
            {"$set": update},
            upsert=True,
        )

    def _update_heartbeat(self):
        _db.sensor_connections.update_one(
            {"user_id": self.user_id, "parcel_id": self.parcel_id},
            {"$set": {
                "last_message_at": self.last_message_at,
                "message_count": self.message_count,
                "status": "active",
            }},
            upsert=True,
        )

    def _log_validation_errors(self, errors: list, payload: dict):
        _db.sensor_validation_errors.insert_one({
            "user_id": self.user_id,
            "parcel_id": self.parcel_id,
            "errors": errors,
            "payload_sample": str(payload)[:500],
            "timestamp": datetime.now(timezone.utc),
        })

    def start(self):
        """Start the sensor client in a background thread."""
        if self.running:
            return
        self.running = True

        def _run():
            self.client = mqtt.Client(
                client_id=f"asdt-{self.user_id[:8]}-{self.parcel_id}",
                clean_session=False,  # Resume subscriptions on reconnect
            )

            if self.config.get("username"):
                self.client.username_pw_set(
                    self.config["username"],
                    self.config.get("password", ""),
                )

            if self.config.get("use_tls"):
                self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            # Auto-reconnect with exponential backoff
            self.client.reconnect_delay_set(min_delay=1, max_delay=60)

            try:
                self.client.connect(
                    self.config["broker"],
                    int(self.config.get("port", 1883)),
                    keepalive=60,
                )
                self.client.loop_forever()
            except Exception as e:
                logger.error(f"[Sensor:{self.user_id[:8]}] Fatal error: {e}")
                self._update_status("error", str(e))

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the sensor client."""
        self.running = False
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
        self._update_status("stopped")


# ── Connection registry (singleton) ─────────────────────────────────────────

_active_clients: dict[str, UserSensorClient] = {}
_lock = threading.Lock()


def start_user_sensor(user_id: str, parcel_id: str) -> dict:
    """Start MQTT subscriber for this user's sensor (idempotent)."""
    key = f"{user_id}:{parcel_id}"

    with _lock:
        # Stop existing if running
        if key in _active_clients:
            _active_clients[key].stop()
            del _active_clients[key]

        # Look up config
        source = get_user_data_source(user_id, parcel_id)
        if not source or source.get("mode") != "sensor":
            return {"success": False, "message": "User does not have sensor mode configured"}

        config = source.get("config", {})
        if not config.get("broker") or not config.get("topic"):
            return {"success": False, "message": "Incomplete sensor config (missing broker or topic)"}

        client = UserSensorClient(user_id, parcel_id, config)
        client.start()
        _active_clients[key] = client

    return {"success": True, "message": "Sensor client started"}


def stop_user_sensor(user_id: str, parcel_id: str) -> dict:
    """Stop MQTT subscriber for this user."""
    key = f"{user_id}:{parcel_id}"
    with _lock:
        if key in _active_clients:
            _active_clients[key].stop()
            del _active_clients[key]
            return {"success": True, "message": "Sensor stopped"}
    return {"success": False, "message": "No active sensor for this user"}


def get_sensor_status(user_id: str, parcel_id: str) -> dict:
    """Get current status of a user's sensor connection."""
    doc = _db.sensor_connections.find_one(
        {"user_id": user_id, "parcel_id": parcel_id},
        {"_id": 0},
    )
    if not doc:
        return {"status": "not_started", "message": "No sensor connection record"}

    # Check if heartbeat is stale
    last_msg = doc.get("last_message_at")
    if last_msg:
        if isinstance(last_msg, str):
            last_msg = datetime.fromisoformat(last_msg.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last_msg).total_seconds()
        doc["seconds_since_last_message"] = round(age, 1)
        if age > 300:  # 5 minutes
            doc["alert"] = "Sensor has not sent data for over 5 minutes"

    return doc


def restart_all_sensors():
    """Start sensors for all users in 'sensor' mode (called on system startup)."""
    sources = _db.user_data_sources.find({"mode": "sensor"})
    started = 0
    for src in sources:
        result = start_user_sensor(src["user_id"], src["parcel_id"])
        if result["success"]:
            started += 1
    logger.info(f"[Sensor] Restarted {started} user sensor connections")
    return started