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
    "bulk_density_g_cm3":             ("high_bulk_density",     "high"),
    "soil_moisture_pct":              ("low_moisture",          "low"),
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

        # For states with a single value (S4, S5)
        value = info.get("value")
        threshold = info.get("threshold")
        if value is not None and threshold is not None and threshold > 0:
            deviation = abs(value - threshold) / threshold
            return base + scale * min(1.0, deviation)

        # For states carrying a list of issues
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

            if code == "S4":
                recommendations.append(self._recommend_compaction(diag, soil_data, confidence))
            elif code == "S5":
                recommendations.append(self._recommend_water(diag, soil_data, confidence))
            elif code == "S8":
                recommendations.append(self._recommend_integrated(diag, diagnoses, confidence))

        return recommendations

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

    def _recommend_integrated(self, diag, diagnoses, confidence):
        active = diag["details"].get("active_states", [])
        sequence = []
        seq_order = {"S5": 1, "S4": 2}
        for code in sorted(active, key=lambda c: seq_order.get(c, 9)):
            state_info = DEPLETION_STATES.get(code, {})
            sequence.append(f"Step {len(sequence)+1}: Address {code} ({state_info.get('name', code)}) — {state_info.get('recovery', 'consult specialist')}")

        return {
            "state": "S8", "type": "integrated_restoration",
            "product": "Multiple amendments (sequenced)", "rate_kg_ha": 0,
            "timing": self._get_timing(
                "Sequence: irrigate → deep rip once moisture allows", "post_harvest"),
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
    # Physical fast path (S4-only, S5-only, or S4+S5 conflict resolution)
    # ─────────────────────────────────────────────────────────────────────────

    def run_physical_fast_path(self) -> dict | None:
        """
        High-confidence fast path for purely physical depletion states.
        Executes without the full 6-step pipeline (no LLM, no KG lookup).
        Returns a result dict if conditions are met, None otherwise.

        Conditions for fast path:
          - Only physical states active (S4 and/or S5), no chemical/biological.
          - Confidence > AUTONOMOUS_THRESHOLD (0.85).
        """
        soil_data  = self.retrieve_soil_data()
        if not soil_data:
            return None

        depletion = self.detect_depletion_states(soil_data)
        if not depletion:
            return None

        physical  = {k: v for k, v in depletion.items()
                     if v.get("category") == "physical"}
        non_phys  = {k: v for k, v in depletion.items()
                     if k != "S8" and v.get("category") != "physical"}

        if non_phys:
            return None  # mixed states — use full pipeline

        if not physical:
            return None

        # Route to the appropriate fast-path handler
        active = set(physical.keys())
        if active == {"S4"}:
            rec = self._fast_path_s4(soil_data, depletion["S4"])
        elif active == {"S5"}:
            rec = self._fast_path_s5(soil_data, depletion["S5"])
        elif active == {"S4", "S5"}:
            rec = self._physical_conflict_s4_s5(soil_data, depletion)
        else:
            return None

        if rec.get("confidence", 0) < AUTONOMOUS_THRESHOLD:
            return None  # not confident enough — fall back to full pipeline

        diagnosis_id = save_diagnosis({
            "primary_state":     rec["state"],
            "all_states":        list(active),
            "confidence":        rec["confidence"],
            "soil_type":         self.soil_type,
            "soil_data":         {k: round(v, 2) for k, v in soil_data.items()},
            "layer_responsible": "autonomous",
        })
        rec["diagnosis_id"] = diagnosis_id
        save_recommendation({**rec, "delivered_via": "fast_path"})
        set_active_diagnosis({
            "primary_state": rec["state"],
            "confidence":    rec["confidence"],
            "active_states": list(active),
        })

        return {
            "fast_path":        True,
            "soil_type":        self.soil_type,
            "active_states":    list(active),
            "recommendation":   rec,
            "soil_data":        {k: round(v, 2) for k, v in soil_data.items()},
        }

    def _fast_path_s4(self, soil_data: dict, s4_info: dict) -> dict:
        """Deep-rip recommendation for S4 compaction only."""
        bd       = s4_info.get("value", soil_data.get("bulk_density_g_cm3", 0))
        bd_warn  = s4_info.get("threshold", 1.6)
        severity = min(1.0, max(0.0, (bd - bd_warn) / 0.2))
        depth_cm = 30 + int(severity * 20)          # 30–50 cm based on severity
        confidence = min(0.95, 0.85 + severity * 0.10)

        return {
            "state": "S4", "type": "mechanical", "layer": "autonomous",
            "product": f"Deep ripping to {depth_cm} cm",
            "rate_kg_ha": 0,
            "timing": self._get_timing("After harvest, before next season", "post_harvest"),
            "method": f"Rip to {depth_cm} cm depth, then adopt reduced tillage",
            "rationale": (
                f"Bulk density {bd:.2f} g/cm³ on {self.soil_type} soil "
                f"(threshold: {bd_warn:.2f}) — {severity:.0%} above warning. "
                f"Root growth and water infiltration restricted."
            ),
            "confidence": round(confidence, 2),
        }

    def _fast_path_s5(self, soil_data: dict, s5_info: dict) -> dict:
        """Irrigation or drainage recommendation for S5 only."""
        stress    = s5_info.get("stress_type", "dry")
        mst       = s5_info.get("value", soil_data.get("soil_moisture_pct", 0))
        threshold = s5_info.get("threshold", 15.0)

        # Check cross-domain plant demand for irrigation volume
        plant_demand_mm = 0.0
        try:
            import json as _j
            raw = get_latest_cached("cross_domain_plant_demand")
            if raw:
                pd_data = _j.loads(raw) if isinstance(raw, str) else raw
                plant_demand_mm = float(pd_data.get("plant_water_demand_mm_day") or 0)
        except Exception:
            pass

        confidence = min(0.95, 0.85 + min(0.10, abs(mst - threshold) / threshold))

        if stress == "dry":
            from shared.config import HEALTHY_RANGES_BY_SOIL_TYPE
            fc_mid = HEALTHY_RANGES_BY_SOIL_TYPE.get(self.soil_type, {}).get(
                "soil_moisture_pct", {}).get("min", 20.0)
            deficit_pct = max(0, fc_mid - mst)
            # Convert VWC deficit % to mm for 30cm depth
            irrig_mm = round(deficit_pct * 0.3 * 10 + max(0, plant_demand_mm * 3), 0)
            return {
                "state": "S5", "type": "irrigation", "layer": "autonomous",
                "product": "Irrigation water",
                "rate_kg_ha": 0,
                "timing": self._get_timing("Irrigate within 24 hours", "immediate"),
                "method": f"Apply {irrig_mm:.0f} mm to restore field capacity",
                "rationale": (
                    f"Soil moisture {mst:.1f}% below warning threshold {threshold:.1f}% "
                    f"on {self.soil_type} soil. Deficit ≈ {deficit_pct:.1f}% VWC "
                    f"→ {irrig_mm:.0f} mm required."
                    + (f" Plant demand: {plant_demand_mm:.1f} mm/day." if plant_demand_mm else "")
                ),
                "confidence": round(confidence, 2),
            }

        return {
            "state": "S5", "type": "drainage", "layer": "autonomous",
            "product": "Subsurface drainage",
            "rate_kg_ha": 0,
            "timing": self._get_timing("Install drainage before wet season", "pre_planting"),
            "method": "Tile drains or raised beds; delay irrigation immediately",
            "rationale": (
                f"Soil moisture {mst:.1f}% above {threshold:.1f}% "
                f"— waterlogged on {self.soil_type} soil."
            ),
            "confidence": round(confidence, 2),
        }

    def _physical_conflict_s4_s5(self, soil_data: dict, depletion: dict) -> dict:
        """
        S4 + S5 conflict resolution.
        Priority: irrigation first (immediate plant need), deep rip post-harvest.
        This is because compaction cannot be remedied during the growing season
        without risking further damage to wet soils.
        """
        s4 = depletion.get("S4", {})
        s5 = depletion.get("S5", {})
        bd  = s4.get("value", 1.65)
        mst = s5.get("value", 12.0)
        stress = s5.get("stress_type", "dry")

        if stress == "dry":
            from shared.config import HEALTHY_RANGES_BY_SOIL_TYPE
            fc_mid = HEALTHY_RANGES_BY_SOIL_TYPE.get(self.soil_type, {}).get(
                "soil_moisture_pct", {}).get("min", 20.0)
            irrig_mm = round(max(0, fc_mid - mst) * 0.3 * 10, 0)
            plan = (
                f"Step 1 (immediate): Irrigate {irrig_mm:.0f} mm — plant water stress is urgent. "
                f"Step 2 (post-harvest): Deep rip to 40 cm to address compaction "
                f"(BD {bd:.2f} g/cm³). Note: do NOT rip while soil is wet — "
                f"risk of smearing. Wait until moisture is at field capacity."
            )
            return {
                "state": "S4+S5", "type": "physical_conflict", "layer": "autonomous",
                "product": "Irrigation now + Deep ripping post-harvest",
                "rate_kg_ha": 0,
                "timing": "Immediate irrigation; deep ripping after harvest",
                "method": plan,
                "rationale": (
                    f"Both S4 (BD {bd:.2f} g/cm³) and S5-dry ({mst:.1f}% moisture) active. "
                    f"Irrigation is prioritised — plants cannot wait. "
                    f"Ripping wet compacted soil causes smearing; schedule post-harvest."
                ),
                "confidence": 0.91,
            }

        # S4 + S5-wet: improve drainage first, then rip when dry
        return {
            "state": "S4+S5", "type": "physical_conflict", "layer": "autonomous",
            "product": "Drainage + deferred deep ripping",
            "rate_kg_ha": 0,
            "timing": "Drainage immediately; ripping when soil reaches field capacity",
            "method": (
                f"Step 1: Improve drainage (raised beds or tile drains) to reduce waterlogging. "
                f"Step 2: Once moisture normalises, deep rip to 40 cm. "
                f"Ripping waterlogged soil worsens structural damage."
            ),
            "rationale": (
                f"S4 (BD {bd:.2f} g/cm³) + S5-wet ({mst:.1f}% moisture) on {self.soil_type} soil. "
                f"Drainage first; compaction remediation deferred."
            ),
            "confidence": 0.90,
        }

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