"""NASA NDVI vegetation stress data source."""

import logging
import os
import requests

logger = logging.getLogger(__name__)

# NASA POWER / AppEEARS NDVI endpoints (placeholders)
NDVI_CURRENT_URL = "https://appeears.earthdatacloud.nasa.gov/api/point"
NDVI_BASELINE_URL = "https://appeears.earthdatacloud.nasa.gov/api/point/baseline"


def fetch_ndvi_anomaly(farm_location: dict) -> float:
    """Fetch NDVI anomaly z-score for the farm location.

    Positive z-score = wetter/greener than normal.
    Negative z-score = drier/more stressed.
    Raises RuntimeError on API failure.
    """
    api_key = os.environ.get("NASA_FIRMS_API_KEY")
    if not api_key:
        raise RuntimeError("NASA_FIRMS_API_KEY not set")

    lat = farm_location["lat"]
    lon = farm_location["lon"]
    params = {"lat": lat, "lon": lon, "product": "MOD13Q1", "layer": "250m_16_days_NDVI"}
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        current_resp = requests.get(NDVI_CURRENT_URL, params=params, headers=headers, timeout=15)
        current_resp.raise_for_status()
        current_data = current_resp.json()
        current_ndvi = float(current_data["ndvi"])

        baseline_resp = requests.get(NDVI_BASELINE_URL, params=params, headers=headers, timeout=15)
        baseline_resp.raise_for_status()
        baseline_data = baseline_resp.json()
        mean_ndvi = float(baseline_data["mean"])
        std_ndvi = float(baseline_data["std"])
    except requests.RequestException as exc:
        raise RuntimeError(f"NASA NDVI fetch failed: {exc}") from exc
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"NASA NDVI parse error: {exc}") from exc

    if std_ndvi == 0:
        raise RuntimeError("NDVI baseline std is zero; cannot compute anomaly")

    anomaly = (current_ndvi - mean_ndvi) / std_ndvi
    logger.info("NASA NDVI: anomaly z-score = %.2f", anomaly)
    return round(anomaly, 3)
