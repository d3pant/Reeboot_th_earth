"""Unit tests for gate condition evaluation logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from forecaster import (
    _fwi_threat,
    _fire_distance_threat,
    _ndvi_threat,
    _convergence_met,
    _escalate,
    _max_threat,
    _threat_index,
    evaluate_gate_condition,
)
import json

FARM_CONFIG_PATH = Path(__file__).parent.parent / "config" / "farm_config.json"

with open(FARM_CONFIG_PATH) as f:
    FARM_CONFIG = json.load(f)


def test_fwi_thresholds():
    assert _fwi_threat(5.9) == "GREEN"
    assert _fwi_threat(6.0) == "WATCH"
    assert _fwi_threat(9.0) == "WARNING"
    assert _fwi_threat(12.0) == "CRITICAL"
    assert _fwi_threat(20.0) == "CRITICAL"
    print("[PASS] test_fwi_thresholds")


def test_fire_distance_thresholds():
    assert _fire_distance_threat(None) == "GREEN"
    assert _fire_distance_threat(250) == "GREEN"
    assert _fire_distance_threat(200) == "WATCH"
    assert _fire_distance_threat(150) == "WATCH"
    assert _fire_distance_threat(100) == "WARNING"
    assert _fire_distance_threat(75) == "WARNING"
    assert _fire_distance_threat(50) == "CRITICAL"
    assert _fire_distance_threat(10) == "CRITICAL"
    print("[PASS] test_fire_distance_thresholds")


def test_ndvi_thresholds():
    assert _ndvi_threat(-0.5) == "GREEN"
    assert _ndvi_threat(-1.0) == "WATCH"   # boundary: WATCH starts at ≤ -1σ
    assert _ndvi_threat(-1.1) == "WATCH"
    assert _ndvi_threat(-1.5) == "WARNING"
    assert _ndvi_threat(-2.0) == "CRITICAL"
    assert _ndvi_threat(-2.5) == "CRITICAL"
    print("[PASS] test_ndvi_thresholds")


def test_convergence():
    # All three conditions met
    assert _convergence_met(fwi=8.0, distance_km=100.0, ndvi=-1.2) is True
    # FWI too low
    assert _convergence_met(fwi=6.0, distance_km=100.0, ndvi=-1.2) is False
    # Fire too far
    assert _convergence_met(fwi=8.0, distance_km=200.0, ndvi=-1.2) is False
    # NDVI not stressed
    assert _convergence_met(fwi=8.0, distance_km=100.0, ndvi=-0.8) is False
    # No fire
    assert _convergence_met(fwi=8.0, distance_km=None, ndvi=-1.2) is False
    print("[PASS] test_convergence")


def test_escalation():
    assert _escalate("GREEN") == "WATCH"
    assert _escalate("WATCH") == "WARNING"
    assert _escalate("WARNING") == "CRITICAL"
    assert _escalate("CRITICAL") == "EMERGENCY"
    assert _escalate("EMERGENCY") == "EMERGENCY"
    print("[PASS] test_escalation")


def test_max_threat():
    assert _max_threat("GREEN", "WATCH") == "WATCH"
    assert _max_threat("CRITICAL", "GREEN") == "CRITICAL"
    assert _max_threat("WARNING", "WARNING") == "WARNING"
    print("[PASS] test_max_threat")


def test_gate_condition_green():
    result = evaluate_gate_condition(
        fwi=4.0,  # FWI < 6 → GREEN; 7 would be WATCH
        fire=None,
        ndvi=-0.5,
        farm_config=FARM_CONFIG,
    )
    assert result["threat_level"] == "GREEN"
    assert result["gate_condition_met"] is False
    print("[PASS] test_gate_condition_green")


def test_gate_condition_critical_hard_floor_fwi():
    result = evaluate_gate_condition(
        fwi=13.0,  # above hard floor (12)
        fire=None,
        ndvi=-0.5,
        farm_config=FARM_CONFIG,
    )
    assert result["threat_level"] == "CRITICAL"
    assert result["gate_condition_met"] is True
    assert result["hard_floor_hit"] is True
    print("[PASS] test_gate_condition_critical_hard_floor_fwi")


def test_gate_condition_critical_hard_floor_fire():
    result = evaluate_gate_condition(
        fwi=5.0,
        fire={"distance_km": 60.0, "location": {"lat": 34.0, "lon": -118.0}},
        ndvi=-0.5,
        farm_config=FARM_CONFIG,
    )
    assert result["threat_level"] == "CRITICAL"
    assert result["gate_condition_met"] is True
    assert result["hard_floor_hit"] is True
    print("[PASS] test_gate_condition_critical_hard_floor_fire")


def test_gate_condition_convergence_escalation():
    # FWI at WATCH (8), fire at WATCH (120 km), NDVI at WATCH (-1.2) → convergence escalates
    result = evaluate_gate_condition(
        fwi=8.0,
        fire={"distance_km": 120.0, "location": {"lat": 34.0, "lon": -118.0}},
        ndvi=-1.2,
        farm_config=FARM_CONFIG,
    )
    # Without escalation: WATCH. With convergence: should escalate to WARNING → gate met
    assert result["convergence"] is True
    assert _threat_index(result["threat_level"]) >= _threat_index("WARNING")
    assert result["gate_condition_met"] is True
    print("[PASS] test_gate_condition_convergence_escalation")


def test_gate_condition_fire_threat_scenario():
    """Matches PRD Scenario 2: FWI=10, fire=75km, NDVI=-1.8."""
    result = evaluate_gate_condition(
        fwi=10.0,
        fire={"distance_km": 75.0, "location": {"lat": 34.12, "lon": -118.56}},
        ndvi=-1.8,
        farm_config=FARM_CONFIG,
    )
    # Fire at 75km hits hard floor → CRITICAL
    assert result["threat_level"] == "CRITICAL"
    assert result["gate_condition_met"] is True
    assert result["hard_floor_hit"] is True
    print("[PASS] test_gate_condition_fire_threat_scenario")


if __name__ == "__main__":
    test_fwi_thresholds()
    test_fire_distance_thresholds()
    test_ndvi_thresholds()
    test_convergence()
    test_escalation()
    test_max_threat()
    test_gate_condition_green()
    test_gate_condition_critical_hard_floor_fwi()
    test_gate_condition_critical_hard_floor_fire()
    test_gate_condition_convergence_escalation()
    test_gate_condition_fire_threat_scenario()
    print("\nAll gate logic tests passed.")
