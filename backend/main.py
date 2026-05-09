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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional

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
    if not SETUP_SENTINEL.exists():
        return RedirectResponse("/static/setup.html")
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


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


# ---------------------------------------------------------------------------
# Setup / onboarding
# ---------------------------------------------------------------------------

FARM_CONFIG_PATH  = FORECASTER_DIR / "config" / "farm_config.json"
LIVESTOCK_DIR_CFG = Path(__file__).parent.parent / "Livestock"
CROP_DIR_CFG      = Path(__file__).parent.parent / "crop_agent"

SETUP_SENTINEL = Path(__file__).parent.parent / ".farm_setup_done"


class PenInput(BaseModel):
    species: str          # cattle | horse | sheep | pig | goat
    count: int
    age: str              # adult | juvenile | mixed
    health: str           # healthy | mixed | sick
    name: Optional[str] = None


class FieldInput(BaseModel):
    crop: str             # avocado | citrus | strawberries | tomatoes | wheat | ...
    acres: float
    planting_date: str    # YYYY-MM-DD


class SetupInput(BaseModel):
    farm_name: str
    lat: float
    lon: float
    total_acres: float
    trailers: int = 2
    vehicle_capacity: int = 100
    pens: List[PenInput]
    fields: List[FieldInput]


@app.get("/api/setup/status")
def setup_status():
    return JSONResponse({"complete": SETUP_SENTINEL.exists()})


