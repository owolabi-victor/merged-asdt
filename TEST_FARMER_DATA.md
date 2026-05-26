# Test Data for Farmer Dashboard

Use these values to test your farmer dashboard measurements across different soil scenarios.

---

## Scenario 1: Healthy Loamy Soil ✅

**Use Case:** Farmer with well-managed, fertile soil — should show all green indicators.

### Physical Properties

```
Soil Moisture:           48%           (range: 40-60% is optimal)
Soil Temperature:        24.5°C        (normal for daytime)
Bulk Density:            1.35 g/cm³    (healthy, not compacted)
```

### Chemical Properties

```
pH:                      6.5           (optimal for most crops)
Nitrogen (N):            22 ppm        (adequate, above 15 threshold)
Phosphorus (P):          14 mg/kg      (adequate)
Potassium (K):           145 ppm       (adequate)
EC (Salinity):           0.6 dS/m      (normal, not saline)
Organic Matter:          2.8%          (good)
```

### Biological Properties

```
Microbial Biomass:       320 mg C/kg   (healthy, >300 is good)
Soil Respiration:        52 mg CO₂/kg/day  (good activity)
```

**Expected Dashboard Output:**

- Health Score: **85-90/100** ✅
- All indicators: GREEN
- No alerts
- Recommendation: "Maintain current practices; consider cover cropping"

---

## Scenario 2: Nitrogen-Deficient Soil 🔴

**Use Case:** Farmer reports yellowing leaves; suspected N deficiency.

### Physical Properties

```
Soil Moisture:           45%           (adequate)
Soil Temperature:        23.2°C        (normal)
Bulk Density:            1.38 g/cm³    (acceptable)
```

### Chemical Properties

```
pH:                      6.2           (neutral, ok)
Nitrogen (N):            8.7 ppm       (CRITICAL: <10 threshold)
Phosphorus (P):          11 mg/kg      (adequate)
Potassium (K):           120 ppm       (adequate)
EC (Salinity):           0.5 dS/m      (normal)
Organic Matter:          2.1%          (adequate)
```

### Biological Properties

```
Microbial Biomass:       280 mg C/kg   (adequate)
Soil Respiration:        45 mg CO₂/kg/day  (adequate)
```

**Expected Dashboard Output:**

- Health Score: **75-78/100** ⚠️
- Indicator: Nitrogen = RED ❌
- Alert: "🚨 CRITICAL: Nitrogen deficiency detected (8.7 ppm). Maize requires ≥15 ppm."
- Recommendation: "Apply 60 kg/ha CAN (Calcium Ammonium Nitrate) immediately"

**Test Instructions:**

1. Enter these values in farmer dashboard
2. Verify N shows RED/CRITICAL
3. Check recommendation appears
4. Try "I applied this" feedback

---

## Scenario 3: Acidic Soil with Low Nutrients 🌧️

**Use Case:** Farmer with weathered, acidic soil (common in tropical regions).

### Physical Properties

```
Soil Moisture:           52%           (slightly high, but ok)
Soil Temperature:        22.8°C        (normal)
Bulk Density:            1.42 g/cm³    (acceptable)
```

### Chemical Properties

```
pH:                      4.9           (CRITICAL: <5.0 is very acidic)
Nitrogen (N):            12 ppm        (low)
Phosphorus (P):          6 mg/kg       (CRITICAL: <10)
Potassium (K):           85 ppm        (low, <100)
EC (Salinity):           0.4 dS/m      (normal)
Organic Matter:          1.6%          (low, <2%)
```

### Biological Properties

```
Microbial Biomass:       180 mg C/kg   (low, <200 indicates stress)
Soil Respiration:        28 mg CO₂/kg/day  (low, <50)
```

**Expected Dashboard Output:**

- Health Score: **58-62/100** 🔴
- Indicators: pH = RED ❌, P = RED ❌, OM = YELLOW ⚠️
- Alerts:
  - "🚨 CRITICAL: Soil pH is 4.9 (very acidic). Liming required."
  - "⚠️ WARNING: Phosphorus deficiency (6 ppm)"
