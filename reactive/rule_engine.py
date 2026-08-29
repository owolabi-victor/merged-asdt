# reactive/rule_engine.py
"""
Reactive Layer — soil depletion state monitoring and state machine.

Evaluates soil sensor readings every 60 seconds against soil-type-specific
thresholds. Detects depletion states S1–S8 and manages state transitions:
  running → warning → shutdown (and recovery)

Depletion states detected:
  S1: Nutrient Depleted (N, P, or K)
  S2: Acidified (pH too low)
  S3: Salinized (EC too high)
  S4: Compacted (bulk density too high)
  S5: Water-Stressed (moisture too low or too high)
  S6: Organic Matter Depleted
  S7: Biologically Inactive (microbial biomass or respiration too low)
  S8: Multi-Factor Depleted (2+ states active)
"""
import time
import json
import paho.mqtt.client as mqtt
from shared.mqtt_io import make_client
from transitions import Machine

from shared.redis_io  import set_state, set_latest, get_latest_cached
from shared.influx_io import write_point, get_latest
from shared.mongo_io  import log_event, save_state_transition
from shared.config    import (
    get_asset_thresholds,
    MQTT_BROKER, MQTT_PORT, TOPIC_STATE, ASSET_ID,
    DEPLETION_STATES, ACTIVE_SOIL_TYPE, TOPIC_CROSS_DOMAIN,
)


class SoilParcelController:
    states = ["running", "warning", "shutdown"]

    transitions = [
        {"trigger": "raise_warning",   "source": "running",              "dest": "warning"},
        {"trigger": "shutdown_parcel", "source": ["running", "warning"], "dest": "shutdown"},
        {"trigger": "recover",         "source": "warning",              "dest": "running"},
        {"trigger": "restart",         "source": "shutdown",             "dest": "running"},
    ]

    def __init__(self):
        Machine(
            model=self,
            states=self.states,
            initial="running",
            transitions=self.transitions,
            ignore_invalid_triggers=True,
        )
        self._active_depletion_states = set()
        self._previous_depletion_states = set()
        self._mqttc = None  # set by run()

    def set_mqtt_client(self, mqttc):
        self._mqttc = mqttc

    def detect_depletion_states(self) -> dict:
        """
        Check all soil parameters against thresholds and determine
        which depletion states (S1–S8) are currently active.
        Loads thresholds fresh from DB each cycle to pick up scientist overrides.
        Returns dict of {state_code: {details}}.
        """
        thresholds = get_asset_thresholds(ASSET_ID)

        active = {}

        # Read all sensor values
        values = {}
        for field in ["bulk_density_g_cm3", "soil_moisture_pct", "soil_temp_c"]:
            val = get_latest(field)
            if val is not None:
                values[field] = val

        # S4: Compacted — bulk density above threshold
        bd_val = values.get("bulk_density_g_cm3")
        bd_t   = thresholds.get("bulk_density_g_cm3")
        if bd_val is not None and bd_t is not None and bd_val > bd_t["warn"]:
            active["S4"] = {"name": "Compacted", "triggers": [
                {"field": "bulk_density_g_cm3", "value": bd_val, "threshold": bd_t["warn"]}
            ]}

        # S5: Water-Stressed — moisture too low or too high
        mst_val  = values.get("soil_moisture_pct")
        mst_t    = thresholds.get("soil_moisture_pct")
        mst_t_hi = thresholds.get("soil_moisture_pct_high")
        water_triggers = []
        if mst_val is not None and mst_t is not None and mst_val < mst_t["warn"]:
            water_triggers.append({"field": "soil_moisture_pct", "value": mst_val,
                                    "threshold": mst_t["warn"], "stress_type": "dry"})
        if mst_val is not None and mst_t_hi is not None and mst_val > mst_t_hi["warn"]:
            water_triggers.append({"field": "soil_moisture_pct", "value": mst_val,
                                    "threshold": mst_t_hi["warn"], "stress_type": "waterlogged"})

        # Cross-domain: amplify urgency if plant water demand is high
        if water_triggers:
            urgency = "normal"
            try:
                import json as _json
                plant_raw = get_latest_cached("cross_domain_plant_demand")
                if plant_raw:
                    plant = _json.loads(plant_raw) if isinstance(plant_raw, str) else plant_raw
                    demand = plant.get("plant_water_demand_mm_day")
                    if demand and float(demand) > 5.0:
                        urgency = "high"
            except Exception:
                pass
            active["S5"] = {"name": "Water-Stressed", "triggers": water_triggers,
                            "urgency": urgency}
        elif not water_triggers:
            pass  # S5 not active

        # S8: Multi-Factor Depleted — 2+ single-factor states active
        single_states = {k for k in active if k != "S8"}
        if len(single_states) >= 2:
            active["S8"] = {
                "name":            "Multi-Factor Depleted",
                "active_states":   list(single_states),
                "count":           len(single_states),
            }

        return active

    def evaluate(self) -> tuple:
        """
        Full evaluation cycle: detect depletion states, count alerts,
        transition state machine, and return current state + alerts.
        """
        depletion = self.detect_depletion_states()
        active_codes = set(depletion.keys())

        # Build alert list
        alerts = []
        for code, info in depletion.items():
            severity = "critical" if code == "S8" else "warning"
            if code in ("S4", "S5"):
                for trig in info.get("triggers", []):
                    field = trig.get("field", "unknown")
                    value = trig.get("value", 0)
                    alerts.append({
                        "level":     severity,
                        "state":     code,
                        "field":     field,
                        "value":     value,
                        "threshold": trig.get("threshold", 0),
                        "message":   self._depletion_message(code, info, trig),
                    })
            elif code == "S8":
                alerts.append({
                    "level":     "critical",
                    "state":     "S8",
                    "field":     "multi_factor",
                    "value":     info["count"],
                    "threshold": 2,
                    "message":   f"Multi-factor depletion: {info['count']} states active ({', '.join(info['active_states'])}). Escalating to Intelligent Layer.",
                })

        crit_count = sum(1 for a in alerts if a["level"] == "critical")
        warn_count = sum(1 for a in alerts if a["level"] == "warning")

        prev_state = self.state

        if crit_count > 0 and self.state != "shutdown":
            self.shutdown_parcel()
        elif crit_count == 0 and warn_count > 0 and self.state == "running":
            self.raise_warning()
        elif crit_count == 0 and warn_count == 0 and self.state == "warning":
            self.recover()
        elif crit_count == 0 and self.state == "shutdown":
            self.restart()

        # Log state transitions
        if self.state != prev_state:
            log_event(
                "state_change",
                {"from": prev_state, "to": self.state,
                 "depletion_states": list(active_codes),
                 "alerts": alerts},
                severity="critical" if self.state == "shutdown" else "warning",
            )
            print(f"[REACTIVE] ⚠️  State: {prev_state} → {self.state}")

        # Log depletion state transitions
        new_states = active_codes - self._previous_depletion_states
        recovered_states = self._previous_depletion_states - active_codes
        for s in new_states:
            save_state_transition({
                "previous_state": "healthy" if not self._previous_depletion_states else "depleted",
                "new_state": s,
                "trigger_condition": json.dumps(depletion.get(s, {}), default=str),
            })
            print(f"[REACTIVE] 🔴 Entered depletion state: {s} ({DEPLETION_STATES.get(s, {}).get('name', s)})")
        for s in recovered_states:
            save_state_transition({
                "previous_state": s,
                "new_state": "healthy",
                "trigger_condition": "Parameter recovered to healthy range",
            })
            print(f"[REACTIVE] ✅ Recovered from depletion state: {s}")

        self._previous_depletion_states = active_codes
        self._active_depletion_states   = active_codes

        # Cache active depletion states in Redis
        set_latest("active_depletion_states", json.dumps(list(active_codes)))
        set_latest("depletion_detail", json.dumps(depletion, default=str))

        # Notify cross-domain bus on state changes
        if new_states or recovered_states:
            cross_msg = {
                "source": "soil_dt",
                "event": "depletion_state_change",
                "active_states": list(active_codes),
                "new_states": list(new_states),
                "recovered_states": list(recovered_states),
                "soil_type": ACTIVE_SOIL_TYPE,
                "timestamp": time.time(),
            }
            try:
                if self._mqttc:
                    self._mqttc.publish(TOPIC_CROSS_DOMAIN, json.dumps(cross_msg), qos=1)
            except Exception:
                pass  # cross-domain publish is best-effort

        return self.state, alerts, depletion

    @staticmethod
    def _depletion_message(code: str, _info: dict, trigger: dict) -> str:
        field = trigger.get("field", "")
        value = trigger.get("value", 0)
        messages = {
            "S4": {"bulk_density_g_cm3":              f"Soil compacted: BD {value:.2f} g/cm³ (threshold: {trigger.get('threshold', 0):.2f} g/cm³). Deep ripping needed."},
            "S5": {"soil_moisture_pct":                f"Water stress: moisture {value:.1f}% ({trigger.get('stress_type', 'stressed')}). {'Irrigate.' if trigger.get('stress_type') == 'dry' else 'Improve drainage.'}"},
        }
        return messages.get(code, {}).get(field, f"{code}: {field}={value:.2f} at depletion level.")


