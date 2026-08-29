# tests/test_physical.py
"""
Physical component tests: S4 (compaction), S5 (water stress), combined S4+S5,
cross-domain plant demand amplification, and model outputs.

Run with: pytest tests/test_physical.py -v
"""
import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — mock soil readings
# ─────────────────────────────────────────────────────────────────────────────

HEALTHY_READINGS = {
    "soil_moisture_pct":              28.0,
    "bulk_density_g_cm3":              1.30,
    "soil_temp_c":                    24.0,
}

S4_ONLY_READINGS = {
    **HEALTHY_READINGS,
    "bulk_density_g_cm3": 1.72,   # compacted — above warn threshold 1.6
}

S5_DRY_READINGS = {
    **HEALTHY_READINGS,
    "soil_moisture_pct": 11.5,    # dry — below warn threshold (varies by soil)
}

S5_WET_READINGS = {
    **HEALTHY_READINGS,
    "soil_moisture_pct": 55.0,    # waterlogged
}

S4_S5_READINGS = {
    **HEALTHY_READINGS,
    "bulk_density_g_cm3": 1.72,
    "soil_moisture_pct":  11.5,
}

MULTI_FACTOR_READINGS = {
    **S4_ONLY_READINGS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Simulator tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSimulatorPhysicalFaultMode:
    def test_physical_fault_mode_overrides_bd_and_moisture(self):
        from physical.simulator import SoilParcelSimulator, PHYSICAL_FAULT_OVERRIDES
        sim = SoilParcelSimulator(soil_type="loamy", fault_mode=False,
                                   fault_mode_physical=True)
        reading = sim.next_reading()
        # BD should be near the physical fault override value
        assert abs(reading["bulk_density_g_cm3"] - PHYSICAL_FAULT_OVERRIDES["bulk_density_g_cm3"]) < 0.1
        assert abs(reading["soil_moisture_pct"] - PHYSICAL_FAULT_OVERRIDES["soil_moisture_pct"]) < 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Rule engine tests — depletion detection
# ─────────────────────────────────────────────────────────────────────────────

def _make_get_latest(readings: dict):
    """Return a mock for get_latest() that serves from readings dict."""
    def _get_latest(field):
        return readings.get(field)
    return _get_latest


def _make_get_asset_thresholds():
    """Return loamy thresholds — enough for S4/S5 detection."""
    return {
        "soil_moisture_pct":       {"warn": 15.0, "crit": 10.0, "low": True},
        "soil_moisture_pct_high":  {"warn": 50.0, "crit": 60.0, "low": False},
        "bulk_density_g_cm3":      {"warn": 1.60, "crit": 1.75, "low": False},
        "soil_temp_c":             {"warn": 5.0,  "crit": 2.0,  "low": True},
    }


class TestRuleEnginePhysicalDetection:
    @patch("reactive.rule_engine.get_latest_cached", return_value=None)
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_s4_detected_when_bd_above_warn(self, mock_latest, mock_thresh, mock_cached):
        mock_latest.side_effect = _make_get_latest(S4_ONLY_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        from reactive.rule_engine import SoilParcelController
        ctrl = SoilParcelController()
        result = ctrl.detect_depletion_states()

        assert "S4" in result, f"Expected S4 in {list(result.keys())}"
        assert result["S4"]["name"] == "Compacted"

    @patch("reactive.rule_engine.get_latest_cached", return_value=None)
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_s5_detected_when_moisture_below_warn(self, mock_latest, mock_thresh, mock_cached):
        mock_latest.side_effect = _make_get_latest(S5_DRY_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        from reactive.rule_engine import SoilParcelController
        ctrl = SoilParcelController()
        result = ctrl.detect_depletion_states()

        assert "S5" in result, f"Expected S5 in {list(result.keys())}"
        triggers = result["S5"]["triggers"]
        assert any(t.get("stress_type") == "dry" for t in triggers)

    @patch("reactive.rule_engine.get_latest_cached", return_value=None)
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_s5_waterlogged_detected(self, mock_latest, mock_thresh, mock_cached):
        mock_latest.side_effect = _make_get_latest(S5_WET_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        from reactive.rule_engine import SoilParcelController
        ctrl = SoilParcelController()
        result = ctrl.detect_depletion_states()

        assert "S5" in result
        triggers = result["S5"]["triggers"]
        assert any(t.get("stress_type") == "waterlogged" for t in triggers)

    @patch("reactive.rule_engine.get_latest_cached", return_value=None)
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_s4_and_s5_both_detected_triggers_s8(self, mock_latest, mock_thresh, mock_cached):
        mock_latest.side_effect = _make_get_latest(S4_S5_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        from reactive.rule_engine import SoilParcelController
        ctrl = SoilParcelController()
        result = ctrl.detect_depletion_states()

        assert "S4" in result
        assert "S5" in result
        assert "S8" in result, "S4+S5 should trigger S8 multi-factor"
        assert result["S8"]["count"] >= 2

    @patch("reactive.rule_engine.get_latest_cached", return_value=None)
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_no_depletion_for_healthy_readings(self, mock_latest, mock_thresh, mock_cached):
        mock_latest.side_effect = _make_get_latest(HEALTHY_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        from reactive.rule_engine import SoilParcelController
        ctrl = SoilParcelController()
        result = ctrl.detect_depletion_states()

        physical_codes = {k for k in result if k in ("S4", "S5")}
        assert not physical_codes, f"Healthy readings should have no physical depletion: {physical_codes}"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-domain: plant demand urgency amplification
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossDomainUrgency:
    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_high_plant_demand_sets_urgency_high(self, mock_latest, mock_thresh):
        mock_latest.side_effect = _make_get_latest(S5_DRY_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        plant_demand_payload = json.dumps({"plant_water_demand_mm_day": 7.5})
        with patch("reactive.rule_engine.get_latest_cached", return_value=plant_demand_payload):
            from reactive.rule_engine import SoilParcelController
            ctrl = SoilParcelController()
            result = ctrl.detect_depletion_states()

        assert "S5" in result
        assert result["S5"].get("urgency") == "high", \
            "plant demand > 5mm/day should set S5 urgency to 'high'"

    @patch("reactive.rule_engine.get_asset_thresholds")
    @patch("reactive.rule_engine.get_latest")
    def test_low_plant_demand_keeps_urgency_normal(self, mock_latest, mock_thresh):
        mock_latest.side_effect = _make_get_latest(S5_DRY_READINGS)
        mock_thresh.return_value = _make_get_asset_thresholds()

        plant_demand_payload = json.dumps({"plant_water_demand_mm_day": 2.0})
        with patch("reactive.rule_engine.get_latest_cached", return_value=plant_demand_payload):
            from reactive.rule_engine import SoilParcelController
            ctrl = SoilParcelController()
            result = ctrl.detect_depletion_states()

        assert "S5" in result
        assert result["S5"].get("urgency") == "normal"


# ─────────────────────────────────────────────────────────────────────────────
# Physical fast path tests
# ─────────────────────────────────────────────────────────────────────────────

def _patch_agent_reads(readings: dict, depletion: dict):
    """Context managers to patch the intelligence agent's data reads."""
    def _get_latest(field):
        return readings.get(field)

    patches = [
        patch("intelligent.soil_intelligence_agent.get_latest", side_effect=_get_latest),
        patch("intelligent.soil_intelligence_agent.get_latest_cached",
              side_effect=lambda k: (
                  json.dumps(list(depletion.keys())) if k == "active_depletion_states"
                  else json.dumps(depletion) if k == "depletion_detail"
                  else None
              )),
        patch("intelligent.soil_intelligence_agent.get_asset_thresholds",
              return_value=_make_get_asset_thresholds()),
    ]
    return patches


class TestPhysicalFastPath:
    def test_s4_only_returns_fast_path(self):
        depletion = {"S4": {"name": "Compacted", "triggers": [
            {"field": "bulk_density_g_cm3", "value": 1.72, "threshold": 1.60}
        ]}}
        with patch("intelligent.soil_intelligence_agent.get_latest",
                   side_effect=_make_get_latest(S4_ONLY_READINGS)), \
             patch("intelligent.soil_intelligence_agent.get_latest_cached",
                   side_effect=lambda k: (
                       json.dumps(list(depletion.keys())) if k == "active_depletion_states"
                       else json.dumps(depletion) if k == "depletion_detail"
                       else None
                   )), \
             patch("intelligent.soil_intelligence_agent.get_asset_thresholds",
                   return_value=_make_get_asset_thresholds()):
            from intelligent.soil_intelligence_agent import SoilIntelligenceAgent
            agent = SoilIntelligenceAgent(soil_type="loamy")
            result = agent.run_physical_fast_path()

        assert result is not None, "S4-only should take fast path"
        assert result.get("action") == "S4_fast_path"
        assert result.get("confidence", 0) >= 0.85

    def test_s5_only_dry_returns_fast_path(self):
        depletion = {"S5": {"name": "Water-Stressed", "urgency": "normal", "triggers": [
            {"field": "soil_moisture_pct", "value": 11.5, "threshold": 15.0, "stress_type": "dry"}
        ]}}
        with patch("intelligent.soil_intelligence_agent.get_latest",
                   side_effect=_make_get_latest(S5_DRY_READINGS)), \
             patch("intelligent.soil_intelligence_agent.get_latest_cached",
                   side_effect=lambda k: (
                       json.dumps(list(depletion.keys())) if k == "active_depletion_states"
                       else json.dumps(depletion) if k == "depletion_detail"
                       else None
                   )), \
             patch("intelligent.soil_intelligence_agent.get_asset_thresholds",
                   return_value=_make_get_asset_thresholds()):
            from intelligent.soil_intelligence_agent import SoilIntelligenceAgent
            agent = SoilIntelligenceAgent(soil_type="loamy")
            result = agent.run_physical_fast_path()

        assert result is not None, "S5-only should take fast path"
        assert result.get("action") == "S5_fast_path"
        assert result.get("confidence", 0) >= 0.85

    def test_s4_s5_conflict_returns_irrigate_first(self):
        depletion = {
            "S4": {"name": "Compacted", "triggers": [
                {"field": "bulk_density_g_cm3", "value": 1.72, "threshold": 1.60}
            ]},
            "S5": {"name": "Water-Stressed", "urgency": "normal", "triggers": [
                {"field": "soil_moisture_pct", "value": 11.5, "threshold": 15.0, "stress_type": "dry"}
            ]},
            "S8": {"name": "Multi-Factor Depleted", "active_states": ["S4", "S5"], "count": 2},
        }
        with patch("intelligent.soil_intelligence_agent.get_latest",
                   side_effect=_make_get_latest(S4_S5_READINGS)), \
             patch("intelligent.soil_intelligence_agent.get_latest_cached",
                   side_effect=lambda k: (
                       json.dumps(list(depletion.keys())) if k == "active_depletion_states"
                       else json.dumps(depletion) if k == "depletion_detail"
                       else None
                   )), \
             patch("intelligent.soil_intelligence_agent.get_asset_thresholds",
                   return_value=_make_get_asset_thresholds()):
            from intelligent.soil_intelligence_agent import SoilIntelligenceAgent
            agent = SoilIntelligenceAgent(soil_type="loamy")
            result = agent.run_physical_fast_path()

        assert result is not None, "S4+S5 should take physical conflict fast path"
        assert result.get("action") == "S4_S5_conflict"
        # Irrigate should be the immediate action (S5 takes priority over S4)
        rec = result.get("recommendation", "")
        assert "irrigat" in rec.lower(), f"S4+S5 conflict should recommend irrigation first: {rec}"


# ─────────────────────────────────────────────────────────────────────────────
# Model tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWaterBalanceModel:
    @patch("simulation.model_runner.get_latest")
    def test_forecast_returns_correct_length(self, mock_latest):
        mock_latest.return_value = 25.0  # healthy VWC
        from simulation.model_runner import get_water_balance_forecast
        forecast = get_water_balance_forecast(horizon_days=7)
        assert len(forecast) == 7

    @patch("simulation.model_runner.get_latest")
    def test_forecast_fields_present(self, mock_latest):
        mock_latest.return_value = 25.0
        from simulation.model_runner import get_water_balance_forecast
        forecast = get_water_balance_forecast(horizon_days=3)
        required_keys = {"day", "vwc_pct", "et0_mm"}
        for entry in forecast:
            assert required_keys.issubset(entry.keys()), \
                f"Missing keys in forecast entry: {entry.keys()}"

    @patch("simulation.model_runner.get_latest")
    def test_dry_soil_shows_stress(self, mock_latest):
        mock_latest.return_value = 8.0   # very dry — below wilting point for loamy
        from simulation.model_runner import get_water_balance_forecast
        forecast = get_water_balance_forecast(horizon_days=5)
        stressed = [d for d in forecast if d.get("stressed")]
        assert len(stressed) > 0, "Very dry VWC should produce stressed days in forecast"


class TestCompactionModel:
    @patch("simulation.model_runner.get_latest")
    def test_high_bd_produces_high_risk(self, mock_latest):
        def _side(field):
            return {"bulk_density_g_cm3": 1.72, "soil_moisture_pct": 25.0}.get(field, 28.0)
        mock_latest.side_effect = _side
        from simulation.model_runner import get_compaction_risk
        result = get_compaction_risk(machinery_passes=0)
        assert result["risk_score"] >= 50, \
            f"BD=1.72 should give high compaction risk, got {result['risk_score']}"
        assert result["risk_level"] in ("high", "critical")

    @patch("simulation.model_runner.get_latest")
    def test_normal_bd_produces_low_risk(self, mock_latest):
        def _side(field):
            return {"bulk_density_g_cm3": 1.30, "soil_moisture_pct": 28.0}.get(field, 28.0)
        mock_latest.side_effect = _side
        from simulation.model_runner import get_compaction_risk
        result = get_compaction_risk(machinery_passes=0)
        assert result["risk_score"] < 40, \
            f"BD=1.30 should give low compaction risk, got {result['risk_score']}"

    @patch("simulation.model_runner.get_latest")
    def test_wet_soil_amplifies_risk(self, mock_latest):
        def _dry(field):
            return {"bulk_density_g_cm3": 1.55, "soil_moisture_pct": 20.0}.get(field, 20.0)
        def _wet(field):
            return {"bulk_density_g_cm3": 1.55, "soil_moisture_pct": 42.0}.get(field, 42.0)
        from simulation.model_runner import get_compaction_risk
        mock_latest.side_effect = _dry
        risk_dry = get_compaction_risk()["risk_score"]
        mock_latest.side_effect = _wet
        risk_wet = get_compaction_risk()["risk_score"]
        assert risk_wet >= risk_dry, \
            f"Wet soil ({risk_wet}) should have >= compaction risk than dry ({risk_dry})"


class TestErosionModel:
    @patch("simulation.model_runner.get_latest")
    def test_erosion_returns_expected_fields(self, mock_latest):
        mock_latest.return_value = 28.0
        from simulation.model_runner import get_erosion_risk
        result = get_erosion_risk(slope_pct=5.0, slope_length_m=50.0,
                                   cover_factor=0.3, support_practice=1.0)
        for key in ("soil_loss_t_ha_yr", "risk_class", "dominant_factor", "factors"):
            assert key in result, f"Missing key '{key}' in erosion result"

    @patch("simulation.model_runner.get_latest")
    def test_steep_slope_increases_soil_loss(self, mock_latest):
        mock_latest.return_value = 28.0
        from simulation.model_runner import get_erosion_risk
        flat   = get_erosion_risk(slope_pct=1.0,  slope_length_m=50.0)["soil_loss_t_ha_yr"]
        steep  = get_erosion_risk(slope_pct=20.0, slope_length_m=50.0)["soil_loss_t_ha_yr"]
        assert steep > flat, f"Steeper slope should increase soil loss: {flat} → {steep}"

    @patch("simulation.model_runner.get_latest")
    def test_cover_crop_reduces_erosion(self, mock_latest):
        mock_latest.return_value = 28.0
        from simulation.model_runner import get_erosion_risk
        bare   = get_erosion_risk(slope_pct=5.0, cover_factor=0.9)["soil_loss_t_ha_yr"]
        covered = get_erosion_risk(slope_pct=5.0, cover_factor=0.05)["soil_loss_t_ha_yr"]
        assert covered < bare, \
            f"Cover crop (C=0.05) should reduce erosion vs bare (C=0.9): {bare} → {covered}"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-domain sync — rooting depth in irrigation calc
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossDomainRootingDepth:
    def test_rooting_depth_used_in_available_water(self):
        from service_layer.cross_domain_sync import get_latest_physical_cross_domain

        plant_payload = json.dumps({
            "plant_water_demand_mm_day": 4.0,
            "rooting_depth_cm": 60.0,
            "crop_phenology_stage": "V6",
        })

        with patch("service_layer.cross_domain_sync.get_latest",
                   side_effect=lambda f: {"soil_moisture_pct": 25.0,
                                           "bulk_density_g_cm3": 1.30,
                                           "soil_temp_c": 24.0}.get(f)), \
             patch("service_layer.cross_domain_sync.get_latest_cached",
                   return_value=plant_payload):
            result = get_latest_physical_cross_domain()

        # available_water = VWC% × depth_m × 10
        # = 25 × 0.6 × 10 = 150 mm
        expected = 25.0 * 0.60 * 10
        assert abs(result["available_water_mm"] - expected) < 1.0, \
            f"Expected {expected} mm available water, got {result['available_water_mm']}"
        assert result["rooting_depth_cm"] == 60.0

    def test_fallback_depth_30cm_when_no_plant_data(self):
        from service_layer.cross_domain_sync import get_latest_physical_cross_domain

        with patch("service_layer.cross_domain_sync.get_latest",
                   side_effect=lambda f: {"soil_moisture_pct": 20.0,
                                           "bulk_density_g_cm3": 1.30,
                                           "soil_temp_c": 24.0}.get(f)), \
             patch("service_layer.cross_domain_sync.get_latest_cached",
                   return_value=None):
            result = get_latest_physical_cross_domain()

        expected = 20.0 * 0.30 * 10   # 30 cm fallback
        assert abs(result["available_water_mm"] - expected) < 1.0
        assert result["rooting_depth_cm"] == 30.0
