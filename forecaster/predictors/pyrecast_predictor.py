"""Pyrecast API async fire spread predictor."""

import logging
import os
import requests

logger = logging.getLogger(__name__)

PYRECAST_BASE_URL = "https://pyrecast.org/api/v1"  # placeholder


class PyrecastPredictor:
    def __init__(self):
        """Initialize Pyrecast predictor. Reads PYRECAST_API_KEY from environment."""
        api_key = os.environ.get("PYRECAST_API_KEY")
        if not api_key:
            raise RuntimeError("PYRECAST_API_KEY not set")
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def predict_spread_async(self, fire_perimeter: dict, simulation_hours: int = 24, ensemble_members: int = 200) -> dict:
        """Trigger async Pyrecast simulation.

        Returns dict with status, deferred_uid, request_sent_at, expected_completion.
        Raises RuntimeError on submission failure.
        """
        payload = {
            "fire_perimeter": fire_perimeter,
            "simulation_hours": simulation_hours,
            "ensemble_members": ensemble_members,
        }
        try:
            response = requests.post(
                f"{PYRECAST_BASE_URL}/simulate",
                json=payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Pyrecast submission failed: {exc}") from exc

        result = {
            "source": "Pyrecast API",
            "request_sent_at": _utc_now(),
            "status": "pending",
            "deferred_uid": data.get("uid"),
            "expected_completion": data.get("expected_completion"),
            "fourteen_day_forecast": "will be populated when Pyrecast returns results",
        }
        logger.info("Pyrecast: simulation queued, uid=%s", result["deferred_uid"])
        return result

    def poll_results(self, deferred_uid: str) -> dict:
        """Poll Pyrecast for simulation results.

        Returns dict with status and results (if ready).
        Raises RuntimeError on API failure.
        """
        try:
            response = requests.get(
                f"{PYRECAST_BASE_URL}/simulate/{deferred_uid}",
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Pyrecast poll failed: {exc}") from exc

        logger.info("Pyrecast: uid=%s status=%s", deferred_uid, data.get("status"))
        return data


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
