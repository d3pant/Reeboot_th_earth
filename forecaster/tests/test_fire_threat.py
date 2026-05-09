"""Test Scenario 2: Active fire threat → CRITICAL status, full wake-up packet."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from forecaster import Forecaster

FARM_CONFIG = Path(__file__).parent.parent / "config" / "farm_config.json"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


def test_fire_threat_scenario():
    forecaster = Forecaster(farm_config_path=FARM_CONFIG)
    status, wake_up_packet = forecaster.run_single_cycle(mock_scenario="fire_threat")

    # Threat level must be WARNING or above
    from forecaster import _threat_index
    assert _threat_index(status["threat_level"]) >= _threat_index("WARNING"), \
        f"Expected WARNING or higher, got {status['threat_level']}"

    assert status["gate_condition_met"] is True, "Gate condition should be met"
    assert wake_up_packet is not None, "Wake-up packet should be created"

    # Check status fields
    assert status["fwi_index"] == 10.0
    assert status["nearest_fire"] is not None
    assert status["nearest_fire"]["distance_km"] == 75.0
    assert status["vegetation_ndvi_anomaly"] == -1.8
    assert status["stage_transition_triggered"] is True

    # Check output files
    status_path = OUTPUT_DIR / "status.json"
    wake_path = OUTPUT_DIR / "wake_up_packet.json"
    assert status_path.exists()
    assert wake_path.exists()

    with open(status_path) as f:
        written_status = json.load(f)
    assert written_status["gate_condition_met"] is True

    with open(wake_path) as f:
        packet = json.load(f)

    # Wake-up packet structure
    assert "activation_timestamp" in packet
    assert "farm_id" in packet
    assert "threat_level" in packet
    assert "affected_zones" in packet
    assert "fire_data" in packet
    assert "wifire_predictions" in packet
    assert "pyrecast_predictions" in packet
    assert "messages_to_agents" in packet

    # Zone impacts
    assert len(packet["affected_zones"]) == 2, "Both zones should have impact data"
    for zone in packet["affected_zones"]:
        assert "time_to_impact_hours" in zone
        assert "threat_level" in zone

    # Agent messages present
    msgs = packet["messages_to_agents"]
    assert "crop_agent" in msgs
    assert "livestock_agent" in msgs
    assert "erpc" in msgs

    # WIFIRE data present
    wifire = packet["wifire_predictions"]
    assert wifire["fire_direction"] == 225
    assert wifire["fire_speed_km_per_hour"] == 6.5

    # Pyrecast queued
    pyrecast = packet["pyrecast_predictions"]
    assert pyrecast["status"] == "pending"
    assert pyrecast["deferred_uid"] is not None

    print(f"[PASS] test_fire_threat_scenario (threat={status['threat_level']})")


if __name__ == "__main__":
    test_fire_threat_scenario()
    print("All fire-threat tests passed.")
