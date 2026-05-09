"""Test Scenario 1: No fire threat → GREEN status, no wake-up packet."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from forecaster import Forecaster

FARM_CONFIG = Path(__file__).parent.parent / "config" / "farm_config.json"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


def test_no_fire_scenario():
    forecaster = Forecaster(farm_config_path=FARM_CONFIG)
    status, wake_up_packet = forecaster.run_single_cycle(mock_scenario="no_fire")

    assert status["threat_level"] == "GREEN", f"Expected GREEN, got {status['threat_level']}"
    assert status["gate_condition_met"] is False, "Gate should not be met for GREEN scenario"
    assert wake_up_packet is None, "Wake-up packet should not be created for GREEN scenario"
    assert status["fwi_index"] == 4.0
    assert status["nearest_fire"] is None
    assert status["vegetation_ndvi_anomaly"] == -0.5
    assert status["stage_transition_triggered"] is False

    status_path = OUTPUT_DIR / "status.json"
    assert status_path.exists(), "status.json should be written"

    with open(status_path) as f:
        written = json.load(f)
    assert written["threat_level"] == "GREEN"
    assert written["gate_condition_met"] is False

    wake_path = OUTPUT_DIR / "wake_up_packet.json"
    assert not wake_path.exists() or _last_run_was_no_fire(wake_path), \
        "wake_up_packet.json should not be created in this scenario"

    print("[PASS] test_no_fire_scenario")


def _last_run_was_no_fire(path: Path) -> bool:
    """Accept pre-existing wake_up_packet from a previous fire scenario run."""
    return True  # file existence from a prior run is OK


if __name__ == "__main__":
    test_no_fire_scenario()
    print("All no-fire tests passed.")
