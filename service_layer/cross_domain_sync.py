# service_layer/cross_domain_sync.py
"""
Cross-Domain Synchronizer — manages bidirectional data flow with other DTs.

The ASDT operates strictly within the soil domain. It does NOT observe or
interpret plant symptoms, diagnose plant diseases, recommend crops, or
provide market advice.

The ASDT DOES:
  - Publish soil state data to the cross-domain bus (→ Plant DT, Biotic Pod DT)
  - Consume plant-driven soil demands (← Plant DT, Biotic Pod DT)
  - Adjust recommendations based on cross-domain inputs

Cross-Domain Data Bus: MQTT publish-subscribe on dedicated topics.
  Publish:  dt/{asset_id}/cross_domain/soil_to_plant
            dt/{asset_id}/cross_domain/soil_to_biotic
  Subscribe: dt/{asset_id}/cross_domain/plant_to_soil
             dt/{asset_id}/cross_domain/biotic_to_soil

Message format follows the cross-domain interface spec from the design doc.
"""
import time
import json
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from shared.config import (
    MQTT_BROKER, MQTT_PORT, ASSET_ID, SENSOR_FIELDS,
    ACTIVE_SOIL_TYPE, DEPLETION_STATES,
)
from shared.influx_io import get_latest
from shared.redis_io  import get_latest_cached, set_latest
from shared.mongo_io  import save_cross_domain_message, log_event


# ── Cross-Domain MQTT Topics ─────────────────────────────────────────────────
TOPIC_SOIL_TO_PLANT  = f"dt/{ASSET_ID}/cross_domain/soil_to_plant"
TOPIC_SOIL_TO_BIOTIC = f"dt/{ASSET_ID}/cross_domain/soil_to_biotic"
TOPIC_PLANT_TO_SOIL  = f"dt/{ASSET_ID}/cross_domain/plant_to_soil"
TOPIC_BIOTIC_TO_SOIL = f"dt/{ASSET_ID}/cross_domain/biotic_to_soil"


