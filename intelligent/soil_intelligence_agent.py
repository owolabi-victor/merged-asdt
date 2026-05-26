# intelligent/soil_intelligence_agent.py
"""
Soil Intelligence Agent — Intelligent Layer.

Primary diagnostic engine for the soil depletion situation:
  A soil parcel has been continuously cropped for multiple seasons.
  The agent detects which depletion states (S1–S8) are active,
  diagnoses the degraded soil parameters, and generates
  soil-focused management recommendations.

6-step pipeline:
  1. Soil data retrieval (all 11 parameters)
  2. Depletion state detection (S1–S8)
  3. Differential diagnosis (rank by severity and confidence)
  4. Recommendation generation (soil amendments)
  5. Explanation generation (plain language report)
  6. Escalation decision (confidence < threshold → Soil Scientist)

Phase 6 changes:
  - P0: Accepts soil_type parameter; uses per-user thresholds/ranges
  - P1: Recommendation rates now scale with deficit severity
  - P2: Confidence scores are proportional to deviation from thresholds
  - P3: Timing strings are season-aware when planting/harvest dates available
"""
import uuid
import json
from datetime import datetime, timezone
from typing import Optional

from shared.influx_io import get_latest
from shared.redis_io  import get_latest_cached, set_active_diagnosis
from shared.mongo_io  import (
    save_diagnosis, save_recommendation,
    save_escalation, save_state_transition, log_event,
)
from shared.config import (
    ACTIVE_SOIL_TYPE, DEPLETION_STATES, SENSOR_FIELDS,
    THRESHOLDS_BY_SOIL_TYPE, HEALTHY_RANGES_BY_SOIL_TYPE,
    get_thresholds_for_soil_type, get_healthy_ranges_for_soil_type,
)
from intelligent.neo4j_kg import (
    diagnose_from_sensor_causes, get_management_for_state,
)

# ── Constants ─────────────────────────────────────────────────────────────────
ESCALATION_THRESHOLD = 0.70
AUTONOMOUS_THRESHOLD = 0.85

# ── Sensor field → soil cause name mapping ────────────────────────────────────
SENSOR_TO_CAUSE = {
    "nitrogen_ppm":                   ("low_nitrogen",          "low"),
    "phosphorus_ppm":                 ("low_phosphorus",        "low"),
    "potassium_ppm":                  ("low_potassium",         "low"),
    "soil_ph":                        ("low_ph",                "low"),
    "ec_ds_m":                        ("high_ec",               "high"),
    "bulk_density_g_cm3":             ("high_bulk_density",     "high"),
    "soil_moisture_pct":              ("low_moisture",          "low"),
    "organic_matter_pct":             ("low_organic_matter",    "low"),
    "microbial_biomass_mg_c_kg":      ("low_microbial_biomass", "low"),
    "soil_respiration_mg_co2_kg_day": ("low_respiration",       "low"),
}


