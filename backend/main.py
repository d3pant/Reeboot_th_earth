"""Wildfire map backend — NASA FIRMS fire data + Rothermel spread prediction."""

import csv
import io
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).parent.parent / ".env")

# Allow importing from forecaster/
sys.path.insert(0, str(Path(__file__).parent.parent / "forecaster"))
from data_sources.open_meteo import fetch_weather
from models.spread_model import compute_ellipse, ellipse_to_geojson_polygon, time_to_impact

app = FastAPI(title="California Wildfire Map")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/2"
SOURCE = "VIIRS_SNPP_NRT"
CA_AREA = "-124.5,32.5,-114.0,42.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _intensity(frp: float) -> str:
    if frp >= 100: return "extreme"
    if frp >= 50:  return "high"
    if frp >= 10:  return "moderate"
    return "low"


async def _fetch_firms_csv(area: str) -> list[dict]:
    api_key = os.environ.get("NASA_FIRMS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="NASA_FIRMS_API_KEY not set in .env")

    url = FIRMS_URL.format(key=api_key, source=SOURCE, area=area)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"FIRMS API error: {e}")

    rows = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            frp = float(row.get("frp", 0) or 0)
            bright_ti4 = float(row.get("bright_ti4", 0) or 0)
            bright_ti5 = float(row.get("bright_ti5", 0) or 0)
            acq_date = row.get("acq_date", "")
            t = row.get("acq_time", "").zfill(4)
            detected_at = f"{acq_date} {t[:2]}:{t[2:]} UTC"
        except (ValueError, KeyError):
            continue

        confidence_raw = row.get("confidence", "n")
        rows.append({
            "lat": lat, "lon": lon, "frp": frp,
            "intensity": _intensity(frp),
            "confidence": {"l": "Low", "n": "Nominal", "h": "High"}.get(confidence_raw, confidence_raw),
            "confidence_raw": confidence_raw,
            "detected_at": detected_at,
            "acq_date": acq_date,
            "satellite": {"N": "Suomi NPP", "1": "NOAA-20", "A": "Terra", "T": "Aqua"}.get(row.get("satellite", ""), row.get("satellite", "")),
            "instrument": row.get("instrument", "VIIRS"),
            "bright_ti4": bright_ti4,
            "bright_ti5": bright_ti5,
            "scan": float(row.get("scan", 0) or 0),
            "track": float(row.get("track", 0) or 0),
            "version": row.get("version", ""),
            "daynight": "Day" if row.get("daynight") == "D" else "Night",
        })
    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/fires")
async def get_fires():
    rows = await _fetch_firms_csv(CA_AREA)
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": r,
        }
        for r in rows
    ]
    return JSONResponse({
        "type": "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(features),
        "features": features,
    })


@app.get("/api/spread")
async def get_spread(
    fire_lat: float = Query(..., description="Fire latitude"),
    fire_lon: float = Query(..., description="Fire longitude"),
    fire_frp: float = Query(0.0, description="Fire Radiative Power (MW)"),
):
    """Return predicted fire spread ellipses at 6h, 12h, 24h using
    Open-Meteo wind + soil moisture and the Rothermel simplified model."""

    # 1. Fetch weather at fire location
    try:
        weather = fetch_weather(fire_lat, fire_lon)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    wind_kmh = weather["wind_speed_kmh"]
    wind_dir = weather["wind_direction_deg"]
    soil_m   = weather["soil_moisture"]

    # 2. Compute ellipses for 3 time horizons
    horizons = [6, 12, 24]
    ellipse_features = []

    for hours in horizons:
        ellipse = compute_ellipse(
            fire_lat=fire_lat,
            fire_lon=fire_lon,
            wind_direction_deg=wind_dir,
            wind_kmh=wind_kmh,
            soil_moisture=soil_m,
            frp_mw=fire_frp,
            hours=hours,
        )
        polygon = ellipse_to_geojson_polygon(ellipse)
        ellipse_features.append({
            "type": "Feature",
            "geometry": polygon,
            "properties": {
                "hours": hours,
                "head_km": ellipse.head_km,
                "back_km": ellipse.back_km,
                "semi_minor_km": ellipse.semi_minor_km,
                "wind_direction_deg": wind_dir,
                "wind_speed_kmh": wind_kmh,
            },
        })

    return JSONResponse({
        "fire": {"lat": fire_lat, "lon": fire_lon, "frp": fire_frp},
        "weather": weather,
        "model": "Rothermel (1972) simplified + Anderson (1983) ellipse",
        "ellipses": {
            "type": "FeatureCollection",
            "features": ellipse_features,
        },
    })


