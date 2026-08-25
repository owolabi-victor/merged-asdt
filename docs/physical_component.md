# Physical Component — ASDT

The physical component of the Agentic Soil Digital Twin (ASDT) covers the mechanical and hydraulic properties of soil: water content, bulk density, and temperature. It is the fastest-changing part of the soil system and the most directly actionable — a farmer can irrigate or schedule deep ripping within hours, whereas chemical or biological remediation takes weeks.

---

## Physical Parameters

| Field | Unit | Depletion trigger |
|---|---|---|
| `soil_moisture_pct` | % VWC | S5: dry < warn / waterlogged > warn\_high |
| `bulk_density_g_cm3` | g/cm³ | S4: compacted > warn |
| `soil_temp_c` | °C | Informational (no depletion state, but affects biological activity) |

### Default thresholds (loamy soil)

| Parameter | Warn | Critical |
|---|---|---|
| Bulk density | > 1.60 g/cm³ | > 1.75 g/cm³ |
| Soil moisture (dry) | < 15% | < 10% |
| Soil moisture (wet) | > 50% | > 60% |

Thresholds are defined per soil type in `shared/config.py` and can be overridden by a scientist via the UI or `POST /api/v1/ui/scientist/threshold`.

---

## Depletion States

### S4 — Compacted
Bulk density exceeds the warn threshold. Root elongation is mechanically impeded. Key indicators: hard, dense topsoil; poor water infiltration; stunted crop growth. **Remediation: subsoil ripping to 30–50 cm when soil moisture is at or below field capacity.**

### S5 — Water-Stressed
Soil moisture is outside the plant-available water window:
- **Dry stress:** moisture < warn threshold → insufficient water for crop transpiration.
- **Waterlogged:** moisture > high warn threshold → anaerobic conditions, root rot risk.

**Remediation:** irrigate (dry) or improve drainage (wet).

### S8 — Multi-Factor Depleted
Triggered automatically when 2 or more single-factor states are active simultaneously. S4+S5 is a common combination in compacted, drought-stressed soils.

---

## Physical Fault Mode

The simulator supports a physical-only fault mode that generates S4+S5 depletion without affecting the chemical or biological parameters. This allows isolated testing of the physical response pipeline.

**Enable via environment variable:**
```bash
FAULT_MODE_PHYSICAL=true docker compose up simulator
```

Or directly:
```python
from physical.simulator import SoilParcelSimulator
sim = SoilParcelSimulator(soil_type="loamy", fault_mode_physical=True)
```

**What it simulates:**
- `bulk_density_g_cm3 = 1.72 g/cm³` — severely compacted (S4 active)
- `soil_moisture_pct = 11.5%` — dry water stress (S5 dry active)
- All chemical and biological parameters remain at healthy nominal values

---

## Physical Models

### Water Balance (FAO-56)
Located in `simulation/model_runner.py: WaterBalanceModel`.

Produces a 7-day forecast of root-zone volumetric water content using the simplified FAO-56 water balance equation:

```
VWC_new = VWC_old + (rainfall + irrigation - ET_c) / (depth_m × 1000) × 100
```

**ET₀** is estimated using the Hargreaves-Samani formula with:
- Fixed extraterrestrial radiation Ra = 12 MJ/m²/day (West Africa, ~7°N)
- Fixed diurnal temperature range ΔT = 8°C (tropical humid)
- Soil-temperature-derived T\_mean proxy when meteorological data is unavailable

**Field capacity and wilting point** are set per soil type (loamy: FC=35%, WP=15%). A forecast day is flagged `stressed=True` when VWC falls below wilting point.

**Cross-domain integration:** the rooting depth published by the Plant DT (`rooting_depth_cm`) is used as the depth parameter, replacing the 30 cm fixed default. The `CrossDomainSynchronizer._react_to_plant_triggers()` method uses this to compute the actual root-zone available water column.

**API:** `GET /api/v1/physical/water_balance?horizon_days=7`

### Compaction Risk (0–100 score)
Located in `simulation/model_runner.py: CompactionModel`.

Risk score based on three factors:
1. **BD deviation** from the soil-type warning threshold (scaled 0–80)
2. **Moisture amplifier** — soils >80% field capacity compact more easily under load: `factor = 1.0 + max(0, (moisture_ratio - 0.8) × 1.5)`
3. **Machinery passes** — each pass above 2 adds 5 points

Risk levels: `low` (0–24) · `moderate` (25–49) · `high` (50–74) · `critical` (75–100)

**API:** `GET /api/v1/physical/compaction_risk?machinery_passes=0`

### Erosion Risk (RUSLE)
Located in `simulation/model_runner.py: ErosionModel`.

Implements a simplified RUSLE (Revised Universal Soil Loss Equation):

```
A = R × K × LS × C × P
```

| Factor | Description | Source |
|---|---|---|
| R | Rainfall erosivity | 650 MJ·mm/ha/hr/yr (West African tropical default) |
| K | Soil erodibility | Empirical lookup by soil type (loamy=0.28, sandy=0.05, clay=0.12, silty=0.45) |
| LS | Slope length-steepness | Computed from `slope_pct` and `slope_length_m` |
| C | Cover management | User-supplied (0.01 = dense cover, 1.0 = bare) |
| P | Support practice | User-supplied (1.0 = no practice, 0.1 = terracing) |