class SoilIntelligenceAgent:
    """
    Soil Intelligence Agent — detects soil depletion and generates
    management recommendations.

    Phase 6: Now accepts soil_type to use per-user thresholds and ranges.
    """

    def __init__(self, soil_type: str = None, season_info: dict = None):
        """
        Args:
            soil_type: One of 'sandy', 'loamy', 'clay', 'silty'.
                       Defaults to ACTIVE_SOIL_TYPE from .env.
            season_info: Optional dict with 'planting_date' and 'harvest_date'
                         (ISO strings) for season-aware timing.
        """
        self.agent_name  = "soil_intelligence_agent"
        self.workflow_id = str(uuid.uuid4())
        self.soil_type   = soil_type or ACTIVE_SOIL_TYPE
        self.thresholds  = get_thresholds_for_soil_type(self.soil_type)
        self.healthy_ranges = get_healthy_ranges_for_soil_type(self.soil_type)
        self.season_info = season_info or {}

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Soil Data Retrieval
    # ─────────────────────────────────────────────────────────────────────────

    def retrieve_soil_data(self) -> dict:
        """Retrieve all current soil sensor readings from InfluxDB/Redis."""
        data = {}
        for field in SENSOR_FIELDS:
            val = get_latest(field)
            if val is not None:
                data[field] = val
        print(f"[SOIL AGENT] Retrieved {len(data)} soil parameters")
        return data

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Depletion State Detection
    # ─────────────────────────────────────────────────────────────────────────

    def detect_depletion_states(self, soil_data: dict) -> dict:
        """
        Determine which depletion states (S1–S8) are active based
        on current sensor readings against thresholds.
        Returns {state_code: {details}}.
        """
        active = {}
        thresholds = self.thresholds

        # S1: Nutrient Depleted (N, P, or K below threshold)
        nutrient_issues = []
        for field in ["nitrogen_ppm", "phosphorus_ppm", "potassium_ppm"]:
            val = soil_data.get(field)
            t   = thresholds.get(field)
            if val is not None and t is not None and val < t["warn"]:
                deficit_pct = round((1 - val / t["warn"]) * 100, 1)
                nutrient_issues.append({
                    "field": field, "value": round(val, 2),
                    "threshold": t["warn"], "deficit_pct": deficit_pct,
                    "critical": val < t["crit"],
                })
        if nutrient_issues:
            active["S1"] = {"name": "Nutrient Depleted", "issues": nutrient_issues,
                            "category": "chemical"}

        # S2: Acidified
        ph = soil_data.get("soil_ph")
        ph_t = thresholds.get("soil_ph")
        if ph is not None and ph_t is not None and ph < ph_t["warn"]:
            active["S2"] = {"name": "Acidified", "value": round(ph, 2),
                            "threshold": ph_t["warn"], "critical": ph < ph_t["crit"],
                            "category": "chemical"}

        # S3: Salinized
        ec = soil_data.get("ec_ds_m")
        ec_t = thresholds.get("ec_ds_m")
        if ec is not None and ec_t is not None and ec > ec_t["warn"]:
            active["S3"] = {"name": "Salinized", "value": round(ec, 2),
                            "threshold": ec_t["warn"], "critical": ec > ec_t["crit"],
                            "category": "chemical"}

        # S4: Compacted
        bd = soil_data.get("bulk_density_g_cm3")
        bd_t = thresholds.get("bulk_density_g_cm3")
        if bd is not None and bd_t is not None and bd > bd_t["warn"]:
            active["S4"] = {"name": "Compacted", "value": round(bd, 2),
                            "threshold": bd_t["warn"], "critical": bd > bd_t["crit"],
                            "category": "physical"}

        # S5: Water-Stressed
        mst = soil_data.get("soil_moisture_pct")
        mst_lo = thresholds.get("soil_moisture_pct")
        mst_hi = thresholds.get("soil_moisture_pct_high")
        if mst is not None:
            if mst_lo and mst < mst_lo["warn"]:
                active["S5"] = {"name": "Water-Stressed (dry)", "value": round(mst, 1),
                                "threshold": mst_lo["warn"], "stress_type": "dry",
                                "category": "physical"}
            elif mst_hi and mst > mst_hi["warn"]:
                active["S5"] = {"name": "Water-Stressed (wet)", "value": round(mst, 1),
                                "threshold": mst_hi["warn"], "stress_type": "waterlogged",
                                "category": "physical"}

        # S6: Organic Matter Depleted
        om = soil_data.get("organic_matter_pct")
        om_t = thresholds.get("organic_matter_pct")
        if om is not None and om_t is not None and om < om_t["warn"]:
            active["S6"] = {"name": "Organic Matter Depleted", "value": round(om, 2),
                            "threshold": om_t["warn"], "critical": om < om_t["crit"],
                            "category": "chemical"}

        # S7: Biologically Inactive
        bio_issues = []
        mb = soil_data.get("microbial_biomass_mg_c_kg")
        mb_t = thresholds.get("microbial_biomass_mg_c_kg")
        if mb is not None and mb_t is not None and mb < mb_t["warn"]:
            bio_issues.append({"field": "microbial_biomass_mg_c_kg",
                                "value": round(mb, 0), "threshold": mb_t["warn"]})
        resp = soil_data.get("soil_respiration_mg_co2_kg_day")
        resp_t = thresholds.get("soil_respiration_mg_co2_kg_day")
        if resp is not None and resp_t is not None and resp < resp_t["warn"]:
            bio_issues.append({"field": "soil_respiration_mg_co2_kg_day",
                                "value": round(resp, 1), "threshold": resp_t["warn"]})
        if bio_issues:
            active["S7"] = {"name": "Biologically Inactive", "issues": bio_issues,
                            "category": "biological"}

        # S8: Multi-Factor Depleted
        single = {k for k in active if k != "S8"}
        if len(single) >= 2:
            active["S8"] = {"name": "Multi-Factor Depleted",
                            "active_states": sorted(single),
                            "count": len(single), "category": "multi"}

        print(f"[SOIL AGENT] Detected depletion states: {list(active.keys())}")
        return active

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Differential Diagnosis
    # ─────────────────────────────────────────────────────────────────────────

    def differential_diagnosis(self, soil_data: dict, depletion: dict) -> list[dict]:
        """
        Rank depletion states by severity and assign confidence scores
        based on how far parameters are below/above thresholds.
        """
        sensor_causes = self._detect_sensor_causes(soil_data)
        kg_results = diagnose_from_sensor_causes(sensor_causes) if sensor_causes else []

        diagnoses = []
        for code, info in depletion.items():
            confidence = self._compute_confidence(code, info, soil_data)
            kg_match = next((r for r in kg_results if r.get("state_code") == code), {})
            diagnoses.append({
                "state_code":   code,
                "state_name":   info["name"],
                "category":     info.get("category", "unknown"),
                "confidence":   min(0.95, confidence),
                "details":      info,
                "kg_actions":   kg_match.get("actions", []),
                "kg_products":  kg_match.get("products", []),
                "kg_timings":   kg_match.get("timings", []),
                "kg_notes":     kg_match.get("notes", []),
            })

        # Sort: S8 first (multi-factor), then by confidence descending
        diagnoses.sort(key=lambda d: (0 if d["state_code"] == "S8" else 1,
                                       -d["confidence"]))

        print(f"[SOIL AGENT] Differential diagnosis ({len(diagnoses)} states):")
        for d in diagnoses:
            print(f"   {d['state_code']} {d['state_name']}: {d['confidence']:.0%}")

        return diagnoses

    def _compute_confidence(self, code: str, info: dict, soil_data: dict) -> float:
        """
        Compute confidence proportional to how far values deviate from thresholds.
        (P2: replaces flat 75%/85% scores with continuous scaling.)

        Formula: confidence = base + deviation_factor * scale
        where deviation_factor = abs(value - threshold) / threshold, capped at 1.0
        """
        if code == "S8":
            # Multi-factor: confidence scales with how many sub-states are active
            count = info.get("count", 2)
            return min(0.95, 0.80 + count * 0.03)

        base  = 0.55
        scale = 0.35  # max boost from deviation

        # For states with a single value (S2, S3, S4, S5, S6)
        value = info.get("value")
        threshold = info.get("threshold")
        if value is not None and threshold is not None and threshold > 0:
            deviation = abs(value - threshold) / threshold
            return base + scale * min(1.0, deviation)

        # For states with multiple issues (S1, S7)
        if "issues" in info:
            deviations = []
            for issue in info.get("issues", []):
                val = issue.get("value")
                thresh = issue.get("threshold")
                if val is not None and thresh is not None and thresh > 0:
                    deviations.append(abs(val - thresh) / thresh)
            if deviations:
                avg_deviation = sum(deviations) / len(deviations)
                # Boost further if any issue is critical
                critical_boost = 0.05 if any(i.get("critical") for i in info.get("issues", [])) else 0.0
                return base + scale * min(1.0, avg_deviation) + critical_boost

        # Fallback: no deviation data available
        return base + 0.15

    def _detect_sensor_causes(self, soil_data: dict) -> list[str]:
        """Check sensor values against thresholds to identify active soil causes."""
        thresholds = self.thresholds
        triggered = []
        for field, (cause_name, direction) in SENSOR_TO_CAUSE.items():
            val = soil_data.get(field)
            if val is None:
                continue
            t = thresholds.get(field)
            if t is None:
                continue
            if direction == "low" and val < t["warn"]:
                triggered.append(cause_name)
            elif direction == "high" and val > t["warn"]:
                triggered.append(cause_name)
        return triggered

    # ─────────────────────────────────────────────────────────────────────────
    # Season-aware timing helper (P3)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_timing(self, default: str, season_context: str = "any") -> str:
        """
        Generate season-aware timing string if planting/harvest dates are available.
        Falls back to the default static string otherwise.

        season_context values:
          'immediate'     → apply now
          'pre_planting'  → before next planting
          'post_harvest'  → after harvest
          'any'           → use default
        """
        planting = self.season_info.get("planting_date")
        harvest  = self.season_info.get("harvest_date")

        if season_context == "immediate":
            return "Apply within the next 7 days"

        if season_context == "pre_planting" and planting:
            return f"Apply before planting ({planting}). Allow 2 weeks for soil incorporation."

        if season_context == "post_harvest" and harvest:
            return f"Apply after harvest ({harvest}), before next season planting."

        if planting and harvest:
            return f"Apply between harvest ({harvest}) and planting ({planting})."

        return default

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: Recommendation Generation (P1: deficit-scaled rates)
    # ─────────────────────────────────────────────────────────────────────────

    def generate_recommendations(self, diagnoses: list[dict], soil_data: dict) -> list[dict]:
        """
        Generate soil management recommendations for each active depletion state.
        For S8 (multi-factor), generate an integrated restoration plan with
        sequenced interventions.
        """
        recommendations = []

        for diag in diagnoses:
            code       = diag["state_code"]
            confidence = diag["confidence"]

            if code == "S1":
                recs = self._recommend_nutrients(diag, soil_data, confidence)
                recommendations.extend(recs)
            elif code == "S2":
                recommendations.append(self._recommend_liming(diag, soil_data, confidence))
            elif code == "S3":
                recommendations.append(self._recommend_salinity(diag, soil_data, confidence))
            elif code == "S4":
                recommendations.append(self._recommend_compaction(diag, soil_data, confidence))
            elif code == "S5":
                recommendations.append(self._recommend_water(diag, soil_data, confidence))
            elif code == "S6":
                recommendations.append(self._recommend_organic_matter(diag, soil_data, confidence))
            elif code == "S7":
                recommendations.append(self._recommend_biological(diag, soil_data, confidence))
            elif code == "S8":
                recommendations.append(self._recommend_integrated(diag, diagnoses, confidence))

        return recommendations

    def _recommend_nutrients(self, diag, soil_data, confidence):
        """P1: All nutrient rates now scale with deficit severity."""
        recs = []
        healthy = self.healthy_ranges

        for issue in diag["details"].get("issues", []):
            field = issue["field"]
            val   = issue["value"]

            if field == "nitrogen_ppm":
                # Target: midpoint of healthy range for this soil type
                target = healthy.get("nitrogen_ppm", {}).get("min", 15.0)
                deficit = max(0, target - val)
                # Standard agronomic conversion: ppm → kg/ha for 15cm plough depth
                n_kg   = deficit * 2.24
                # CAN is 27% N — chemistry constant
                can_kg = round(max(n_kg / 0.27, 50))
                recs.append({
                    "state": "S1", "type": "fertiliser",
                    "product": "CAN (27% N)", "rate_kg_ha": can_kg,
                    "timing": self._get_timing("Apply immediately", "immediate"),
                    "method": "Broadcast evenly, avoid wet foliage",
                    "rationale": f"Nitrogen is {val:.1f} ppm (target: ≥{target:.0f} ppm for {self.soil_type} soil). "
                                 f"Deficit: {deficit:.1f} ppm → {can_kg} kg/ha CAN required.",
                    "confidence": confidence,
                    "layer": "autonomous" if confidence >= AUTONOMOUS_THRESHOLD else "intelligent",
                })

            elif field == "phosphorus_ppm":
                # P1: Scale phosphorus rate with deficit
                target = healthy.get("phosphorus_ppm", {}).get("min", 15.0)
                deficit = max(0, target - val)
                # DAP is 46% P₂O₅ ≈ 20% P; conversion: P deficit ppm × 2.24 / 0.20
                p_kg = deficit * 2.24
                dap_kg = round(max(p_kg / 0.20, 50))
                recs.append({
                    "state": "S1", "type": "fertiliser",
                    "product": "DAP (18-46-0)", "rate_kg_ha": dap_kg,
                    "timing": self._get_timing("Incorporate into soil at planting", "pre_planting"),
                    "method": "Band or broadcast and incorporate",
                    "rationale": f"Phosphorus is {val:.1f} mg/kg (target: ≥{target:.0f} mg/kg for {self.soil_type} soil). "
                                 f"Deficit: {deficit:.1f} ppm → {dap_kg} kg/ha DAP required.",
                    "confidence": confidence,
                    "layer": "autonomous" if confidence >= AUTONOMOUS_THRESHOLD else "intelligent",
                })

            elif field == "potassium_ppm":
                # P1: Scale potassium rate with deficit
                target = healthy.get("potassium_ppm", {}).get("min", 150.0)
                deficit = max(0, target - val)
                # MOP is 60% K₂O ≈ 50% K; conversion: K deficit ppm × 2.24 / 0.50
                k_kg = deficit * 2.24
                mop_kg = round(max(k_kg / 0.50, 50))
                recs.append({
                    "state": "S1", "type": "fertiliser",
                    "product": "MOP (60% K₂O)", "rate_kg_ha": mop_kg,
                    "timing": self._get_timing("Apply and incorporate before planting", "pre_planting"),
                    "method": "Broadcast and incorporate",
                    "rationale": f"Potassium is {val:.0f} mg/kg (target: ≥{target:.0f} mg/kg for {self.soil_type} soil). "
                                 f"Deficit: {deficit:.0f} ppm → {mop_kg} kg/ha MOP required.",
                    "confidence": confidence,
                    "layer": "autonomous" if confidence >= AUTONOMOUS_THRESHOLD else "intelligent",
                })
        return recs

    def _recommend_liming(self, diag, soil_data, confidence):
        """P1: Lime rate scales with pH deficit. Standard agronomic formula."""
        ph = diag["details"].get("value", 0)
        # Target: lower bound of healthy range for soil type
        target_ph = self.healthy_ranges.get("soil_ph", {}).get("min", 5.5)
        ph_deficit = max(0, target_ph - ph)
        # Lime requirement: ~1500 kg/ha per 0.5 pH unit for loamy/silty,
        # higher for clay (buffering), lower for sandy
        lime_factor = {"sandy": 1000, "loamy": 1500, "clay": 2500, "silty": 1500}
        factor = lime_factor.get(self.soil_type, 1500)
        lime_kg = round(max(ph_deficit / 0.5 * factor, 500))
        return {
            "state": "S2", "type": "liming",
            "product": "Agricultural lime (CaCO₃)", "rate_kg_ha": lime_kg,
            "timing": self._get_timing("Apply after harvest, before next planting", "post_harvest"),
            "method": "Incorporate to 15 cm depth using disc harrow",
            "rationale": f"Soil pH is {ph:.2f} (target: ≥{target_ph:.1f} for {self.soil_type} soil). "
                         f"pH deficit of {ph_deficit:.2f} units requires {lime_kg} kg/ha lime.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_salinity(self, diag, soil_data, confidence):
        """P1: Gypsum rate scales with EC excess."""
        ec = diag["details"].get("value", 0)
        target_ec = self.healthy_ranges.get("ec_ds_m", {}).get("max", 1.5)
        ec_excess = max(0, ec - target_ec)
        # Gypsum: ~1000 kg/ha per 1.0 dS/m excess
        gypsum_kg = round(max(ec_excess * 1000, 500))
        return {
            "state": "S3", "type": "salinity_management",
            "product": "Gypsum + leaching irrigation", "rate_kg_ha": gypsum_kg,
            "timing": self._get_timing("Apply gypsum then heavy irrigate to flush salts", "immediate"),
            "method": "Broadcast gypsum, then apply 50–80 mm irrigation",
            "rationale": f"EC is {ec:.2f} dS/m (target: ≤{target_ec:.1f} dS/m for {self.soil_type} soil). "
                         f"Excess of {ec_excess:.2f} dS/m requires {gypsum_kg} kg/ha gypsum.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_compaction(self, diag, soil_data, confidence):
        bd = diag["details"].get("value", 0)
        return {
            "state": "S4", "type": "mechanical",
            "product": "Deep ripping + conservation tillage", "rate_kg_ha": 0,
            "timing": self._get_timing("After harvest, before next season planting", "post_harvest"),
            "method": "Deep rip to 30–40 cm, then adopt reduced tillage",
            "rationale": f"Bulk density is {bd:.2f} g/cm³ — above threshold for {self.soil_type} soil. "
                         "Compaction restricts root growth and water infiltration.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_water(self, diag, soil_data, confidence):
        stress = diag["details"].get("stress_type", "dry")
        val    = diag["details"].get("value", 0)
        if stress == "dry":
            return {
                "state": "S5", "type": "irrigation",
                "product": "Water", "rate_kg_ha": 0,
                "timing": self._get_timing("Irrigate within 24 hours", "immediate"),
                "method": "Apply 30–40 mm to restore field capacity",
                "rationale": f"Soil moisture is {val:.1f}% — water stress detected on {self.soil_type} soil.",
                "confidence": confidence, "layer": "autonomous",
            }
        return {
            "state": "S5", "type": "drainage",
            "product": "Drainage system", "rate_kg_ha": 0,
            "timing": self._get_timing("Install subsurface drainage before wet season", "pre_planting"),
            "method": "Tile drains or raised beds",
            "rationale": f"Soil moisture is {val:.1f}% — waterlogged on {self.soil_type} soil.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_organic_matter(self, diag, soil_data, confidence):
        """P1: Compost rate scales with OM deficit."""
        om = diag["details"].get("value", 0)
        target_om = self.healthy_ranges.get("organic_matter_pct", {}).get("min", 2.0)
        om_deficit = max(0, target_om - om)
        # Compost: ~5000 kg/ha per 0.5% OM deficit (empirical agronomic estimate)
        compost_kg = round(max(om_deficit / 0.5 * 5000, 3000))
        return {
            "state": "S6", "type": "organic_amendment",
            "product": "Compost + cover crops", "rate_kg_ha": compost_kg,
            "timing": self._get_timing(
                "Apply compost now, plant cover crops after harvest", "post_harvest"),
            "method": f"Spread {compost_kg / 1000:.0f} t/ha compost, incorporate to 15 cm",
            "rationale": f"Organic matter is {om:.2f}% (target: ≥{target_om:.1f}% for {self.soil_type} soil). "
                         f"Deficit of {om_deficit:.2f}% requires {compost_kg} kg/ha compost.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_biological(self, diag, soil_data, confidence):
        """P1: Compost rate scales with microbial deficit severity."""
        # Determine severity from worst bio indicator
        mb = soil_data.get("microbial_biomass_mg_c_kg")
        mb_t = self.thresholds.get("microbial_biomass_mg_c_kg", {}).get("warn", 150)
        resp = soil_data.get("soil_respiration_mg_co2_kg_day")
        resp_t = self.thresholds.get("soil_respiration_mg_co2_kg_day", {}).get("warn", 25)

        # Calculate worst deficit ratio
        deficit_ratios = []
        if mb is not None and mb_t > 0:
            deficit_ratios.append(max(0, 1 - mb / mb_t))
        if resp is not None and resp_t > 0:
            deficit_ratios.append(max(0, 1 - resp / resp_t))

        severity = max(deficit_ratios) if deficit_ratios else 0.3
        # Base 4000 kg/ha, scales up to 12000 for severe deficit
        compost_kg = round(max(4000 + severity * 8000, 4000))
        return {
            "state": "S7", "type": "biological_restoration",
            "product": "Compost + mulch + reduced chemical inputs", "rate_kg_ha": compost_kg,
            "timing": self._get_timing("Begin this season, continue for 2–3 seasons", "immediate"),
            "method": "Apply compost, maintain surface mulch, reduce pesticide use",
            "rationale": f"Microbial activity is below threshold for {self.soil_type} soil "
                         f"({severity:.0%} depleted). Restoring biological function requires "
                         f"{compost_kg} kg/ha sustained organic matter inputs.",
            "confidence": confidence, "layer": "intelligent",
        }

    def _recommend_integrated(self, diag, diagnoses, confidence):
        active = diag["details"].get("active_states", [])
        sequence = []
        seq_order = {"S2": 1, "S6": 2, "S7": 2, "S4": 3, "S1": 4, "S3": 4, "S5": 5}
        for code in sorted(active, key=lambda c: seq_order.get(c, 9)):
            state_info = DEPLETION_STATES.get(code, {})
            sequence.append(f"Step {len(sequence)+1}: Address {code} ({state_info.get('name', code)}) — {state_info.get('recovery', 'consult specialist')}")

        return {
            "state": "S8", "type": "integrated_restoration",
            "product": "Multiple amendments (sequenced)", "rate_kg_ha": 0,
            "timing": self._get_timing(
                "Sequence: lime → compost → deep rip → wait 2 weeks → fertilise", "post_harvest"),
            "method": "\n".join(sequence),
            "rationale": f"Multi-factor depletion on {self.soil_type} soil: {len(active)} states active "
                         f"({', '.join(active)}). Integrated restoration required.",
            "confidence": confidence, "layer": "intelligent",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: Explanation Generation
    # ─────────────────────────────────────────────────────────────────────────

    def generate_explanation(self, soil_data: dict, depletion: dict,
                              recommendations: list[dict]) -> str:
        """Generate a plain-language soil depletion report."""
        lines = [
            "SOIL DEPLETION DIAGNOSIS REPORT",
            "=" * 50,
            f"Parcel: {self.soil_type.title()} soil — Soil type: {self.soil_type}",
            f"Active depletion states: {len(depletion)}",
            "",
            "CURRENT SOIL PARAMETERS:",
        ]

        for field in SENSOR_FIELDS:
            val = soil_data.get(field)
            if val is None:
                continue
            hr = self.healthy_ranges.get(field, {})
            unit = hr.get("unit", "")
            status = "✅ OK"
            if hr:
                if val < hr.get("min", float("-inf")):
                    status = "🔴 BELOW HEALTHY"
                elif val > hr.get("max", float("inf")):
                    status = "🔴 ABOVE HEALTHY"
            lines.append(f"  {field:<40} {val:>8.2f} {unit:<15} {status}")

        lines.append("")
        lines.append("DEPLETION STATES DETECTED:")
        if not depletion:
            lines.append("  None — soil is healthy")
        for code, info in sorted(depletion.items()):
            lines.append(f"  {code}: {info['name']} ({info.get('category', '')})")

        lines.append("")
        lines.append("RECOMMENDED ACTIONS:")
        for i, rec in enumerate(recommendations, 1):
            lines.append(f"  {i}. [{rec['state']}] {rec['product']}")
            lines.append(f"     Rate: {rec['rate_kg_ha']} kg/ha")
            lines.append(f"     Timing: {rec['timing']}")
            lines.append(f"     Rationale: {rec['rationale']}")

        lines.append("")
        lines.append("FOLLOW-UP:")
        lines.append("  • Re-test soil parameters in 30 days")
        lines.append("  • Monitor for state transitions (depletion → healthy)")
        lines.append("  • If no improvement, escalate to Soil Scientist")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # Full pipeline
    # ─────────────────────────────────────────────────────────────────────────

    def run_full_pipeline(self, soil_type: str = None) -> dict:
        """
        Execute the complete 6-step soil depletion diagnostic pipeline.

        If soil_type is provided, it overrides the instance's soil_type
        (for backward compatibility with callers that pass it directly).
        """
        if soil_type and soil_type in THRESHOLDS_BY_SOIL_TYPE:
            self.soil_type = soil_type
            self.thresholds = get_thresholds_for_soil_type(soil_type)
            self.healthy_ranges = get_healthy_ranges_for_soil_type(soil_type)

        effective_soil_type = self.soil_type

        print(f"\n[SOIL AGENT] {'='*50}")
        print(f"[SOIL AGENT] Starting soil depletion diagnostic pipeline")
        print(f"[SOIL AGENT] Soil type: {effective_soil_type}")

        # Step 1: Retrieve soil data
        soil_data = self.retrieve_soil_data()
        if not soil_data:
            return {"error": "No soil sensor data available.", "should_escalate": True}

        # Step 2: Detect depletion states
        depletion = self.detect_depletion_states(soil_data)

        # Step 3: Differential diagnosis
        diagnoses = self.differential_diagnosis(soil_data, depletion)

        if not diagnoses:
            return {
                "soil_data": soil_data,
                "depletion_states": [],
                "message": "No depletion detected — soil is healthy.",
                "should_escalate": False,
            }

        # Step 4: Generate recommendations
        recommendations = self.generate_recommendations(diagnoses, soil_data)

        # Step 5: Generate explanation
        explanation = self.generate_explanation(soil_data, depletion, recommendations)

        # Step 6: Escalation decision
        primary = diagnoses[0]
        confidence = primary["confidence"]
        should_escalate = confidence < ESCALATION_THRESHOLD

        # Persist
        diagnosis_id = save_diagnosis({
            "primary_state":    primary["state_code"],
            "all_states":       [d["state_code"] for d in diagnoses],
            "confidence":       confidence,
            "soil_type":        effective_soil_type,
            "soil_data":        {k: round(v, 2) for k, v in soil_data.items()},
            "layer_responsible": primary.get("layer", "intelligent") if recommendations else "intelligent",
        })

        for rec in recommendations:
            rec["diagnosis_id"] = diagnosis_id
            save_recommendation({**rec, "delivered_via": "api"})

        set_active_diagnosis({
            "primary_state": primary["state_code"],
            "confidence":    confidence,
            "active_states": [d["state_code"] for d in diagnoses],
        })

        if should_escalate:
            save_escalation({
                "diagnosis_id": diagnosis_id,
                "reason": f"Confidence {confidence:.0%} below threshold {ESCALATION_THRESHOLD:.0%}",
            })

        result = {
            "workflow_id":       self.workflow_id,
            "soil_type":         effective_soil_type,
            "soil_data":         {k: round(v, 2) for k, v in soil_data.items()},
            "depletion_states":  [
                {"code": d["state_code"], "name": d["state_name"],
                 "confidence": round(d["confidence"], 2), "category": d["category"]}
                for d in diagnoses
            ],
            "primary_state":     {
                "code":       primary["state_code"],
                "name":       primary["state_name"],
                "confidence": round(confidence, 2),
            },
            "recommendations":   recommendations,
            "explanation":       explanation,
            "should_escalate":   should_escalate,
        }

        print(f"\n[SOIL AGENT] Pipeline complete.")
        print(f"   Active states: {[d['state_code'] for d in diagnoses]}")
        print(f"   Primary: {primary['state_code']} ({confidence:.0%})")
        print(f"   Escalate: {should_escalate}")
        print(f"\n{explanation}")

        return result


# ── Run standalone ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("AGENTIC SOIL DIGITAL TWIN — Soil Depletion Diagnostic Pipeline")
    print("=" * 60)
    print()
    print("Situation: A soil parcel has been continuously cropped for")
    print("multiple seasons without adequate management. The ASDT")
    print("diagnoses soil depletion across physical, chemical, and")
    print("biological parameters.")
    print()

    agent = SoilIntelligenceAgent()
    result = agent.run_full_pipeline()

    if "error" in result:
        print(f"\n❌ Error: {result['error']}")
        print("Make sure python main.py is running first.")
    else:
        print("\n" + "=" * 60)
        print("PIPELINE COMPLETE — Summary")
        print("=" * 60)
        print(f"  Soil type:         {result['soil_type']}")
        print(f"  Active states:     {[s['code'] for s in result['depletion_states']]}")
        print(f"  Primary state:     {result['primary_state']['code']} ({result['primary_state']['name']})")
        print(f"  Confidence:        {result['primary_state']['confidence']:.0%}")
        print(f"  Should escalate:   {result['should_escalate']}")
        print(f"  Recommendations:   {len(result['recommendations'])}")
        for r in result["recommendations"]:
            print(f"    [{r['state']}] {r['product']} — {r['rate_kg_ha']} kg/ha")