- Recommendations (prioritized):
  1. "Apply 2.5 t/ha agricultural limestone (CaCO₃) to raise pH to 6.0"
  2. "Wait 4 weeks after liming"
  3. "Apply 40 kg/ha SSP (Single Super Phosphate) for P"
  4. "Add 5 t/ha compost to boost OM and soil biology"

**Test Instructions:**

1. Enter acidic soil values
2. Verify multiple RED alerts appear
3. Check recommendation prioritization
4. Test "Need more info" → Chat with advisor

---

## Scenario 4: Compacted Clay Soil with Waterlogging 💧

**Use Case:** Farmer with clay soil; heavy machinery caused compaction; poor drainage.

### Physical Properties

```
Soil Moisture:           62%           (CRITICAL: >60% indicates waterlogging)
Soil Temperature:        18.5°C        (cool, typical after rain)
Bulk Density:            1.68 g/cm³    (CRITICAL: >1.65 indicates compaction)
```

### Chemical Properties

```
pH:                      6.8           (neutral, ok)
Nitrogen (N):            18 ppm        (adequate for now)
Phosphorus (P):          12 mg/kg      (adequate)
Potassium (K):           110 ppm       (adequate)
EC (Salinity):           0.7 dS/m      (normal)
Organic Matter:          2.0%          (adequate)
```

### Biological Properties

```
Microbial Biomass:       150 mg C/kg   (low due to waterlogging)
Soil Respiration:        22 mg CO₂/kg/day  (very low, indicates anaerobic)
```

**Expected Dashboard Output:**

- Health Score: **52-56/100** 🔴
- Indicators:
  - Moisture = RED ❌ (waterlogging)
  - Bulk Density = RED ❌ (compaction)
  - Respiration = YELLOW ⚠️ (anaerobic stress)
- Alerts:
  - "🚨 CRITICAL: Soil waterlogged (62%). Drainage urgently needed."
  - "🚨 CRITICAL: Bulk density 1.68 g/cm³ indicates severe compaction."
- Recommendations:
  1. "Install drainage system (French drains or tile drains)"
  2. "Deep tillage to 40 cm depth to break compaction"
  3. "Add 3 t/ha compost + mulch to improve structure"
  4. "Avoid machinery on field when wet"

**Test Instructions:**

1. Enter waterlogged + compacted soil values
2. Verify Health Score is RED (50-60 range)
3. Check multiple CRITICAL alerts
4. Test escalation: "Report a problem" → should flag for soil scientist review

---

## Scenario 5: Saline/High EC Soil 🧂

**Use Case:** Farmer in irrigated area with salt accumulation.

### Physical Properties

```
Soil Moisture:           55%           (adequate)
Soil Temperature:        26.2°C        (hot)
Bulk Density:            1.45 g/cm³    (acceptable)
```

### Chemical Properties

```
pH:                      7.9           (alkaline, typical for saline)
Nitrogen (N):            20 ppm        (adequate but may be locked)
Phosphorus (P):          10 mg/kg      (adequate)
Potassium (K):           95 ppm        (low, suppressed by Na)
EC (Salinity):           4.2 dS/m      (CRITICAL: >2.0 is saline)
Organic Matter:          2.2%          (adequate)
```

### Biological Properties

```
Microbial Biomass:       220 mg C/kg   (reduced by salt stress)
Soil Respiration:        35 mg CO₂/kg/day  (reduced activity)
```

**Expected Dashboard Output:**

- Health Score: **45-50/100** 🔴
- Indicators: EC = RED ❌, K = YELLOW ⚠️
- Alerts:
  - "🚨 CRITICAL: Soil is saline (EC 4.2 dS/m). Most crops will suffer."
  - "⚠️ WARNING: Potassium suppression due to sodium competition."
- Recommendations:
  1. "Use low-salt irrigation water (EC < 1.0 dS/m)"
  2. "Apply 25% leaching fraction (extra irrigation to flush salt)"
  3. "Apply 5 t/ha gypsum (CaSO₄) to displace sodium"
  4. "Install subsurface drainage for salt removal"
  5. "Plant salt-tolerant crops until EC < 2.0"

**Test Instructions:**

1. Enter high EC values
2. Verify K shows as suppressed
3. Check recommendations mention irrigation & gypsum
4. Note that this is long-term recovery (3-5 years estimated)

---

## Scenario 6: Depleted Soil (Low OM, Low Biology) 🍂