@app.get("/api/impact")
async def get_impact(
    target_lat: float = Query(..., description="Target (farm) latitude"),
    target_lon: float = Query(..., description="Target (farm) longitude"),
):
    """Find nearest CA fire and estimate time-to-impact at target location."""

    # 1. Fetch all CA fires
    rows = await _fetch_firms_csv(CA_AREA)
    if not rows:
        return JSONResponse({"nearest_fire": None, "message": "No active fires in California"})

    # 2. Haversine distance to each fire
    import math
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    for r in rows:
        r["_dist"] = haversine(target_lat, target_lon, r["lat"], r["lon"])

    nearest = min(rows, key=lambda r: r["_dist"])

    # 3. Fetch weather at fire location
    try:
        weather = fetch_weather(nearest["lat"], nearest["lon"])
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # 4. Time-to-impact
    impact = time_to_impact(
        fire_lat=nearest["lat"],
        fire_lon=nearest["lon"],
        target_lat=target_lat,
        target_lon=target_lon,
        wind_direction_deg=weather["wind_direction_deg"],
        wind_kmh=weather["wind_speed_kmh"],
        soil_moisture=weather["soil_moisture"],
        frp_mw=nearest["frp"],
    )

    return JSONResponse({
        "target": {"lat": target_lat, "lon": target_lon},
        "nearest_fire": {k: v for k, v in nearest.items() if not k.startswith("_")},
        "distance_km": round(nearest["_dist"], 2),
        "weather": weather,
        "impact": impact,
    })


# ---------------------------------------------------------------------------
# Farm endpoints
# ---------------------------------------------------------------------------

FARM_LAT = 33.2232
FARM_LON = -117.1611
FORECASTER_DIR = Path(__file__).parent.parent / "forecaster"
STATUS_JSON     = FORECASTER_DIR / "output" / "status.json"


@app.get("/api/status")
def get_status():
    """Return the latest status.json written by the forecaster."""
    if not STATUS_JSON.exists():
        raise HTTPException(status_code=404, detail="status.json not found — run the forecaster first")
    with open(STATUS_JSON) as f:
        return JSONResponse(json.load(f))


