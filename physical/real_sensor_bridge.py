"""
Publish real ESP32 soil readings into the twin, in place of the simulator.

The simulator in physical/simulator.py invents every field with random.gauss().
This publisher uses measurements actually taken by the hardware node for the one
field it can measure — soil moisture — and is explicit about the rest.

Fields with no sensor are still emitted, because the reactive and intelligent
layers expect the full SENSOR_FIELDS contract, but each reading carries a
`measured_fields` list naming what came from hardware. Nothing downstream has to
guess which numbers are real.

    python -m physical.real_sensor_bridge --csv data/soil_dataset.csv
    python -m physical.real_sensor_bridge --csv data/soil_dataset.csv --interval 2
"""
import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from shared.config import (
    ACTIVE_SOIL_TYPE,
    ASSET_ID,
    MQTT_BROKER,
    MQTT_PORT,
    SENSOR_FIELDS,
    TOPIC_TELEMETRY,
)
# Nominal baselines live with the simulator, not in shared config.
from physical.simulator import NOMINAL_BY_TYPE

# The node carries a resistive moisture probe only.
MEASURED_FIELDS = ["soil_moisture_pct"]


def load_readings(csv_path: Path):
    """Real readings, oldest first, as (timestamp, moisture_pct, temp_c)."""
    rows = []
    with open(csv_path) as handle:
        for row in csv.DictReader(handle):
            try:
                moisture = float(row["moisture_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            temp = row.get("air_temp_c")
            rows.append((
                row.get("timestamp_utc"),
                moisture,
                float(temp) if temp not in (None, "", "None") else None,
            ))
    return rows


def _to_epoch(iso_timestamp):
    """
    The ingestor's schema declares `timestamp: float`, so an ISO string is
    rejected outright. Convert, keeping the real capture time rather than
    stamping "now" - the twin's time series depends on when the soil was
    actually measured.
    """
    if not iso_timestamp:
        return datetime.now(timezone.utc).timestamp()
    try:
        return datetime.fromisoformat(
            iso_timestamp.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return datetime.now(timezone.utc).timestamp()


def build_payload(timestamp, moisture_pct, temp_c, nominal):
    """One telemetry message matching the simulator's contract."""
    payload = {f: nominal.get(f) for f in SENSOR_FIELDS}

    # The measured value replaces the nominal one.
    payload["soil_moisture_pct"] = round(moisture_pct, 2)
    if temp_c is not None:
        payload["soil_temp_c"] = round(temp_c, 2)

    payload.update({
        "asset_id": ASSET_ID,
        "timestamp": _to_epoch(timestamp),   # float epoch, as the schema requires
        "soil_type": ACTIVE_SOIL_TYPE,
        "fault_mode": False,
        "spike_injected": False,
        # Provenance. The simulator publishes neither, so downstream layers can
        # tell a measured reading from an invented one.
        "source": "esp32_node",
        "measured_fields": MEASURED_FIELDS,
    })
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path,
                        help="CSV of captured readings")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between messages (the simulator uses 30)")
    parser.add_argument("--loop", action="store_true",
                        help="restart from the beginning when the series ends")
    parser.add_argument("--tail", type=int, default=0,
                        help="replay only the last N readings. The wetting events "
                             "sit at the end of the series, so a short demo that "
                             "starts from the beginning shows only dry baseline.")
    args = parser.parse_args()

    readings = load_readings(args.csv)
    if not readings:
        raise SystemExit(f"no usable readings in {args.csv}")

    if args.tail:
        readings = readings[-args.tail:]

    moistures = [m for _, m, _ in readings]
    print(f"[REAL] {len(readings)} readings, "
          f"moisture {min(moistures):.1f}%..{max(moistures):.1f}%")

    nominal = NOMINAL_BY_TYPE.get(ACTIVE_SOIL_TYPE, NOMINAL_BY_TYPE["loamy"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, f"real_sensor_{ASSET_ID}")
    client.connect(MQTT_BROKER, MQTT_PORT)
    client.loop_start()
    print(f"[REAL] publishing to {TOPIC_TELEMETRY} every {args.interval}s")
    print(f"[REAL] measured from hardware: {', '.join(MEASURED_FIELDS)}")

    sent = 0
    while True:
        for timestamp, moisture, temp in readings:
            payload = build_payload(timestamp, moisture, temp, nominal)
            client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)
            sent += 1
            bar = "#" * int(moisture / 5)
            print(f"  [{sent:>4}] moisture={moisture:>5.1f}%  {bar}")
            time.sleep(args.interval)
        if not args.loop:
            break

    print(f"[REAL] published {sent} readings")
    client.loop_stop()


if __name__ == "__main__":
    main()