**Use Case:** Farmer with years of continuous cropping, no crop rotation or amendments.

### Physical Properties

```
Soil Moisture:           42%           (adequate)
Soil Temperature:        25.1°C        (normal)
Bulk Density:            1.52 g/cm³    (acceptable but trending high)
```

### Chemical Properties

```
pH:                      6.1           (slightly acidic)
Nitrogen (N):            11 ppm        (low, trending down)
Phosphorus (P):          7 mg/kg       (low, trending down)
Potassium (K):           65 ppm        (low)
EC (Salinity):           0.45 dS/m     (normal)
Organic Matter:          1.2%          (CRITICAL: <1.5% = depleted)
```

### Biological Properties

```
Microbial Biomass:       95 mg C/kg    (LOW: <100 = degraded)
Soil Respiration:        15 mg CO₂/kg/day  (VERY LOW: <25 = dead soil)
```

**Expected Dashboard Output:**

- Health Score: **48-52/100** 🔴
- Indicators:
  - Organic Matter = RED ❌ (1.2%)
  - Microbial Biomass = RED ❌ (95)
  - Respiration = RED ❌ (15)
  - N = YELLOW ⚠️ (11)
- Alerts:
  - "🚨 CRITICAL: Soil is biologically dead. No microbial activity detected."
  - "🚨 CRITICAL: Organic matter critically low (1.2%). Soil structure failing."
  - "⚠️ WARNING: Nitrogen trending downward. Crop residues not recycling."
- Recommendations (long-term):
  1. "Immediately: Add 8 t/ha high-quality compost + biochar"
  2. "Inoculate with microbial consortium (2 L/ha)"
  3. "Switch to minimum tillage / no-till practices"
  4. "Implement crop rotation with legumes (N fixation)"
  5. "Plant off-season cover crops (Sesbania, Mucuna)"
  6. "Timeline: 12-18 months for recovery; 2-3 years to optimal"

**Test Instructions:**

1. Enter depleted soil values
2. Verify multiple biological indicators are RED
3. Check health score is in red zone (<55)
4. Verify recommendations emphasize long-term recovery
5. Test: "Record an observation" → mention "planning to add compost"

---

## Scenario 7: Recently Amended Soil (Over-amended) 🌳

**Use Case:** Farmer who just added 15 t/ha manure; biological activity spiking.

### Physical Properties

```
Soil Moisture:           50%           (adequate)
Soil Temperature:        29.0°C        (HOT: from microbial metabolism)
Bulk Density:            1.40 g/cm³    (good)
```

### Chemical Properties

```
pH:                      6.9           (good)
Nitrogen (N):            45 ppm        (VERY HIGH: risk of leaching)
Phosphorus (P):          28 mg/kg      (VERY HIGH)
Potassium (K):           210 ppm       (VERY HIGH)
EC (Salinity):           1.8 dS/m      (elevated from decomposition)
Organic Matter:          6.5%          (VERY HIGH: typical after 15 t/ha manure)
```

### Biological Properties

```
Microbial Biomass:       580 mg C/kg   (VERY HIGH: decomposition surge)
Soil Respiration:        92 mg CO₂/kg/day  (VERY HIGH: active decomposition)
```

**Expected Dashboard Output:**

- Health Score: **72-75/100** ⚠️ (paradoxically lower despite high values)
- Indicators:
  - N = ORANGE ⚠️ (45 is excessive, risk of leaching)
  - Temp = ORANGE ⚠️ (29°C from microbial heat)
  - Respiration = ORANGE ⚠️ (92 is over-active, may be anaerobic risk)
- Alerts:
  - "⚠️ WARNING: Nitrogen levels very high (45 ppm). Risk of leaching loss."
  - "⚠️ WARNING: Soil temperature elevated (29°C) from decomposition."
- Recommendations:
  1. "Monitor soil N weekly for next 4 weeks (track leaching)"
  2. "Add P & K to balance nutrients (40 kg/ha SSP + 50 kg/ha MOP)"
  3. "Apply heavy mulch (5 cm) to cool soil, reduce metabolism"
  4. "Ensure drainage to prevent anaerobic conditions"
  5. "For future: stagger manure (4 t/ha every 8 weeks, not 15 at once)"

