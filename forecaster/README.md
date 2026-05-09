# Wildfire Agricultural Advisory System — Forecaster Agent

Single-job runner that monitors wildfire risk for a San Diego County farm. Performs one check cycle, writes `output/status.json`, and — if the gate condition is met — triggers Stage 2 and writes `output/wake_up_packet.json`.

---

## Quick Start

### Run with mock data (no credentials needed)

```bash
# Scenario 1: no fire threat → GREEN, no wake-up packet
python forecaster.py --scenario no_fire

# Scenario 2: active fire threat → CRITICAL, full wake-up packet
python forecaster.py --scenario fire_threat
```

### Run with real API data

```bash
cp config/mock_credentials.json config/credentials.json
# Edit config/credentials.json and fill in your API keys
python forecaster.py --use-real-data
```

---

## Project Structure

```
forecaster/
├── forecaster.py              # Main orchestrator (Forecaster class + CLI)
├── mock_data.py               # Mock scenarios for testing
├── predictors/
│   ├── wifire_predictor.py    # Real-time spread (WIFIRE Firemap)
│   └── pyrecast_predictor.py  # Detailed 14-day forecast (Pyrecast API)
├── data_sources/
│   ├── sdge_fpi.py            # SDG&E Fire Potential Index (FWI)
│   ├── nasa_firms.py          # NASA FIRMS active fires
│   ├── nasa_ndvi.py           # NASA NDVI vegetation stress anomaly
│   └── weather.py             # NOAA weather forecast
├── config/
│   ├── farm_config.json       # Farm thresholds and zone polygons
│   └── mock_credentials.json  # Credential template (never commit credentials.json)
├── tests/
│   ├── test_no_fire.py        # Scenario 1: GREEN, gate not met
│   ├── test_fire_threat.py    # Scenario 2: CRITICAL, full wake-up packet
│   └── test_gate_logic.py     # Unit tests for gate condition logic
└── output/
    ├── status.json            # Written every cycle
    └── wake_up_packet.json    # Written only when gate condition is met
```

---

## Gate Condition Logic

The Forecaster evaluates three independent signals:

| Signal | GREEN | WATCH | WARNING | CRITICAL |
|--------|-------|-------|---------|----------|
| FWI | < 6 | 6–9 | 9–12 | ≥ 12 |
| Fire distance | > 200 km / none | 100–200 km | 50–100 km | ≤ 50 km |
| NDVI anomaly | > -1σ | -1σ to -1.5σ | -1.5σ to -2σ | ≤ -2σ |

**Multi-signal convergence**: If FWI > 7, fire distance < 150 km, and NDVI < -1σ all hold simultaneously, the overall threat level is escalated by one step.

**Hard safety floors** (always trigger Stage 2 regardless of farmer thresholds):
- FWI ≥ 12
- Fire distance ≤ 75 km

**Gate condition met**: Stage 2 activates when combined threat level is WARNING or above.

---

## Outputs

### `output/status.json`
Written every cycle. Contains FWI, nearest fire, NDVI anomaly, weather, threat level, and gate condition verdict.

### `output/wake_up_packet.json`
Written only when gate condition is met. Contains:
- Per-zone time-to-impact and threat level
- WIFIRE real-time spread predictions
- Pyrecast async simulation UID (results arrive asynchronously)
- Weather forecast
- Smoke trajectory estimate
- Prioritized messages to Crop Agent, Livestock Agent, and ERPC

---

## Running Tests

```bash
# Unit tests for gate logic (no files written)
python tests/test_gate_logic.py

# Integration test: no fire scenario
python tests/test_no_fire.py

# Integration test: fire threat scenario
python tests/test_fire_threat.py
```

---

## Adding Real API Support

Each data source module follows the same pattern: if credentials are present it calls the real API; otherwise raise `RuntimeError`. To swap in a real source:

1. Add the API key to `config/credentials.json`
2. The corresponding module (`sdge_fpi.py`, `nasa_firms.py`, etc.) will automatically use it
3. Run with `--use-real-data`

---

## Credentials Required (Real Mode)

| Key | Source |
|-----|--------|
| `nasa_firms_api_key` | https://firms.modaps.eosdis.nasa.gov/api/ |
| `sdge_fpi_api_key` | SDG&E developer portal |
| `pyrecast_api_key` | https://pyrecast.org |
| `wifire_api_key` | https://wifire.ucsd.edu |
| `noaa_api_key` | https://www.weather.gov/documentation/services-web-api (no key needed for basic use) |
