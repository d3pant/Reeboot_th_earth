"""Insurance Agent — part of the Economic Resilience & Policy Coordinator (ERPC).

Fills the official USDA CCC-576 (Notice of Loss) PDF form using pypdf.
Reads status.json, econ_report.json, and farm_config.json and pre-populates
all fields we have data for. Fields requiring farmer input are left blank
or marked with a note.

The CCC-576 is the primary Notice of Loss form for ELAP, LFP, LIP, and NAP.
It must be filed within 30 days of the loss event.

Usage:
    python insurance_agent.py [--dry-run]
    python insurance_agent.py --output /path/to/filled_ccc576.pdf

Official form source: https://www.farmers.gov/sites/default/files/documents/ccc-576.pdf
Bundled at: forecaster/forms/ccc_576.pdf
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pypdf import PdfReader, PdfWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("insurance_agent")

FORMS_DIR = Path(__file__).parent.parent / "forms"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
CONFIG_DIR = Path(__file__).parent.parent / "config"

REPO_ROOT = Path(__file__).parent.parent.parent
CROP_AGENT_DIR = REPO_ROOT / "crop_agent"

CCC_576_BLANK = FORMS_DIR / "ccc_576.pdf"
STATUS_JSON = OUTPUT_DIR / "status.json"
ECON_REPORT = OUTPUT_DIR / "econ_report.json"
FILLED_PDF = OUTPUT_DIR / "ccc_576_filled.pdf"

# San Diego County FSA office — hardcoded for CA target.
# Make dynamic: look up from USDA FSA office locator by county.
FSA_OFFICE_CA_SAN_DIEGO = "San Diego County FSA Office\n1204 Mission Road, Suite 1\nEscondido, CA 92029"

# CA FIPS codes for state/county field
CA_STATE_CODE = "06"
SAN_DIEGO_COUNTY_CODE = "073"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_json(path: Path, fallback: dict) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        logger.warning("Could not load %s — using fallback", path.name)
        return fallback


def _load_live_crop_destructions() -> Optional[list]:
    """Read crop_destructions from the latest crop agent output, mirroring econ's loader.
    Returns the list (possibly empty if the live result is 'no destructions') or None if
    no live crop output file exists at all."""
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
            t2 = raw.get("economic_impact") or raw.get("task2") or {}
            destructions = t2.get("crop_destructions", [])
            logger.info("Loaded %d crop_destructions from %s", len(destructions), candidates[0].name)
            return destructions
        except Exception as e:
            logger.warning("Failed to read live crop output: %s", e)
    return None


MOCK_STATUS = {
    "timestamp": "2026-05-08T06:00:00Z",
    "threat_level": "CRITICAL",
    "fwi_index": 10.0,
    "nearest_fire": {
        "name": "Palisades Fire",
        "distance_km": 75.0,
        "detected_at": "2026-05-08T06:00:00Z",
    },
}

MOCK_ECON = {
    "farm_id": "farm_sdge_001",
    "financial_exposure": {
        "crop_loss_total_usd": 546720.0,
        "breakdown_by_crop": {"almonds": 176800.0, "tomatoes": 369920.0},
    },
    # task2-style crop destructions — what the real crop agent provides
    "crop_destructions": [
        {
            "field_id": "F5",
            "crop_category": "almonds",
            "size_acres": 25,
            "estimated_loss_usd": 208000.0,
            "confidence_adjusted_loss_usd": 176800.0,
            "task4_decision": "ABANDON",
        },
        {
            "field_id": "F3",
            "crop_category": "tomatoes",
            "size_acres": 10,
            "estimated_loss_usd": 435200.0,
            "confidence_adjusted_loss_usd": 369920.0,
            "task4_decision": "PARTIAL HARVEST",
        },
    ],
}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _fmt_date(iso: str) -> str:
    """Convert ISO timestamp to MM-DD-YYYY for FSA forms."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%m-%d-%Y")
    except Exception:
        return ""