**Test Instructions:**

1. Enter over-amended soil values
2. Verify high N/P/K don't show as "good" but as risky
3. Check recommendations emphasize balancing & monitoring
4. Test chat: "Is my soil too rich?" → should explain N leaching risk

---

## Scenario 8: Mixed Constraints (Realistic Complexity) 🎯

**Use Case:** Real farmer scenario with multiple issues; tests diagnostic confidence.

### Physical Properties

```
Soil Moisture:           35%           (YELLOW: getting dry)
Soil Temperature:        21.5°C        (cool after rain)
Bulk Density:            1.58 g/cm³    (YELLOW: mild compaction)
```

### Chemical Properties

```
pH:                      5.3           (ORANGE: acidic)
Nitrogen (N):            14 ppm        (YELLOW: borderline)
Phosphorus (P):          9 mg/kg       (YELLOW: borderline)
Potassium (K):           92 ppm        (YELLOW: low)
EC (Salinity):           0.6 dS/m      (normal)
Organic Matter:          2.0%          (YELLOW: could be higher)
```

### Biological Properties

```
Microbial Biomass:       260 mg C/kg   (adequate but below optimal)
Soil Respiration:        38 mg CO₂/kg/day  (adequate but below optimal)
```

**Expected Dashboard Output:**

- Health Score: **66-70/100** ⚠️
- Multiple YELLOW indicators (no clear dominant problem)
- Alerts:
  - "⚠️ WARNING: pH is 5.3 (mildly acidic). Monitor for crop stress."
  - "⚠️ WARNING: Nitrogen borderline (14 ppm). May need supplementation."
  - "⚠️ WARNING: Soil moisture dropping (35%). Consider irrigation."
- Recommendations: Graduated/conditional
  1. "If crop shows stress: Apply lime (1.5 t/ha) to raise pH to 6.0"
  2. "If moisture stays <30%: Plan irrigation schedule"
  3. "Add 3 t/ha compost to build OM & improve water holding"
  4. "Test N in 2 weeks; if still 14→15 ppm, apply 30 kg/ha CAN"

**Test Instructions:**

1. Enter mixed constraints
2. Verify confidence score is moderate (65-70%, not 90%+)
3. Check recommendations are conditional ("if... then...")
4. Test escalation logic: if confidence <70%, should suggest soil scientist review
5. Chat test: "Should I worry about my soil?" → should present all issues in priority order

---

## Scenario 9: Optimal Soil (Baseline for Comparison) 🌟

**Use Case:** Reference scenario for teaching farmers what "ideal" looks like.

### Physical Properties

```
Soil Moisture:           52%           (perfect: mid-field capacity)
Soil Temperature:        24.0°C        (perfect: crop growth zone)
Bulk Density:            1.32 g/cm³    (excellent: well-structured)
```

### Chemical Properties

```
pH:                      6.5           (perfect: neutral)
Nitrogen (N):            28 ppm        (excellent: abundant)
Phosphorus (P):          18 mg/kg      (excellent)
Potassium (K):           160 ppm       (excellent)
EC (Salinity):           0.55 dS/m     (perfect: fresh, non-saline)
Organic Matter:          3.2%          (excellent: high)
```

### Biological Properties

```
Microbial Biomass:       420 mg C/kg   (excellent: diverse, active)
Soil Respiration:        68 mg CO₂/kg/day  (excellent: healthy metabolism)
```

**Expected Dashboard Output:**

- Health Score: **92-95/100** ✅
- ALL indicators: GREEN
- No alerts
- Recommendation: "Excellent soil health! Recommendations: Continue current practices, consider certified organic transition, experiment with rotational grazing or cover crops for further improvement"

**Test Instructions:**

1. Enter optimal values
2. Verify Health Score is in 90-95 range
3. All greens, no alerts
4. Message should be affirming: "You're doing great!"

---

## Quick Reference Table

