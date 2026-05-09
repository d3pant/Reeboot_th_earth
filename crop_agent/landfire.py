"""
Fetch surface fuel model flammability per field from LANDFIRE.
Uses Landfire_CONUS_2022 MapServer layer 24 (FBFM40 — Scott & Burgan 40).
Falls back to crop-type estimate if LANDFIRE returns non-burnable or fails
(agricultural fields are sometimes misclassified NB in LANDFIRE).
"""

import requests
from api_config import LANDFIRE, APP_USER_AGENT

# Numeric FBFM40 code → flammability score (1–5)
# NB  91–99:  non-burnable (urban, water, bare ground) → 0
# GR 101–109: grass (high spread)                      → 3–5
# GS 121–124: grass-shrub mix                          → 3–4
# SH 141–149: shrub                                    → 2–4
# TU 161–165: timber-understory                        → 2–3
# TL 181–189: timber litter                            → 2–3
# SB 201–204: slash-blowdown                           → 4–5
_FBFM40 = {
    91: 0, 92: 0, 93: 0, 94: 0, 95: 0, 96: 0, 97: 0, 98: 0, 99: 0,
    101: 3, 102: 3, 103: 4, 104: 4, 105: 4, 106: 5, 107: 5, 108: 5, 109: 5,
    121: 3, 122: 3, 123: 4, 124: 4,
    141: 2, 142: 3, 143: 3, 144: 3, 145: 4, 146: 3, 147: 4, 148: 4, 149: 4,
    161: 2, 162: 2, 163: 3, 164: 3, 165: 3,
    181: 2, 182: 2, 183: 2, 184: 2, 185: 2, 186: 3, 187: 3, 188: 3, 189: 3,
    201: 4, 202: 4, 203: 5, 204: 4,
}

# Fallback when LANDFIRE fails or returns NB for a crop field
_CROP_FALLBACK = {
    "cover_crop":   5,
    "wheat":        4,
    "corn":         4,
    "sorghum":      4,
    "tomatoes":     3,
    "strawberries": 3,
    "potatoes":     3,
    "alfalfa":      3,
    "grapes":       2,
    "citrus":       2,
    "almonds":      2,
    "walnuts":      2,
    "avocado":      1,
    "olive":        1,
}


def fetch_flammability(lat: float, lon: float, crop_category: str = "") -> dict:
    """
    Query LANDFIRE FBFM40 fuel model at lat/lon.
    Returns {"flammability": int, "fuel_model_code": int|None, "source": str}.
    """
    try:
        import json as _json
        r = requests.get(
            LANDFIRE["identify_endpoint"],
            params={
                "geometry":       _json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
                "geometryType":   "esriGeometryPoint",
                "returnGeometry": "false",
                "f":              "json",
            },
            headers={"User-Agent": APP_USER_AGENT},
            timeout=12,
        )
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")

        data = r.json()
        raw = data.get("value") or (data.get("properties", {}).get("Values") or [None])[0]
        if raw is None or raw == "NoData":
            raise ValueError("NoData")

        code  = int(raw)
        score = _FBFM40.get(code)

        if score is None or score == 0:
            # NB or unknown code — LANDFIRE often misclassifies active cropland as NB
            fallback = _CROP_FALLBACK.get(crop_category.lower(), 3)
            return {
                "flammability":    fallback,
                "fuel_model_code": code,
                "source":          f"LANDFIRE NB({code}) → crop-type fallback",
            }

        return {
            "flammability":    score,
            "fuel_model_code": code,
            "source":          "LANDFIRE FBFM40",
        }

    except Exception as e:
        fallback = _CROP_FALLBACK.get(crop_category.lower(), 3)
        return {
            "flammability":    fallback,
            "fuel_model_code": None,
            "source":          f"LANDFIRE error ({e}) → crop-type fallback",
        }


def fetch_field_flammability(fields: list) -> dict:
    """
    Fetch flammability score for every field.
    Returns dict keyed by field_id.
    """
    results = {}
    for field in fields:
        fid  = field["field_id"]
        lat  = field["location"]["lat"]
        lon  = field["location"]["lon"]
        crop = field.get("crop_category", "")

        print(f"  Field {fid} ({crop}): LANDFIRE fuel model...")
        data = fetch_flammability(lat, lon, crop)
        results[fid] = data
        print(f"    flammability={data['flammability']}  code={data['fuel_model_code']}  [{data['source']}]")

    return results
