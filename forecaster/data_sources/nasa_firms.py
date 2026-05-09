"""NASA FIRMS active fire data source."""

import logging
import math
import os
import requests

logger = logging.getLogger(__name__)

FIRMS_API_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{api_key}/VIIRS_SNPP_NRT/{area}/1"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in km."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_active_fires(farm_location: dict, radius_km: float = 200) -> dict | None:
    """Fetch active fires from NASA FIRMS and return the nearest one.

    Returns dict with keys: name, distance_km, location, detected_at, spread_rate_km_per_day, direction_degrees
    or None if no fires detected within radius_km.
    Raises RuntimeError on API failure.
    """
    api_key = os.environ.get("NASA_FIRMS_API_KEY")
    if not api_key:
        raise RuntimeError("NASA_FIRMS_API_KEY not set")

    farm_lat = farm_location["lat"]
    farm_lon = farm_location["lon"]
    # Rough bounding box: ~1 degree ≈ 111 km
    deg = radius_km / 111.0
    area = f"{farm_lon - deg},{farm_lat - deg},{farm_lon + deg},{farm_lat + deg}"
    url = FIRMS_API_URL.format(api_key=api_key, area=area)

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        lines = response.text.strip().splitlines()
    except requests.RequestException as exc:
        raise RuntimeError(f"NASA FIRMS fetch failed: {exc}") from exc

    if len(lines) <= 1:
        logger.info("NASA FIRMS: no active fires detected")
        return None

    nearest = None
    min_dist = float("inf")
    # CSV header: latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,...
    for line in lines[1:]:
        try:
            parts = line.split(",")
            lat, lon = float(parts[0]), float(parts[1])
            acq_date = parts[5]
            acq_time = parts[6]
        except (IndexError, ValueError):
            continue
        dist = _haversine_km(farm_lat, farm_lon, lat, lon)
        if dist < min_dist:
            min_dist = dist
            nearest = {
                "name": "Active Fire",
                "distance_km": round(dist, 2),
                "location": {"lat": lat, "lon": lon},
                "detected_at": f"{acq_date}T{acq_time[:2]}:{acq_time[2:]}:00Z",
                "spread_rate_km_per_day": None,
                "direction_degrees": None,
                "current_size_acres": None,
                "current_perimeter": None,
            }

    if nearest and nearest["distance_km"] <= radius_km:
        logger.info("NASA FIRMS: nearest fire %.1f km away", nearest["distance_km"])
        return nearest

    logger.info("NASA FIRMS: no fires within %.0f km", radius_km)
    return None
