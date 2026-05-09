"""WIFIRE Firemap real-time fire spread predictor."""

import logging
import math
import os
import requests

logger = logging.getLogger(__name__)

WIFIRE_API_URL = "https://wifire-data.sdsc.edu/api/v1/firemap"  # placeholder


class WIFIREPredictor:
    def __init__(self, farm_location: dict):
        """Initialize WIFIRE predictor."""
        self.farm_location = farm_location

    def predict_spread(self, fire_data: dict, affected_zones: list) -> dict:
        """Fetch real-time fire spread from WIFIRE Firemap.

        Returns dict with fire_direction, fire_speed_km_per_hour,
        time_to_impact_per_zone, affected_roads, nearest_community.
        Raises RuntimeError on API failure.
        """
        api_key = os.environ.get("WIFIRE_API_KEY")
        if not api_key:
            raise RuntimeError("WIFIRE_API_KEY not set")

        payload = {
            "fire_location": fire_data["location"],
            "fire_perimeter": fire_data.get("current_perimeter"),
            "farm_location": self.farm_location,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        try:
            response = requests.post(WIFIRE_API_URL, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"WIFIRE API call failed: {exc}") from exc

        fire_dir = data.get("fire_direction", fire_data.get("direction_degrees", 225))
        fire_speed = data.get("fire_speed_km_per_hour", 0.5)

        time_to_impact = {}
        for zone in affected_zones:
            zone_center = _zone_center(zone["polygon"])
            dist = _haversine_km(
                fire_data["location"]["lat"], fire_data["location"]["lon"],
                zone_center["lat"], zone_center["lon"]
            )
            hours = round(dist / fire_speed, 1) if fire_speed > 0 else None
            time_to_impact[zone["zone_id"]] = {
                "hours": hours,
                "uncertainty_hours": round(hours * 0.25, 1) if hours else None,
            }

        result = {
            "source": "WIFIRE Firemap",
            "fetched_at": _utc_now(),
            "fire_direction": fire_dir,
            "fire_speed_km_per_hour": fire_speed,
            "fire_spread_probability_next_6h": data.get("spread_probability_6h"),
            "affected_roads": data.get("affected_roads", []),
            "nearest_community": data.get("nearest_community"),
            "time_to_impact_per_zone": time_to_impact,
        }
        logger.info("WIFIRE: fire moving at %.2f km/h, direction %d°", fire_speed, fire_dir)
        return result


def _zone_center(polygon: dict) -> dict:
    coords = polygon["coordinates"][0]
    lats = [c[1] for c in coords]
    lons = [c[0] for c in coords]
    return {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