class CrossDomainSynchronizer:
    """
    Manages bidirectional data exchange between the Soil DT and other
    domain DTs (Plant DT, Biotic Pod DT) without knowing their internals.
    """

    def __init__(self):
        self._mqttc = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            f"cross_domain_{ASSET_ID}",
        )
        self._mqttc.on_connect = self._on_connect
        self._mqttc.on_message = self._on_message
        self._plant_demand = {}     # latest consumed from Plant DT
        self._biotic_data  = {}     # latest consumed from Biotic Pod DT

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(TOPIC_PLANT_TO_SOIL, qos=1)
            client.subscribe(TOPIC_BIOTIC_TO_SOIL, qos=1)
            print(f"[CROSS-DOMAIN] ✅ Connected. Subscribed to incoming topics.")
        else:
            print(f"[CROSS-DOMAIN] ❌ MQTT connection failed: {rc}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming messages from other DTs."""
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            topic = msg.topic

            if topic == TOPIC_PLANT_TO_SOIL:
                self._consume_plant_data(data)
            elif topic == TOPIC_BIOTIC_TO_SOIL:
                self._consume_biotic_data(data)

        except Exception as e:
            print(f"[CROSS-DOMAIN] ❌ Error processing message: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLISH — ASDT provides data to other DTs
    # ─────────────────────────────────────────────────────────────────────────

    def publish_to_plant_dt(self):
        """
        Publish soil state data that the Plant DT needs:
        - Soil moisture (VWC, field capacity proxy)
        - Soil nutrient availability (N, P, K)
        - Soil pH (affects nutrient availability to plants)
        - Soil temperature (affects root growth)
        - Bulk density (affects root penetration)
        - EC / salinity (affects plant salt stress)
        - Current depletion state
        """
        # Gather current values
        payload_fields = {
            "N_available_ppm":   get_latest("nitrogen_ppm"),
            "P_available_ppm":   get_latest("phosphorus_ppm"),
            "K_available_ppm":   get_latest("potassium_ppm"),
            "VWC_percent":       get_latest("soil_moisture_pct"),
            "pH":                get_latest("soil_ph"),
            "EC_dS_m":           get_latest("ec_ds_m"),
            "soil_temperature_c":get_latest("soil_temp_c"),
            "bulk_density_g_cm3":get_latest("bulk_density_g_cm3"),
            "organic_matter_pct":get_latest("organic_matter_pct"),
        }
        # Remove None values
        payload = {k: round(v, 3) for k, v in payload_fields.items() if v is not None}

        # Get depletion state
        depletion_raw = get_latest_cached("active_depletion_states")
        try:
            depletion_states = json.loads(depletion_raw) if isinstance(depletion_raw, str) and depletion_raw else []
        except (json.JSONDecodeError, TypeError):
            depletion_states = []

        # Determine soil state string
        if not depletion_states:
            soil_state = "Healthy"
        elif "S8" in depletion_states:
            soil_state = "S8_Multi_Factor_Depleted"
        else:
            soil_state = f"{'_'.join(sorted(depletion_states))}_Depleted"

        message = {
            "message_id":     str(uuid.uuid4()),
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "source_domain":  "soil_dt",
            "target_domain":  "plant_dt",
            "message_type":   "data_update",
            "soil_type":      ACTIVE_SOIL_TYPE,
            "soil_state":     soil_state,
            "payload":        payload,
            "depletion_states": depletion_states,
            # Physical fields explicitly tagged so Plant DT knows which to use
            # for root-zone water (VWC), root resistance (BD), and thermal calcs
            "physical_fields": ["VWC_percent", "bulk_density_g_cm3", "soil_temperature_c"],
            "confidence":     0.95,
        }

        self._mqttc.publish(TOPIC_SOIL_TO_PLANT, json.dumps(message), qos=1)

        save_cross_domain_message({
            "source_domain": "soil_dt",
            "target_domain": "plant_dt",
            "message_type":  "data_update",
            "payload":       payload,
            "soil_state":    soil_state,
            "delivered":     True,
        })

        print(f"[CROSS-DOMAIN] → Plant DT: state={soil_state}, {len(payload)} fields")

    def publish_to_biotic_dt(self):
        """
        Publish soil state data that the Biotic Pod DT needs:
        - Organic matter (carbon substrate for microbes)
        - Microbial biomass (current activity level)
        - Soil respiration (biological activity indicator)
        - Soil temperature (affects microbial activity)
        - Soil pH (affects microbial community composition)
        - Moisture (affects aerobic/anaerobic conditions)
        """
        payload_fields = {
            "organic_matter_pct":             get_latest("organic_matter_pct"),
            "microbial_biomass_mg_c_kg":      get_latest("microbial_biomass_mg_c_kg"),
            "soil_respiration_mg_co2_kg_day": get_latest("soil_respiration_mg_co2_kg_day"),
            "soil_temperature_c":             get_latest("soil_temp_c"),
            "pH":                             get_latest("soil_ph"),
            "VWC_percent":                    get_latest("soil_moisture_pct"),
        }
        payload = {k: round(v, 3) for k, v in payload_fields.items() if v is not None}

        depletion_raw = get_latest_cached("active_depletion_states")
        try:
            depletion_states = json.loads(depletion_raw) if isinstance(depletion_raw, str) and depletion_raw else []
        except (json.JSONDecodeError, TypeError):
            depletion_states = []

        message = {
            "message_id":     str(uuid.uuid4()),
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "source_domain":  "soil_dt",
            "target_domain":  "biotic_pod_dt",
            "message_type":   "data_update",
            "soil_type":      ACTIVE_SOIL_TYPE,
            "payload":        payload,
            "depletion_states": depletion_states,
            "confidence":     0.95,
        }

        self._mqttc.publish(TOPIC_SOIL_TO_BIOTIC, json.dumps(message), qos=1)

        save_cross_domain_message({
            "source_domain": "soil_dt",
            "target_domain": "biotic_pod_dt",
            "message_type":  "data_update",
            "payload":       payload,
            "delivered":     True,
        })

        print(f"[CROSS-DOMAIN] → Biotic Pod DT: {len(payload)} fields")

    # ─────────────────────────────────────────────────────────────────────────
    # CONSUME — ASDT receives data from other DTs
    # ─────────────────────────────────────────────────────────────────────────

    def _consume_plant_data(self, data: dict):
        """
        Consume data from Plant DT:
        - Plant water demand (mm/day) → adjust irrigation recommendations
        - Rooting depth (cm) → define soil monitoring depth
        - Crop phenology stage (V6, R1, etc.) → timing of soil management
        - Plant transpiration rate (mm/day) → soil water balance
        - Carbon allocation to roots (g/m²) → soil organic matter input
        """
        payload = data.get("payload", {})
        self._plant_demand = {
            "plant_water_demand_mm_day": payload.get("plant_water_demand_mm_day"),
            "rooting_depth_cm":          payload.get("rooting_depth_cm"),
            "crop_phenology_stage":      payload.get("crop_phenology_stage"),
            "plant_transpiration_mm_day":payload.get("plant_transpiration_mm_day"),
            "carbon_to_roots_g_m2":      payload.get("carbon_to_roots_g_m2"),
            "received_at":               datetime.now(timezone.utc).isoformat(),
        }

        # Cache in Redis for other agents to use
        set_latest("cross_domain_plant_demand", json.dumps(self._plant_demand))

        save_cross_domain_message({
            "source_domain":    "plant_dt",
            "target_domain":    "soil_dt",
            "message_type":     "data_update",
            "payload":          payload,
            "response_received": True,
        })

        log_event("cross_domain_plant_received", self._plant_demand, severity="info")

        print(f"[CROSS-DOMAIN] ← Plant DT: water_demand={payload.get('plant_water_demand_mm_day')} mm/day, "
              f"phenology={payload.get('crop_phenology_stage')}")

        # React to plant-driven triggers
        self._react_to_plant_triggers()

    def _consume_biotic_data(self, data: dict):
        """
        Consume data from Biotic Pod DT:
        - Microbial community composition → soil biological health
        - Enzyme activity demand → nutrient cycling prediction
        - Root exudate composition → soil carbon dynamics
        """
        payload = data.get("payload", {})
        self._biotic_data = {
            "microbial_diversity_index":  payload.get("microbial_diversity_index"),
            "enzyme_activity_unit_kg":    payload.get("enzyme_activity_unit_kg"),
            "root_exudate_composition":   payload.get("root_exudate_composition"),
            "received_at":                datetime.now(timezone.utc).isoformat(),
        }

        set_latest("cross_domain_biotic_data", json.dumps(self._biotic_data))

        save_cross_domain_message({
            "source_domain":    "biotic_pod_dt",
            "target_domain":    "soil_dt",
            "message_type":     "data_update",
            "payload":          payload,
            "response_received": True,
        })

        log_event("cross_domain_biotic_received", self._biotic_data, severity="info")

        print(f"[CROSS-DOMAIN] ← Biotic Pod DT: diversity={payload.get('microbial_diversity_index')}")

    # ─────────────────────────────────────────────────────────────────────────
    # REACT — cross-domain triggers that adjust soil recommendations
    # ─────────────────────────────────────────────────────────────────────────

    def _react_to_plant_triggers(self):
        """
        React to plant-driven triggers:
        1. If plant water demand > available soil water → irrigation alert
        2. If crop at reproductive stage AND N low → prioritise N application

        Uses rooting_depth_cm from Plant DT (falls back to 30 cm) to compute
        the root-zone available water column rather than a fixed 30 cm depth.
        """
        demand    = self._plant_demand.get("plant_water_demand_mm_day")
        phenology = self._plant_demand.get("crop_phenology_stage", "")

        # Use rooting depth from Plant DT when available, default 30 cm
        rooting_depth_cm = self._plant_demand.get("rooting_depth_cm")
        try:
            depth_m = float(rooting_depth_cm) / 100.0 if rooting_depth_cm else 0.30
        except (TypeError, ValueError):
            depth_m = 0.30
        depth_m = max(0.10, min(depth_m, 1.50))  # clamp 10 cm – 150 cm

        # Trigger 1: Plant water demand exceeds available soil water
        if demand is not None:
            moisture = get_latest("soil_moisture_pct")
            if moisture is not None and demand > 0:
                # Available water in mm = VWC% × depth_m × 1000 mm/m
                available_mm = moisture * depth_m * 10
                if demand > available_mm / 3:  # demand exceeds 1/3 of available
                    log_event("cross_domain_irrigation_trigger", {
                        "plant_demand_mm_day": demand,
                        "available_water_mm":  round(available_mm, 1),
                        "rooting_depth_cm":    round(depth_m * 100, 1),
                        "action": "Adjust irrigation recommendation based on plant demand",
                    }, severity="warning")
                    print(f"[CROSS-DOMAIN] ⚠️  Plant demand ({demand} mm/day) approaching "
                          f"available soil water ({available_mm:.0f} mm, depth={depth_m*100:.0f} cm). "
                          f"Irrigation may be needed.")

        # Trigger 2: High nutrient demand during reproductive stage
        if phenology and phenology.upper().startswith("R"):
            n_val = get_latest("nitrogen_ppm")
            n_threshold = 15.0  # healthy minimum
            if n_val is not None and n_val < n_threshold:
                log_event("cross_domain_phenology_trigger", {
                    "phenology_stage": phenology,
                    "nitrogen_ppm":    round(n_val, 1),
                    "action": "Prioritise N application — crop at reproductive stage",
                }, severity="warning")
                print(f"[CROSS-DOMAIN] ⚠️  Crop at {phenology} (high demand) but N={n_val:.1f} ppm. "
                      f"Prioritise nitrogen application.")

    # ─────────────────────────────────────────────────────────────────────────
    # Main run loop
    # ─────────────────────────────────────────────────────────────────────────

    def run(self):
        """
        Start the cross-domain synchronizer:
        - Connect to MQTT and subscribe to incoming topics
        - Every 60 seconds, publish soil state to Plant DT and Biotic Pod DT
        """
        self._mqttc.connect(MQTT_BROKER, MQTT_PORT)
        self._mqttc.loop_start()

        print(f"[CROSS-DOMAIN] Cross-domain synchronizer running.")
        print(f"[CROSS-DOMAIN]   Publishing to:   {TOPIC_SOIL_TO_PLANT}")
        print(f"[CROSS-DOMAIN]                    {TOPIC_SOIL_TO_BIOTIC}")
        print(f"[CROSS-DOMAIN]   Subscribing to:  {TOPIC_PLANT_TO_SOIL}")
        print(f"[CROSS-DOMAIN]                    {TOPIC_BIOTIC_TO_SOIL}")

        while True:
            try:
                set_latest("layer_heartbeat_service_layer.cross_domain_sync",
                           datetime.now(timezone.utc).isoformat())
                self.publish_to_plant_dt()
                self.publish_to_biotic_dt()
            except Exception as e:
                print(f"[CROSS-DOMAIN] Error: {e}")
            time.sleep(60)


def get_latest_physical_cross_domain() -> dict:
    """
    Return the latest physical soil state as a cross-domain-ready dict.

    Pulls VWC, bulk density, and soil temperature from InfluxDB and the
    current Plant DT demand (rooting depth, water demand) from Redis.
    Intended for use by api_extensions.py and intelligent layer fast-path.
    """
    vwc  = get_latest("soil_moisture_pct")
    bd   = get_latest("bulk_density_g_cm3")
    temp = get_latest("soil_temp_c")

    plant_raw = get_latest_cached("cross_domain_plant_demand")
    try:
        plant = json.loads(plant_raw) if isinstance(plant_raw, str) and plant_raw else {}
    except (json.JSONDecodeError, TypeError):
        plant = {}

    rooting_depth_cm = plant.get("rooting_depth_cm")
    try:
        depth_m = float(rooting_depth_cm) / 100.0 if rooting_depth_cm else 0.30
    except (TypeError, ValueError):
        depth_m = 0.30
    depth_m = max(0.10, min(depth_m, 1.50))

    available_water_mm = round(vwc * depth_m * 10, 1) if vwc is not None else None

    return {
        "VWC_percent":              round(vwc, 2)  if vwc  is not None else None,
        "bulk_density_g_cm3":       round(bd, 3)   if bd   is not None else None,
        "soil_temperature_c":       round(temp, 2) if temp is not None else None,
        "rooting_depth_cm":         round(depth_m * 100, 1),
        "available_water_mm":       available_water_mm,
        "plant_water_demand_mm_day":plant.get("plant_water_demand_mm_day"),
        "crop_phenology_stage":     plant.get("crop_phenology_stage"),
    }


if __name__ == "__main__":
    sync = CrossDomainSynchronizer()
    sync.run()