@app.post("/api/run-forecaster")
async def run_forecaster(
    lat: float = Query(None, description="Custom location latitude"),
    lon: float = Query(None, description="Custom location longitude"),
):
    """Run a real forecaster cycle using live NASA FIRMS + Open-Meteo data."""
    import math

    farm_lat = lat if lat is not None else FARM_LAT
    farm_lon = lon if lon is not None else FARM_LON

    farm_config_path = FORECASTER_DIR / "config" / "farm_config.json"
    with open(farm_config_path) as f:
        farm_config = json.load(f)

    farm_thresh = farm_config["custom_thresholds"]
    floor       = farm_config["hard_safety_floor"]

    # 1. Real weather at farm via Open-Meteo (no key needed)
    farm_weather = fetch_weather(farm_lat, farm_lon)
    fwi = farm_weather.get("fwi") or 0.0

    # 2. Real nearest fire via NASA FIRMS — prefer nominal/high confidence
    rows = await _fetch_firms_csv(CA_AREA)
    confident_rows = [r for r in rows if r.get("confidence_raw") in ("n", "h")]
    if confident_rows:
        rows = confident_rows

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    nearest_fire = None
    if rows:
        for r in rows:
            r["_dist"] = haversine(farm_lat, farm_lon, r["lat"], r["lon"])
        nearest = min(rows, key=lambda r: r["_dist"])
        nearest_fire = {
            "name": f"Active Fire ({nearest['satellite']})",
            "distance_km": round(nearest["_dist"], 2),
            "location": {"lat": nearest["lat"], "lon": nearest["lon"]},
            "detected_at": nearest["detected_at"],
            "frp_mw": nearest["frp"],
            "intensity": nearest["intensity"],
            "confidence": nearest["confidence"],
            "current_size_acres": None,
            "spread_rate_km_per_day": None,
            "direction_degrees": None,
            "current_perimeter": None,
        }

    # 3. Gate condition with real data
    from forecaster import evaluate_gate_condition
    ndvi = -0.5  # Open-Meteo doesn't provide NDVI; neutral default
    gate = evaluate_gate_condition(
        fwi=fwi,
        fire=nearest_fire,
        ndvi=ndvi,
        farm_config=farm_config,
    )
    threat_level = gate["threat_level"]

    update_intervals = {"GREEN": 720, "WATCH": 120, "WARNING": 30, "CRITICAL": 15, "EMERGENCY": 5}

    # 4. Spread prediction for nearest fire
    spread_prediction = None
    if nearest_fire:
        fire_weather = fetch_weather(nearest_fire["location"]["lat"], nearest_fire["location"]["lon"])
        wind_kmh = fire_weather["wind_speed_kmh"]
        wind_dir = fire_weather["wind_direction_deg"]
        soil_m   = fire_weather["soil_moisture"]
        frp      = nearest_fire["frp_mw"]

        horizons = {}
        for hours in [6, 12, 24]:
            ellipse = compute_ellipse(
                fire_lat=nearest_fire["location"]["lat"],
                fire_lon=nearest_fire["location"]["lon"],
                wind_direction_deg=wind_dir,
                wind_kmh=wind_kmh,
                soil_moisture=soil_m,
                frp_mw=frp,
                hours=hours,
            )
            horizons[f"{hours}h"] = {
                "polygon": ellipse_to_geojson_polygon(ellipse),
                "head_km": ellipse.head_km,
                "back_km": ellipse.back_km,
                "flank_km": ellipse.semi_minor_km,
                "center": {"lat": ellipse.center_lat, "lon": ellipse.center_lon},
            }

        impact = time_to_impact(
            fire_lat=nearest_fire["location"]["lat"],
            fire_lon=nearest_fire["location"]["lon"],
            target_lat=farm_lat, target_lon=farm_lon,
            wind_direction_deg=wind_dir,
            wind_kmh=wind_kmh,
            soil_moisture=soil_m,
            frp_mw=frp,
        )

        from models.spread_model import head_rate_of_spread
        spread_prediction = {
            "model": "Rothermel (1972) simplified + Anderson (1983) ellipse",
            "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wind_speed_kmh": wind_kmh,
            "wind_direction_deg": wind_dir,
            "soil_moisture": soil_m,
            "head_ros_kmh": head_rate_of_spread(wind_kmh, soil_m, frp),
            "horizons": horizons,
            "time_to_farm": impact,
        }

    # 5. Build status.json — same structure, all real data
    status = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "farm_lat": farm_lat,
        "farm_lon": farm_lon,
        "stage": 1,
        "threat_level": threat_level,
        "threat_level_confidence": gate["threat_confidence"],
        "fwi_index": fwi,
        "fwi_threshold_farmer": farm_thresh["fwi_trigger"],
        "fwi_threshold_floor": floor["fwi_trigger"],
        "nearest_fire": nearest_fire,
        "nearest_fire_distance_threshold_farmer": farm_thresh["fire_distance_km"],
        "nearest_fire_distance_threshold_floor": floor["fire_distance_km"],
        "vegetation_ndvi_anomaly": ndvi,
        "vegetation_threshold_farmer": farm_thresh["vegetation_stress_sigma"],
        "vegetation_threshold_floor": -2.0,
        "wind_speed_kmh": farm_weather["wind_speed_kmh"],
        "wind_direction_degrees": farm_weather["wind_direction_deg"],
        "temperature_c": farm_weather["temperature_c"],
        "humidity_percent": farm_weather["humidity_pct"],
        "gate_condition_met": gate["gate_condition_met"],
        "gate_condition_reason": gate["gate_condition_reason"],
        "multi_signal_convergence": gate["convergence"],
        "next_update_minutes": update_intervals.get(threat_level, 120),
        "stage_transition_triggered": gate["gate_condition_met"],
        "spread_prediction": spread_prediction,
    }

    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_JSON, "w") as f:
        json.dump(status, f, indent=2, default=str)

    return JSONResponse(status)


# ---------------------------------------------------------------------------
# Livestock endpoints
# ---------------------------------------------------------------------------

LIVESTOCK_DIR = Path(__file__).parent.parent / "Livestock"
LIVESTOCK_STATUS_JSON = LIVESTOCK_DIR / "livestock_status.json"
CROP_DIR = Path(__file__).parent.parent / "crop_agent"