@app.post("/api/setup")
async def run_setup(data: SetupInput):
    """Write farm_config.json, farm_profile.json, farm_fields.json from form data,
    then run the forecaster with the provided location."""

    farm_id = "farm_001"
    lat, lon = data.lat, data.lon

    # ── 1. forecaster/config/farm_config.json ──
    deg = 0.005  # ~0.5 km offset per zone
    farm_config = {
        "farm_id": farm_id,
        "farm_name": data.farm_name,
        "location": {"lat": lat, "lon": lon, "region": "southern_california", "state": "CA", "county": "San Diego"},
        "farmer_risk_tolerance": "moderate",
        "custom_thresholds": {"fire_distance_km": 100, "fwi_trigger": 9, "vegetation_stress_sigma": -1.5},
        "hard_safety_floor": {"fire_distance_km": 75, "fwi_trigger": 12, "spread_rate_km_per_day": 5, "multi_signal_convergence": True},
        "affected_zones": [
            {
                "zone_id": "Z1", "name": "North Zone",
                "polygon": {"type": "Polygon", "coordinates": [[[lon - deg, lat + deg], [lon + deg, lat + deg], [lon + deg, lat], [lon - deg, lat], [lon - deg, lat + deg]]]},
                "crops": list({f.crop for f in data.fields[:len(data.fields)//2 + 1]}),
                "animals": sum(p.count for p in data.pens[:len(data.pens)//2 + 1]),
                "harvest_readiness": 0.6,
            },
            {
                "zone_id": "Z2", "name": "South Zone",
                "polygon": {"type": "Polygon", "coordinates": [[[lon - deg, lat], [lon + deg, lat], [lon + deg, lat - deg], [lon - deg, lat - deg], [lon - deg, lat]]]},
                "crops": list({f.crop for f in data.fields[len(data.fields)//2:]}),
                "animals": sum(p.count for p in data.pens[len(data.pens)//2:]),
                "harvest_readiness": 0.4,
            },
        ],
    }
    FARM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FARM_CONFIG_PATH, "w") as f:
        json.dump(farm_config, f, indent=2)

    # ── 2. Livestock/farm_profile.json ──
    import math as _math
    farm_radius_deg = _math.sqrt(data.total_acres * 4047 / _math.pi) / 111000  # degrees
    pen_step = farm_radius_deg * 0.55  # pens spread within ~55% of radius
    pens_json = []
    for i, pen in enumerate(data.pens):
        zone = "Z1" if i < len(data.pens) // 2 + 1 else "Z2"
        sign = 1 if zone == "Z1" else -1
        offset_lat = lat + sign * pen_step * (0.6 + (i % 3) * 0.15)
        offset_lon = lon + (i % 2) * pen_step * 0.5 - pen_step * 0.25
        pens_json.append({
            "pen_id": f"pen_{i+1:03d}",
            "zone": zone,
            "name": pen.name or f"{pen.species.title()} Pen {i+1}",
            "species": pen.species,
            "count": pen.count,
            "age_distribution": pen.age,
            "health_status": pen.health,
            "avg_market_value_usd": {"cattle": 1450, "horse": 3500, "sheep": 350, "pig": 550, "goat": 300}.get(pen.species, 1000),
            "centroid": {"lat": round(offset_lat, 5), "lon": round(offset_lon, 5)},
        })

    farm_profile = {
        "farm_id": farm_id,
        "farm_name": data.farm_name,
        "centroid": {"lat": lat, "lon": lon},
        "total_acres": data.total_acres,
        "pens": pens_json,
        "infrastructure": {
            "water_sources": 2,
            "vehicle_capacity_head": data.vehicle_capacity,
            "available_trailers": data.trailers,
        },
    }
    with open(LIVESTOCK_DIR_CFG / "farm_profile.json", "w") as f:
        json.dump(farm_profile, f, indent=2)

    # ── 3. crop_agent/farm_fields.json ──
    fields_json = []
    for i, field in enumerate(data.fields):
        offset_lat = lat + (0.003 - i * 0.003)
        offset_lon = lon + (i % 2) * 0.004 - 0.002
        fields_json.append({
            "field_id": f"F{i+1}",
            "crop_category": field.crop,
            "location": {"lat": round(offset_lat, 5), "lon": round(offset_lon, 5)},
            "size_acres": field.acres,
            "planting_date": field.planting_date,
        })

    farm_fields = {"farm_id": farm_id, "farm_name": data.farm_name, "fields": fields_json}
    with open(CROP_DIR_CFG / "farm_fields.json", "w") as f:
        json.dump(farm_fields, f, indent=2)

    # ── 4. Mark setup done ──
    SETUP_SENTINEL.touch()

    return JSONResponse({"ok": True, "farm_id": farm_id, "lat": lat, "lon": lon})


@app.get("/api/status")
def get_status():
    """Return the latest status.json written by the forecaster."""
    if not STATUS_JSON.exists():
        raise HTTPException(status_code=404, detail="status.json not found — run the forecaster first")
    with open(STATUS_JSON) as f:
        data = json.load(f)
    profile_path = LIVESTOCK_DIR_CFG / "farm_profile.json"
    if profile_path.exists():
        with open(profile_path) as f:
            profile = json.load(f)
        data["total_acres"] = profile.get("total_acres")
        data["farm_name"] = profile.get("farm_name")
    return JSONResponse(data)


@app.get("/api/econ")
def get_econ_report():
    econ_path = FORECASTER_DIR / "output" / "econ_report.json"
    if not econ_path.exists():
        raise HTTPException(status_code=404, detail="No econ report yet — run forecaster first")
    with open(econ_path) as f:
        return JSONResponse(json.load(f))


@app.get("/api/policy")
def get_policy_report():
    policy_path = FORECASTER_DIR / "output" / "policy_report.json"
    if not policy_path.exists():
        raise HTTPException(status_code=404, detail="No policy report yet — run policy agent first")
    with open(policy_path) as f:
        return JSONResponse(json.load(f))


@app.get("/api/farm-profile")
def get_farm_profile():
    profile_path = LIVESTOCK_DIR_CFG / "farm_profile.json"
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail="Farm profile not set up yet")
    with open(profile_path) as f:
        return JSONResponse(json.load(f))


async def _run_forecaster_cycle(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> dict:
    """Run a real forecaster cycle using live NASA FIRMS + Open-Meteo data.
    Writes status.json and returns the status dict."""
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

    return status


@app.post("/api/run-forecaster")
async def run_forecaster(
    lat: float = Query(None, description="Custom location latitude"),
    lon: float = Query(None, description="Custom location longitude"),
):
    """Run a real forecaster cycle using live NASA FIRMS + Open-Meteo data."""
    status = await _run_forecaster_cycle(lat, lon)
    return JSONResponse(status)


def _run_econ_subprocess() -> Optional[dict]:
    """Run econ agent and return econ_report.json contents (or None on failure).
    Econ agent reads the latest crop and livestock outputs from disk."""
    import subprocess
    try:
        subprocess.run(
            ["python3", "-m", "forecaster.agents.econ_agent"],
            cwd=Path(__file__).parent.parent,
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        pass  # best-effort; we still try to read whatever econ_report.json exists
    econ_path = FORECASTER_DIR / "output" / "econ_report.json"
    if econ_path.exists():
        try:
            with open(econ_path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


INSURANCE_PDF = FORECASTER_DIR / "output" / "ccc_576_filled.pdf"


def _run_insurance_subprocess() -> dict:
    """Run insurance agent to fill the CCC-576 PDF. Returns metadata dict
    {"ok": bool, "path": str, "size_bytes": int, "filled_at": iso, "error"?: str}."""
    import subprocess
    try:
        proc = subprocess.run(
            ["python3", "-m", "forecaster.agents.insurance_agent"],
            cwd=Path(__file__).parent.parent,
            capture_output=True, text=True, timeout=60,
        )
        if not INSURANCE_PDF.exists():
            detail = proc.stderr[-500:] if proc.stderr else proc.stdout[-500:] or "No PDF produced"
            return {"ok": False, "error": detail}
        st = INSURANCE_PDF.stat()
        return {
            "ok": True,
            "path": str(INSURANCE_PDF),
            "size_bytes": st.st_size,
            "filled_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Insurance agent timed out (60s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/insurance/status")
def get_insurance_status():
    """Return metadata about the filled CCC-576 PDF (or 404 if not generated yet)."""
    if not INSURANCE_PDF.exists():
        raise HTTPException(status_code=404, detail="No filled insurance form yet — run the pipeline first")
    st = INSURANCE_PDF.stat()
    fields_filled, fields_total = None, None
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(INSURANCE_PDF))
        fields = reader.get_fields() or {}
        fields_total = len(fields)
        fields_filled = sum(1 for v in fields.values() if v.get("/V"))
    except Exception:
        pass
    return JSONResponse({
        "ok": True,
        "form_name": "USDA CCC-576 — Notice of Loss",
        "agency": "USDA Farm Service Agency",
        "fsa_office": "San Diego County FSA Office, 1204 Mission Road, Suite 1, Escondido, CA 92029",
        "deadline_days": 30,
        "filename": INSURANCE_PDF.name,
        "size_bytes": st.st_size,
        "fields_filled": fields_filled,
        "fields_total": fields_total,
        "filled_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "download_url": "/api/insurance/pdf",
    })


@app.get("/api/insurance/pdf")
def download_insurance_pdf():
    """Stream the filled CCC-576 PDF as a download."""
    if not INSURANCE_PDF.exists():
        raise HTTPException(status_code=404, detail="No filled insurance form yet — run the pipeline first")
    return FileResponse(
        INSURANCE_PDF,
        media_type="application/pdf",
        filename="ccc_576_filled.pdf",
    )


@app.post("/api/insurance/run")
def run_insurance_agent():
    """Generate (or regenerate) the filled CCC-576 PDF."""
    result = _run_insurance_subprocess()
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Insurance agent failed"))
    return JSONResponse(result)


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


def _run_livestock_subprocess() -> dict:
    """Sync forecaster data then run livestock agent.
    Returns livestock_status dict or {"error": "..."}."""
    import subprocess
    _sync_forecaster_to_livestock()
    try:
        result = subprocess.run(
            ["python3", "livestock_agent.py"],
            cwd=LIVESTOCK_DIR,
            capture_output=True, text=True, timeout=180,
        )
        if LIVESTOCK_STATUS_JSON.exists():
            with open(LIVESTOCK_STATUS_JSON) as f:
                return json.load(f)
        detail = result.stderr[-400:] if result.stderr else "No output produced"
        return {"error": detail}
    except subprocess.TimeoutExpired:
        if LIVESTOCK_STATUS_JSON.exists():
            with open(LIVESTOCK_STATUS_JSON) as f:
                return json.load(f)
        return {"error": "Livestock agent timed out but may have completed"}
    except Exception as e:
        return {"error": f"Livestock agent error: {e}"}


@app.post("/api/livestock/run")
async def run_livestock_agent():
    """Sync forecaster data then run the livestock evacuation agent."""
    result = _run_livestock_subprocess()
    if isinstance(result, dict) and result.get("error") and "timed out" not in result["error"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Crop endpoints
# ---------------------------------------------------------------------------

def _normalize_crop_output(data: dict) -> dict:
    """Map task1/2/3/4 keys to descriptive names the UI expects."""
    key_map = {
        "task4": "field_decisions",
        "task1": "fire_reduction",
        "task2": "economic_impact",
        "task3": "hydration_strategy",
    }
    for old, new in key_map.items():
        if old in data and new not in data:
            data[new] = data.pop(old)
    return data


def _filter_crop_output_to_farm(data: dict) -> dict:
    """Remove hallucinated fields not in current farm_fields.json."""
    fields_path = CROP_DIR_CFG / "farm_fields.json"
    if not fields_path.exists():
        return data
    with open(fields_path) as f:
        farm = json.load(f)
    valid_ids = {fld["field_id"] for fld in farm.get("fields", [])}
    if not valid_ids:
        return data
    for key in ("field_decisions", "fire_reduction", "hydration_strategy"):
        if isinstance(data.get(key), list):
            data[key] = [r for r in data[key] if r.get("field_id") in valid_ids]
    econ = data.get("economic_impact", {})
    if isinstance(econ.get("crop_destructions"), list):
        econ["crop_destructions"] = [r for r in econ["crop_destructions"] if r.get("field_id") in valid_ids]
    return data


@app.get("/api/crop/status")
def get_crop_status():
    """Return the latest crop agent output."""
    outputs = sorted(
        list(CROP_DIR.glob("output_*.json")) + list(CROP_DIR.glob("crop_agent_output_*.json")),
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    outputs = [p for p in outputs if "raw" not in p.name and "erpc" not in p.name]
    if not outputs:
        raise HTTPException(status_code=404, detail="No crop agent output — run crop agent first")
    with open(outputs[0]) as f:
        data = json.load(f)
    data = _normalize_crop_output(data)
    erpc = CROP_DIR / "output_to_erpc.json"
    if erpc.exists():
        with open(erpc) as f:
            data["erpc_output"] = json.load(f)
    return JSONResponse(_filter_crop_output_to_farm(data))


def _run_crop_subprocess() -> dict:
    """Run crop agent. Returns normalized crop output dict, or {"error": "..."}."""
    import subprocess
    if not STATUS_JSON.exists():
        return {"error": "status.json not found — run forecaster first"}
    env = os.environ.copy()
    if not env.get("GROQ_API_KEY"):
        return {"error": "GROQ_API_KEY not set"}
    try:
        proc = subprocess.run(
            ["python3", "crop_agent.py", str(STATUS_JSON)],
            cwd=CROP_DIR,
            capture_output=True, text=True, timeout=180, env=env,
        )
        outputs = sorted(
            list(CROP_DIR.glob("output_*.json")) + list(CROP_DIR.glob("crop_agent_output_*.json")),
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        outputs = [p for p in outputs if "raw" not in p.name and "erpc" not in p.name]
        if outputs:
            with open(outputs[0]) as f:
                data = json.load(f)
            data = _normalize_crop_output(data)
            erpc = CROP_DIR / "output_to_erpc.json"
            if erpc.exists():
                with open(erpc) as f:
                    data["erpc_output"] = json.load(f)
            return _filter_crop_output_to_farm(data)
        detail = proc.stderr[-500:] if proc.stderr else proc.stdout[-500:] or "No output produced"
        return {"error": detail}
    except subprocess.TimeoutExpired:
        return {"error": "Crop agent timed out (180s)"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/crop/run")
async def run_crop_agent():
    """Feed forecaster status into the crop agent and return field decisions."""
    result = _run_crop_subprocess()
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        if "GROQ_API_KEY" in err:
            raise HTTPException(status_code=400, detail=err)
        if "status.json" in err:
            raise HTTPException(status_code=404, detail=err)
        if "timed out" in err:
            raise HTTPException(status_code=504, detail=err)
        raise HTTPException(status_code=500, detail=err)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------

@app.post("/api/run-pipeline")
async def run_pipeline(
    lat: float = Query(None, description="Custom location latitude"),
    lon: float = Query(None, description="Custom location longitude"),
):
    """End-to-end run: forecaster → (crop ‖ livestock) → econ.
    Returns combined report. Long-running (~120-180s)."""
    import asyncio

    forecaster_status = await _run_forecaster_cycle(lat, lon)

    crop_result, livestock_result = await asyncio.gather(
        asyncio.to_thread(_run_crop_subprocess),
        asyncio.to_thread(_run_livestock_subprocess),
        return_exceptions=True,
    )
    if isinstance(crop_result, BaseException):
        crop_result = {"error": str(crop_result)}
    if isinstance(livestock_result, BaseException):
        livestock_result = {"error": str(livestock_result)}

    econ_result = _run_econ_subprocess()
    insurance_result = _run_insurance_subprocess()

    return JSONResponse({
        "forecaster": forecaster_status,
        "crop": crop_result,
        "livestock": livestock_result,
        "econ": econ_result,
        "insurance": insurance_result,
        "data_sources": (econ_result or {}).get("data_sources"),
    })
