# Captured telemetry — 28 August 2026

Exported from InfluxDB Cloud on 29 August 2026, covering 04:00–08:30Z on
28 August: the longest continuous period in which the ESP32 node ran with
working instruments.

Exported because both cloud buckets carry 30-day retention and expire around
27 September 2026, and because the node's hardware was subsequently taken
apart — this is the only record of the sensors working.

| File | Twin | Bucket |
|---|---|---|
| `pdt-maize_telemetry-2026-08-28.csv` | PDT (a-opdt) | `maize_telemetry` |
| `asdt-soil_telemetry-2026-08-28.csv` | ASDT (merged-asdt) | `soil_telemetry` |

## What is measured, and what is not

PDT carries four measured fields — `air_temperature` and `relative_humidity`
from the DHT11, `canopy_temperature` from the MLX90614, and `canopy_air_delta`
derived from the two temperatures. Everything else in `asset_telemetry` is the
growth-stage nominal from `config/sensor_profiles.yaml`, not an observation.

ASDT carries one measured field, `soil_moisture_pct`. Its simulation layer
produced only two points across the window, so `soil_simulation/*` and
`soil_residuals/*` are too sparse to draw anything from.

## Reading soil_moisture

`soil_moisture` (PDT) and `soil_moisture_pct` (ASDT) both span 0–100 with a
mean below 2. The probe was making intermittent contact: genuine low readings
in dry air, punctuated by 100 % spikes when the input floated and the
calibration map clamped it. Filter on the reading's `measured_fields` before
using these quantitatively, or the spikes will dominate any average.

The source-side health check that rejects those spikes was added after this
capture, so the artefact is present here in a way it should not be in later
data.