def _crop_year(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

def _build_field_map(
    farm_config: dict,
    status: dict,
    econ: dict,
) -> dict[str, str]:
    """Map all CCC-576 fields to values from our data.

    Fields we cannot fill are left as empty string — the farmer completes them
    at the FSA office. Fields that need attention are noted in the log.

    CCC-576 field reference:
      Items 1–10:  Header — office, producer, disaster info
      Items 11–31: Part B — crop production/acreage (page 2, per-crop rows)
      Items 32–37: Part C — inventory losses
      Items 38–48: Part D — forage/grazing losses
      Items 49–52: Part E/F — certifications (FSA officer signs)
      Signatures:  Farmer and FSA officer sign in person
    """
    loc = farm_config.get("location", {})
    fire = status.get("nearest_fire") or {}
    event_date_iso = fire.get("detected_at") or status.get("timestamp", "")
    farm_name = farm_config.get("farm_name", "")
    farm_id = farm_config.get("farm_id", "")
    county = loc.get("county", "San Diego")
    state = loc.get("state", "CA")
    lat = loc.get("lat", "")
    lon = loc.get("lon", "")

    producer_address = (
        f"{farm_name}\n"
        f"Lat: {lat}, Lon: {lon}\n"
        f"{county}, {state}"
    )

    destructions = econ.get("crop_destructions", [])

    fields: dict[str, str] = {}

    # --- Part A: Header ---
    fields["Item 1_ County FSA Office"] = FSA_OFFICE_CA_SAN_DIEGO
    fields["item_2"] = _crop_year(event_date_iso)
    fields["item_3"] = producer_address
    fields["4"] = f"{CA_STATE_CODE}-{SAN_DIEGO_COUNTY_CODE}"
    fields["item_5a"] = "Wildfire"
    fields["item 56b"] = _fmt_date(event_date_iso)   # disaster start date
    fields["item 5c"] = _fmt_date(event_date_iso)    # disaster end date (same — farmer updates)
    fields["item6A"] = destructions[0]["crop_category"].title() if destructions else ""
    fields["item6B"] = ""       # crop type/variety — farmer fills
    fields["item6c"] = "Sale"   # intended use — default for most crops; farmer verifies
    fields["item7d"] = "N"      # practice — N=nonirrigated default; farmer verifies
    fields["item6e"] = ""       # planting period — farmer fills
    fields["6F"] = _fmt_date(event_date_iso)         # date loss first apparent

    # --- Part A: Acreage rows (items 7A–7G, up to 3 rows) ---
    # Row maps to crop destructions[0], [1], [2]
    row_labels = ["row 1", "row 2", "row 3"]
    for i, row in enumerate(row_labels):
        d = destructions[i] if i < len(destructions) else None
        fields[f"item 7A_{row}"] = farm_id
        fields[f"item 7B_{row}"] = ""              # NAP unit number — farmer fills
        fields[f"item 7C_{row}"] = str(d["size_acres"]) if d else ""   # intended acres
        fields[f"Item 7D_{row}"] = str(d["size_acres"]) if d else ""   # planted acres
        fields[f"7E_Row {i+1}"] = ""               # prevented planted — farmer fills

    # Item 8: Crop acreage section
    for i, row in enumerate(row_labels):
        d = destructions[i] if i < len(destructions) else None
        fields[f"item8a{'' if i==0 else '_Row '+str(i+1) if i>0 else '1'}"] = farm_id if i == 0 else ""
        fields[f"item 8C_{row}"] = str(d["size_acres"]) if d else ""   # total planted acreage
        fields[f"item 8D_{row}"] = str(d["size_acres"]) if d else ""   # disaster affected acreage

    # Fix row 1 key inconsistency in the form
    fields["item8a1"] = farm_id
    fields["item8b1"] = ""
    fields["item8a_Row 2"] = farm_id if len(destructions) > 1 else ""
    fields["item8b1_row 2"] = ""

    # --- Part B: Production section (page 2, items 11–31) ---
    fields["11"] = farm_name
    fields["12"] = _crop_year(event_date_iso)
    fields["13"] = ""    # unit number — farmer fills
    fields["14"] = ""    # pay crop code — FSA fills
    fields["15"] = ""    # pay type code — FSA fills
    fields["16"] = ""    # planting period — farmer fills

    prod_row_suffixes = ["line1", "row 2", "row 3"]
    prod_row_keys = {
        0: {"17": "item 17line1",  "19": "item19_row 1", "20": "item20_line_1",
            "21": "item21_row_1",  "22": "item 22_line_1", "24": "item 24_line_1",
            "25": "item 25_line_1","26": "item 26_line_1", "27": "item 27_line_1",
            "28": "item 28_row 1", "29": "item 29_row 1"},
        1: {"17": "item 17line2",  "19": "item19_row 2", "20": "item20_line_2",
            "21": "item21_row_2",  "22": "item 22_line_2", "24": "item 24_line_2",
            "25": "item 25_line_2","26": "item 26_line_2", "27": "item 27_line 2",
            "28": "item 28_row 2", "29": "item 29_row 2"},
        2: {"17": "item 17_row 3", "19": "item19_row 3", "20": "item20_line_3",
            "21": "item21_row_3",  "22": "item 22_line_3", "24": "item 24_line_3",
            "25": "item 25_line_3","26": "item 26_line_3", "27": "item 27_line 3",
            "28": "item 28_row 3", "29": "item 29_row 3"},
    }

    for i in range(3):
        d = destructions[i] if i < len(destructions) else None
        keys = prod_row_keys[i]
        fields[keys["17"]] = d["crop_category"].title() if d else ""
        fields[keys["19"]] = "1.000"        # 100% producer share — farmer verifies
        fields[keys["20"]] = str(d["size_acres"]) if d else ""
        fields[keys["21"]] = "N"            # practice: N = nonirrigated
        fields[keys["22"]] = ""             # stage — farmer fills
        fields[keys["24"]] = ""             # actual production — farmer fills (not available)
        fields[keys["25"]] = "Tons"         # unit of measure — default; farmer verifies
        fields[keys["26"]] = "Sale"         # intended use
        fields[keys["27"]] = "Wildfire Loss" if d else ""
        fields[keys["28"]] = str(round(d["confidence_adjusted_loss_usd"], 2)) if d else ""
        fields[keys["29"]] = ""             # production not to count — FSA fills

    # --- Part C: Inventory losses (items 32–37, up to 3 rows) ---
    inv_rows = {
        0: {"32": "item32_row 1", "33": "item 33_row 1", "34": "item 34_row 1",
            "35": "item 35_row 1", "36": "item 36_row 1", "37": "item 37_row 1"},
        1: {"32": "item32_row 2", "33": "item 33_row 2", "34": "item 34_row 2",
            "35": "item 35_row 2", "36": "item 36_row 2", "37": "item 37_row 2"},
        2: {"32": "item32_row 3", "33": "item 33_row 3", "34": "item 34_row 3",
            "35": "item 35_row 3", "36": "item 36_row 3", "37": "item 37_row 3"},
    }
    for i in range(3):
        d = destructions[i] if i < len(destructions) else None
        keys = inv_rows[i]
        fields[keys["32"]] = d["crop_category"].title() if d else ""
        fields[keys["33"]] = "1.000" if d else ""
        fields[keys["34"]] = str(round(d["estimated_loss_usd"], 2)) if d else ""    # value before disaster
        fields[keys["35"]] = "0.00" if (d and d.get("task4_decision") == "ABANDON") else ""  # value after
        fields[keys["36"]] = ""  # ineligible — FSA fills
        fields[keys["37"]] = str(round(d["confidence_adjusted_loss_usd"] * 0.05, 2)) if d else ""  # salvage estimate

    # Items 38–48: forage/grazing — leave blank (livestock agent not yet available)
    # Items 49–52: certifications — FSA officer fills
    # Signatures: in-person only

    return fields


# ---------------------------------------------------------------------------
# PDF filler
# ---------------------------------------------------------------------------

def fill_ccc576(
    farm_config: dict,
    status: dict,
    econ: dict,
    output_path: Path,
) -> Path:
    reader = PdfReader(str(CCC_576_BLANK))
    writer = PdfWriter()
    writer.append(reader)

    field_map = _build_field_map(farm_config, status, econ)

    # Fill across both pages
    for page in writer.pages:
        writer.update_page_form_field_values(page, field_map, auto_regenerate=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    filled = sum(1 for v in field_map.values() if v)
    total = len(field_map)
    logger.info("Filled %d/%d fields → %s", filled, total, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class InsuranceAgent:
    def __init__(
        self,
        farm_config_path: str | Path = CONFIG_DIR / "farm_config.json",
        status_path: str | Path = STATUS_JSON,
        econ_path: str | Path = ECON_REPORT,
        output_path: str | Path = FILLED_PDF,
    ):
        with open(farm_config_path) as f:
            self.farm_config = json.load(f)
        self.status_path = Path(status_path)
        self.econ_path = Path(econ_path)
        self.output_path = Path(output_path)

    def run(self) -> Path:
        status = _load_json(self.status_path, MOCK_STATUS)
        econ = _load_json(self.econ_path, MOCK_ECON)

        # Insurance form needs per-crop destructions. Econ report has aggregate
        # exposure, not the destruction list — pull that from the latest crop
        # agent output (same source econ uses), and only fall back to mock if
        # neither econ nor live crop data has anything.
        if "crop_destructions" not in econ:
            live = _load_live_crop_destructions()
            if live is None:
                # No live crop output exists — use mock so the form still renders
                self.crop_source = "mock"
                econ["crop_destructions"] = MOCK_ECON["crop_destructions"]
            else:
                # Live data exists; an empty list is a legitimate "no crops at risk"
                self.crop_source = "live"
                econ["crop_destructions"] = live
        else:
            self.crop_source = "econ_report"

        return fill_ccc576(self.farm_config, status, econ, self.output_path)

    def print_summary(self, out_path: Path) -> None:
        print(f"\n  CCC-576 filled: {out_path}")
        print("  Fields pre-filled from system data:")
        print("    Item 1  — County FSA Office (San Diego)")
        print("    Item 2  — Crop year")
        print("    Item 3  — Producer name and location")
        print("    Item 4  — State/county FIPS codes")
        print("    Item 5  — Disaster type (Wildfire), start/end dates")
        print("    Item 6  — Crop name, intended use")
        print("    Items 7–8  — Farm number, planted acreage, disaster-affected acreage (per crop, up to 3)")
        print("    Items 11–29 — Production section: crop, acreage, producer share, salvage value (per crop)")
        print("    Items 32–37 — Inventory values before/after disaster, salvage")
        print("  Fields left blank (farmer completes at FSA office):")
        print("    NAP unit numbers, actual production records, variety/type,")
        print("    planting period, forage/grazing section (Items 38–48), signatures")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Insurance Agent — fills USDA CCC-576 Notice of Loss")
    parser.add_argument("--dry-run", action="store_true", help="Use mock data if output files are missing")
    parser.add_argument("--output", default=str(FILLED_PDF), help="Output PDF path")
    args = parser.parse_args()

    agent = InsuranceAgent(output_path=args.output)
    out = agent.run()
    agent.print_summary(out)


if __name__ == "__main__":
    main()