Risk classes (t/ha/yr): `Very Low` (<2) · `Low` (2–5) · `Moderate` (5–10) · `High` (10–20) · `Very High` (>20)

**API:** `GET /api/v1/physical/erosion_risk?slope_pct=2&slope_length_m=50&cover_factor=0.3&support_practice=1.0`

---

## Physical Fast Path (Intelligence Layer)

Located in `intelligent/soil_intelligence_agent.py: run_physical_fast_path()`.

When **only** physical states (S4, S5, or S4+S5) are active and no chemical or biological states are present, the intelligence agent bypasses the full 6-step pipeline and returns a high-confidence recommendation directly.

**Confidence threshold:** 0.85 (same as `AUTONOMOUS_THRESHOLD`)

### S4-only fast path
- Recommends deep ripping to a depth scaled with BD severity (30–50 cm)
- Confidence: `min(0.95, 0.85 + BD_severity × 0.10)`

### S5-only fast path
- Dry stress: recommends irrigation volume based on VWC deficit + Plant DT demand
  - `irrigation_mm = deficit_pct × 0.3 × 10 + max(0, plant_demand_mm × 3)`
- Wet stress: recommends drainage improvement
- Confidence: `min(0.95, 0.85 + deviation_from_threshold)`

### S4+S5 conflict resolution
When both compaction and drought stress are active simultaneously, ripping wet soil causes smearing of pore structure and worsens long-term porosity. The conflict resolution is:

1. **Immediate:** Irrigate to bring moisture to field capacity (plant survival takes priority)
2. **Deferred:** Schedule deep ripping post-harvest when moisture has normalised

Confidence: 0.91 (dry) / 0.90 (wet).

**API:** `POST /api/v1/physical/fast_diagnose`

---

## API Endpoints

All physical endpoints are under `/api/v1/physical/`:

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/physical/status` | Any user | Current VWC, BD, temp, S4/S5 state |
| GET | `/physical/water_balance` | Scientist | 7-day FAO-56 forecast |
| GET | `/physical/compaction_risk` | Scientist | Compaction risk score + action |
| GET | `/physical/erosion_risk` | Scientist | RUSLE soil loss estimate |
| POST | `/physical/override` | Scientist | Override a physical field value |
| POST | `/physical/fast_diagnose` | Scientist | Run physical fast path |

---

## UI

### Scientist UI (`app_scientist.py`, port 8501) — Physical Segment tab
- Live gauges for VWC, bulk density, soil temperature with status badges
- S4/S5 depletion state alerts with trigger details
- Physical fast diagnose button
- Time-series selector (1h / 6h / 24h / 7 days) for all three fields
- 7-day water balance forecast chart with field capacity / wilting point lines
- Compaction risk meter (0–100 score with colour-coded severity)
- RUSLE erosion risk panel with configurable site parameters
- Physical field override form (writes to InfluxDB + Redis)
- Per-field threshold editing

### Farmer UI (`app_farmer.py`, port 8502) — Dashboard
- Physical status cards: moisture (with drought/waterlogged detection), compaction, temperature
- Cross-domain urgency badge (high plant water demand)
- Action buttons:
  - **"I will irrigate"** — logs observation, appears when S5 dry is active
  - **"I will deep rip"** — logs observation, appears when S4 is active
  - **"I need advice"** — sends a context-aware question to the LLM advisor and shows the answer inline
- Physical quick-question buttons in the Ask Advisor tab

---

## Running Physical Fault Mode (Docker)

```bash
# Full multi-factor depletion (S1+S2+S4+S6+S7 → S8)
docker compose up

# Physical-only depletion (S4+S5, chemical/bio healthy)
FAULT_MODE_PHYSICAL=true docker compose up simulator rule_engine
```

In the Scientist UI → Physical Segment tab, you will see:
- S4 (Compacted) and S5 (Water-Stressed) depletion states flagged
- Compaction risk score ≥ 70 (critical)
- VWC below wilting point with stressed days in the forecast

---

## Running Tests

```bash
cd /path/to/merged-asdt
pytest tests/test_physical.py -v
```

Tests cover:
- Simulator: `fault_mode_physical` overrides BD and moisture while keeping chemical/bio healthy
- Rule engine: S4 detected when BD > warn, S5 dry/wet, S4+S5 → S8, healthy readings produce no depletion
- Cross-domain urgency: high plant demand (>5 mm/day) sets S5 urgency to `"high"`
- Physical fast path: S4-only, S5-only, S4+S5 conflict (irrigate first), non-physical blocks fast path
- Water balance: forecast length, field presence, dry soil shows stressed days
- Compaction model: high BD → high risk, normal BD → low risk, wet soil amplifies risk
- Erosion model: fields present, steep slope > flat, cover crop < bare
- Cross-domain: rooting depth from Plant DT used in available water calculation
