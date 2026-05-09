"""
Fetch per-field soil moisture and evapotranspiration.

  Soil moisture  — NASA POWER API (GWETROOT: 0=bone dry, 1=saturated)
                   No auth required; uses same Earthdata account for higher-res
                   SMAP data if needed in future.

  Evapotranspiration — OpenET API (mm/day actual water use per field)
                       Requires OPENET_API_KEY from .env
"""

import os
import requests
from datetime import datetime, timedelta

POWER_BASE  = "https://power.larc.nasa.gov/api/temporal/daily/point"
OPENET_BASE = "https://openet-api.org/raster/timeseries/point"


def fetch_soil_moisture(lat: float, lon: float) -> dict | None:
    """
    Root-zone soil wetness from NASA POWER.
    GWETROOT scale: 0.0 = bone dry, 1.0 = fully saturated.
    """
    end   = datetime.now()
    start = end - timedelta(days=7)
    try:
        r = requests.get(
            POWER_BASE,
            params={
                "parameters": "GWETROOT",
                "community":  "AG",
                "longitude":  lon,
                "latitude":   lat,
                "start":      start.strftime("%Y%m%d"),
                "end":        end.strftime("%Y%m%d"),
                "format":     "JSON",
            },
            timeout=12,
        )
        if r.status_code != 200:
            return None
        values = r.json()["properties"]["parameter"]["GWETROOT"]
        for date in sorted(values.keys(), reverse=True):
            v = values[date]
            if v != -999:
                return {
                    "soil_wetness": round(float(v), 3),
                    "date":         date,
                    "source":       "NASA POWER (GWETROOT)",
                }
        return None
    except Exception as e:
        print(f"    NASA POWER error: {e}")
        return None


def fetch_et(lat: float, lon: float) -> dict | None:
    """
    Actual evapotranspiration from OpenET ensemble model (mm/day).
    Higher ET = more water stress / demand.
    """
    api_key = os.getenv("OPENET_API_KEY", "")
    if not api_key:
        return None

    end   = datetime.now()
    start = end - timedelta(days=60)
    try:
        r = requests.post(
            OPENET_BASE,
            json={
                "date_range":   [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
                "interval":     "monthly",
                "geometry":     [lon, lat],
                "model":        "Ensemble",
                "variable":     "ET",
                "reference_et": "gridMET",
                "units":        "mm",
                "file_format":  "JSON",
                "version":      2.1,
            },
            headers={
                "Authorization": api_key,
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    OpenET HTTP {r.status_code}: {r.text[:120]}")
            return None
        data = r.json()
        entries = data if isinstance(data, list) else data.get("data", [])
        if entries:
            et_mm_month = entries[-1].get("et") or entries[-1].get("ET")
            if et_mm_month is not None:
                return {
                    "et_mm_per_day": round(float(et_mm_month) / 30, 2),
                    "source":        "OpenET ensemble",
                }
        return None
    except Exception as e:
        print(f"    OpenET error: {e}")
        return None


def fetch_field_water_data(fields: list) -> dict:
    """
    Fetch soil moisture + ET for every field.
    Returns dict keyed by field_id.
    """
    results = {}
    for field in fields:
        fid = field["field_id"]
        lat = field["location"]["lat"]
        lon = field["location"]["lon"]
        print(f"  Field {fid} ({field.get('crop_category', '?')}): soil moisture + ET...")

        sm = fetch_soil_moisture(lat, lon)
        et = fetch_et(lat, lon)

        results[fid] = {
            "soil_wetness":        sm["soil_wetness"] if sm else None,
            "soil_wetness_source": sm["source"]       if sm else "unavailable",
            "et_mm_per_day":       et["et_mm_per_day"] if et else None,
            "et_source":           et["source"]        if et else "unavailable",
        }

        sm_str = f"{sm['soil_wetness']:.3f}" if sm else "N/A"
        et_str = f"{et['et_mm_per_day']:.2f} mm/day" if et else "N/A"
        print(f"    soil_wetness={sm_str}  ET={et_str}")

    return results
