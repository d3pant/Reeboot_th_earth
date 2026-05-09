"""Open-Meteo weather + soil moisture fetcher. No API key required."""

import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_VARS = [
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "relative_humidity_2m",
    "temperature_2m",
    "soil_moisture_0_to_1cm",
]



def fetch_weather(lat: float, lon: float) -> dict:
    """Fetch current weather + soil moisture for any lat/lon.

    Returns:
        wind_speed_kmh       : float
        wind_direction_deg   : float  (meteorological: 0=N, 90=E, 180=S, 270=W)
        wind_gusts_kmh       : float
        humidity_pct         : float
        temperature_c        : float
        soil_moisture        : float  (m³/m³, 0=bone dry, ~0.4=saturated)
        fetched_at           : str    (ISO 8601 UTC)
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(CURRENT_VARS),
        "wind_speed_unit": "kmh",
        "timezone": "UTC",
    }

    try:
        resp = requests.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Open-Meteo request failed: {exc}") from exc
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Open-Meteo parse error: {exc}") from exc

    current = data["current"]
    result = {
        "wind_speed_kmh":     current["wind_speed_10m"],
        "wind_direction_deg": current["wind_direction_10m"],
        "wind_gusts_kmh":     current["wind_gusts_10m"],
        "humidity_pct":       current["relative_humidity_2m"],
        "temperature_c":      current["temperature_2m"],
        "soil_moisture":      current["soil_moisture_0_to_1cm"],
        "fwi":                None,
        "fetched_at":         current["time"] + "Z",
    }

    logger.info(
        "Open-Meteo @ (%.4f, %.4f): wind %.1f km/h @ %d°, soil moisture %.3f",
        lat, lon,
        result["wind_speed_kmh"],
        result["wind_direction_deg"],
        result["soil_moisture"],
    )
    return result
