# data_mgmt/pipeline.py
"""
Data Management Layer — continuous background processing pipeline.
Runs every 60 seconds. Computes smoothed values, quality flags, and health score
across all 11 soil parameters (physical, chemical, biological).
"""
import time
import numpy as np
import warnings

from influxdb_client.client.warnings import MissingPivotFunction
warnings.simplefilter("ignore", MissingPivotFunction)

from shared.influx_io import query_recent, write_point, get_latest
from shared.redis_io  import set_latest
from shared.config    import SENSOR_FIELDS, get_asset_thresholds, ASSET_ID

# Quality flag labels for display
QUALITY_LABEL = {1.0: "OK", 2.0: "⚠ WARN", 3.0: "🔴 CRIT"}


class SoilDataPipeline:
    INTERVAL = 60

    def run_once(self):
        # Load thresholds fresh each run so scientist overrides are honoured
        thresholds = get_asset_thresholds(ASSET_ID)

        processed    = {}
        quality_vals = {}
        field_status = {}

        for field in SENSOR_FIELDS:
            df = query_recent(field, minutes=10)
            if len(df) < 2:
                continue

            vals     = df["_value"].astype(float).values
            window   = min(5, len(vals))
            smoothed = float(np.convolve(vals, np.ones(window) / window, mode="valid")[-1])
            roc      = float(vals[-1] - vals[-2])

            processed[f"{field}_smooth"] = round(smoothed, 4)
            processed[f"{field}_roc"]    = round(roc, 4)

            if field in thresholds:
                t   = thresholds[field]
                v   = vals[-1]
                low = t.get("low", False)

                if (low and v < t["crit"]) or (not low and v > t["crit"]):
                    quality = 3.0
                elif (low and v < t["warn"]) or (not low and v > t["warn"]):
                    quality = 2.0
                else:
                    quality = 1.0

                processed[f"{field}_quality"] = quality
                quality_vals[field]            = quality
                field_status[field]            = (round(v, 3), quality)

        health_score = self._compute_health_score(quality_vals, thresholds)
        processed["soil_health_score"] = health_score

        if processed:
            write_point("soil_processed", processed)
            for k, v in processed.items():
                set_latest(k, v)

        # Status printout
        print(f"\n[DATA MGMT] ─── Soil Health Report ───────────────────────────")
        for field, (val, q) in field_status.items():
            label = QUALITY_LABEL.get(q, "?")
            print(f"[DATA MGMT]   {field:<42} {val:>8}   {label}")
        print(f"[DATA MGMT]   {'HEALTH SCORE':<42} {health_score:>7.1f}/100")
        print(f"[DATA MGMT] ──────────────────────────────────────────────────")

    def _compute_health_score(self, quality_vals: dict, thresholds: dict) -> float:
        """
        Health score = 100 minus penalty for each monitored field
        that is at warning or critical level.
        Uses the effective thresholds passed in (scientist overrides applied).
        """
        monitored = [f for f in thresholds if f not in
                     ("soil_moisture_pct_high",)]
        if not monitored:
            return 100.0
        share = 100.0 / len(monitored)
        score = 100.0
        for field in monitored:
            q = quality_vals.get(field)
            if q is None:
                continue
            if q == 3.0:
                score -= share
            elif q == 2.0:
                score -= share * 0.4
        return round(max(0.0, score), 1)

    def run(self):
        print("[DATA MGMT] Pipeline running (monitoring all soil parameters)...")
        while True:
            try:
                set_latest("layer_heartbeat_data_mgmt.pipeline",
                           __import__("datetime").datetime.utcnow().isoformat())
                self.run_once()
            except Exception as e:
                print(f"[DATA MGMT] Error: {e}")
            time.sleep(self.INTERVAL)


if __name__ == "__main__":
    SoilDataPipeline().run()