"""BPDT Knowledge Graph Seeder — Neo4j domain ontology for the Biotic Pod Digital Twin.

Node types (from BPDT design §3.1, Table 8):
    BioticPod         — the physical pod enclosure / field plot
    PodConfiguration  — versioned SDT+PDT configuration snapshots
    SubordinateTwin   — SDT and PDT registered in the pod
    IrrigationEvent   — every irrigation event with outcome
    SystemAnomaly     — unexpected drainage, sensor drift, actuator failures
    WaterBudget       — weekly water budget tracking
    EmergentBehaviour — root-uptake-drying feedback, stress amplification
    FarmManager       — HITL actor for override and escalation

The BPDT KG is the system-level knowledge graph that links the SDT and PDT
sub-graphs into a unified pod representation and tracks all interventions.

Usage:
    from cross_domain.bpdt.knowledge_graph import BPDTKnowledgeGraph
    kg = BPDTKnowledgeGraph(neo4j_uri, neo4j_user, neo4j_password)
    kg.seed()
    kg.close()
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static domain data (structural schema + example seed nodes)
# ---------------------------------------------------------------------------

_EMERGENT_BEHAVIOURS = [
    {
        "behaviour_id":   "EB-01",
        "name":           "root_uptake_drying_feedback",
        "description":    (
            "SDT depletion rate correlates with PDT transpiration demand "
            "(Pearson r > 0.85 for > 60 min). Indicates plant is actively "
            "drawing down soil reservoir faster than can be replenished."
        ),
        "detection_rule": "R-BPDT-03",
        "correlation_threshold": 0.85,
        "window_min":     60,
        "action":         "Accelerate irrigation urgency; log to KG",
        "reference":      "Tardieu & Simonneau (1998); Bonan (2019) Climate & Ecosystems",
    },
    {
        "behaviour_id":   "EB-02",
        "name":           "stress_amplification_loop",
        "description":    (
            "Severe soil depletion (CRITICAL) co-occurs with severe plant "
            "stress (SEVERE_STRESS or WILTING), creating a positive feedback "
            "loop where reduced root water uptake capacity worsens soil-plant "
            "water flux."
        ),
        "detection_rule": "R-BPDT-01 (CompoundCritical)",
        "action":         "Trigger EMERGENCY; dispatch farm manager + agronomist",
        "reference":      "Dodd (2009) J. Exp. Bot.; Sperry et al. (2002) Plant Cell Environ.",
    },
    {
        "behaviour_id":   "EB-03",
        "name":           "unexpected_drainage",
        "description":    (
            "VWC drops faster than ET demand can explain without rainfall "
            "(drainage_rate > 0.005 m³/m³/hr without rain). Indicates "
            "preferential flow path, cracking, or sensor anomaly."
        ),
        "detection_rule": "R-BPDT-02",
        "threshold_m3m3hr": 0.005,
        "action":         "Flag anomaly; set MAINTENANCE; request inspection",
        "reference":      "Beven & Germann (1982) Water Resour. Res.",
    },
]

_BOUNDARY_CONDITIONS = [
    {
        "bc_id":         "BC-01",
        "source_twin":   "sdt",
        "source_field":  "vwc_10cm",
        "target_twin":   "pdt",
        "target_field":  "vwc_root_zone_10",
        "transform":     "pass-through (m³/m³ → m³/m³)",
        "refresh_rate":  "each OODA cycle (10 min)",
        "purpose":       "Soil surface moisture input to plant water balance",
    },
    {
        "bc_id":         "BC-02",
        "source_twin":   "sdt",
        "source_field":  "vwc_30cm",
        "target_twin":   "pdt",
        "target_field":  "vwc_root_zone_30",
        "transform":     "pass-through",
        "refresh_rate":  "each OODA cycle",
        "purpose":       "Root zone 30cm moisture for stomatal conductance model",
    },
    {
        "bc_id":         "BC-03",
        "source_twin":   "sdt",
        "source_field":  "soil_water_potential_kpa",
        "target_twin":   "pdt",
        "target_field":  "root_zone_potential",
        "transform":     "kPa × 0.001 → MPa",
        "refresh_rate":  "each OODA cycle",
        "purpose":       "Soil water potential drives root zone potential for P-V model",
    },
    {
        "bc_id":         "BC-04",
        "source_twin":   "pdt",
        "source_field":  "transpiration_demand_mm",
        "target_twin":   "sdt",
        "target_field":  "et_demand_input",
        "transform":     "pass-through (mm/day)",
        "refresh_rate":  "each OODA cycle",
        "purpose":       "Plant transpiration demand updates SDT ETForecastAgent weighting",
    },
    {
        "bc_id":         "BC-05",
        "source_twin":   "sdt",
        "source_field":  "depletion_rate",
        "target_twin":   "pdt",
        "target_field":  "root_zone_depletion",
        "transform":     "pass-through (m³/m³/hr)",
        "refresh_rate":  "each OODA cycle",
        "purpose":       "SDT depletion rate feeds into PDT wilting-time forecast (R-PDT-08)",
    },
]

_SYSTEM_RULES = [
    {
        "rule_id":    "R-BPDT-01",
        "name":       "CompoundCritical",
        "condition":  "SDT state = CRITICAL AND PDT state ≥ MODERATE_STRESS",
        "action":     "EMERGENCY; dispatch farm manager + agronomist",
    },
    {
        "rule_id":    "R-BPDT-02",
        "name":       "UnexpectedDrainage",
        "condition":  "SDT drainage_rate > 0.005 m³/m³/hr without rainfall",
        "action":     "Flag anomaly; log to ledger; set MAINTENANCE state",
    },
    {
        "rule_id":    "R-BPDT-03",
        "name":       "FeedbackLoopDetection",
        "condition":  "d(SDT_depletion)/dt correlates with PDT transpiration_demand r > 0.85 for 60 min",
        "action":     "Log 'root-uptake-drying feedback'; adjust irrigation timing model",
    },
    {
        "rule_id":    "R-BPDT-04",
        "name":       "IrrigationNonResponse",
        "condition":  "SDT VWC not rising within 30 min of irrigation start",
        "action":     "Escalate: check actuator; notify farm manager",
    },
    {
        "rule_id":    "R-BPDT-05",
        "name":       "WaterBudgetExceeded",
        "condition":  "Cumulative irrigation volume > weekly target",
        "action":     "Suppress next irrigation advisory; notify farm manager",
    },
    {
        "rule_id":    "R-BPDT-06",
        "name":       "SensorDriftSuspect",
        "condition":  "Model residuals for SDT AND PDT both > threshold simultaneously",
        "action":     "Request cross-validation; flag to Intelligent layer",
    },
    {
        "rule_id":    "R-BPDT-07",
        "name":       "ActuatorTimeout",
        "condition":  "IRRIGATION_PENDING > 10 min without actuator confirmation",
        "action":     "EMERGENCY + MAINTENANCE; HITL farm manager",
    },
]


class BPDTKnowledgeGraph:
    """Neo4j knowledge graph manager for the Biotic Pod Digital Twin.

    Creates the system-level pod ontology that links SDT and PDT sub-graphs
    and tracks all interventions, configurations, and emergent behaviours.
    All operations are MERGE-based (idempotent).
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password",
        pod_id: str = "bpdt_component_overseer_001",
        sdt_id: str = "sdt_water_depletion_001",
        pdt_id: str = "pdt_maize_drought_001",
    ):
        self._uri    = uri
        self._user   = user
        self._password = password
        self._pod_id = pod_id
        self._sdt_id = sdt_id
        self._pdt_id = pdt_id
        self._driver = None
        self._available = False

    def connect(self) -> bool:
        try:
            from neo4j import GraphDatabase
            self._driver    = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
            self._available = True
            log.info("BPDT KG connected to Neo4j at %s", self._uri)
            return True
        except ImportError:
            log.warning("neo4j driver not installed — BPDT KG seeding skipped")
        except Exception as exc:
            log.warning("Neo4j unavailable (%s) — BPDT KG seeding skipped", exc)
        return False

    def seed(self) -> None:
        if not self._available or self._driver is None:
            return
        with self._driver.session() as s:
            self._seed_constraints(s)
            self._seed_pod(s)
            self._seed_configuration(s)
            self._seed_subordinate_twins(s)
            self._seed_farm_manager(s)
            self._seed_water_budget(s)
            self._seed_emergent_behaviours(s)
            self._seed_boundary_conditions(s)
            self._seed_system_rules(s)
            self._seed_relationships(s)
        log.info("BPDT knowledge graph seeded (pod=%s)", self._pod_id)

    def _seed_constraints(self, session) -> None:
        constraints = [
            "CREATE CONSTRAINT bpdt_pod_id IF NOT EXISTS FOR (b:BioticPod) REQUIRE b.pod_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_config_id IF NOT EXISTS FOR (c:PodConfiguration) REQUIRE c.config_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_twin_id IF NOT EXISTS FOR (t:SubordinateTwin) REQUIRE t.twin_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_manager_id IF NOT EXISTS FOR (f:FarmManager) REQUIRE f.operator_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_eb_id IF NOT EXISTS FOR (e:EmergentBehaviour) REQUIRE e.behaviour_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_bc_id IF NOT EXISTS FOR (bc:BoundaryCondition) REQUIRE bc.bc_id IS UNIQUE",
            "CREATE CONSTRAINT bpdt_rule_id IF NOT EXISTS FOR (r:SystemRule) REQUIRE r.rule_id IS UNIQUE",
        ]
        for cq in constraints:
            try:
                session.run(cq)
            except Exception:
                pass

    def _seed_pod(self, session) -> None:
        session.run(
            """
            MERGE (b:BioticPod {pod_id: $pod_id})
            SET b.name              = 'Biotic Pod Digital Twin — Component Overseer',
                b.location          = 'Field plot (arid region ~25°N)',
                b.current_config_id = $config_id,
                b.framework         = 'DT-Forge v1.0',
                b.primary_goal      = '>=30% water saving; maize yield >=95% unstressed',
                b.response_time_min = 10
            """,
            pod_id=self._pod_id,
            config_id="cfg_v1_001",
        )

    def _seed_configuration(self, session) -> None:
        session.run(
            """
            MERGE (c:PodConfiguration {config_id: 'cfg_v1_001'})
            SET c.sdt_id         = $sdt_id,
                c.pdt_id         = $pdt_id,
                c.soil_type      = 'sandy_loam',
                c.crop_species   = 'Zea mays L.',
                c.growth_stage   = 'V6-V8',
                c.activation_date = '2025-04-01',
                c.pid_kp         = 15.0,
                c.pid_ki         = 0.8,
                c.pid_kd         = 2.0,
                c.pid_setpoint   = 0.08,
                c.version        = '1.0.0'
            """,
            sdt_id=self._sdt_id,
            pdt_id=self._pdt_id,
        )

    def _seed_subordinate_twins(self, session) -> None:
        twins = [
            {
                "twin_id":           self._sdt_id,
                "twin_type":         "soil",
                "status":            "active",
                "connector_endpoint": "http://localhost:8503/api/sdt",
                "fsm_states":        "OPTIMAL,DEPLETING,CRITICAL,WILTING_RISK,SENSOR_FAULT",
            },
            {
                "twin_id":           self._pdt_id,
                "twin_type":         "plant",
                "status":            "active",
                "connector_endpoint": "http://localhost:8503/api/pdt",
                "fsm_states":        "UNSTRESSED,MILD_STRESS,MODERATE_STRESS,SEVERE_STRESS,WILTING,RECOVERY",
            },
        ]
        for twin in twins:
            session.run(
                """
                MERGE (t:SubordinateTwin {twin_id: $twin_id})
                SET t += $props
                """,
                twin_id=twin["twin_id"],
                props={k: v for k, v in twin.items() if k != "twin_id"},
            )

    def _seed_farm_manager(self, session) -> None:
        session.run(
            """
            MERGE (f:FarmManager {operator_id: 'farm_manager_001'})
            SET f.role              = 'HITL override authority',
                f.contact_info      = 'webhook/SMS notifier (configured in bpdt.env)',
                f.response_time_avg = 10,
                f.override_authority = true,
                f.totp_required     = true
            """
        )

    def _seed_water_budget(self, session) -> None:
        session.run(
            """
            MERGE (wb:WaterBudget {period: 'weekly_template'})
            SET wb.description       = 'Weekly FAO-56 water balance template',
                wb.target_L          = 0.0,
                wb.consumed_L        = 0.0,
                wb.remaining_L       = 0.0,
                wb.method            = 'FAO-56: ΔS = Rain + Irr - ET - Drainage',
                wb.baseline_mm_day   = 2.5,
                wb.target_saving_pct = 30.0
            """
        )

    def _seed_emergent_behaviours(self, session) -> None:
        for eb in _EMERGENT_BEHAVIOURS:
            session.run(
                """
                MERGE (e:EmergentBehaviour {behaviour_id: $behaviour_id})
                SET e += $props
                """,
                behaviour_id=eb["behaviour_id"],
                props={k: v for k, v in eb.items() if k != "behaviour_id"},
            )

    def _seed_boundary_conditions(self, session) -> None:
        for bc in _BOUNDARY_CONDITIONS:
            session.run(
                """
                MERGE (bc:BoundaryCondition {bc_id: $bc_id})
                SET bc += $props
                """,
                bc_id=bc["bc_id"],
                props={k: v for k, v in bc.items() if k != "bc_id"},
            )

    def _seed_system_rules(self, session) -> None:
        for rule in _SYSTEM_RULES:
            session.run(
                """
                MERGE (r:SystemRule {rule_id: $rule_id})
                SET r += $props
                """,
                rule_id=rule["rule_id"],
                props={k: v for k, v in rule.items() if k != "rule_id"},
            )

    def _seed_relationships(self, session) -> None:
        # Pod USES configuration
        session.run(
            """
            MATCH (b:BioticPod {pod_id: $pod_id})
            MATCH (c:PodConfiguration {config_id: 'cfg_v1_001'})
            MERGE (b)-[:USES_CONFIG]->(c)
            """,
            pod_id=self._pod_id,
        )
        # Configuration INCLUDES sub-twins
        for twin_id in [self._sdt_id, self._pdt_id]:
            session.run(
                """
                MATCH (c:PodConfiguration {config_id: 'cfg_v1_001'})
                MATCH (t:SubordinateTwin {twin_id: $twin_id})
                MERGE (c)-[:INCLUDES]->(t)
                """,
                twin_id=twin_id,
            )
        # Pod OVERSEES sub-twins
        for twin_id in [self._sdt_id, self._pdt_id]:
            session.run(
                """
                MATCH (b:BioticPod {pod_id: $pod_id})
                MATCH (t:SubordinateTwin {twin_id: $twin_id})
                MERGE (b)-[:OVERSEES]->(t)
                """,
                pod_id=self._pod_id,
                twin_id=twin_id,
            )
        # Pod HAS boundary conditions
        for bc in _BOUNDARY_CONDITIONS:
            session.run(
                """
                MATCH (b:BioticPod {pod_id: $pod_id})
                MATCH (bc:BoundaryCondition {bc_id: $bc_id})
                MERGE (b)-[:HAS_BOUNDARY_CONDITION]->(bc)
                """,
                pod_id=self._pod_id,
                bc_id=bc["bc_id"],
            )
        # Pod MONITORS emergent behaviours
        for eb in _EMERGENT_BEHAVIOURS:
            session.run(
                """
                MATCH (b:BioticPod {pod_id: $pod_id})
                MATCH (e:EmergentBehaviour {behaviour_id: $eb_id})
                MERGE (b)-[:MONITORS]->(e)
                """,
                pod_id=self._pod_id,
                eb_id=eb["behaviour_id"],
            )
        # Pod MANAGED_BY farm manager
        session.run(
            """
            MATCH (b:BioticPod {pod_id: $pod_id})
            MATCH (f:FarmManager {operator_id: 'farm_manager_001'})
            MERGE (b)-[:MANAGED_BY]->(f)
            """,
            pod_id=self._pod_id,
        )
        # BoundaryCondition links sub-twins
        bc_twin_map = {
            "BC-01": (self._sdt_id, self._pdt_id),
            "BC-02": (self._sdt_id, self._pdt_id),
            "BC-03": (self._sdt_id, self._pdt_id),
            "BC-04": (self._pdt_id, self._sdt_id),
            "BC-05": (self._sdt_id, self._pdt_id),
        }
        for bc_id, (src_id, tgt_id) in bc_twin_map.items():
            session.run(
                """
                MATCH (bc:BoundaryCondition {bc_id: $bc_id})
                MATCH (src:SubordinateTwin {twin_id: $src_id})
                MATCH (tgt:SubordinateTwin {twin_id: $tgt_id})
                MERGE (bc)-[:FROM]->(src)
                MERGE (bc)-[:TO]->(tgt)
                """,
                bc_id=bc_id, src_id=src_id, tgt_id=tgt_id,
            )

    def log_irrigation_event(
        self,
        event_id: str,
        volume_L: float,
        duration_min: float,
        pre_vwc: float,
        trigger_reason: str,
    ) -> None:
        """Append a real-time IrrigationEvent node to the KG."""
        if not self._available or self._driver is None:
            return
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        with self._driver.session() as s:
            s.run(
                """
                MERGE (ie:IrrigationEvent {event_id: $event_id})
                SET ie.timestamp     = $ts,
                    ie.volume_L      = $volume,
                    ie.duration_min  = $duration,
                    ie.pre_vwc       = $pre_vwc,
                    ie.trigger_reason = $reason
                WITH ie
                MATCH (b:BioticPod {pod_id: $pod_id})
                MERGE (b)-[:HAS_EVENT]->(ie)
                """,
                event_id=event_id, ts=ts, volume=volume_L, duration=duration_min,
                pre_vwc=pre_vwc, reason=trigger_reason, pod_id=self._pod_id,
            )

    def log_anomaly(
        self,
        anomaly_id: str,
        anomaly_type: str,
        description: str,
    ) -> None:
        """Append a SystemAnomaly node."""
        if not self._available or self._driver is None:
            return
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        with self._driver.session() as s:
            s.run(
                """
                MERGE (sa:SystemAnomaly {anomaly_id: $anomaly_id})
                SET sa.type             = $atype,
                    sa.detected_at      = $ts,
                    sa.description      = $description,
                    sa.resolution_status = 'open'
                WITH sa
                MATCH (b:BioticPod {pod_id: $pod_id})
                MERGE (b)-[:HAS_ANOMALY]->(sa)
                """,
                anomaly_id=anomaly_id, atype=anomaly_type, ts=ts,
                description=description, pod_id=self._pod_id,
            )

    def close(self) -> None:
        if self._driver:
            self._driver.close()


def seed_bpdt_kg(
    uri: str = "bolt://localhost:7687",
    user: str = "neo4j",
    password: str = "password",
    pod_id: str = "bpdt_component_overseer_001",
    sdt_id: str = "sdt_water_depletion_001",
    pdt_id: str = "pdt_maize_drought_001",
) -> Optional["BPDTKnowledgeGraph"]:
    """Convenience function called at BPDT startup."""
    kg = BPDTKnowledgeGraph(uri, user, password, pod_id, sdt_id, pdt_id)
    if kg.connect():
        kg.seed()
        return kg
    return None