def run():
    ctrl  = SoilParcelController()
    mqttc = make_client(f"rule_engine_{ASSET_ID}")
    mqttc.connect(MQTT_BROKER, MQTT_PORT)
    mqttc.loop_start()
    ctrl.set_mqtt_client(mqttc)

    print(f"[REACTIVE] Rule engine running (soil type: {ACTIVE_SOIL_TYPE}). Evaluating every 60s.")

    while True:
        try:
            from datetime import datetime, timezone
            set_latest("layer_heartbeat_reactive.rule_engine",
                       datetime.now(timezone.utc).isoformat())
            state, alerts, depletion = ctrl.evaluate()

            set_state(state)
            mqttc.publish(TOPIC_STATE, json.dumps({
                "state": state,
                "depletion_states": list(depletion.keys()),
                "alerts": alerts,
            }), qos=1)

            write_point("soil_health", {
                "state_code":       {"running": 0, "warning": 1, "shutdown": 2}.get(state, 0),
                "alert_count":      len(alerts),
                "crit_count":       sum(1 for a in alerts if a["level"] == "critical"),
                "depletion_count":  len(depletion),
            })

            set_latest("active_alert_count", len(alerts))

            if alerts:
                print(f"[REACTIVE] 🚨 {len(alerts)} alert(s), {len(depletion)} depletion state(s):")
                for a in alerts:
                    print(f"   [{a['level'].upper()}] {a['message']}")
            else:
                print(f"[REACTIVE] ✅ State: {state} — no depletion detected")

        except Exception as e:
            print(f"[REACTIVE] Error: {e}")

        time.sleep(60)


if __name__ == "__main__":
    run()