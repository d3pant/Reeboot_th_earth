# Reeboot the Earth — Wildfire Agricultural Advisory System

An AI-powered wildfire monitoring and agricultural advisory system built for **California farmers**. Targets Southern California agricultural operations — San Diego, Riverside, San Bernardino, and surrounding counties. Detects fire threats in real time and activates downstream agents (Crop, Livestock, ERPC) when risk thresholds are crossed.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Agents](#agents)
3. [Running the System](#running-the-system)
4. [External APIs & Data Sources](#external-apis--data-sources)
5. [Aid & Recovery Programs](#aid--recovery-programs)
6. [Science & Threshold References](#science--threshold-references)
7. [License](#license)

---

## System Overview

Four agents. One linear pipeline with a parallel middle stage. Dormant until a real threat exists, then activates fully.

```
┌─────────────────┐
│  FORECASTING    │  ← Always running
│     AGENT       │
└────────┬────────┘
         │ Gate condition met → wakes both simultaneously
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌──────────┐
│ CROP  │ │LIVESTOCK │  ← Parallel, communicate with each other
│ AGENT │ │  AGENT   │
└───┬───┘ └────┬─────┘
    └─────┬─────┘
          ▼
┌──────────────────────┐
│        ERPC          │  ← Econ + Policy coordinator
└──────────┬───────────┘
           ▼
      FARMER DASHBOARD
```

Threat levels used system-wide: `GREEN → WATCH → WARNING → CRITICAL → EMERGENCY`

### File Structure

| Path | Description |
|------|-------------|
| `forecaster/forecaster.py` | Forecasting agent — threat assessment, gate condition, wake-up packet |
| `forecaster/agents/econ_agent.py` | ERPC econ module — financial exposure, ROI action ranking |
| `forecaster/agents/policy_agent.py` | ERPC policy module — aid/grant eligibility engine |
| `forecaster/agents/insurance_agent.py` | ERPC insurance module — fills official USDA CCC-576 Notice of Loss form |
| `forecaster/models/spread_model.py` | Rothermel fire spread model + Anderson ellipse |
| `forecaster/data_sources/` | Data fetchers: NASA FIRMS, NDVI, Open-Meteo, NOAA, SDG&E |
| `forecaster/predictors/` | WIFIRE + Pyrecast spread prediction integrations |
| `forecaster/config/farm_config.json` | Farm profile: location, thresholds, zones, crops, animals |
| `forecaster/output/` | Runtime outputs: `status.json`, `wake_up_packet.json`, `econ_report.json`, `policy_report.json`, `ccc_576_filled.pdf` |
| `forecaster/forms/` | Bundled government form templates: `ccc_576.pdf` (official USDA Notice of Loss) |
| `backend/main.py` | FastAPI server — serves live NASA FIRMS fire map |
| `backend/static/index.html` | Leaflet.js map visualization |

---

## Agents

### Forecasting Agent

`forecaster/forecaster.py`

**Stage 1 (passive):** Checks FWI, nearest fire distance, NDVI anomaly on a schedule. No downstream agents active.

**Gate condition:** Threat crosses WARNING or above → activates Stage 2.

**Stage 2 (active):** Full pipeline — fire spread prediction, per-zone time-to-impact, writes `wake_up_packet.json` and wakes Crop + Livestock agents simultaneously.

Update intervals by threat level: GREEN=720min, WATCH=120min, WARNING=30min, CRITICAL=15min, EMERGENCY=5min.

---

### Econ Agent (ERPC)

`forecaster/agents/econ_agent.py` → `forecaster/output/econ_report.json`

Runs during Stage 2. Each cycle computes total financial exposure and produces a prioritized ROI action queue for the farmer dashboard.

**Financial loss categories:**

| Category | What It Covers | Source |
|----------|---------------|--------|
| Crop loss (confirmed) | ABANDON fields — loss locked in | Crop Agent `task2.confidence_adjusted_loss_usd` |
| Crop loss (recoverable) | HARVEST NOW / PARTIAL HARVEST fields — preventable with action | Crop Agent `task2` + `task4.maturity_pct` |
| Livestock at risk | `total_head × value/head × (1 − evacuated_pct)` | Livestock Agent (hardcoded stub until agent exists) |
| Opportunity cost | 1 lost season × price/acre × acres for ABANDON; partial season for PARTIAL HARVEST | Crop Agent `task2.price_per_acre_usd` + `task4.maturity_pct` |

Loss figure used throughout: `confidence_adjusted_loss_usd` — conservative floor, not best-case.

Not yet computable: soil rehabilitation, replanting cost, market timing losses, labor disruption.

**ROI formula:**
```
ROI = confidence_adjusted_loss_avoided / estimated_action_cost
```

**Action types:**

| Action | Loss Avoided | Cost Basis | Time Constraint |
|--------|-------------|-----------|-----------------|
| HARVEST NOW | Full `confidence_adjusted_loss_usd` | Labor rate × harvest hours | Before `fire_arrival_hours` |
| PARTIAL HARVEST | `confidence_adjusted_loss_usd × maturity_pct` | Labor rate × hours × maturity fraction | Before `fire_arrival_hours` |
| TRANSPLANT | Seedling replacement value | Labor rate × `labor_hours_needed` | Before `time_window` |
| WET FIREBREAK | `confidence_adjusted_loss_usd` of protected field | Firebreak cost × field acres | IMMEDIATE or SCHEDULED |
| EVACUATE LIVESTOCK | Livestock at-risk value | Transport cost × head count | Zone time-to-impact |

Feasibility gates (hard — action dropped if failed): `feasible_with_farm_resources = False`, insufficient time window, or field decision is ABANDON.

**Hardcoded cost assumptions** (all logged in output JSON under `cost_assumptions_used`):

| Constant | Value | Unit | Replace With |
|----------|-------|------|--------------|
| Harvest labor rate | $25 | $/hr | USDA regional farm wage data |
| Harvest time | 4 | hrs/acre | Per-crop estimate from Crop Agent |
| Firebreak cost | $150 | $/acre | CAL FIRE cost estimates |
| Livestock transport | $35 | $/head | Regional hauling rates |
| Livestock value | $1,500 | $/head | Livestock Agent market price |
| Livestock head count | 750 | head | Livestock Agent inventory |
| Transplant seedling value | $800 | $/acre | Nursery price index |
| Opportunity cost horizon | 1 | season | Agronomist input |

---

### Insurance Agent (ERPC)

`forecaster/agents/insurance_agent.py` → `forecaster/output/ccc_576_filled.pdf`

Runs post-event. Reads `econ_report.json` and `status.json` and fills the official USDA **CCC-576 (Notice of Loss)** PDF form using `pypdf`. The CCC-576 is the primary form for ELAP, LFP, LIP, and NAP claims — it must be filed within 30 days of the loss event at the local FSA office.

The agent pre-populates all fields it has data for and leaves the rest blank so the farmer can complete them on-site. The filled form is print-ready.

**What gets pre-filled:**

| CCC-576 Section | Fields Pre-filled | Fields Left Blank (farmer fills) |
|----------------|------------------|----------------------------------|
| Part A Header (Items 1–6) | FSA office address, crop year, producer name/location, state/county FIPS, disaster type "Wildfire", disaster dates, crop name, intended use | Crop variety/type, planting period |
| Part A Acreage (Items 7–8) | Farm ID, intended acres, planted acres, disaster-affected acreage (up to 3 crops) | NAP unit numbers, prevented-planted |
| Part B Production (Items 11–29) | Crop name, producer share (100%), acreage, loss description, salvage value (up to 3 crops) | Pay codes, stage, actual production records |
| Part C Inventory (Items 32–37) | Crop value before disaster, value after (ABANDON = $0), salvage estimate (up to 3 crops) | Ineligible value (FSA fills) |
| Part D Forage (Items 38–48) | — | All (needs Livestock Agent) |
| Signatures | — | In-person at FSA office |

56 of 97 mapped fields pre-filled from system data.

**Form source:** official USDA CCC-576 from https://www.farmers.gov/sites/default/files/documents/ccc-576.pdf, bundled at `forecaster/forms/ccc_576.pdf` (181 AcroForm fields, 2 pages).

---

### Policy Agent (ERPC)

`forecaster/agents/policy_agent.py` → `forecaster/output/policy_report.json`

Runs post-event (after all-clear). Evaluates farm eligibility for 21 wildfire recovery programs across USDA, FEMA, SBA, and CA state agencies. Outputs a ranked list (confirmed → likely → check_required → ineligible) with deadlines, required documents, and direct links.

Key logic: FEMA Disaster Declarations API is queried first — that single boolean gates whether FEMA IA, FEMA HMGP, FSA Emergency Loans, and SBA EIDL are `confirmed` or `check_required`. All other programs are evaluated against hardcoded farm profile constants.

---

## Running the System

```bash
# Forecasting agent — mock scenario
cd forecaster
python forecaster.py --scenario fire_threat     # writes output/status.json + wake_up_packet.json
python forecaster.py --scenario no_fire

# Forecasting agent — real APIs (requires .env)
python forecaster.py --use-real-data

# Econ agent
python agents/econ_agent.py                     # requires crop agent output or uses mock
python agents/econ_agent.py --dry-run           # uses bundled mock crop data, no APIs

# Policy agent
python agents/policy_agent.py                   # live FEMA + Grants.gov APIs
python agents/policy_agent.py --dry-run         # no network calls

# Insurance agent — fills USDA CCC-576 Notice of Loss
python agents/insurance_agent.py               # reads output/*.json, writes output/ccc_576_filled.pdf
python agents/insurance_agent.py --dry-run    # uses mock data if output files missing

# Map backend
cd backend
uvicorn main:app --reload                       # requires NASA_FIRMS_API_KEY in .env
```

API keys go in `forecaster/.env` — copy from `forecaster/.env.example`.

---

## External APIs & Data Sources

### Fire Detection & Weather

| # | Source | Data Used | Access | Key Required |
|---|--------|----------|--------|-------------|
| 1 | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) | VIIRS S-NPP active fire detections (375m, ~4x/day) | Free — register at earthdata.nasa.gov | Yes — MAP KEY |
| 2 | [Open-Meteo](https://open-meteo.com) | FWI, wind, temperature, humidity, soil moisture (hourly) | Free | No |
| 3 | [SDG&E FPI](https://www.sdge.com/wildfire-safety) | Localized FWI for San Diego County (2x/day) | Utility partner agreement | Yes — stubbed |
| 4 | [NASA AppEEARS / NDVI](https://appeears.earthdatacloud.nasa.gov) | MODIS MOD13Q1 NDVI 16-day composite at 250m | Free — NASA Earthdata account | Yes |
| 5 | [NOAA Weather API](https://www.weather.gov/documentation/services-web-api) | Hourly wind, temperature, humidity forecast | Free | No |
| 6 | [WIFIRE Firemap](https://wifire.ucsd.edu) | Real-time fire spread direction, speed, community proximity | Research affiliation required | Yes — stubbed |
| 7 | [Pyrecast](https://pyrecast.org) | 14-day ensemble fire spread (200 members, async) | WIFIRE Lab approval required | Yes — stubbed |
| 8 | [Copernicus EFFIS](https://effis.jrc.ec.europa.eu) | Fire danger forecast, global coverage (daily) | Free — EU Copernicus account | No |

### Policy & Aid

| # | Source | Data Used | Access | Key Required |
|---|--------|----------|--------|-------------|
| 9 | [OpenFEMA Disaster Declarations](https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries) | Fire declarations by county — gates program eligibility | Free | No |
| 10 | [Grants.gov Search API](https://api.grants.gov/v1/api/search2) | Live federal grant opportunities (wildfire + agriculture) | Free | No |
| 11 | [Farmers.gov Program Deadlines](https://www.farmers.gov/working-with-us/program-deadlines) | FSA enrollment windows — scraped weekly to local cache | Public HTML | No |

### Visualization

| Library | Version | License | URL |
|---------|---------|---------|-----|
| Leaflet.js | 1.9.4 | BSD 2-Clause | https://leafletjs.com |
| CartoDB Dark Matter Tiles | — | CC BY 3.0 | https://carto.com/basemaps/ |

### Academic Citations

> Giglio, L., Schroeder, W., & Justice, C. O. (2016). The collection 6 MODIS active fire detection algorithm. *Remote Sensing of Environment*, 178, 31–41. https://doi.org/10.1016/j.rse.2016.02.054

> Zippenfenig, P. (2023). Open-Meteo.com Weather API. Zenodo. https://doi.org/10.5281/zenodo.7970649

> Didan, K. (2021). *MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061*. NASA LP DAAC. https://doi.org/10.5067/MODIS/MOD13Q1.061

> Altintas, I. et al. (2015). Towards integrated cyberinfrastructure for WIFIRE. *Workshop on Big Data from Stream to Knowledge*. https://doi.org/10.1145/2834976.2834985

> Van Wagner, C. E. (1987). *Development and structure of the Canadian Forest Fire Weather Index System* (Tech. Report 35). Canadian Forestry Service.

---

## Aid & Recovery Programs

Programs evaluated by the Policy Agent for every post-event report. All links are live.

### USDA — Farm Service Agency (FSA)

| Program | What It Covers | Link |
|---------|---------------|------|
| Emergency Livestock Relief Program (ELRP) | Feed cost losses from wildfire; auto-payment if LFP on file | https://www.fsa.usda.gov/resources/disaster-recovery/emergency-livestock-relief-program-elrp |
| Emergency Assistance for Livestock (ELAP) | Grazing/feed losses, water hauling, livestock transport | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/emergency-assist-for-livestock-honey-bees-fish/index |
| Livestock Forage Disaster Program (LFP) | Forage losses; gateway to ELRP auto-payment | https://www.fsa.usda.gov/resources/disaster-recovery/livestock-forage-disaster-program-lfp |
| Livestock Indemnity Program (LIP) | Livestock deaths above normal mortality | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/livestock-indemnity/index |
| Noninsured Crop Disaster Assistance (NAP) | Crop losses for producers without federal crop insurance | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index |
| Emergency Conservation Program (ECP) | Fencing, water restoration, debris removal | https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-conservation/index |
| Emergency Forest Restoration Program (EFRP) | Non-industrial private forest restoration | https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-forest-restoration/index |
| Supplemental Disaster Relief Program (SDRP) | Crop revenue losses from 2023/2024 events | https://www.fsa.usda.gov/resources/programs/20232024-supplemental-disaster-assistance |
| FSA Emergency Farm Loans | Up to $500,000; requires federal disaster declaration | https://www.fsa.usda.gov/programs-and-services/farm-loan-programs/emergency-farm-loans/index |

### USDA — NRCS

| Program | What It Covers | Link |
|---------|---------------|------|
| EQIP — Wildfire | Conservation practices on cropland, rangeland, private forest | https://www.nrcs.usda.gov/programs-and-initiatives/eqip-environmental-quality-incentives |
| Emergency Watershed Protection (EWP) | Debris removal, bank reshaping, levee repair, reseeding | https://www.nrcs.usda.gov/programs-and-initiatives/ewp-emergency-watershed-protection-program |

### FEMA

| Program | What It Covers | Link |
|---------|---------------|------|
| Individual Assistance (IA) | Home/property repair grants; requires Presidential declaration | https://www.disasterassistance.gov |
| Fire Management Assistance Grant (FMAG) | State/tribal fire mitigation; activates downstream programs | https://www.fema.gov/assistance/public/fire-management-assistance |
| Hazard Mitigation Grant Program (HMGP) | Long-term mitigation (firebreaks); up to 12 months post-declaration | https://www.fema.gov/grants/mitigation/hazard-mitigation |

### SBA

| Program | What It Covers | Link |
|---------|---------------|------|
| Economic Injury Disaster Loans (EIDL) | Up to $2M for cash flow losses for small agricultural businesses | https://www.sba.gov/funding-programs/disaster-assistance |

### UN / International

| Program | What It Covers | Link |
|---------|---------------|------|
| FAO Global Fire Management Hub | Coordination body for international farmers; not direct US aid | https://www.fao.org/partnerships/fire-hub/en |
| Green Climate Fund (GCF) via FAO | Climate resilience funding via national government applications | https://www.greenclimate.fund/ae/fao |

### California State

| Program | Agency | What It Covers | Link |
|---------|--------|---------------|------|
| Emergency Relief Programs | CDFA | CA wildfire farm losses; requires governor's declaration | https://www.cdfa.ca.gov/grants/ |
| Forest Health Grants | CAL FIRE | Reforestation for private forest landowners | https://www.fire.ca.gov/grants |
| Office of Emergency Food and Farming Infrastructure | CDFA | Small/mid-scale farms with food system disruption | https://www.cdfa.ca.gov/oefi/ |
| Disaster Unemployment Assistance | CA EDD | Self-employed farmers who lost work from declared disaster | https://edd.ca.gov/en/unemployment/disaster/ |

---

## Science & Threshold References

### Canadian Forest Fire Weather Index (FWI) System

The FWI system is the international standard for fire weather. Six components:

| Component | Measures |
|-----------|---------|
| FFMC | Moisture of fine fuels (litter, grass) |
| DMC | Moisture of loosely compacted organic layers |
| DC | Seasonal drought effect on deep organic layers |
| ISI | Expected rate of fire spread |
| BUI | Total fuel available for combustion |
| FWI | Overall fire intensity potential (0–180) |

### Fire Spread Model

`forecaster/models/spread_model.py` implements the Rothermel (1972) rate-of-spread model with Anderson (1985) ellipse geometry, calibrated for SoCal chaparral fuel types.

### Threshold Sources

FWI and fire distance thresholds in `forecaster/config/farm_config.json` are derived from:
- SDG&E Wildfire Mitigation Plan 2023–2025
- CAL FIRE Fire Hazard Severity Zone classifications
- USDA Forest Service Fire Danger Rating System (FDRS)

---

## License

Code: MIT
Data: Subject to individual provider licenses. NASA and NOAA data are U.S. Government public domain. Open-Meteo data is CC BY 4.0. CartoDB tiles are CC BY 3.0.
