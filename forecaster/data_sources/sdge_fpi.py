"""SDG&E Fire Potential Index data source."""

import logging
import os
import requests

logger = logging.getLogger(__name__)

SDGE_FPI_ENDPOINT = "https://api.sdge.com/fpi/v1/current"  # placeholder


def fetch_fwi() -> float:
    """Fetch current Fire Weather Index from SDG&E FPI.

    Returns FWI float (0-180 scale).
    Raises RuntimeError if the API call fails.
    """
    api_key = os.environ.get("SDGE_FPI_API_KEY")
    if not api_key:
        raise RuntimeError("SDGE_FPI_API_KEY not set")

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(SDGE_FPI_ENDPOINT, headers=headers, timeout=10)  # noqa: S113
        response.raise_for_status()
        data = response.json()
        fwi = float(data["fwi"])
        logger.info("SDG&E FPI: FWI = %.1f", fwi)
        return fwi
    except requests.RequestException as exc:
        raise RuntimeError(f"SDG&E FPI fetch failed: {exc}") from exc
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"SDG&E FPI parse error: {exc}") from exc