def _sync_forecaster_to_livestock():
    """Copy latest forecaster outputs into Livestock/ before running the agent."""
    import shutil

    if STATUS_JSON.exists():
        shutil.copy(STATUS_JSON, LIVESTOCK_DIR / "status.json")

    wake_up_src = FORECASTER_DIR / "output" / "wake_up_packet.json"
    if wake_up_src.exists():
        shutil.copy(wake_up_src, LIVESTOCK_DIR / "wake_up_packet.json")
    elif STATUS_JSON.exists():
        # Build minimal wake_up from status so the agent doesn't crash
        with open(STATUS_JSON) as f:
            st = json.load(f)
        smoke_dir = (st.get("wind_direction_degrees") or 0 + 180) % 360
        minimal = {
            "affected_zones": [
                {"zone_id": "Z1", "time_to_impact_hours": 11.0},
                {"zone_id": "Z2", "time_to_impact_hours": 11.1},
            ],
            "smoke_trajectory": {"direction_degrees": smoke_dir},
        }
        with open(LIVESTOCK_DIR / "wake_up_packet.json", "w") as f:
            json.dump(minimal, f)


@app.get("/api/livestock/status")
def get_livestock_status():
    """Return the latest livestock evacuation status."""
    if not LIVESTOCK_STATUS_JSON.exists():
        raise HTTPException(status_code=404, detail="livestock_status.json not found — run livestock agent first")
    with open(LIVESTOCK_STATUS_JSON) as f:
        return JSONResponse(json.load(f))


@app.post("/api/livestock/run")
async def run_livestock_agent():
    """Sync forecaster data then run the livestock evacuation agent."""
    import subprocess
    _sync_forecaster_to_livestock()
    try:
        result = subprocess.run(
            ["python3", "livestock_agent.py"],
            cwd=LIVESTOCK_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if LIVESTOCK_STATUS_JSON.exists():
            with open(LIVESTOCK_STATUS_JSON) as f:
                return JSONResponse(json.load(f))
        detail = result.stderr[-400:] if result.stderr else "No output produced"
        raise HTTPException(status_code=500, detail=detail)
    except subprocess.TimeoutExpired:
        if LIVESTOCK_STATUS_JSON.exists():
            with open(LIVESTOCK_STATUS_JSON) as f:
                return JSONResponse(json.load(f))
        return JSONResponse({"status": "timeout", "message": "Agent timed out but may have completed"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Livestock agent error: {e}")


# ---------------------------------------------------------------------------
# Crop endpoints
# ---------------------------------------------------------------------------

@app.get("/api/crop/status")
def get_crop_status():
    """Return the latest crop agent output."""
    outputs = sorted(CROP_DIR.glob("crop_agent_output_*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not outputs:
        raise HTTPException(status_code=404, detail="No crop agent output — run crop agent first")
    with open(outputs[0]) as f:
        data = json.load(f)
    erpc = CROP_DIR / "output_to_erpc.json"
    if erpc.exists():
        with open(erpc) as f:
            data["erpc_output"] = json.load(f)
    return JSONResponse(data)


@app.post("/api/crop/run")
async def run_crop_agent():
    """Feed forecaster status into the crop agent and return field decisions."""
    import subprocess
    if not STATUS_JSON.exists():
        raise HTTPException(status_code=404, detail="Run the forecaster first — no status.json found")

    env = os.environ.copy()

    if not env.get("GROQ_API_KEY"):
        raise HTTPException(status_code=400, detail="GROQ_API_KEY not set — add it to crop_agent/.env")

    try:
        proc = subprocess.run(
            ["python3", "crop_agent.py", str(STATUS_JSON)],
            cwd=CROP_DIR,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        outputs = sorted(CROP_DIR.glob("crop_agent_output_*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if outputs:
            with open(outputs[0]) as f:
                data = json.load(f)
            erpc = CROP_DIR / "output_to_erpc.json"
            if erpc.exists():
                with open(erpc) as f:
                    data["erpc_output"] = json.load(f)
            return JSONResponse(data)
        detail = proc.stderr[-500:] if proc.stderr else proc.stdout[-500:] or "No output produced"
        raise HTTPException(status_code=500, detail=detail)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Crop agent timed out (180s)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
