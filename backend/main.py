"""Minimal wildfire map backend — serves live NASA FIRMS data as GeoJSON."""

import csv
import io
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / "forecaster" / ".env")

app = FastAPI(title="Wildfire Map")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/1"
SOURCE = "VIIRS_SNPP_NRT"

# Bounding box: covers California + Nevada + Arizona
AREA = "-125,32,-113,42"


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/fires")
async def get_fires():
    api_key = os.environ.get("NASA_FIRMS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="NASA_FIRMS_API_KEY not set in .env")

    url = FIRMS_URL.format(key=api_key, source=SOURCE, area=AREA)

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"FIRMS API error: {e}")

    features = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            frp = float(row.get("frp", 0))
            confidence = row.get("confidence", "n")
            acq_date = row.get("acq_date", "")
            acq_time = row.get("acq_time", "")
            bright = float(row.get("bright_ti4", 0))
        except (ValueError, KeyError):
            continue

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "frp": frp,
                "confidence": confidence,
                "detected_at": f"{acq_date} {acq_time[:2]}:{acq_time[2:]}Z" if acq_time else acq_date,
                "brightness": bright,
                "intensity": _intensity(frp),
            },
        })

    return JSONResponse({
        "type": "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(features),
        "features": features,
    })


def _intensity(frp: float) -> str:
    if frp >= 100:
        return "extreme"
    if frp >= 50:
        return "high"
    if frp >= 10:
        return "moderate"
    return "low"
