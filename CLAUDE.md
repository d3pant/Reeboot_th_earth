# Reeboot the Earth — Wildfire Agricultural Advisory System

## Project Overview
Farm wildfire threat monitoring system for a San Diego County farm (lat: 33.2232, lon: -117.1611). Three agents:
1. **Forecaster** — evaluates fire threat, writes `forecaster/output/status.json`
2. **Livestock Agent** — reads status.json + farm_profile.json, outputs evacuation plan
3. **Crop Agent** — reads status.json + farm_fields.json, calls Groq LLM for field decisions

Backend is FastAPI + Leaflet dark map showing live NASA FIRMS fires for California.

## Running the Server
```bash
uvicorn backend.main:app --port 8000 --app-dir /Users/devangpant/Reeboot_th_earth
```
To hard-kill and restart: `lsof -ti:8000 | xargs kill -9 && uvicorn ...`

## Key Files
- `backend/main.py` — FastAPI server; all API endpoints
- `backend/static/index.html` — Leaflet dark map frontend
- `backend/static/setup.html` — 3-step farm onboarding form (shown on first visit)
- `.env` — All API keys at project root (never commit)
- `forecaster/forecaster.py` — Gate condition logic, `evaluate_gate_condition()`
- `forecaster/data_sources/open_meteo.py` — Weather fetcher (no API key needed)
- `forecaster/data_sources/fwi.py` — Canadian FWI implementation (Van Wagner 1987)
- `forecaster/models/spread_model.py` — Rothermel + Anderson ellipse fire spread model
- `forecaster/config/farm_config.json` — Farm thresholds and location (written by setup)
- `forecaster/output/status.json` — Latest forecaster output (gitignored)
- `Livestock/farm_profile.json` — Pen inventory written by setup form
- `crop_agent/farm_fields.json` — Field inventory written by setup form
- `crop_agent/crop_agent.py` — Groq LLM (llama-3.3-70b-versatile) crop decisions
- `.farm_setup_done` — Sentinel file; if absent, `/` redirects to setup.html

## APIs Used
- **NASA FIRMS** — Active fire detections (VIIRS Suomi NPP NRT, 375m, 2-day range). Key in `.env`
- **Open-Meteo** — Wind, humidity, temp, soil moisture, precipitation. Free, no key required.
- **Groq** — LLM inference for crop agent. Key: `GROQ_API_KEY` in `.env`

## Architecture Notes
- `forecaster/` is added to `sys.path` in `backend/main.py`, so imports are:
  - `from data_sources.open_meteo import fetch_weather` ✓
  - `from models.spread_model import ...` ✓
  - `from forecaster import evaluate_gate_condition` ✓ (imports `forecaster.py` module directly)
  - `from forecaster.forecaster import ...` ✗ (fails — forecaster/ is not a package namespace)
- `load_dotenv` points to root `.env` in all agents (not `backend/.env`)
- FIRMS URL uses `/2` day range (not `/1`) — single day often returns no data
- FIRMS filters to nominal/high confidence only (`confidence_raw in ("n", "h")`)
- Agent data flow: Forecaster → `_sync_forecaster_to_livestock()` copies to `Livestock/` → Livestock agent runs → Crop agent gets STATUS_JSON as CLI arg
- Subprocesses inherit env via `os.environ.copy()` so all agents get root `.env` keys

## Farm Setup Flow
1. User visits `/` → redirected to `/static/setup.html` if `.farm_setup_done` missing
2. 3-step form: farm name + Leaflet map pin + acres → pens → fields
3. `POST /api/setup` writes `farm_config.json`, `farm_profile.json`, `farm_fields.json`, touches `.farm_setup_done`
4. Pen positions auto-distributed within farm radius; evac sites are geographically outside

## Farm Radius Circle
- Circle radius = `sqrt(total_acres × 4047 / π)` meters (actual farm footprint)
- Pen markers placed within ~55% of that radius
- Evac sites rendered outside the circle by geography

## Threat Levels
GREEN → WATCH → WARNING → CRITICAL → EMERGENCY

## Status JSON Structure
Never change existing fields — only additive changes allowed. `spread_prediction` is the last field.
`/api/status` also injects `total_acres` from `Livestock/farm_profile.json`.

## Farm Config Thresholds
- Custom: fire_distance_km=100, fwi_trigger=9, vegetation_stress_sigma=-1.5
- Hard floor: fire_distance_km=75, fwi_trigger=12

## Notes
- NDVI is hardcoded to -0.5 (neutral) — no free real-time NDVI source available
- FWI computed locally via Van Wagner (1987) from Open-Meteo weather variables
- Ellipses use Rothermel (1972) simplified + Anderson (1983) geometry
- Fire spreads **downwind**: bearing = `(wind_direction_deg + 180) % 360`
- Groq client has 60s timeout; prompt JSON is compacted (no indent) to reduce tokens
- Crop agent `max_tokens=2048` — JSON output doesn't need more
