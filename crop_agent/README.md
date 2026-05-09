# Crop Agent — Wildfire Agricultural Advisory System

Part of the **Reboot the Earth** multi-agent wildfire response system. The crop agent receives a wildfire forecast and produces four actionable decisions for every farm field: what to harvest or save, how to reduce fire fuel, what the economic loss is, and how to irrigate.

---

## How It Works

```
forecast_status.json  (from Forecaster Agent)
        ↓
  crop_agent.py
        ↓
  ┌─────────────────────────────────┐
  │  Step 0   Fetch live USDA prices + yields (NASS API)       │
  │  Step 0b  Fetch soil moisture per field (NASA POWER)       │
  │  Step 0b  Fetch evapotranspiration per field (OpenET)      │
  │  Step 0c  Fetch fuel flammability per field (LANDFIRE)     │
  └─────────────────────────────────┘
        ↓
  Groq LLM (llama-3.3-70b-versatile)
        ↓
  crop_agent_output_<timestamp>.json
  output_to_erpc.json  (economic slice → ERPC Agent)
```

---

## Output JSON Structure

```json
{
  "field_decisions":    [...],   // harvest / transplant / abandon per field
  "fire_reduction":     [...],   // priority-ranked fire fuel strategy per field
  "economic_impact":    {...},   // USDA-priced loss estimate
  "hydration_strategy": [...]    // irrigation technique per field
}
```

### field_decisions
For each field: calculates fire arrival time, crop maturity %, and decides:
- **HARVEST NOW** — crop ≥ 70% mature, can clear before fire
- **PARTIAL HARVEST** — crop 40–69% mature, salvage what's possible
- **TRANSPLANT** — young/transplantable crop, move it before fire arrives
- **ABANDON** — immature and not transplantable, field enters fire reduction

### fire_reduction
Only for fields that could not be cleared. Ranks fields by:
- Flammability score (from LANDFIRE live fuel model, 1–5)
- Fuel load = flammability × size in acres
- Wind alignment factor (1.0 / 1.2 / 1.5 based on fire bearing vs wind)

Outputs action (CLEAR / FIREBREAK / MONITOR) and a full uprooting strategy per field using agronomic data from `crop_properties.json`.

### economic_impact
For ABANDON and PARTIAL HARVEST fields only. Uses live USDA NASS prices × live NASS yields × field size to calculate estimated and confidence-adjusted loss. Saved to `output_to_erpc.json` for the ERPC agent.

### hydration_strategy
Farmer-executable irrigation only (no aerial drops, no government resources). Computes fire intensity score from weather data, adjusts based on live soil moisture and ET, and assigns:
- **DRIP IRRIGATION** — low risk, conserve water
- **SPRINKLER IRRIGATION** — medium risk, wet canopy
- **WET FIREBREAK** — high risk, flood field perimeter
- **ABANDON** — unsafe to operate in field

---

## Live Data Sources

| Data | Source | Auth |
|------|--------|------|
| Crop prices | [USDA NASS QuickStats API](https://quickstats.nass.usda.gov/api) | API key (free) |
| Crop yields per acre | [USDA NASS QuickStats API](https://quickstats.nass.usda.gov/api) | Same key |
| Soil moisture (GWETROOT) | [NASA POWER API](https://power.larc.nasa.gov/) | None — open |
| Evapotranspiration (ET) | [OpenET API](https://openetdata.org/access) | API key (request form) |
| Fuel flammability (FBFM40) | [LANDFIRE LF2022 via USGS](https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer) | None — open |
| LLM reasoning | [Groq API](https://console.groq.com) — llama-3.3-70b-versatile | API key (free tier) |

### Fallback chain
If a live source is unavailable the agent falls back gracefully:
- NASS unavailable → `FALLBACK_PRICES_2025` (hardcoded 2025 USDA averages)
- NASS yield unavailable → `CROP_YIELDS` (hardcoded averages per crop)
- LANDFIRE returns non-burnable (NB) for farmland → crop-type flammability estimate
- OpenET quota exceeded → `et_mm_per_day: null`, LLM notes data unavailable

---

## Static Data Files

| File | What it contains | Source |
|------|-----------------|--------|
| `crop_properties.json` | Root depth, transplantability, equipment needed, labor hours, survival times for 11 crops | Provided via input form (agronomic standards) |
| `farm_resources.json` | Farm equipment, labor count, truck capacity, irrigation system | Provided via input form (farmer-supplied) |
| `farm_fields.json` | Field locations, crop type, size, planting date | Provided via input form |

---

## Open Source Libraries

| Library | Version | Purpose |
|---------|---------|---------|
| [groq](https://github.com/groq/groq-python) | ≥ 0.9.0 | Groq API client for LLM calls |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | ≥ 1.0.0 | Load `.env` credentials |
| [requests](https://github.com/psf/requests) | ≥ 2.31.0 | HTTP calls to all data APIs |

---

## Setup

```bash
cd crop_agent
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

### Required keys in `.env`

```
GROQ_API_KEY=         # console.groq.com — free tier, 100k tokens/day
USDA_NASS_API_KEY=    # quickstats.nass.usda.gov/api — free, instant
NASA_EARTHDATA_USERNAME=  # urs.earthdata.nasa.gov — free account
NASA_EARTHDATA_PASSWORD=
OPENET_API_KEY=       # openetdata.org/access — free, request form
```

---

## Running

```bash
# Default (uses forecast_status.json + farm_fields.json in current dir)
python3 crop_agent.py

# Custom input files
python3 crop_agent.py path/to/forecast_status.json path/to/farm_fields.json
```

### Test scenarios

```bash
# All 4 decision paths (HARVEST / PARTIAL / TRANSPLANT / ABANDON)
python3 crop_agent.py test_scenarios/scenario_1_all_paths/forecast_status.json \
                      test_scenarios/scenario_1_all_paths/farm_fields.json

# Extreme fast fire — most fields ABANDON, hydration unsafe
python3 crop_agent.py test_scenarios/scenario_2_imminent_fire/forecast_status.json \
                      test_scenarios/scenario_2_imminent_fire/farm_fields.json

# Gate check — no threat, agent exits without calling LLM
python3 crop_agent.py test_scenarios/scenario_3_no_threat/forecast_status.json \
                      test_scenarios/scenario_3_no_threat/farm_fields.json
```

---

## System Context

The crop agent is one of four agents in the Reboot the Earth wildfire response pipeline:

```
Forecaster Agent  →  forecast_status.json
                          ↓
              ┌───────────┴───────────┐
         Crop Agent            Livestock Agent
              └───────────┬───────────┘
                          ↓
                      ERPC Agent
                          ↓
                     Farmer Interface
```

The forecaster monitors NASA FIRMS satellite fire data, calculates FWI, and triggers downstream agents when a fire threat is detected within threshold distance of the farm.
