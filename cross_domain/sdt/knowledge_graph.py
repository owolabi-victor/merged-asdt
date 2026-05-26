"""SDT Knowledge Graph Seeder — Neo4j domain ontology for the Soil Digital Twin.

Node types (from SDT design §3.1):
    Asset           — the physical field plot / SDT identity
    SoilLayer       — individual soil horizons with Van Genuchten parameters
    SensorNode      — physical or simulated sensor specifications
    DepletionMode   — named drought / depletion scenarios
    HydraulicProperty — measured or literature soil hydraulic constants
    WaterThreshold  — FSM alarm levels linked to agronomic consequences

All parameters are calibrated for sandy loam soil matching the SDT simulation
(Van Genuchten 1980, Mualem 1976, FAO-56 Allen et al. 1998).

Usage:
    from cross_domain.sdt.knowledge_graph import SDTKnowledgeGraph
    kg = SDTKnowledgeGraph(neo4j_uri, neo4j_user, neo4j_password)
    kg.seed()
    kg.close()
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain data (literature-calibrated for sandy loam + maize V6-V8)
# ---------------------------------------------------------------------------

_SOIL_LAYERS = [
    {
        "layer_id":    "L1_10cm",
        "depth_cm":    10,
        "texture":     "sandy_loam",
        "theta_r":     0.050,    # Van Genuchten (1980) residual water content [m³/m³]
        "theta_s":     0.430,    # saturated water content [m³/m³]
        "alpha":       0.022,    # Van Genuchten α [cm⁻¹]
        "n":           2.00,     # Van Genuchten n
        "m":           0.50,     # m = 1 - 1/n (Mualem 1976)
        "Ks_m_day":    0.44,     # saturated hydraulic conductivity [m/day]
        "fc_m3m3":     0.100,    # field capacity @ -33 kPa [m³/m³]
        "pwp_m3m3":    0.050,    # permanent wilting point @ -1500 kPa [m³/m³]
        "reference":   "Carsel & Parrish (1988) Water Resour. Res.",
    },
    {
        "layer_id":    "L2_30cm",
        "depth_cm":    30,
        "texture":     "sandy_loam",
        "theta_r":     0.050,
        "theta_s":     0.415,    # slight compaction with depth
        "alpha":       0.021,
        "n":           1.95,
        "m":           0.487,
        "Ks_m_day":    0.38,
        "fc_m3m3":     0.098,
        "pwp_m3m3":    0.050,
        "reference":   "Carsel & Parrish (1988) Water Resour. Res.",
    },
    {
        "layer_id":    "L3_60cm",
        "depth_cm":    60,
        "texture":     "sandy_loam",
        "theta_r":     0.048,
        "theta_s":     0.400,
        "alpha":       0.020,
        "n":           1.90,
        "m":           0.474,
        "Ks_m_day":    0.32,
        "fc_m3m3":     0.096,
        "pwp_m3m3":    0.048,
        "reference":   "Carsel & Parrish (1988) Water Resour. Res.",
    },
]

_SENSOR_NODES = [
    {
        "sensor_id":    "TEROS12_10cm",
        "type":         "volumetric_water_content",
        "depth_cm":     10,
        "model":        "METER TEROS-12",
        "accuracy_m3m3": 0.010,   # ±0.010 m³/m³ in mineral soils (METER spec)
        "resolution":   0.001,
        "principle":    "frequency_domain_reflectometry",
        "output_units": "m³/m³",
        "mqtt_topic":   "sdt/soil/vwc/10cm",
        "calibration":  "Topp et al. (1980) mineral soil equation",
    },
    {
        "sensor_id":    "TEROS12_30cm",
        "type":         "volumetric_water_content",
        "depth_cm":     30,
        "model":        "METER TEROS-12",
        "accuracy_m3m3": 0.010,
        "resolution":   0.001,
        "principle":    "frequency_domain_reflectometry",
        "output_units": "m³/m³",
        "mqtt_topic":   "sdt/soil/vwc/30cm",
        "calibration":  "Topp et al. (1980) mineral soil equation",
    },
    {
        "sensor_id":    "TEROS12_60cm",
        "type":         "volumetric_water_content",
        "depth_cm":     60,
        "model":        "METER TEROS-12",
        "accuracy_m3m3": 0.010,
        "resolution":   0.001,
        "principle":    "frequency_domain_reflectometry",
        "output_units": "m³/m³",
        "mqtt_topic":   "sdt/soil/vwc/60cm",
        "calibration":  "Topp et al. (1980) mineral soil equation",
    },
    {
        "sensor_id":    "MPS6_10cm",
        "type":         "matric_potential",
        "depth_cm":     10,
        "model":        "METER MPS-6",
        "accuracy_kpa": 10.0,     # ±10% or ±2 kPa in wet range (METER spec)
        "range_kpa":    [-9.0, -100000.0],
        "principle":    "dielectric_water_potential",
        "output_units": "kPa",
        "mqtt_topic":   "sdt/soil/potential/10cm",
        "calibration":  "Direct soil matric potential measurement",
    },
    {
        "sensor_id":    "DS18B20_10cm",
        "type":         "soil_temperature",
        "depth_cm":     10,
        "model":        "Dallas DS18B20",
        "accuracy_c":   0.5,      # ±0.5°C (DS18B20 datasheet)
        "resolution":   0.0625,
        "principle":    "1-wire_thermistor",
        "output_units": "°C",
        "mqtt_topic":   "sdt/soil/temperature/10cm",
        "calibration":  "Factory calibrated",
    },
]

_DEPLETION_MODES = [
    {
        "mode_id":          "DM-01",
        "name":             "normal_evapotranspiration",
        "description":      "Steady VWC decline driven by FAO-56 ET demand",
        "depl_rate_m3m3hr": 0.0008,
        "typical_duration_h": 24,
        "trigger":          "VWC below FC, no irrigation, ET demand > 0",
        "reference":        "Allen et al. (1998) FAO-56",
    },
    {
        "mode_id":          "DM-02",
        "name":             "heat_stress_depletion",
        "description":      "Accelerated depletion when soil_temp > 35°C (stomata collapse)",
        "depl_rate_m3m3hr": 0.0018,
        "typical_duration_h": 4,
        "trigger":          "soil_temp_10cm > 35°C AND VWC < FC",
        "reference":        "Lecoeur & Sinclair (1996) Plant Cell Environ.",
    },
    {
        "mode_id":          "DM-03",
        "name":             "drainage_loss",
        "description":      "Gravity drainage after heavy irrigation or rain event",
        "depl_rate_m3m3hr": 0.005,
        "typical_duration_h": 2,
        "trigger":          "VWC > FC (above field capacity)",
        "reference":        "Richards (1931) — gravity drainage term in RE",
    },
    {
        "mode_id":          "DM-04",
        "name":             "root_uptake_peak",
        "description":      "Maximum root water uptake at mid-day (10:00–14:00 local)",
        "depl_rate_m3m3hr": 0.0015,
        "typical_duration_h": 6,
        "trigger":          "VPD > 2.0 kPa AND solar_rad > 600 W/m²",
        "reference":        "Feddes et al. (1978) root water uptake model",
    },
]

_HYDRAULIC_PROPERTIES = [
    {
        "property_id":  "HP-01",
        "name":         "field_capacity",
        "value":        0.100,
        "unit":         "m³/m³",
        "condition":    "matric_potential = -33 kPa (1/3 bar)",
        "reference":    "Rawls et al. (1982) Trans. ASAE sandy loam",
    },
    {
        "property_id":  "HP-02",
        "name":         "permanent_wilting_point",
        "value":        0.050,
        "unit":         "m³/m³",
        "condition":    "matric_potential = -1500 kPa (15 bar)",
        "reference":    "Rawls et al. (1982) Trans. ASAE sandy loam",
    },
    {
        "property_id":  "HP-03",
        "name":         "plant_available_water",
        "value":        0.050,
        "unit":         "m³/m³",
        "formula":      "PAW = FC - PWP = 0.100 - 0.050",
        "reference":    "Doorenbos & Kassam (1979) FAO Irrigation Paper 33",
    },
    {
        "property_id":  "HP-04",
        "name":         "saturated_hydraulic_conductivity",
        "value":        0.44,
        "unit":         "m/day",
        "condition":    "fully_saturated",
        "reference":    "Carsel & Parrish (1988) Water Resour. Res. 24(5)",
    },
    {
        "property_id":  "HP-05",
        "name":         "depletion_fraction_maize",
        "value":        0.55,
        "unit":         "dimensionless",
        "description":  "FAO-56 p-factor: fraction of PAW maize can extract without stress",
        "reference":    "Allen et al. (1998) FAO-56 Table 22 (maize, mid-season)",
    },
    {
        "property_id":  "HP-06",
        "name":         "bulk_density",
        "value":        1.55,
        "unit":         "g/cm³",
        "reference":    "Typical value for cultivated sandy loam (Brady & Weil 2008)",
    },
]

_WATER_THRESHOLDS = [
    {
        "threshold_id":  "WT-01",
        "level":         "optimal",
        "vwc_lower":     0.060,
        "vwc_upper":     0.100,
        "psi_kpa_lower": -33.0,
        "psi_kpa_upper": -5.0,
        "fsm_state":     "OPTIMAL",
        "action":        "No intervention required",
    },
    {
        "threshold_id":  "WT-02",
        "level":         "depleting_warn",
        "vwc_10cm":      0.045,
        "vwc_30cm":      0.042,
        "vwc_60cm":      0.038,
        "psi_kpa":       -50.0,
        "fsm_state":     "DEPLETING",
        "action":        "Monitor; prepare irrigation schedule",
        "reference":     "Allen et al. (1998) FAO-56 §8.2",
    },
    {
        "threshold_id":  "WT-03",
        "level":         "critical",
        "vwc_10cm":      0.030,
        "vwc_30cm":      0.028,
        "vwc_60cm":      0.025,
        "psi_kpa":       -80.0,
        "fsm_state":     "CRITICAL",
        "action":        "Irrigate within 4 hours; notify farm manager",
        "reference":     "Doorenbos & Kassam (1979)",
    },
    {
        "threshold_id":  "WT-04",
        "level":         "wilting_risk",
        "vwc_10cm":      0.055,
        "description":   "Near permanent wilting point; entered from CRITICAL only",
        "fsm_state":     "WILTING_RISK",
        "action":        "Emergency irrigation; log ProvenanceEvent",
        "reference":     "Rawls et al. (1982); Jones (2014) Plants and Microclimate",
    },
]


class SDTKnowledgeGraph:
    """Neo4j knowledge graph manager for the Soil Digital Twin.

    Seeds the static domain ontology on first call; subsequent calls are
    idempotent (MERGE-based Cypher). Does not require the neo4j driver to
    be installed — gracefully degrades if unavailable.
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password",
        asset_id: str = "sdt_water_depletion_001",
    ):
        self._uri      = uri
        self._user     = user
        self._password = password
        self._asset_id = asset_id
        self._driver   = None
        self._available = False

    def connect(self) -> bool:
        try:
            from neo4j import GraphDatabase
            self._driver    = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
            self._available = True
            log.info("SDT KG connected to Neo4j at %s", self._uri)
            return True
        except ImportError:
            log.warning("neo4j driver not installed — KG seeding skipped")
        except Exception as exc:
            log.warning("Neo4j unavailable (%s) — KG seeding skipped", exc)
        return False

    def seed(self) -> None:
        """Seed all domain nodes. Safe to call repeatedly (MERGE idempotent)."""
        if not self._available or self._driver is None:
            return
        with self._driver.session() as s:
            self._seed_constraints(s)
            self._seed_asset(s)
            self._seed_soil_layers(s)
            self._seed_sensors(s)
            self._seed_depletion_modes(s)
            self._seed_hydraulic_properties(s)
            self._seed_water_thresholds(s)
            self._seed_relationships(s)
        log.info("SDT knowledge graph seeded (%d layers, %d sensors)",
                 len(_SOIL_LAYERS), len(_SENSOR_NODES))

    def _seed_constraints(self, session) -> None:
        constraints = [
            "CREATE CONSTRAINT sdt_asset_id IF NOT EXISTS FOR (a:Asset) REQUIRE a.asset_id IS UNIQUE",
            "CREATE CONSTRAINT sdt_layer_id IF NOT EXISTS FOR (l:SoilLayer) REQUIRE l.layer_id IS UNIQUE",
            "CREATE CONSTRAINT sdt_sensor_id IF NOT EXISTS FOR (s:SensorNode) REQUIRE s.sensor_id IS UNIQUE",
            "CREATE CONSTRAINT sdt_threshold_id IF NOT EXISTS FOR (t:WaterThreshold) REQUIRE t.threshold_id IS UNIQUE",
            "CREATE CONSTRAINT sdt_hp_id IF NOT EXISTS FOR (h:HydraulicProperty) REQUIRE h.property_id IS UNIQUE",
            "CREATE CONSTRAINT sdt_dm_id IF NOT EXISTS FOR (d:DepletionMode) REQUIRE d.mode_id IS UNIQUE",
        ]
        for cq in constraints:
            try:
                session.run(cq)
            except Exception:
                pass   # already exists

    def _seed_asset(self, session) -> None:
        session.run(
            """
            MERGE (a:Asset {asset_id: $asset_id})
            SET a.name = 'Soil Digital Twin — Sandy Loam Water Depletion',
                a.type = 'soil_digital_twin',
                a.soil_texture = 'sandy_loam',
                a.crop = 'Maize (Zea mays L.) V6-V8',
                a.location = 'Field plot (arid region ~25°N)',
                a.framework = 'DT-Forge v1.0',
                a.design_version = 'v1.0'
            """,
            asset_id=self._asset_id,
        )

    def _seed_soil_layers(self, session) -> None:
        for layer in _SOIL_LAYERS:
            session.run(
                """
                MERGE (l:SoilLayer {layer_id: $layer_id})
                SET l += $props
                """,
                layer_id=layer["layer_id"],
                props={k: v for k, v in layer.items() if k != "layer_id"},
            )

    def _seed_sensors(self, session) -> None:
        for sensor in _SENSOR_NODES:
            session.run(
                """
                MERGE (s:SensorNode {sensor_id: $sensor_id})
                SET s += $props
                """,
                sensor_id=sensor["sensor_id"],
                props={k: v for k, v in sensor.items() if k != "sensor_id"},
            )

    def _seed_depletion_modes(self, session) -> None:
        for mode in _DEPLETION_MODES:
            session.run(
                """
                MERGE (d:DepletionMode {mode_id: $mode_id})
                SET d += $props
                """,
                mode_id=mode["mode_id"],
                props={k: v for k, v in mode.items() if k != "mode_id"},
            )

    def _seed_hydraulic_properties(self, session) -> None:
        for hp in _HYDRAULIC_PROPERTIES:
            session.run(
                """
                MERGE (h:HydraulicProperty {property_id: $property_id})
                SET h += $props
                """,
                property_id=hp["property_id"],
                props={k: v for k, v in hp.items() if k != "property_id"},
            )

    def _seed_water_thresholds(self, session) -> None:
        for wt in _WATER_THRESHOLDS:
            session.run(
                """
                MERGE (t:WaterThreshold {threshold_id: $threshold_id})
                SET t += $props
                """,
                threshold_id=wt["threshold_id"],
                props={k: v for k, v in wt.items() if k != "threshold_id"},
            )

    def _seed_relationships(self, session) -> None:
        # Asset HAS_LAYER for each soil layer
        for layer in _SOIL_LAYERS:
            session.run(
                """
                MATCH (a:Asset {asset_id: $asset_id})
                MATCH (l:SoilLayer {layer_id: $layer_id})
                MERGE (a)-[:HAS_LAYER]->(l)
                """,
                asset_id=self._asset_id,
                layer_id=layer["layer_id"],
            )
        # Asset MONITORED_BY each sensor
        for sensor in _SENSOR_NODES:
            session.run(
                """
                MATCH (a:Asset {asset_id: $asset_id})
                MATCH (s:SensorNode {sensor_id: $sensor_id})
                MERGE (a)-[:MONITORED_BY]->(s)
                """,
                asset_id=self._asset_id,
                sensor_id=sensor["sensor_id"],
            )
        # Link sensors to layers by depth
        depth_map = {"10": "L1_10cm", "30": "L2_30cm", "60": "L3_60cm"}
        for sensor in _SENSOR_NODES:
            depth = str(sensor.get("depth_cm", ""))
            layer_id = depth_map.get(depth)
            if layer_id:
                session.run(
                    """
                    MATCH (s:SensorNode {sensor_id: $sensor_id})
                    MATCH (l:SoilLayer {layer_id: $layer_id})
                    MERGE (s)-[:MEASURES]->(l)
                    """,
                    sensor_id=sensor["sensor_id"],
                    layer_id=layer_id,
                )
        # Asset HAS_HYDRAULIC_PROPERTY
        for hp in _HYDRAULIC_PROPERTIES:
            session.run(
                """
                MATCH (a:Asset {asset_id: $asset_id})
                MATCH (h:HydraulicProperty {property_id: $property_id})
                MERGE (a)-[:HAS_HYDRAULIC_PROPERTY]->(h)
                """,
                asset_id=self._asset_id,
                property_id=hp["property_id"],
            )
        # Asset USES_THRESHOLD
        for wt in _WATER_THRESHOLDS:
            session.run(
                """
                MATCH (a:Asset {asset_id: $asset_id})
                MATCH (t:WaterThreshold {threshold_id: $threshold_id})
                MERGE (a)-[:USES_THRESHOLD]->(t)
                """,
                asset_id=self._asset_id,
                threshold_id=wt["threshold_id"],
            )
        # THRESHOLD_TRIGGERS_MODE relationships
        session.run(
            """
            MATCH (t:WaterThreshold {threshold_id: 'WT-02'})
            MATCH (d:DepletionMode {mode_id: 'DM-01'})
            MERGE (t)-[:INDICATES_MODE]->(d)
            """
        )
        session.run(
            """
            MATCH (t:WaterThreshold {threshold_id: 'WT-03'})
            MATCH (d:DepletionMode {mode_id: 'DM-02'})
            MERGE (t)-[:INDICATES_MODE]->(d)
            """
        )

    def close(self) -> None:
        if self._driver:
            self._driver.close()


def seed_sdt_kg(
    uri: str = "bolt://localhost:7687",
    user: str = "neo4j",
    password: str = "password",
    asset_id: str = "sdt_water_depletion_001",
) -> Optional["SDTKnowledgeGraph"]:
    """Convenience function called at SDT startup."""
    kg = SDTKnowledgeGraph(uri, user, password, asset_id)
    if kg.connect():
        kg.seed()
        return kg
    return None
