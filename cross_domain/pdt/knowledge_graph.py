"""PDT Knowledge Graph Seeder — Neo4j domain ontology for the Plant Digital Twin.

Node types (from PDT design §3.1):
    Plant           — the monitored maize V6-V8 individual / canopy
    PlantOrgan      — leaf, root, stem with water relation parameters
    DroughtMechanism — avoidance / tolerance mechanisms active at each stress level
    StressIndicator  — measured physiological variables with FSM alarm thresholds
    RecoveryAction   — interventions that drive FSM toward RECOVERY / UNSTRESSED

All parameters are calibrated for Zea mays (maize) at V6-V8 growth stage:
    Pressure-Volume: Tyree & Hammel (1972) — π₀=-0.70 MPa, ε=20 MPa, TLP=-1.20 MPa
    CWSI: Jackson et al. (1981) — A=-0.21, B=-0.56
    Stomatal conductance: Lecoeur & Sinclair (1996) — gs_nom=260 mmol/m²/s
    Wilting threshold: Westgate & Boyer (1985) — ψ_wilt=-2.5 MPa
    Yield-water: Hsiao et al. (2009) AquaCrop — Ky=1.25

Usage:
    from cross_domain.pdt.knowledge_graph import PDTKnowledgeGraph
    kg = PDTKnowledgeGraph(neo4j_uri, neo4j_user, neo4j_password)
    kg.seed()
    kg.close()
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain data (literature-calibrated for maize V6-V8)
# ---------------------------------------------------------------------------

_PLANT_ORGANS = [
    {
        "organ_id":           "ORG-01",
        "name":               "leaf",
        "function":           "photosynthesis_transpiration",
        "rwc_nominal":        0.95,   # relative water content at full turgor
        "rwc_tlp":            0.85,   # RWC at turgor loss point
        "water_potential_nom_mpa": -0.30,
        "water_potential_tlp_mpa": -1.20,  # Tyree & Hammel (1972) TLP for maize
        "osmotic_potential_mpa":   -0.70,  # π₀ for maize V6 (Morgan 1992)
        "bulk_elastic_modulus_mpa": 20.0,  # ε for maize (Tyree & Hammel 1972)
        "reference":          "Tyree & Hammel (1972) Can. J. Bot.; Morgan (1992) Plant Physiol.",
    },
    {
        "organ_id":           "ORG-02",
        "name":               "root",
        "function":           "water_nutrient_uptake",
        "root_depth_m":       0.70,   # effective rooting depth maize V6-V8
        "root_density_cm_cm3": 0.8,
        "uptake_model":       "Feddes et al. (1978) piecewise linear",
        "psi_critical_mpa":   -1.5,   # root uptake stops below this
        "reference":          "Feddes et al. (1978); Hund et al. (2009) Plant Soil",
    },
    {
        "organ_id":           "ORG-03",
        "name":               "stem",
        "function":           "structural_support_water_transport",
        "stem_water_potential_nom_mpa": -0.25,
        "hydraulic_conductance_mmol_mpa_s": 8.0,
        "reference":          "Sperry & Tyree (1988) Plant Physiol.",
    },
    {
        "organ_id":           "ORG-04",
        "name":               "stomata",
        "function":           "gas_exchange_regulation",
        "gs_nominal_mmol_m2_s": 260.0,  # Lecoeur & Sinclair (1996)
        "gs_mild_frac":       0.75,
        "gs_severe_frac":     0.40,
        "gs_wilting_frac":    0.15,
        "vpd_sensitivity":    "Ball-Berry model m=9.0, b=0.01",
        "reference":          "Lecoeur & Sinclair (1996) Plant Cell Environ.",
    },
]

_DROUGHT_MECHANISMS = [
    {
        "mechanism_id":  "DM-PDT-01",
        "name":          "stomatal_closure",
        "type":          "avoidance",
        "trigger_state": "MILD_STRESS",
        "description":   "ABA-mediated stomatal closure reduces transpiration at ψ < -0.8 MPa",
        "sensor_indicator": "stomatal_conductance_mmol",
        "threshold_mpa": -0.80,
        "efficacy":      "Reduces water loss 25-60%; maintained until ψ < TLP",
        "reference":     "Tardieu & Simonneau (1998) J. Exp. Bot.",
    },
    {
        "mechanism_id":  "DM-PDT-02",
        "name":          "osmotic_adjustment",
        "type":          "tolerance",
        "trigger_state": "MODERATE_STRESS",
        "description":   "Solute accumulation lowers osmotic potential to maintain turgor",
        "sensor_indicator": "leaf_water_potential_mpa",
        "threshold_mpa": -1.20,
        "adjustment_mpa": 0.3,   # typical OA capacity for maize (~0.3 MPa)
        "efficacy":      "Extends turgor maintenance by ~0.3 MPa; enables continued gs",
        "reference":     "Morgan (1984) Adv. Agron.; Ludlow & Muchow (1990) Adv. Agron.",
    },
    {
        "mechanism_id":  "DM-PDT-03",
        "name":          "leaf_rolling",
        "type":          "avoidance",
        "trigger_state": "SEVERE_STRESS",
        "description":   "Leaf rolling reduces effective radiation interception and transpiration",
        "sensor_indicator": "cwsi",
        "threshold":     0.70,
        "reduction_pct": 30,     # ~30% reduction in effective LAI
        "reference":     "Hsiao (1973) Annu. Rev. Plant Physiol.",
    },
    {
        "mechanism_id":  "DM-PDT-04",
        "name":          "root_growth_acceleration",
        "type":          "avoidance",
        "trigger_state": "MODERATE_STRESS",
        "description":   "Deeper root growth triggered by soil drying — accesses sub-surface water",
        "sensor_indicator": "vwc_root_zone",
        "reference":     "Tardieu et al. (2011) J. Exp. Bot.",
    },
    {
        "mechanism_id":  "DM-PDT-05",
        "name":          "wilting_collapse",
        "type":          "failure",
        "trigger_state": "WILTING",
        "description":   "Cell turgor loss below TLP; irreversible damage possible if prolonged",
        "sensor_indicator": "leaf_water_potential_mpa",
        "threshold_mpa": -2.50,
        "permanent_if_duration_h": 4,
        "reference":     "Westgate & Boyer (1985) Plant Physiol.; Hsiao (1973)",
    },
]

_STRESS_INDICATORS = [
    {
        "indicator_id":   "SI-01",
        "name":           "cwsi",
        "full_name":      "Crop Water Stress Index",
        "unit":           "dimensionless",
        "nominal":        0.0,
        "mild_threshold": 0.20,
        "moderate_threshold": 0.45,
        "severe_threshold":   0.70,
        "direction":      "high",   # stress increases with value
        "model":          "Jackson et al. (1981) A=-0.21, B=-0.56, ΔT_UL=3.5°C",
        "sensor":         "Infrared thermometer (Apogee SI-121 ±0.5°C) + weather station",
        "reference":      "Jackson et al. (1981) Agric. Meteorol.; Idso et al. (1981)",
    },
    {
        "indicator_id":   "SI-02",
        "name":           "leaf_water_potential_mpa",
        "full_name":      "Leaf Water Potential",
        "unit":           "MPa",
        "nominal":        -0.30,
        "mild_threshold": -0.80,
        "moderate_threshold": -1.20,
        "severe_threshold":   -1.80,
        "wilting_threshold":  -2.50,
        "direction":      "low",   # stress increases (more negative) with lower value
        "sensor":         "Pressure chamber (PMS-1000 ±0.05 MPa) or stem psychrometer",
        "reference":      "Westgate & Boyer (1985) Plant Physiol. 78(2)",
    },
    {
        "indicator_id":   "SI-03",
        "name":           "stomatal_conductance_mmol",
        "full_name":      "Stomatal Conductance",
        "unit":           "mmol m⁻² s⁻¹",
        "nominal":        260.0,
        "mild_fraction":  0.75,   # below 75% nominal = mild
        "severe_fraction": 0.40,  # below 40% nominal = severe
        "wilting_fraction": 0.15, # below 15% nominal = wilting signal
        "direction":      "low",
        "sensor":         "Porometer/IRGA (LI-600 LI-COR ±15 mmol/m²/s in-situ)",
        "reference":      "Lecoeur & Sinclair (1996) Plant Cell Environ. 19(8)",
    },
    {
        "indicator_id":   "SI-04",
        "name":           "sap_flow_L_hr",
        "full_name":      "Sap Flow per Plant",
        "unit":           "L hr⁻¹ plant⁻¹",
        "nominal":        0.25,   # V6-V8 midday at well-watered conditions
        "wilting_fraction": 0.10, # < 10% of nominal = wilting signal
        "direction":      "low",
        "sensor":         "Granier TDP probe (±8% of reading)",
        "reference":      "Granier (1985) Ann. Sci. For.; Williams et al. (1996) Plant Cell Environ.",
    },
    {
        "indicator_id":   "SI-05",
        "name":           "relative_water_content",
        "full_name":      "Leaf Relative Water Content",
        "unit":           "dimensionless (0–1)",
        "nominal":        0.95,
        "moderate_threshold": 0.90,
        "severe_threshold":   0.85,
        "wilting_threshold":  0.80,
        "direction":      "low",
        "measurement":    "Gravimetric: (FW-DW)/(TW-DW) (Barrs & Weatherley 1962)",
        "reference":      "Barrs & Weatherley (1962) Aust. J. Biol. Sci.",
    },
]

_RECOVERY_ACTIONS = [
    {
        "action_id":      "RA-01",
        "name":           "drip_irrigation",
        "triggering_state": "MILD_STRESS",
        "target_vwc_m3m3":  0.08,
        "rate_L_hr":        5.0,
        "expected_recovery_h": 6,
        "pid_controlled":   True,
        "reference":        "FAO-56 net irrigation requirement calculation",
    },
    {
        "action_id":      "RA-02",
        "name":           "emergency_irrigation",
        "triggering_state": "SEVERE_STRESS",
        "rate_L_hr":        20.0,
        "expected_recovery_h": 2,
        "pid_controlled":   True,
        "note":             "Bypasses water budget check; requires HITL acknowledgement",
    },
    {
        "action_id":      "RA-03",
        "name":           "misting_canopy_cooling",
        "triggering_state": "SEVERE_STRESS",
        "description":     "Overhead mist to reduce canopy temperature and CWSI",
        "expected_cwsi_reduction": 0.20,
        "note":            "Supplemental to root irrigation; not modelled in PID",
    },
    {
        "action_id":      "RA-04",
        "name":           "shading",
        "triggering_state": "WILTING",
        "description":     "Temporary shading reduces radiation load; allows partial recovery",
        "expected_recovery_h": 12,
        "note":            "Physical intervention; not autonomously triggered by digital twin",
    },
]


class PDTKnowledgeGraph:
    """Neo4j knowledge graph manager for the Plant Digital Twin.

    Seeds the physiological ontology for maize V6-V8 drought stress.
    All node creation is MERGE-based (idempotent).
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password",
        asset_id: str = "pdt_maize_drought_001",
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
            log.info("PDT KG connected to Neo4j at %s", self._uri)
            return True
        except ImportError:
            log.warning("neo4j driver not installed — PDT KG seeding skipped")
        except Exception as exc:
            log.warning("Neo4j unavailable (%s) — PDT KG seeding skipped", exc)
        return False

    def seed(self) -> None:
        if not self._available or self._driver is None:
            return
        with self._driver.session() as s:
            self._seed_constraints(s)
            self._seed_plant(s)
            self._seed_organs(s)
            self._seed_mechanisms(s)
            self._seed_indicators(s)
            self._seed_recovery_actions(s)
            self._seed_relationships(s)
        log.info("PDT knowledge graph seeded (%d organs, %d stress indicators)",
                 len(_PLANT_ORGANS), len(_STRESS_INDICATORS))

    def _seed_constraints(self, session) -> None:
        constraints = [
            "CREATE CONSTRAINT pdt_plant_id IF NOT EXISTS FOR (p:Plant) REQUIRE p.plant_id IS UNIQUE",
            "CREATE CONSTRAINT pdt_organ_id IF NOT EXISTS FOR (o:PlantOrgan) REQUIRE o.organ_id IS UNIQUE",
            "CREATE CONSTRAINT pdt_mech_id IF NOT EXISTS FOR (m:DroughtMechanism) REQUIRE m.mechanism_id IS UNIQUE",
            "CREATE CONSTRAINT pdt_si_id IF NOT EXISTS FOR (s:StressIndicator) REQUIRE s.indicator_id IS UNIQUE",
            "CREATE CONSTRAINT pdt_ra_id IF NOT EXISTS FOR (r:RecoveryAction) REQUIRE r.action_id IS UNIQUE",
        ]
        for cq in constraints:
            try:
                session.run(cq)
            except Exception:
                pass

    def _seed_plant(self, session) -> None:
        session.run(
            """
            MERGE (p:Plant {plant_id: $asset_id})
            SET p.species       = 'Zea mays L.',
                p.cultivar      = 'Generic maize (drought-tolerant inbred)',
                p.growth_stage  = 'V6-V8',
                p.planting_density_m2 = 8.0,
                p.lai           = 2.5,
                p.kc            = 1.10,
                p.ky            = 1.25,
                p.framework     = 'DT-Forge v1.0'
            """,
            asset_id=self._asset_id,
        )

    def _seed_organs(self, session) -> None:
        for organ in _PLANT_ORGANS:
            session.run(
                """
                MERGE (o:PlantOrgan {organ_id: $organ_id})
                SET o += $props
                """,
                organ_id=organ["organ_id"],
                props={k: v for k, v in organ.items() if k != "organ_id"},
            )

    def _seed_mechanisms(self, session) -> None:
        for mech in _DROUGHT_MECHANISMS:
            session.run(
                """
                MERGE (m:DroughtMechanism {mechanism_id: $mechanism_id})
                SET m += $props
                """,
                mechanism_id=mech["mechanism_id"],
                props={k: v for k, v in mech.items() if k != "mechanism_id"},
            )

    def _seed_indicators(self, session) -> None:
        for si in _STRESS_INDICATORS:
            session.run(
                """
                MERGE (s:StressIndicator {indicator_id: $indicator_id})
                SET s += $props
                """,
                indicator_id=si["indicator_id"],
                props={k: v for k, v in si.items() if k != "indicator_id"},
            )

    def _seed_recovery_actions(self, session) -> None:
        for ra in _RECOVERY_ACTIONS:
            session.run(
                """
                MERGE (r:RecoveryAction {action_id: $action_id})
                SET r += $props
                """,
                action_id=ra["action_id"],
                props={k: v for k, v in ra.items() if k != "action_id"},
            )

    def _seed_relationships(self, session) -> None:
        # Plant HAS_ORGAN
        for organ in _PLANT_ORGANS:
            session.run(
                """
                MATCH (p:Plant {plant_id: $asset_id})
                MATCH (o:PlantOrgan {organ_id: $organ_id})
                MERGE (p)-[:HAS_ORGAN]->(o)
                """,
                asset_id=self._asset_id,
                organ_id=organ["organ_id"],
            )
        # Plant EXHIBITS_MECHANISM
        for mech in _DROUGHT_MECHANISMS:
            session.run(
                """
                MATCH (p:Plant {plant_id: $asset_id})
                MATCH (m:DroughtMechanism {mechanism_id: $mechanism_id})
                MERGE (p)-[:EXHIBITS_MECHANISM {when_stressed: $trigger}]->(m)
                """,
                asset_id=self._asset_id,
                mechanism_id=mech["mechanism_id"],
                trigger=mech["trigger_state"],
            )
        # Plant MEASURED_BY indicator
        for si in _STRESS_INDICATORS:
            session.run(
                """
                MATCH (p:Plant {plant_id: $asset_id})
                MATCH (s:StressIndicator {indicator_id: $indicator_id})
                MERGE (p)-[:MEASURED_BY]->(s)
                """,
                asset_id=self._asset_id,
                indicator_id=si["indicator_id"],
            )
        # Mechanism DETECTED_BY indicator
        mech_indicator_map = {
            "DM-PDT-01": "SI-03",   # stomatal closure → gs
            "DM-PDT-02": "SI-02",   # osmotic adjustment → ψ
            "DM-PDT-03": "SI-01",   # leaf rolling → CWSI
            "DM-PDT-05": "SI-02",   # wilting → ψ
        }
        for mech_id, si_id in mech_indicator_map.items():
            session.run(
                """
                MATCH (m:DroughtMechanism {mechanism_id: $mech_id})
                MATCH (s:StressIndicator {indicator_id: $si_id})
                MERGE (m)-[:DETECTED_BY]->(s)
                """,
                mech_id=mech_id, si_id=si_id,
            )
        # Recovery actions TREATS plant
        for ra in _RECOVERY_ACTIONS:
            session.run(
                """
                MATCH (p:Plant {plant_id: $asset_id})
                MATCH (r:RecoveryAction {action_id: $action_id})
                MERGE (r)-[:TREATS {when_state: $trigger}]->(p)
                """,
                asset_id=self._asset_id,
                action_id=ra["action_id"],
                trigger=ra["triggering_state"],
            )

    def close(self) -> None:
        if self._driver:
            self._driver.close()


def seed_pdt_kg(
    uri: str = "bolt://localhost:7687",
    user: str = "neo4j",
    password: str = "password",
    asset_id: str = "pdt_maize_drought_001",
) -> Optional["PDTKnowledgeGraph"]:
    """Convenience function called at PDT startup."""
    kg = PDTKnowledgeGraph(uri, user, password, asset_id)
    if kg.connect():
        kg.seed()
        return kg
    return None
