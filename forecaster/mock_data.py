"""Mock data for testing without real API credentials."""

# --- Scenario: NO_FIRE (GREEN) ---
NO_FIRE = {
    "fwi": 4.0,
    "fire": None,
    "ndvi_anomaly": -0.5,
    "weather": {
        "wind_speed_kmh": 12,
        "wind_direction_degrees": 180,
        "wind_gusts_kmh": 18,
        "temperature_c": 24,
        "humidity_percent": 45,
    },
}

# --- Scenario: FIRE_THREAT (CRITICAL) ---
FIRE_THREAT = {
    "fwi": 10.0,
    "fire": {
        "name": "Palisades Fire",
        "distance_km": 75.0,
        "location": {"lat": 33.72, "lon": -117.66},
        "detected_at": "2026-05-08T06:00:00Z",
        "current_size_acres": 15000,
        "spread_rate_km_per_day": 156.0,
        "direction_degrees": 225,
        "current_perimeter": {
            "type": "Polygon",
            "coordinates": [[[-118.58, 34.13], [-118.56, 34.13], [-118.56, 34.11], [-118.58, 34.11], [-118.58, 34.13]]]
        },
    },
    "ndvi_anomaly": -1.8,
    "weather": {
        "wind_speed_kmh": 35,
        "wind_direction_degrees": 225,
        "wind_gusts_kmh": 48,
        "temperature_c": 32,
        "humidity_percent": 22,
    },
}

# --- WIFIRE mock spread prediction ---
WIFIRE_SPREAD = {
    "source": "WIFIRE Firemap (mock)",
    "fire_direction": 225,
    "fire_speed_km_per_hour": 6.5,
    "fire_spread_probability_next_6h": 0.89,
    "affected_roads": ["Highway 27", "Topanga Canyon Blvd"],
    "nearest_community": {"name": "Brentwood Heights", "distance_km": 3},
}

# --- Pyrecast mock async response ---
PYRECAST_QUEUED = {
    "source": "Pyrecast API (mock)",
    "status": "pending",
    "deferred_uid": "mock_abc123def456",
    "expected_completion": "2026-05-08T14:35:00Z",
    "fourteen_day_forecast": "will be populated when Pyrecast returns results",
}
