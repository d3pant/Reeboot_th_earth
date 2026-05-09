"""Weather data source (NOAA / SDG&E)."""

import logging
import requests

logger = logging.getLogger(__name__)

NOAA_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"


def fetch_weather(farm_location: dict) -> dict:
    """Fetch current weather conditions for the farm location.

    Returns dict with: wind_speed_kmh, wind_direction_degrees, wind_gusts_kmh,
    temperature_c, humidity_percent, forecast_valid_until.
    Raises RuntimeError on failure.
    """
    lat = farm_location["lat"]
    lon = farm_location["lon"]
    headers = {"User-Agent": "WildfireForecasterAgent/1.0 (contact@example.com)"}

    try:
        points_resp = requests.get(NOAA_POINTS_URL.format(lat=lat, lon=lon), headers=headers, timeout=10)
        points_resp.raise_for_status()
        points_data = points_resp.json()
        forecast_url = points_data["properties"]["forecastHourly"]

        forecast_resp = requests.get(forecast_url, headers=headers, timeout=10)
        forecast_resp.raise_for_status()
        forecast_data = forecast_resp.json()
        period = forecast_data["properties"]["periods"][0]

        wind_speed_kmh = round(period["windSpeed"].split(" ")[0] * 1.60934, 1) if "mph" in period["windSpeed"] else float(period["windSpeed"].split(" ")[0])
        temperature_c = round((period["temperature"] - 32) * 5 / 9, 1) if period["temperatureUnit"] == "F" else float(period["temperature"])

        result = {
            "wind_speed_kmh": wind_speed_kmh,
            "wind_direction_degrees": _compass_to_degrees(period.get("windDirection", "N")),
            "wind_gusts_kmh": None,
            "temperature_c": temperature_c,
            "humidity_percent": period.get("relativeHumidity", {}).get("value"),
            "forecast_valid_until": period.get("endTime"),
        }
        logger.info("Weather: wind %.0f km/h, temp %.1f°C, humidity %s%%", result["wind_speed_kmh"], result["temperature_c"], result["humidity_percent"])
        return result

    except requests.RequestException as exc:
        raise RuntimeError(f"NOAA weather fetch failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"NOAA weather parse error: {exc}") from exc


def _compass_to_degrees(direction: str) -> int:
    mapping = {
        "N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90,
        "ESE": 112, "SE": 135, "SSE": 157, "S": 180,
        "SSW": 202, "SW": 225, "WSW": 247, "W": 270,
        "WNW": 292, "NW": 315, "NNW": 337,
    }
    return mapping.get(direction.upper(), 0)
