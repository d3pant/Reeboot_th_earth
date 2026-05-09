# Reeboot the Earth — Wildfire Agricultural Advisory System

## Project Overview
Farm wildfire threat monitoring system for a San Diego County farm (lat: 33.2232, lon: -117.1611). Two components:
1. **Forecaster** — evaluates fire threat, writes `forecaster/output/status.json`
2. **Backend** — FastAPI + Leaflet map showing live NASA FIRMS fires for California with spread ellipses

## Running the Server
```bash
uvicorn backend.main:app --port 8000 --app-dir /Users/devangpant/Reeboot_th_earth
```

## Key Files
- `backend/main.py` — FastAPI server; all API endpoints
- `backend/static/index.html` — Leaflet dark map frontend
- `backend/.env` — NASA_FIRMS_API_KEY (real key, never commit)
- `forecaster/forecaster.py` — Gate condition logic, `evaluate_gate_condition()`
- `forecaster/data_sources/open_meteo.py` — Weather fetcher (no API key needed)
- `forecaster/models/spread_model.py` — Rothermel + Anderson ellipse fire spread model
- `forecaster/config/farm_config.json` — Farm thresholds and location
- `forecaster/output/status.json` — Latest forecaster output (gitignored)

## APIs Used
- **NASA FIRMS** — Active fire detections (VIIRS Suomi NPP NRT, 375m, 2-day range). Key in `backend/.env`
- **Open-Meteo** — Wind, humidity, temp, soil moisture. Free, no key required.

## Architecture Notes
- `forecaster/` is added to `sys.path` in `backend/main.py`, so imports are:
  - `from data_sources.open_meteo import fetch_weather` ✓
  - `from models.spread_model import ...` ✓
  - `from forecaster import evaluate_gate_condition` ✓ (imports `forecaster.py` module directly)
  - `from forecaster.forecaster import ...` ✗ (fails — forecaster/ is not a package namespace)
- `load_dotenv` points to `backend/.env`, not `forecaster/.env`
- FIRMS URL uses `/2` day range (not `/1`) — single day often returns no data

## Threat Levels
GREEN → WATCH → WARNING → CRITICAL → EMERGENCY

## Status JSON Structure
Never change existing fields — only additive changes allowed. `spread_prediction` is the last field.

## Farm Config Thresholds
- Custom: fire_distance_km=100, fwi_trigger=9, vegetation_stress_sigma=-1.5
- Hard floor: fire_distance_km=75, fwi_trigger=12

## Notes
- NDVI is hardcoded to -0.5 (neutral) — no free real-time NDVI source available
- FWI from Open-Meteo is always None (not a supported variable) — computed separately
- Ellipses use Rothermel (1972) simplified + Anderson (1983) geometry
