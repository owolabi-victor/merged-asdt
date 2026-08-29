# intelligent/neo4j_kg.py
"""
Soil Knowledge Graph — Neo4j.

Encodes semantic knowledge about soil depletion across three domains:
  - Physical: texture, moisture, bulk density (compaction)
  - Chemical: pH, N, P, K, EC (salinity), organic matter
  - Biological: microbial biomass, soil respiration

Node types:
  Asset, SoilComponent, DepletionState, SoilCause, ManagementAction

Relationship types:
  Asset          -[HAS_COMPONENT]->     SoilComponent
  SoilComponent  -[CAN_DEPLETE_AS]->    DepletionState
  DepletionState -[DETECTED_BY]->       SoilCause
  DepletionState -[REQUIRES]->          ManagementAction
  DepletionState -[ESCALATES_TO]->      DepletionState (S8)

This graph is queried by the Soil Intelligence Agent to diagnose soil
depletion and generate management recommendations.
"""
import json
from neo4j import GraphDatabase
from shared.config import NEO4J_URI, NEO4J_USER, NEO4J_PASS, ASSET_ID, ASSET_TYPE

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


GRAPH_SETUP = [

    # ── Soil Parcel Asset ──────────────────────────────────────────────────────
    f"MERGE (a:Asset {{id:'{ASSET_ID}', type:'{ASSET_TYPE}'}})",

    # ── Soil Components (Physical, Chemical, Biological) ──────────────────────
    # Physical
    f"MERGE (c1:SoilComponent  {{name:'soil_texture',         category:'physical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c2:SoilComponent  {{name:'soil_moisture',        category:'physical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c3:SoilComponent  {{name:'soil_structure',       category:'physical',   asset:'{ASSET_ID}'}})",

    # Asset HAS_COMPONENT
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_texture'}})        MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_moisture'}})       MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_structure'}})      MERGE (a)-[:HAS_COMPONENT]->(c)",

    # ── Depletion States (S4, S5, S8) ──────────────────────────────────────────────
    "MERGE (d4:DepletionState {code:'S4', name:'Compacted',                category:'physical',   severity:'medium'})",
    "MERGE (d5:DepletionState {code:'S5', name:'Water-Stressed',           category:'physical',   severity:'high'})",
    "MERGE (d8:DepletionState {code:'S8', name:'Multi-Factor Depleted',    category:'multi',      severity:'critical'})",

    # Component → DepletionState
    "MATCH (c:SoilComponent {name:'soil_structure'}),      (d:DepletionState {code:'S4'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'soil_moisture'}),       (d:DepletionState {code:'S5'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",

    # S4/S5 ESCALATE_TO S8 when both are active
    "MATCH (d4:DepletionState {code:'S4'}),(d8:DepletionState {code:'S8'}) MERGE (d4)-[:ESCALATES_TO]->(d8)",
    "MATCH (d5:DepletionState {code:'S5'}),(d8:DepletionState {code:'S8'}) MERGE (d5)-[:ESCALATES_TO]->(d8)",

    # ── Soil Causes (what sensors detect) ────────────────────────────────────
    "MERGE (sc6:SoilCause  {name:'high_bulk_density',     sensor:'bulk_density_g_cm3',             direction:'above'})",
    "MERGE (sc7:SoilCause  {name:'low_moisture',          sensor:'soil_moisture_pct',              direction:'below'})",
    "MERGE (sc8:SoilCause  {name:'high_moisture',         sensor:'soil_moisture_pct',              direction:'above'})",

    # DepletionState → SoilCause (DETECTED_BY)
    "MATCH (d:DepletionState {code:'S4'}),(sc:SoilCause {name:'high_bulk_density'})     MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S5'}),(sc:SoilCause {name:'low_moisture'})          MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S5'}),(sc:SoilCause {name:'high_moisture'})         MERGE (d)-[:DETECTED_BY]->(sc)",

    # ── Management Actions ───────────────────────────────────────────────────
    # S4 — Compaction
    "MERGE (m9:ManagementAction  {name:'deep_ripping',             product:'Mechanical ripping',   rate_kg_ha:0,    timing:'After harvest, before next season',            notes:'Break plough pan at 25–40 cm depth'})",
    "MERGE (m10:ManagementAction {name:'reduce_tillage',           product:'Conservation tillage', rate_kg_ha:0,    timing:'Adopt from next season onward',                notes:'Reduces re-compaction over time'})",
    # S5 — Water stress
    "MERGE (m11:ManagementAction {name:'irrigate_immediately',     product:'Water',                rate_kg_ha:0,    timing:'Irrigate within 24 hours',                     notes:'Apply 30–40 mm to restore field capacity'})",
    "MERGE (m12:ManagementAction {name:'improve_drainage',         product:'Drainage system',      rate_kg_ha:0,    timing:'Install before wet season',                    notes:'Subsurface drainage for waterlogged soils'})",
    # S8 — Integrated restoration
    "MERGE (m17:ManagementAction {name:'integrated_restoration',   product:'Multiple amendments',  rate_kg_ha:0,    timing:'Sequence: lime → compost → deep rip → fertilise', notes:'Full soil restoration protocol for multi-factor depletion'})",

    # DepletionState → ManagementAction (REQUIRES)
    "MATCH (d:DepletionState {code:'S4'}),(m:ManagementAction {name:'deep_ripping'})            MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S4'}),(m:ManagementAction {name:'reduce_tillage'})          MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S5'}),(m:ManagementAction {name:'irrigate_immediately'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S5'}),(m:ManagementAction {name:'improve_drainage'})        MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S8'}),(m:ManagementAction {name:'integrated_restoration'})  MERGE (d)-[:REQUIRES]->(m)",
]


