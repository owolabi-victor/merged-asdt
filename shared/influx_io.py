# shared/influx_io.py
import warnings
import pandas as pd
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.client.warnings import MissingPivotFunction
from shared.config import INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET, ASSET_ID

# Suppress the pivot warning — we query one field at a time intentionally
warnings.simplefilter("ignore", MissingPivotFunction)

_client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_write_api = _client.write_api(write_options=SYNCHRONOUS)
_query_api = _client.query_api()


def write_point(measurement: str, fields: dict, tags: dict = None):
    """Write one data point to InfluxDB."""
    p = Point(measurement).tag("asset_id", ASSET_ID)
    if tags:
        for k, v in tags.items():
            p = p.tag(k, v)
    for k, v in fields.items():
        p = p.field(k, float(v))
    _write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)


def query_recent(field: str, minutes: int = 10) -> pd.DataFrame:
    """Return a DataFrame [_time, _value] for the given field over the last N minutes."""
    flux = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{minutes}m)
          |> filter(fn:(r) => r._measurement == "soil_telemetry")
          |> filter(fn:(r) => r._field == "{field}")
          |> sort(columns:["_time"])
    '''
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MissingPivotFunction)
        result = _query_api.query_data_frame(flux, org=INFLUX_ORG)

    if isinstance(result, list):
        result = pd.concat(result) if result else pd.DataFrame()
    if result.empty:
        return pd.DataFrame(columns=["_time", "_value"])
    return result[["_time", "_value"]].reset_index(drop=True)


def get_latest(field: str):
    """
    Return the most recent sensor value, or None if no data yet.
    Returns None — NOT 0.0 — so callers can distinguish missing data from a zero reading.
    """
    df = query_recent(field, minutes=5)
    if df.empty:
        return None
    return float(df["_value"].iloc[-1])