| Scenario        | N (ppm) | pH  | EC (dS/m) | BD (g/cm³) | Moisture (%) | Health Score | Color     |
| --------------- | ------- | --- | --------- | ---------- | ------------ | ------------ | --------- |
| 1. Healthy      | 22      | 6.5 | 0.6       | 1.35       | 48           | 85-90        | 🟢 GREEN  |
| 2. N-Deficient  | 8.7     | 6.2 | 0.5       | 1.38       | 45           | 75-78        | 🔴 RED    |
| 3. Acidic       | 12      | 4.9 | 0.4       | 1.42       | 52           | 58-62        | 🔴 RED    |
| 4. Compacted    | 18      | 6.8 | 0.7       | 1.68       | 62           | 52-56        | 🔴 RED    |
| 5. Saline       | 20      | 7.9 | 4.2       | 1.45       | 55           | 45-50        | 🔴 RED    |
| 6. Depleted     | 11      | 6.1 | 0.45      | 1.52       | 42           | 48-52        | 🔴 RED    |
| 7. Over-amended | 45      | 6.9 | 1.8       | 1.40       | 50           | 72-75        | 🟡 YELLOW |
| 8. Mixed Issues | 14      | 5.3 | 0.6       | 1.58       | 35           | 66-70        | 🟡 YELLOW |
| 9. Optimal      | 28      | 6.5 | 0.55      | 1.32       | 52           | 92-95        | 🟢 GREEN  |

---

## How to Enter Test Data

### Option A: Manual Entry via Dashboard

1. Open farmer dashboard: `streamlit run ui/app_farmer.py --server.port 8502`
2. Login (create test account)
3. Select scenario above
4. Click "📝 Enter Data Manually"
5. Enter values one-by-one via onboarding flow
6. Observe dashboard updates with health score & alerts

### Option B: Direct API Post (faster for testing)

```bash
# Get your auth token first
curl -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"farmer@test.com","password":"password123"}'

# Then post readings
curl -X POST http://localhost:8080/api/v1/readings/manual \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "parcel_id": "soil_parcel_001",
    "field": "nitrogen_ppm",
    "value": 8.7,
    "notes": "Scenario 2: N-deficient"
  }'
```

### Option C: Bulk Load via Python Script

```python
import requests

BASE_URL = "http://localhost:8080/api/v1"
TOKEN = "your_token_here"
PARCEL_ID = "soil_parcel_001"

SCENARIO_2 = {
    "soil_moisture_pct": 45,
    "soil_temp_c": 23.2,
    "bulk_density_g_cm3": 1.38,
    "soil_ph": 6.2,
    "nitrogen_ppm": 8.7,
    "phosphorus_ppm": 11,
    "potassium_ppm": 120,
    "ec_ds_m": 0.5,
    "organic_matter_pct": 2.1,
    "microbial_biomass_mg_c_kg": 280,
    "soil_respiration_mg_co2_kg_day": 45,
}

headers = {"Authorization": f"Bearer {TOKEN}"}

for field, value in SCENARIO_2.items():
    r = requests.post(
        f"{BASE_URL}/readings/manual",
        headers=headers,
        json={"parcel_id": PARCEL_ID, "field": field, "value": value}
    )
    print(f"{field}: {r.status_code}")
```

---

## Testing Checklist

- [ ] Scenario 1 (Healthy): Health Score 85-90, all green, no alerts
- [ ] Scenario 2 (N-deficient): N shows RED, recommendation for CAN appears
- [ ] Scenario 3 (Acidic): pH shows RED, P shows RED, lime recommendation appears
- [ ] Scenario 4 (Waterlogged): Moisture RED, BD RED, multiple alerts, escalation ready
- [ ] Scenario 5 (Saline): EC RED, K suppressed, gypsum recommendation
- [ ] Scenario 6 (Depleted): OM RED, Respiration RED, long-term recovery message
- [ ] Scenario 7 (Over-amended): N/P/K high but flagged as risky, not good
- [ ] Scenario 8 (Mixed): Health 66-70, multiple YELLOW, conditional recommendations
- [ ] Scenario 9 (Optimal): Health 92-95, all green, affirming message
- [ ] Chat works: Ask "What should I do?" → gets prioritized recommendations
- [ ] Feedback works: "I applied this" → marks as applied
- [ ] Escalation works: Low confidence → suggests soil scientist review
- [ ] History tab shows recommendations with timestamps
- [ ] Mobile view: All indicators legible on small screens

---

**Last Updated:** May 9, 2026  
**For:** ASDT Farmer Dashboard Testing  
**Status:** Ready for QA