def setup_graph():
    """Create the entire soil knowledge graph. Safe to re-run (all MERGE)."""
    with driver.session() as s:
        for q in GRAPH_SETUP:
            s.run(q)
    _count_graph()
    print("[KG] ✅ Soil depletion knowledge graph initialised.")


def _count_graph():
    """Print node and relationship counts."""
    with driver.session() as s:
        nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels  = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"[KG]    Nodes: {nodes}, Relationships: {rels}")


def diagnose_from_sensor_causes(sensor_causes: list) -> list:
    """
    Given a list of detected soil cause names (from sensor thresholds),
    return depletion states with their required management actions.
    """
    cypher = """
        MATCH (sc:SoilCause)<-[:DETECTED_BY]-(d:DepletionState)-[:REQUIRES]->(m:ManagementAction)
        WHERE sc.name IN $causes
        RETURN d.code               AS state_code,
               d.name               AS state_name,
               d.severity           AS severity,
               d.category           AS category,
               collect(DISTINCT sc.sensor)     AS sensors,
               collect(DISTINCT m.name)        AS actions,
               collect(DISTINCT m.product)     AS products,
               collect(DISTINCT m.rate_kg_ha)  AS rates,
               collect(DISTINCT m.timing)      AS timings,
               collect(DISTINCT m.notes)       AS notes
        ORDER BY CASE d.severity
                   WHEN 'critical' THEN 0
                   WHEN 'high'     THEN 1
                   WHEN 'medium'   THEN 2
                   ELSE 3
                 END
    """
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, causes=sensor_causes)]


def get_management_for_state(state_code: str) -> list:
    """
    Get all management actions for a given depletion state.
    """
    cypher = """
        MATCH (d:DepletionState {code: $code})-[:REQUIRES]->(m:ManagementAction)
        RETURN m.name       AS action,
               m.product    AS product,
               m.rate_kg_ha AS rate_kg_ha,
               m.timing     AS timing,
               m.notes      AS notes
    """
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, code=state_code)]


def get_soil_components() -> list:
    cypher = f"""
        MATCH (:Asset {{id:'{ASSET_ID}'}})-[:HAS_COMPONENT]->(c:SoilComponent)
        RETURN c.name AS name, c.category AS category
    """
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher)]


def get_all_depletion_states() -> list:
    cypher = "MATCH (d:DepletionState) RETURN d.code, d.name, d.severity, d.category ORDER BY d.code"
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher)]


def get_depletion_chain(state_code: str) -> list:
    """
    Trace the full chain: Component → DepletionState → SoilCause + ManagementAction.
    """
    cypher = """
        MATCH (c:SoilComponent)-[:CAN_DEPLETE_AS]->(d:DepletionState {code: $code})
        OPTIONAL MATCH (d)-[:DETECTED_BY]->(sc:SoilCause)
        OPTIONAL MATCH (d)-[:REQUIRES]->(m:ManagementAction)
        RETURN c.name AS component, c.category AS component_category,
               d.code AS state_code, d.name AS state_name,
               collect(DISTINCT sc.name)   AS sensor_causes,
               collect(DISTINCT m.name)    AS management_actions
    """
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, code=state_code)]


def get_full_graph() -> dict:
    """
    Return all nodes and relationships for UI visualization.
    Returns {nodes: [{id, name, type, ...}], edges: [{source, target, type}]}.
    """
    nodes = []
    edges = []

    # Get all nodes
    node_cypher = """
        MATCH (n)
        WHERE n:Asset OR n:SoilComponent OR n:DepletionState OR n:SoilCause OR n:ManagementAction
        RETURN labels(n)[0] AS type, properties(n) AS props
    """
    with driver.session() as s:
        for r in s.run(node_cypher):
            props = dict(r["props"])
            node_id = props.get("id") or props.get("name") or props.get("code", "unknown")
            nodes.append({
                "id": node_id,
                "name": props.get("name", props.get("code", node_id)),
                "type": r["type"],
                "properties": props,
            })

    # Get all relationships
    edge_cypher = """
        MATCH (a)-[r]->(b)
        WHERE (a:Asset OR a:SoilComponent OR a:DepletionState OR a:SoilCause OR a:ManagementAction)
          AND (b:Asset OR b:SoilComponent OR b:DepletionState OR b:SoilCause OR b:ManagementAction)
        RETURN
            coalesce(a.id, a.name, a.code) AS source,
            type(r) AS type,
            coalesce(b.id, b.name, b.code) AS target
    """
    with driver.session() as s:
        for r in s.run(edge_cypher):
            edges.append({
                "source": r["source"],
                "type": r["type"],
                "target": r["target"],
            })

    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    setup_graph()
    print("\n--- Diagnose from sensor causes: low_nitrogen, low_ph, low_organic_matter ---")
    print(json.dumps(
        diagnose_from_sensor_causes(["low_nitrogen", "low_ph", "low_organic_matter"]),
        indent=2,
    ))
    print("\n--- Soil components ---")
    print(json.dumps(get_soil_components(), indent=2))
    print("\n--- All depletion states ---")
    print(json.dumps(get_all_depletion_states(), indent=2))