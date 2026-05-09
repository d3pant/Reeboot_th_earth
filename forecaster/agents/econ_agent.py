"""Econ Agent — part of the Economic Resilience & Policy Coordinator (ERPC).

Runs during Stage 2 (fire threat active). Each monitoring cycle:
  1. Computes total financial exposure: crop loss, livestock at risk, opportunity cost.
  2. Ranks all available response actions by ROI.
  3. Writes output/econ_report.json for the farmer dashboard.

Usage:
    python econ_agent.py [--dry-run]

Live data sources (fall back to mock/hardcoded if unavailable):
  - Crop data: crop_agent/crop_agent_output_*.json (latest file)
  - Livestock data: Livestock/erpc_message.json

All cost constants are in COST_ASSUMPTIONS. See ECON_AGENT_PLAN.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("econ_agent")

OUTPUT_DIR = Path(__file__).parent.parent / "output"
CONFIG_DIR = Path(__file__).parent.parent / "config"
STATUS_JSON = OUTPUT_DIR / "status.json"

REPO_ROOT = Path(__file__).parent.parent.parent
CROP_AGENT_DIR = REPO_ROOT / "crop_agent"
LIVESTOCK_ERPC_MSG = REPO_ROOT / "Livestock" / "erpc_message.json"
FARM_FIELDS_JSON = CROP_AGENT_DIR / "farm_fields.json"


def _valid_field_ids() -> set[str]:
    """Return the set of field_ids actually entered by the farmer."""
    try:
        with open(FARM_FIELDS_JSON) as f:
            data = json.load(f)
        return {fld["field_id"] for fld in data.get("fields", [])}
    except Exception:
        return set()


def _filter_to_valid_fields(data: dict, valid_ids: set[str]) -> dict:
    """Strip any field_id not in valid_ids from all crop task lists."""
    if not valid_ids:
        return data
    for key in ("task4", "task1", "task3"):
        if isinstance(data.get(key), list):
            data[key] = [r for r in data[key] if r.get("field_id") in valid_ids]
    t2 = data.get("task2")
    if isinstance(t2, dict) and "crop_destructions" in t2:
        t2["crop_destructions"] = [
            r for r in t2["crop_destructions"] if r.get("field_id") in valid_ids
        ]
    return data

# ---------------------------------------------------------------------------
# Hardcoded cost assumptions
# All values here are placeholders. See ECON_AGENT_PLAN.md — "Hardcoded Values"
# table for what each should be replaced with and which source provides it.
# ---------------------------------------------------------------------------

COST_ASSUMPTIONS = {
    "harvest_labor_rate_usd_per_hour": 25.0,
    "harvest_hours_per_acre": 4.0,
    "firebreak_cost_usd_per_acre": 150.0,
    "livestock_transport_cost_usd_per_head": 35.0,
    "livestock_value_per_head_usd": 1500.0,
    "transplant_seedling_value_usd_per_acre": 800.0,
    "opportunity_cost_seasons": 1,
}

# Hardcoded livestock stub — replace with Livestock Agent output.
# total_head matches farm_config.json zones (250 + 500).
HARDCODED_LIVESTOCK = {
    "total_head": 750,
    "value_per_head_usd": COST_ASSUMPTIONS["livestock_value_per_head_usd"],
    "evacuated_pct": 0.0,
}

def _build_fallback_crop_data() -> dict:
    """Build minimal crop data from farm_fields.json when crop agent hasn't run yet.
    Uses only what the farmer actually entered — no hardcoded field IDs or crop types."""
    try:
        with open(FARM_FIELDS_JSON) as f:
            farm = json.load(f)
        fields = farm.get("fields", [])
    except Exception:
        fields = []

    task4, task1, task3, crop_destructions = [], [], [], []
    for i, fld in enumerate(fields):
        fid = fld["field_id"]
        crop = fld.get("crop_category") or fld.get("crop", "unknown")
        acres = float(fld.get("size_acres") or fld.get("acres") or 0)
        hours = 24.0 + i * 2  # placeholder spread time, evenly spaced
        task4.append({
            "field_id": fid, "crop_category": crop,
            "maturity_pct": 80, "fire_arrival_hours": hours,
            "decision": "HARVEST NOW",
            "reason": "Crop agent not yet run — defaulting to harvest recommendation",
            "enters_task1": True,
        })
        task1.append({
            "field_id": fid, "flammability": 2, "fuel_load": 30,
            "wind_factor": 1.0, "priority_score": 30, "rank": i + 1,
            "action": "MONITOR",
            "uprooting_strategy": {"transplantable": False, "labor_hours_needed": 0,
                                   "method": "Awaiting crop agent analysis.", "time_window": hours},
            "feasible_with_farm_resources": False,
        })
        task3.append({
            "field_id": fid, "intensity_score": 10.0,
            "hours_to_arrival": hours, "technique": "DRIP IRRIGATION",
            "urgency": "MONITOR", "reason": "Default — run full pipeline for live analysis",
        })
        crop_destructions.append({
            "field_id": fid, "crop_category": crop, "size_acres": acres,
            "price_per_acre_usd": 0.0, "usda_report_date": "pending",
            "estimated_loss_usd": 0.0, "confidence_adjusted_loss_usd": 0.0,
            "economic_impact_score": 0, "task4_decision": "HARVEST NOW",
            "reason": "Price lookup pending — run full pipeline",
        })

    return {
        "task4": task4,
        "task1": task1,
        "task2": {
            "generated_at": "pending",
            "threat_level": "UNKNOWN",
            "price_source": "pending",
            "crop_destructions": crop_destructions,
            "total_estimated_loss_usd": 0.0,
            "total_confidence_adjusted_loss_usd": 0.0,
        },
        "task3": task3,
    }

# ---------------------------------------------------------------------------
# Live data loaders
# ---------------------------------------------------------------------------

def _load_crop_data() -> tuple[dict, str]:
    """Load crop agent output. Returns (data, source) where source is 'live' or 'mock'."""
    candidates = (
        list(CROP_AGENT_DIR.glob("output_*.json"))
        + list(CROP_AGENT_DIR.glob("crop_agent_output_*.json"))
    )
    candidates = [p for p in candidates if "raw" not in p.name and "erpc" not in p.name]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        try:
            with open(candidates[0]) as f:
                raw = json.load(f)
            # Accept both raw task1/2/3/4 keys (on-disk) and normalized descriptive keys
            data = {
                "task4": raw.get("field_decisions") or raw.get("task4", []),
                "task1": raw.get("fire_reduction") or raw.get("task1", []),
                "task2": raw.get("economic_impact") or raw.get("task2", {}),
                "task3": raw.get("hydration_strategy") or raw.get("task3", []),
            }
            # Treat as live whenever the file parsed and has the expected schema —
            # an empty crop_destructions list is a valid "no crops at risk" answer.
            if isinstance(data["task2"], dict) and "crop_destructions" in data["task2"]:
                data = _filter_to_valid_fields(data, _valid_field_ids())
                logger.info("Loaded crop data from %s", candidates[0].name)
                return data, "live"
        except Exception as e:
            logger.warning("Failed to load %s: %s", candidates[0].name, e)
    logger.warning("No crop agent output found — building fallback from farm_fields.json")
    return _build_fallback_crop_data(), "fallback"


def _load_livestock_data() -> tuple[dict, str]:
    """Load livestock agent output. Returns (data, source)."""
    try:
        with open(LIVESTOCK_ERPC_MSG) as f:
            msg = json.load(f)
        total = msg.get("cost_optimization", {}).get("total_animals_at_risk", 0)
        at_risk_value = msg.get("animal_valuation_at_risk", 0)
        transport_cost = msg.get("transport_costs_usd", 0)
        if total > 0:
            return {
                "total_head": total,
                "value_per_head_usd": round(at_risk_value / total, 2),
                "evacuated_pct": 0.0,
                "transport_cost_usd": transport_cost,
            }, "live"
    except Exception as e:
        logger.warning("Failed to load livestock erpc_message.json: %s", e)
    logger.warning("Using HARDCODED_LIVESTOCK")
    return {**HARDCODED_LIVESTOCK, "transport_cost_usd": None}, "hardcoded_stub"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class EconAction:
    action_id: str
    action_type: str          # HARVEST_NOW | PARTIAL_HARVEST | TRANSPLANT | FIREBREAK | EVACUATE_LIVESTOCK
    field_id: Optional[str]
    crop_category: Optional[str]
    priority: int             # 1 = highest
    roi: float
    confidence_adjusted_loss_avoided_usd: float
    estimated_action_cost_usd: float
    time_window_hours: Optional[float]
    urgency: str              # IMMEDIATE | HIGH | SCHEDULED
    feasible: bool
    infeasibility_reason: Optional[str]
    action_description: str
    required_resources: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Financial exposure computation
# ---------------------------------------------------------------------------

def _compute_financial_exposure(crop_data: dict, livestock: dict) -> dict:
    task2 = crop_data["task2"]
    task4 = crop_data["task4"]

    # Index task4 by field_id for quick lookup
    t4 = {f["field_id"]: f for f in task4}

    crop_loss_confirmed = 0.0
    crop_loss_recoverable = 0.0
    opportunity_cost = 0.0
    breakdown_by_crop: dict[str, float] = {}

    for destruction in task2["crop_destructions"]:
        fid = destruction["field_id"]
        adj_loss = destruction["confidence_adjusted_loss_usd"]
        decision = destruction.get("task4_decision") or t4.get(fid, {}).get("decision", "ABANDON")
        maturity = t4.get(fid, {}).get("maturity_pct", 100) / 100.0
        size_acres = destruction["size_acres"]
        price_per_acre = destruction["price_per_acre_usd"]
        crop = destruction["crop_category"]

        breakdown_by_crop[crop] = breakdown_by_crop.get(crop, 0.0) + adj_loss

        if decision == "ABANDON":
            crop_loss_confirmed += adj_loss
            # Opportunity cost: 1 full lost season
            opportunity_cost += price_per_acre * size_acres * COST_ASSUMPTIONS["opportunity_cost_seasons"]
        elif decision == "PARTIAL HARVEST":
            # Recoverable portion: what can be salvaged at current maturity
            recoverable = adj_loss * maturity
            confirmed = adj_loss * (1.0 - maturity)
            crop_loss_recoverable += recoverable
            crop_loss_confirmed += confirmed
            # Partial season loss — the unharvested fraction
            opportunity_cost += price_per_acre * size_acres * (1.0 - maturity) * COST_ASSUMPTIONS["opportunity_cost_seasons"]
        elif decision in ("HARVEST NOW",):
            crop_loss_recoverable += adj_loss
        else:
            # TRANSPLANT or unknown — treat as recoverable
            crop_loss_recoverable += adj_loss

    crop_loss_total = crop_loss_confirmed + crop_loss_recoverable

    livestock_at_risk = (
        livestock["total_head"]
        * livestock["value_per_head_usd"]
        * (1.0 - livestock["evacuated_pct"])
    )

    total = crop_loss_total + livestock_at_risk + opportunity_cost

    return {
        "crop_loss_confirmed_usd": round(crop_loss_confirmed, 2),
        "crop_loss_recoverable_usd": round(crop_loss_recoverable, 2),
        "crop_loss_total_usd": round(crop_loss_total, 2),
        "livestock_at_risk_usd": round(livestock_at_risk, 2),
        "opportunity_cost_usd": round(opportunity_cost, 2),
        "total_exposure_usd": round(total, 2),
        "breakdown_by_crop": {k: round(v, 2) for k, v in breakdown_by_crop.items()},
    }


# ---------------------------------------------------------------------------
# ROI action builder
# ---------------------------------------------------------------------------

def _build_actions(crop_data: dict, livestock: dict) -> tuple[list[EconAction], list[EconAction]]:
    task2 = crop_data["task2"]
    task3 = crop_data["task3"]
    task4 = crop_data["task4"]
    task1 = crop_data["task1"]

    t2 = {d["field_id"]: d for d in task2["crop_destructions"]}
    t3 = {f["field_id"]: f for f in task3}
    t4 = {f["field_id"]: f for f in task4}
    t1 = {f["field_id"]: f for f in task1}

    c = COST_ASSUMPTIONS
    feasible: list[EconAction] = []
    infeasible: list[EconAction] = []

    # --- Crop actions from task4 ---
    for field in task4:
        fid = field["field_id"]
        decision = field["decision"]
        maturity = field["maturity_pct"] / 100.0
        arrival_hours = field["fire_arrival_hours"]
        crop = field["crop_category"]

        destruction = t2.get(fid)
        adj_loss = destruction["confidence_adjusted_loss_usd"] if destruction else 0.0
        size_acres = destruction["size_acres"] if destruction else 0.0

        if decision == "ABANDON":
            continue  # no actionable harvest/transplant for abandoned fields

        elif decision in ("HARVEST NOW", "PARTIAL HARVEST"):
            loss_avoided = adj_loss * maturity if decision == "PARTIAL HARVEST" else adj_loss
            # size_acres may be 0 if field not in task2 (HARVEST NOW fields aren't in crop_destructions)
            harvest_hours = c["harvest_hours_per_acre"] * size_acres if size_acres > 0 else None
            action_cost = c["harvest_labor_rate_usd_per_hour"] * harvest_hours if harvest_hours else 0.0
            roi = round(loss_avoided / action_cost, 1) if (action_cost > 0 and loss_avoided > 0) else None
            time_ok = (harvest_hours is None) or (arrival_hours >= harvest_hours)

            action_type = "HARVEST_NOW" if decision == "HARVEST NOW" else "PARTIAL_HARVEST"
            if loss_avoided > 0:
                desc = f"Harvest {fid} {crop} ({field['maturity_pct']}% mature) — saves ${loss_avoided:,.0f}, {arrival_hours:.1f}h window"
            else:
                desc = f"Harvest {fid} {crop} ({field['maturity_pct']}% mature) — value unknown (not in crop destructions), {arrival_hours:.1f}h window"
            resources = ["harvest crew", "transport truck"]
            t3_entry = t3.get(fid)
            urgency = t3_entry["urgency"] if t3_entry else "SCHEDULED"

            action = EconAction(
                action_id=f"{action_type}_{fid}",
                action_type=action_type,
                field_id=fid,
                crop_category=crop,
                priority=0,
                roi=roi if roi is not None else 0.0,
                confidence_adjusted_loss_avoided_usd=round(loss_avoided, 2),
                estimated_action_cost_usd=round(action_cost, 2),
                time_window_hours=arrival_hours,
                urgency=urgency,
                feasible=time_ok,
                infeasibility_reason=None if time_ok else f"Harvest requires ~{harvest_hours:.1f}h but only {arrival_hours:.1f}h until fire arrival",
                action_description=desc,
                required_resources=resources,
            )
            (feasible if time_ok else infeasible).append(action)

        elif decision == "TRANSPLANT":
            t1_entry = t1.get(fid)
            farm_feasible = t1_entry["feasible_with_farm_resources"] if t1_entry else False
            labor_hours = t1_entry["uprooting_strategy"]["labor_hours_needed"] if t1_entry else 0
            time_window = t1_entry["uprooting_strategy"]["time_window"] if t1_entry else arrival_hours
            equipment = t1_entry["uprooting_strategy"].get("uproot_equipment", []) if t1_entry else []

            seedling_value = c["transplant_seedling_value_usd_per_acre"] * size_acres
            action_cost = c["harvest_labor_rate_usd_per_hour"] * labor_hours
            roi = round(seedling_value / action_cost, 1) if action_cost > 0 else 0.0
            time_ok = time_window >= labor_hours

            reason = None
            if not farm_feasible:
                reason = ("Equipment not available on farm: " + ", ".join(equipment)) if equipment else "Farm resources insufficient for transplant"
            elif not time_ok:
                reason = f"Transplant requires {labor_hours}h but only {time_window:.1f}h window available"

            action = EconAction(
                action_id=f"TRANSPLANT_{fid}",
                action_type="TRANSPLANT",
                field_id=fid,
                crop_category=crop,
                priority=0,
                roi=roi,
                confidence_adjusted_loss_avoided_usd=round(seedling_value, 2),
                estimated_action_cost_usd=round(action_cost, 2),
                time_window_hours=time_window,
                urgency="SCHEDULED",
                feasible=(farm_feasible and time_ok),
                infeasibility_reason=reason,
                action_description=f"Transplant {fid} {crop} ({field['maturity_pct']}% mature) — saves ${seedling_value:,.0f} in seedling value",
                required_resources=equipment,
            )
            (feasible if (farm_feasible and time_ok) else infeasible).append(action)

    # --- Firebreak actions from task3 ---
    for fb in task3:
        fid = fb["field_id"]
        destruction = t2.get(fid)
        if not destruction:
            continue
        adj_loss = destruction["confidence_adjusted_loss_usd"]
        size_acres = destruction["size_acres"]
        action_cost = c["firebreak_cost_usd_per_acre"] * size_acres
        roi = round(adj_loss / action_cost, 1) if action_cost > 0 else 0.0
        urgency = fb["urgency"]

        feasible.append(EconAction(
            action_id=f"FIREBREAK_{fid}",
            action_type="FIREBREAK",
            field_id=fid,
            crop_category=destruction["crop_category"],
            priority=0,
            roi=roi,
            confidence_adjusted_loss_avoided_usd=round(adj_loss, 2),
            estimated_action_cost_usd=round(action_cost, 2),
            time_window_hours=fb["hours_to_arrival"],
            urgency=urgency,
            feasible=True,
            infeasibility_reason=None,
            action_description=f"{fb['technique']} on {fid} {destruction['crop_category']} — protects ${adj_loss:,.0f} ({urgency})",
            required_resources=["water tanker", "irrigation equipment"],
        ))

    # --- Livestock evacuation ---
    livestock_at_risk = (
        livestock["total_head"]
        * livestock["value_per_head_usd"]
        * (1.0 - livestock["evacuated_pct"])
    )
    if livestock_at_risk > 0:
        # Use actual transport cost from Livestock Agent if available, else estimate
        transport_cost = livestock.get("transport_cost_usd") or (
            c["livestock_transport_cost_usd_per_head"] * livestock["total_head"]
        )
        roi = round(livestock_at_risk / transport_cost, 1) if transport_cost > 0 else 0.0
        feasible.append(EconAction(
            action_id="EVACUATE_LIVESTOCK",
            action_type="EVACUATE_LIVESTOCK",
            field_id=None,
            crop_category=None,
            priority=0,
            roi=roi,
            confidence_adjusted_loss_avoided_usd=round(livestock_at_risk, 2),
            estimated_action_cost_usd=round(transport_cost, 2),
            time_window_hours=None,
            urgency="HIGH",
            feasible=True,
            infeasibility_reason=None,
            action_description=f"Evacuate {livestock['total_head']} head ({int(livestock['evacuated_pct']*100)}% already moved) — protects ${livestock_at_risk:,.0f}",
            required_resources=["livestock trailers", "transport crew", "receiving site"],
        ))

    # --- Sort and assign priority ---
    # IMMEDIATE urgency from firebreaks/crop agent jumps to top regardless of ROI
    immediate = [a for a in feasible if a.urgency == "IMMEDIATE"]
    rest = sorted([a for a in feasible if a.urgency != "IMMEDIATE"], key=lambda a: a.roi, reverse=True)
    ordered = immediate + rest
    for i, action in enumerate(ordered):
        action.priority = i + 1

    return ordered, infeasible


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class EconAgent:
    def __init__(self, farm_config_path: str | Path, status_path: str | Path = STATUS_JSON):
        with open(farm_config_path) as f:
            self.farm_config = json.load(f)
        self.status_path = Path(status_path)
        self.report: dict = {}

    def _load_threat_level(self) -> str:
        try:
            with open(self.status_path) as f:
                return json.load(f).get("threat_level", "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def run(self, crop_data: Optional[dict] = None, livestock: Optional[dict] = None) -> dict:
        """Run the full econ pipeline. Loads live data unless overridden."""
        crop_source = "override"
        livestock_source = "override"

        if crop_data is None:
            crop_data, crop_source = _load_crop_data()
        if livestock is None:
            livestock, livestock_source = _load_livestock_data()

        threat_level = self._load_threat_level()
        exposure = _compute_financial_exposure(crop_data, livestock)
        action_queue, infeasible_actions = _build_actions(crop_data, livestock)

        self.report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "farm_id": self.farm_config["farm_id"],
            "threat_level": threat_level,
            "financial_exposure": exposure,
            "cost_assumptions_used": COST_ASSUMPTIONS,
            "action_queue": [a.to_dict() for a in action_queue],
            "infeasible_actions": [a.to_dict() for a in infeasible_actions],
            "data_sources": {
                "crop_agent": crop_source,
                "livestock_agent": livestock_source,
            },
        }

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / "econ_report.json"
        with open(out_path, "w") as f:
            json.dump(self.report, f, indent=2, default=str)
        logger.info("Wrote %s", out_path)
        return self.report

    def print_summary(self) -> None:
        e = self.report.get("financial_exposure", {})
        print("\n--- ECON REPORT SUMMARY ---")
        print(f"  Threat level          : {self.report.get('threat_level')}")
        print(f"  Total exposure        : ${e.get('total_exposure_usd', 0):>12,.2f}")
        print(f"    Crop (confirmed)    : ${e.get('crop_loss_confirmed_usd', 0):>12,.2f}")
        print(f"    Crop (recoverable)  : ${e.get('crop_loss_recoverable_usd', 0):>12,.2f}")
        print(f"    Livestock at risk   : ${e.get('livestock_at_risk_usd', 0):>12,.2f}")
        print(f"    Opportunity cost    : ${e.get('opportunity_cost_usd', 0):>12,.2f}")
        if e.get("breakdown_by_crop"):
            print(f"\n  Crop breakdown:")
            for crop, val in e["breakdown_by_crop"].items():
                print(f"    {crop:<20} ${val:>12,.2f}")
        print(f"\n  Action queue ({len(self.report.get('action_queue', []))} actions):")
        for a in self.report.get("action_queue", []):
            roi_str = f"{a['roi']:5.1f}x" if a["confidence_adjusted_loss_avoided_usd"] > 0 else "  N/Ax"
            print(
                f"  [{a['priority']:2}] ROI {roi_str}  [{a['urgency']:<10}]  "
                f"{a['action_description']}"
            )
        if self.report.get("infeasible_actions"):
            print(f"\n  Infeasible ({len(self.report['infeasible_actions'])} actions):")
            for a in self.report["infeasible_actions"]:
                print(f"       BLOCKED  {a['action_id']}: {a['infeasibility_reason']}")
        print(f"\n  output/econ_report.json written.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Econ Agent — financial exposure and ROI action ranking")
    parser.add_argument("--dry-run", action="store_true", help="Use mock crop data, skip status.json requirement")
    parser.add_argument("--status", default=str(STATUS_JSON), help="Path to forecaster status.json")
    args = parser.parse_args()

    farm_config_path = CONFIG_DIR / "farm_config.json"
    agent = EconAgent(farm_config_path=farm_config_path, status_path=args.status)
    agent.run()
    agent.print_summary()


if __name__ == "__main__":
    main()
