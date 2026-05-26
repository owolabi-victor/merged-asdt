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
    # Chemical
    f"MERGE (c4:SoilComponent  {{name:'nitrogen_pool',        category:'chemical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c5:SoilComponent  {{name:'phosphorus_pool',      category:'chemical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c6:SoilComponent  {{name:'potassium_pool',       category:'chemical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c7:SoilComponent  {{name:'soil_acidity',         category:'chemical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c8:SoilComponent  {{name:'salinity_level',       category:'chemical',   asset:'{ASSET_ID}'}})",
    f"MERGE (c9:SoilComponent  {{name:'organic_matter',       category:'chemical',   asset:'{ASSET_ID}'}})",
    # Biological
    f"MERGE (c10:SoilComponent {{name:'microbial_community',  category:'biological', asset:'{ASSET_ID}'}})",
    f"MERGE (c11:SoilComponent {{name:'soil_respiration',     category:'biological', asset:'{ASSET_ID}'}})",

    # Asset HAS_COMPONENT
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_texture'}})        MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_moisture'}})       MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_structure'}})      MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'nitrogen_pool'}})       MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'phosphorus_pool'}})     MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'potassium_pool'}})      MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_acidity'}})        MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'salinity_level'}})      MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'organic_matter'}})      MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'microbial_community'}}) MERGE (a)-[:HAS_COMPONENT]->(c)",
    f"MATCH (a:Asset {{id:'{ASSET_ID}'}}),(c:SoilComponent {{name:'soil_respiration'}})    MERGE (a)-[:HAS_COMPONENT]->(c)",

    # ── Depletion States (S1–S8) ──────────────────────────────────────────────
    "MERGE (d1:DepletionState {code:'S1', name:'Nutrient Depleted',        category:'chemical',   severity:'high'})",
    "MERGE (d2:DepletionState {code:'S2', name:'Acidified',                category:'chemical',   severity:'medium'})",
    "MERGE (d3:DepletionState {code:'S3', name:'Salinized',                category:'chemical',   severity:'high'})",
    "MERGE (d4:DepletionState {code:'S4', name:'Compacted',                category:'physical',   severity:'medium'})",
    "MERGE (d5:DepletionState {code:'S5', name:'Water-Stressed',           category:'physical',   severity:'high'})",
    "MERGE (d6:DepletionState {code:'S6', name:'Organic Matter Depleted',  category:'chemical',   severity:'medium'})",
    "MERGE (d7:DepletionState {code:'S7', name:'Biologically Inactive',    category:'biological', severity:'medium'})",
    "MERGE (d8:DepletionState {code:'S8', name:'Multi-Factor Depleted',    category:'multi',      severity:'critical'})",

    # Component → DepletionState
    "MATCH (c:SoilComponent {name:'nitrogen_pool'}),       (d:DepletionState {code:'S1'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'phosphorus_pool'}),     (d:DepletionState {code:'S1'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'potassium_pool'}),      (d:DepletionState {code:'S1'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'soil_acidity'}),        (d:DepletionState {code:'S2'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'salinity_level'}),      (d:DepletionState {code:'S3'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'soil_structure'}),      (d:DepletionState {code:'S4'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'soil_moisture'}),       (d:DepletionState {code:'S5'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'organic_matter'}),      (d:DepletionState {code:'S6'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'microbial_community'}), (d:DepletionState {code:'S7'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",
    "MATCH (c:SoilComponent {name:'soil_respiration'}),    (d:DepletionState {code:'S7'}) MERGE (c)-[:CAN_DEPLETE_AS]->(d)",

    # S1–S7 ESCALATES_TO S8 when multiple active
    "MATCH (d1:DepletionState {code:'S1'}),(d8:DepletionState {code:'S8'}) MERGE (d1)-[:ESCALATES_TO]->(d8)",
    "MATCH (d2:DepletionState {code:'S2'}),(d8:DepletionState {code:'S8'}) MERGE (d2)-[:ESCALATES_TO]->(d8)",
    "MATCH (d3:DepletionState {code:'S3'}),(d8:DepletionState {code:'S8'}) MERGE (d3)-[:ESCALATES_TO]->(d8)",
    "MATCH (d4:DepletionState {code:'S4'}),(d8:DepletionState {code:'S8'}) MERGE (d4)-[:ESCALATES_TO]->(d8)",
    "MATCH (d5:DepletionState {code:'S5'}),(d8:DepletionState {code:'S8'}) MERGE (d5)-[:ESCALATES_TO]->(d8)",
    "MATCH (d6:DepletionState {code:'S6'}),(d8:DepletionState {code:'S8'}) MERGE (d6)-[:ESCALATES_TO]->(d8)",
    "MATCH (d7:DepletionState {code:'S7'}),(d8:DepletionState {code:'S8'}) MERGE (d7)-[:ESCALATES_TO]->(d8)",

    # ── Soil Causes (what sensors detect) ────────────────────────────────────
    "MERGE (sc1:SoilCause  {name:'low_nitrogen',          sensor:'nitrogen_ppm',                   direction:'below'})",
    "MERGE (sc2:SoilCause  {name:'low_phosphorus',        sensor:'phosphorus_ppm',                 direction:'below'})",
    "MERGE (sc3:SoilCause  {name:'low_potassium',         sensor:'potassium_ppm',                  direction:'below'})",
    "MERGE (sc4:SoilCause  {name:'low_ph',                sensor:'soil_ph',                        direction:'below'})",
    "MERGE (sc5:SoilCause  {name:'high_ec',               sensor:'ec_ds_m',                        direction:'above'})",
    "MERGE (sc6:SoilCause  {name:'high_bulk_density',     sensor:'bulk_density_g_cm3',             direction:'above'})",
    "MERGE (sc7:SoilCause  {name:'low_moisture',          sensor:'soil_moisture_pct',              direction:'below'})",
    "MERGE (sc8:SoilCause  {name:'high_moisture',         sensor:'soil_moisture_pct',              direction:'above'})",
    "MERGE (sc9:SoilCause  {name:'low_organic_matter',    sensor:'organic_matter_pct',             direction:'below'})",
    "MERGE (sc10:SoilCause {name:'low_microbial_biomass', sensor:'microbial_biomass_mg_c_kg',      direction:'below'})",
    "MERGE (sc11:SoilCause {name:'low_respiration',       sensor:'soil_respiration_mg_co2_kg_day', direction:'below'})",

    # DepletionState → SoilCause (DETECTED_BY)
    "MATCH (d:DepletionState {code:'S1'}),(sc:SoilCause {name:'low_nitrogen'})          MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S1'}),(sc:SoilCause {name:'low_phosphorus'})        MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S1'}),(sc:SoilCause {name:'low_potassium'})         MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S2'}),(sc:SoilCause {name:'low_ph'})                MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S3'}),(sc:SoilCause {name:'high_ec'})               MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S4'}),(sc:SoilCause {name:'high_bulk_density'})     MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S5'}),(sc:SoilCause {name:'low_moisture'})          MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S5'}),(sc:SoilCause {name:'high_moisture'})         MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S6'}),(sc:SoilCause {name:'low_organic_matter'})    MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S7'}),(sc:SoilCause {name:'low_microbial_biomass'}) MERGE (d)-[:DETECTED_BY]->(sc)",
    "MATCH (d:DepletionState {code:'S7'}),(sc:SoilCause {name:'low_respiration'})       MERGE (d)-[:DETECTED_BY]->(sc)",

    # ── Management Actions ───────────────────────────────────────────────────
    # S1 — Nutrient amendments
    "MERGE (m1:ManagementAction  {name:'apply_CAN_fertiliser',     product:'CAN (27% N)',          rate_kg_ha:200,  timing:'Apply immediately, broadcast',               notes:'Calcium Ammonium Nitrate provides immediate N'})",
    "MERGE (m2:ManagementAction  {name:'apply_urea',               product:'Urea (46% N)',         rate_kg_ha:130,  timing:'Apply if rain expected within 24 hrs',        notes:'Urea volatilises without rain'})",
    "MERGE (m3:ManagementAction  {name:'apply_DAP_fertiliser',     product:'DAP (18-46-0)',        rate_kg_ha:100,  timing:'Incorporate into soil at planting',            notes:'Di-ammonium phosphate for P deficiency'})",
    "MERGE (m4:ManagementAction  {name:'apply_MOP_fertiliser',     product:'MOP (60% K₂O)',        rate_kg_ha:150,  timing:'Apply and incorporate before planting',        notes:'Muriate of potash for K deficiency'})",
    # S2 — Liming
    "MERGE (m5:ManagementAction  {name:'apply_agricultural_lime',  product:'Agricultural lime',    rate_kg_ha:1500, timing:'Apply before next planting season',            notes:'Raises pH by ~0.5 units per t/ha. Incorporate to 15 cm.'})",
    "MERGE (m6:ManagementAction  {name:'apply_dolomitic_lime',     product:'Dolomitic lime',       rate_kg_ha:1200, timing:'Apply 3 months before planting',               notes:'Also provides Mg. Preferred on Mg-deficient soils.'})",
    # S3 — Salinity
    "MERGE (m7:ManagementAction  {name:'leach_salts',              product:'Irrigation water',     rate_kg_ha:0,    timing:'Apply heavy irrigation to flush salts',        notes:'Requires good drainage. Apply gypsum if sodic.'})",
    "MERGE (m8:ManagementAction  {name:'apply_gypsum',             product:'Gypsum (CaSO₄)',       rate_kg_ha:2000, timing:'Apply and incorporate before leaching',        notes:'Displaces sodium on clay surfaces.'})",
    # S4 — Compaction
    "MERGE (m9:ManagementAction  {name:'deep_ripping',             product:'Mechanical ripping',   rate_kg_ha:0,    timing:'After harvest, before next season',            notes:'Break plough pan at 25–40 cm depth'})",
    "MERGE (m10:ManagementAction {name:'reduce_tillage',           product:'Conservation tillage', rate_kg_ha:0,    timing:'Adopt from next season onward',                notes:'Reduces re-compaction over time'})",
    # S5 — Water stress
    "MERGE (m11:ManagementAction {name:'irrigate_immediately',     product:'Water',                rate_kg_ha:0,    timing:'Irrigate within 24 hours',                     notes:'Apply 30–40 mm to restore field capacity'})",
    "MERGE (m12:ManagementAction {name:'improve_drainage',         product:'Drainage system',      rate_kg_ha:0,    timing:'Install before wet season',                    notes:'Subsurface drainage for waterlogged soils'})",
    # S6 — Organic matter
    "MERGE (m13:ManagementAction {name:'apply_compost',            product:'Compost',              rate_kg_ha:10000,timing:'Apply and incorporate before planting',         notes:'10 t/ha well-decomposed compost'})",
    "MERGE (m14:ManagementAction {name:'plant_cover_crops',        product:'Cover crop seed',      rate_kg_ha:25,   timing:'Plant after harvest',                          notes:'Legumes fix N and add organic matter'})",
    # S7 — Biological
    "MERGE (m15:ManagementAction {name:'add_organic_amendments',   product:'Compost + mulch',      rate_kg_ha:8000, timing:'Apply each season',                            notes:'Restores microbial food source'})",
    "MERGE (m16:ManagementAction {name:'reduce_chemical_inputs',   product:'Reduced pesticide',    rate_kg_ha:0,    timing:'Phase in from next season',                    notes:'Excessive chemicals suppress microbial activity'})",
    # S8 — Integrated restoration
    "MERGE (m17:ManagementAction {name:'integrated_restoration',   product:'Multiple amendments',  rate_kg_ha:0,    timing:'Sequence: lime → compost → deep rip → fertilise', notes:'Full soil restoration protocol for multi-factor depletion'})",

    # DepletionState → ManagementAction (REQUIRES)
    "MATCH (d:DepletionState {code:'S1'}),(m:ManagementAction {name:'apply_CAN_fertiliser'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S1'}),(m:ManagementAction {name:'apply_urea'})              MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S1'}),(m:ManagementAction {name:'apply_DAP_fertiliser'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S1'}),(m:ManagementAction {name:'apply_MOP_fertiliser'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S2'}),(m:ManagementAction {name:'apply_agricultural_lime'}) MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S2'}),(m:ManagementAction {name:'apply_dolomitic_lime'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S3'}),(m:ManagementAction {name:'leach_salts'})             MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S3'}),(m:ManagementAction {name:'apply_gypsum'})            MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S4'}),(m:ManagementAction {name:'deep_ripping'})            MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S4'}),(m:ManagementAction {name:'reduce_tillage'})          MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S5'}),(m:ManagementAction {name:'irrigate_immediately'})    MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S5'}),(m:ManagementAction {name:'improve_drainage'})        MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S6'}),(m:ManagementAction {name:'apply_compost'})           MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S6'}),(m:ManagementAction {name:'plant_cover_crops'})       MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S7'}),(m:ManagementAction {name:'add_organic_amendments'})  MERGE (d)-[:REQUIRES]->(m)",
    "MATCH (d:DepletionState {code:'S7'}),(m:ManagementAction {name:'reduce_chemical_inputs'})  MERGE (d)-[:REQUIRES]->(m)",
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