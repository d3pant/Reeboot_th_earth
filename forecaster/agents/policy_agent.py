"""Policy Agent — part of the Economic Resilience & Policy Coordinator (ERPC).

Activated post-event (after Forecasting Agent issues all-clear).
Evaluates farm eligibility for wildfire aid programs, grants, and recovery
initiatives. Writes output/policy_report.json.

Usage:
    python policy_agent.py [--status path/to/status.json] [--dry-run]

All farm profile fields (insurance status, land types, etc.) are hardcoded
constants in FARM_PROFILE below. Make dynamic by moving to farm_config.json
or farmer_profile.json and injecting at runtime — no changes needed to the
eligibility engine itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("policy_agent")

OUTPUT_DIR = Path(__file__).parent.parent / "output"
CONFIG_DIR = Path(__file__).parent.parent / "config"
DATA_DIR = Path(__file__).parent.parent / "data"
DEADLINES_CACHE = DATA_DIR / "program_deadlines_cache.json"
STATUS_JSON = OUTPUT_DIR / "status.json"
ECON_REPORT = OUTPUT_DIR / "econ_report.json"
DEADLINES_CACHE_MAX_AGE_DAYS = 7

# ---------------------------------------------------------------------------
# Hardcoded farm profile
# All booleans below should eventually come from farm_config.json or
# farmer_profile.json. See POLICY_AGENT_PLAN.md — "Farm Profile Fields" table.
# ---------------------------------------------------------------------------

FARM_PROFILE = {
    "has_livestock": True,            # → ELRP, ELAP, LFP, LIP
    "has_crops": True,                # → NAP, SDRP
    "has_federal_crop_insurance": False,  # NAP hard exclusion if True
    "has_nap_coverage": True,         # NAP requires pre-event coverage election
    "has_forested_parcels": False,    # → EFRP, EQIP_FIRE (forested variant)
    "land_types": ["cropland", "rangeland"],  # → EQIP, EWP
    "underserved_producer": False,    # ECP cost-share: 90% if True, 75% if False
    "country": "US",                  # FAO/GCF gate: only non-US farms qualify
    "has_approved_lfp": False,        # ELRP gateway: auto-payment requires approved LFP
}

# Hardcoded loss summary — replace with real data from Crop + Livestock agents.
# See POLICY_AGENT_PLAN.md — "Loss Summary Input" section.
HARDCODED_LOSS_SUMMARY = {
    "crop_loss": True,
    "livestock_loss": True,
    "livestock_deaths": True,
    "forage_loss": True,
    "infrastructure_damage": True,
    "forested_parcel_damage": False,
    "watershed_damage": False,
    "economic_injury": True,
}

# ---------------------------------------------------------------------------
# Path constants for Tavily enrichment
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
LIVESTOCK_ERPC_MSG = REPO_ROOT / "Livestock" / "erpc_message.json"
CROP_AGENT_DIR = REPO_ROOT / "crop_agent"

# Program query templates for Tavily search
_PROGRAM_QUERY_TEMPLATES = {
    "ELRP_2025": "ELRP {year} Emergency Livestock Relief Program wildfire eligibility requirements",
    "ELAP":      "ELAP {year} Emergency Assistance Livestock wildfire {state} eligibility deadline",
    "LFP":       "LFP {year} Livestock Forage Disaster Program wildfire eligibility payment rates",
    "LIP":       "LIP {year} Livestock Indemnity Program wildfire {state} acceptance rate",
    "NAP":       "NAP {year} Noninsured Crop Disaster Assistance wildfire eligibility California",
    "ECP":       "ECP {year} Emergency Conservation Program wildfire eligibility cost-share",
    "FSA_LOAN":  "FSA Emergency Farm Loan {year} wildfire disaster eligibility requirements",
    "EQIP_FIRE": "EQIP {year} wildfire conservation practice eligibility {state}",
    "EWP":       "NRCS EWP Emergency Watershed Protection {year} wildfire eligibility",
    "FEMA_IA":   "FEMA Individual Assistance {year} wildfire California eligibility",
    "FEMA_HMGP": "FEMA HMGP {year} wildfire hazard mitigation grant eligibility",
    "SBA_EIDL":  "SBA EIDL {year} wildfire disaster loan agricultural eligibility",
    "CDFA_ERL":  "CDFA Emergency Relief Program {year} California wildfire farm eligibility",
}

_PROGRAM_LOSS_THRESHOLDS = {
    "ELRP_2025": ("livestock_value_at_risk_usd", 1000),
    "ELAP":      ("livestock_value_at_risk_usd", 500),
    "LFP":       ("livestock_value_at_risk_usd", 1000),
    "LIP":       ("livestock_potential_loss_usd", 1),
    "NAP":       ("crop_loss_usd", 5000),
    "ECP":       ("crop_loss_usd", 1000),
    "FSA_LOAN":  ("crop_loss_usd", 10000),
    "SBA_EIDL":  ("crop_loss_usd", 5000),
}

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class EligibleProgram:
    program_id: str
    name: str
    agency: str
    category: str           # livestock | crop | conservation | loan | mitigation | international | state
    eligibility_status: str # confirmed | likely | check_required | ineligible
    eligibility_reason: str
    deadline: Optional[str]
    deadline_trigger: Optional[str]
    estimated_value: Optional[str]
    required_docs: list[str]
    link: str
    notes: Optional[str]
    requires_disaster_declaration: bool
    declaration_confirmed: Optional[bool]
    acceptance_chance: Optional[int] = None
    web_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Status ordering for sort
# ---------------------------------------------------------------------------

_STATUS_ORDER = {"confirmed": 0, "likely": 1, "check_required": 2, "ineligible": 3}


# ---------------------------------------------------------------------------
# FEMA declaration check
# ---------------------------------------------------------------------------

def _check_fema_declaration(state: str, county: str) -> Optional[bool]:
    """Query OpenFEMA for an active Fire declaration in this county.

    Returns True if found, False if not found, None if API call fails.
    Cache TTL is handled by the caller; this always makes a live request.
    """
    url = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
    params = {
        "$filter": f"incidentType eq 'Fire' and state eq '{state}' and designatedArea eq '{county} (County)'",
        "$orderby": "declarationDate desc",
        "$top": 1,
        "$format": "json",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        found = len(data.get("DisasterDeclarationsSummaries", [])) > 0
        logger.info("FEMA declaration check: %s County, %s → %s", county, state, "FOUND" if found else "NOT FOUND")
        return found
    except Exception as exc:
        logger.warning("FEMA declaration API failed: %s — treating as unknown", exc)
        return None


# ---------------------------------------------------------------------------
# Loss summary from JSONs (Tavily enrichment)
# ---------------------------------------------------------------------------

def _derive_loss_from_jsons() -> dict:
    """Read crop + livestock JSONs. Returns loss_summary with _numeric passthrough."""
    loss = dict(HARDCODED_LOSS_SUMMARY)
    numeric = {
        "crop_loss_usd": 0.0,
        "crop_confidence_adjusted_loss_usd": 0.0,
        "livestock_value_at_risk_usd": 0.0,
        "livestock_total_animals": 0,
        "livestock_potential_loss_usd": 0.0,
        "transport_costs_usd": 0.0,
    }

    # Livestock JSON
    try:
        with open(LIVESTOCK_ERPC_MSG) as f:
            lv = json.load(f)
        opt = lv.get("cost_optimization", {})
        numeric["livestock_value_at_risk_usd"] = lv.get("animal_valuation_at_risk", 0)
        numeric["livestock_total_animals"] = opt.get("total_animals_at_risk", 0)
        numeric["livestock_potential_loss_usd"] = opt.get("potential_loss_usd", 0)
        numeric["transport_costs_usd"] = lv.get("transport_costs_usd", 0)
        loss["livestock_loss"] = numeric["livestock_value_at_risk_usd"] > 0
        loss["livestock_deaths"] = numeric["livestock_potential_loss_usd"] > 0
        logger.info("Derived livestock loss: at_risk=$%s", numeric["livestock_value_at_risk_usd"])
    except Exception as exc:
        logger.warning("Could not read erpc_message.json: %s", exc)

    # Crop JSON (latest file)
    try:
        outputs = sorted(CROP_AGENT_DIR.glob("crop_agent_output_*.json"))
        if outputs:
            with open(outputs[-1]) as f:
                crop = json.load(f)
            ei = crop.get("economic_impact", {})
            numeric["crop_loss_usd"] = ei.get("total_estimated_loss_usd", 0)
            numeric["crop_confidence_adjusted_loss_usd"] = ei.get("total_confidence_adjusted_loss_usd", 0)
            loss["crop_loss"] = numeric["crop_loss_usd"] > 0
            field_decisions = crop.get("field_decisions", [])
            abandoned = any(d.get("decision") == "ABANDON" for d in field_decisions)
            loss["infrastructure_damage"] = abandoned or HARDCODED_LOSS_SUMMARY["infrastructure_damage"]
            logger.info("Derived crop loss: total=$%s", numeric["crop_loss_usd"])
    except Exception as exc:
        logger.warning("Could not read crop_agent_output: %s", exc)

    loss["economic_injury"] = numeric["crop_loss_usd"] > 0 or numeric["livestock_value_at_risk_usd"] > 0
    loss["_numeric"] = numeric
    return loss


# ---------------------------------------------------------------------------
# Scoring helpers for Tavily enrichment
# ---------------------------------------------------------------------------

def _score_loss_match(program_id: str, numeric: dict) -> int:
    """Return 0-30 pts based on loss amounts vs. program thresholds."""
    entry = _PROGRAM_LOSS_THRESHOLDS.get(program_id)
    if not entry:
        return 15
    field, threshold = entry
    amount = numeric.get(field, 0)
    if amount <= 0:
        return 0
    if amount >= threshold * 10:
        return 30
    if amount >= threshold:
        return 20
    return 5


def _score_web_signals(snippets: str, program_id: str) -> int:
    """Scan snippets for acceptance rates/approval signals. Return 0-20 pts."""
    pct_pattern = re.compile(r'(\d{1,3})\s*%\s*(?:approval|acceptance|funded|approved)', re.I)
    near_pattern = re.compile(r'(?:approval|acceptance)\s+rate[^.]{0,50}?(\d{1,3})\s*%', re.I)
    matches = pct_pattern.findall(snippets) + near_pattern.findall(snippets)
    if matches:
        try:
            rate = max(int(m) for m in matches)
            return min(20, int(rate * 0.2))
        except ValueError:
            pass
    positive = sum(1 for kw in ["approved", "eligible", "qualify", "available", "open"]
                   if kw in snippets)
    negative = sum(1 for kw in ["closed", "expired", "ineligible", "no longer", "ended"]
                   if kw in snippets)
    net = positive - negative
    if net >= 3:
        return 15
    if net >= 1:
        return 10
    if net <= -1:
        return 2
    return 8


def _score_profile_match(program: EligibleProgram, numeric: dict) -> int:
    """Return 0 or 10 pts based on farm profile match."""
    if program.category == "livestock":
        return 10 if numeric.get("livestock_value_at_risk_usd", 0) > 0 else 0
    if program.category == "crop":
        return 10 if numeric.get("crop_loss_usd", 0) > 0 else 0
    return 10 if program.category in ("conservation", "loan", "mitigation", "state") else 5


def _maybe_update_deadline_from_web(program: EligibleProgram, snippets: str) -> None:
    """Opportunistically update vague deadlines from Tavily text."""
    if program.deadline and re.match(r'\d{4}-\d{2}-\d{2}', str(program.deadline)):
        return
    date_pattern = re.compile(
        r'(?:deadline|apply by|due by|by)\s*:?\s*'
        r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d\d)',
        re.I
    )
    match = date_pattern.search(snippets)
    if match:
        program.deadline = f"Web-sourced: {match.group(1).strip()}"
        logger.info("Updated deadline for %s: %s", program.program_id, program.deadline)


# ---------------------------------------------------------------------------
# Grants.gov live query
# ---------------------------------------------------------------------------

def _fetch_grants_gov(keywords: list[str]) -> list[EligibleProgram]:
    """Query Grants.gov for open opportunities matching wildfire + agriculture."""
    url = "https://api.grants.gov/v1/api/search2"
    results = []

    for keyword in keywords:
        payload = {
            "keyword": keyword,
            "oppStatuses": "forecasted|posted",
            "rows": 10,
            "sortBy": "openDate|desc",
        }
        try:
            resp = httpx.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("data", {}).get("oppHits", [])
            for hit in hits:
                program_id = f"GRANTSGOV_{hit.get('id', 'UNKNOWN')}"
                if any(p.program_id == program_id for p in results):
                    continue
                results.append(EligibleProgram(
                    program_id=program_id,
                    name=hit.get("title", "Unknown Grant"),
                    agency=hit.get("agencyName", "Federal Agency"),
                    category="grant",
                    eligibility_status="check_required",
                    eligibility_reason="Live grant from Grants.gov — verify eligibility directly",
                    deadline=hit.get("closeDate"),
                    deadline_trigger=None,
                    estimated_value=f"${hit['awardCeiling']:,}" if hit.get("awardCeiling") else None,
                    required_docs=[],
                    link=f"https://www.grants.gov/search-results-detail/{hit.get('id', '')}",
                    notes=f"Keyword match: '{keyword}'",
                    requires_disaster_declaration=False,
                    declaration_confirmed=None,
                ))
            logger.info("Grants.gov '%s': %d results", keyword, len(hits))
        except Exception as exc:
            logger.warning("Grants.gov query failed for '%s': %s", keyword, exc)

    return results


# ---------------------------------------------------------------------------
# Tavily search enrichment
# ---------------------------------------------------------------------------

def _enrich_with_tavily(programs: list[EligibleProgram], farm_context: dict) -> list[EligibleProgram]:
    """Enrich each program with acceptance_chance and web_sources via Tavily."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY not set — skipping Tavily enrichment")
        return programs

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
    except ImportError:
        logger.warning("tavily-python not installed — skipping Tavily enrichment")
        return programs

    state = farm_context.get("state", "CA")
    year = datetime.now().year
    loss = farm_context.get("loss", {})
    numeric = loss.pop("_numeric", {})

    for program in programs:
        # Ineligible: score 0, no API call
        if program.eligibility_status == "ineligible":
            program.acceptance_chance = 0
            continue

        # Build targeted query
        template = _PROGRAM_QUERY_TEMPLATES.get(program.program_id)
        if template:
            query = template.format(year=year, state=state)
        else:
            query = f"{program.name} {year} wildfire eligibility {state}"

        # Call Tavily
        try:
            response = client.search(
                query=query,
                search_depth="basic",
                max_results=5,
                include_answer=True,
                include_raw_content=False,
            )
            results = response.get("results", [])
            answer = response.get("answer", "") or ""
            sources = [r["url"] for r in results if r.get("url")]
            snippets = " ".join(
                r.get("content", "") for r in results
            ).lower() + answer.lower()

            program.web_sources = sources[:5]

        except Exception as exc:
            logger.warning("Tavily search failed for %s: %s", program.program_id, exc)
            continue

        # Score components
        status_score = {"confirmed": 40, "likely": 30, "check_required": 15}.get(
            program.eligibility_status, 0
        )
        loss_score = _score_loss_match(program.program_id, numeric)
        web_score = _score_web_signals(snippets, program.program_id)
        profile_score = _score_profile_match(program, numeric)

        program.acceptance_chance = min(100, status_score + loss_score + web_score + profile_score)

        # Opportunistically update deadline
        _maybe_update_deadline_from_web(program, snippets)

    return programs


# ---------------------------------------------------------------------------
# Deadlines cache
# ---------------------------------------------------------------------------

def _load_deadlines_cache() -> dict:
    """Read program_deadlines_cache.json. Returns empty dict if missing or stale."""
    if not DEADLINES_CACHE.exists():
        logger.warning("Deadlines cache not found at %s — using hardcoded deadlines", DEADLINES_CACHE)
        return {}
    try:
        with open(DEADLINES_CACHE) as f:
            cache = json.load(f)
        written_at = datetime.fromisoformat(cache.get("written_at", "2000-01-01"))
        age_days = (datetime.now(timezone.utc) - written_at.replace(tzinfo=timezone.utc)).days
        if age_days > DEADLINES_CACHE_MAX_AGE_DAYS:
            logger.warning("Deadlines cache is %d days old (max %d) — using hardcoded deadlines", age_days, DEADLINES_CACHE_MAX_AGE_DAYS)
            return {}
        logger.info("Loaded deadlines cache (age: %d days)", age_days)
        return cache.get("programs", {})
    except Exception as exc:
        logger.warning("Failed to read deadlines cache: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Loss summary loader
# ---------------------------------------------------------------------------

def _load_loss_summary() -> dict:
    """Derive boolean loss flags from econ_report.json. Falls back to HARDCODED_LOSS_SUMMARY."""
    try:
        with open(ECON_REPORT) as f:
            report = json.load(f)
        exp = report.get("financial_exposure", {})
        derived = {
            **HARDCODED_LOSS_SUMMARY,
            "crop_loss": exp.get("crop_loss_total_usd", 0) > 0,
            "livestock_loss": exp.get("livestock_at_risk_usd", 0) > 0,
            "economic_injury": exp.get("total_exposure_usd", 0) > 0,
        }
        logger.info("Loss summary derived from econ_report.json")
        return derived
    except Exception:
        logger.warning("Could not read econ_report.json — using HARDCODED_LOSS_SUMMARY")
        return HARDCODED_LOSS_SUMMARY


# ---------------------------------------------------------------------------
# Disaster event date
# ---------------------------------------------------------------------------

def _load_event_date(status_path: Path) -> Optional[datetime]:
    """Extract fire event date from status.json.

    Prefers nearest_fire.detected_at; falls back to status timestamp.
    Make dynamic: replace with official FEMA declaration date once integrated.
    """
    try:
        with open(status_path) as f:
            status = json.load(f)
        fire = status.get("nearest_fire") or {}
        detected = fire.get("detected_at") or status.get("timestamp")
        if detected:
            return datetime.fromisoformat(detected.replace("Z", "+00:00"))
    except Exception as exc:
        logger.warning("Could not read event date from status.json: %s", exc)
    return None


def _deadline_from_event(event_date: Optional[datetime], days: int) -> Optional[str]:
    """Compute an absolute deadline date from event_date + offset days."""
    if event_date is None:
        return None
    deadline = event_date + timedelta(days=days)
    if deadline < datetime.now(timezone.utc):
        return f"EXPIRED ({deadline.strftime('%Y-%m-%d')})"
    return deadline.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Eligibility engine — hardcoded program catalog
# ---------------------------------------------------------------------------

def _build_catalog(
    farm_config: dict,
    declaration: Optional[bool],
    event_date: Optional[datetime],
    deadlines_cache: dict,
    loss: dict,
) -> list[EligibleProgram]:
    """Evaluate all programs against farm profile and return list of EligibleProgram."""

    profile = FARM_PROFILE
    state = farm_config["location"]["state"]
    ecp_rate = "90%" if profile["underserved_producer"] else "75%"
    decl_status = declaration  # True / False / None

    def _decl_note(program_id: str) -> str:
        if decl_status is True:
            return "Presidential disaster declaration confirmed for this county"
        if decl_status is False:
            return "No Presidential disaster declaration found for this county — verify at disasterassistance.gov"
        return "Could not verify disaster declaration — check manually"

    def _decl_confirmed_status(fallback: str = "likely") -> str:
        if decl_status is True:
            return "confirmed"
        if decl_status is False:
            return "check_required"
        return fallback

    programs: list[EligibleProgram] = []

    # --- USDA FSA ---

    # ELRP
    if profile["has_livestock"] and loss["forage_loss"]:
        if profile["has_approved_lfp"]:
            status, reason = "confirmed", "Has livestock, forage loss confirmed, approved LFP on file — auto-payment triggered"
        else:
            status = "likely"
            reason = "Has livestock and forage loss, but no approved LFP on file — file LFP first to unlock ELRP auto-payment"
        deadline = deadlines_cache.get("ELRP_2025", {}).get("deadline", "Nov 21, 2025")
        programs.append(EligibleProgram(
            program_id="ELRP_2025",
            name="Emergency Livestock Relief Program (ELRP) — Wildfire",
            agency="USDA-FSA",
            category="livestock",
            eligibility_status=status,
            eligibility_reason=reason,
            deadline=deadline,
            deadline_trigger="Enrollment window Sep 15 – Nov 21, 2025",
            estimated_value="Up to $1 billion pool — individual payment auto-calculated",
            required_docs=["Approved LFP application", "Livestock inventory records"],
            link="https://www.fsa.usda.gov/resources/disaster-recovery/emergency-livestock-relief-program-elrp",
            notes="LFP application is the gateway — must be on file before ELRP payment is issued automatically",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))
    else:
        programs.append(EligibleProgram(
            program_id="ELRP_2025",
            name="Emergency Livestock Relief Program (ELRP) — Wildfire",
            agency="USDA-FSA",
            category="livestock",
            eligibility_status="ineligible",
            eligibility_reason="No livestock or no forage loss recorded",
            deadline=None, deadline_trigger=None, estimated_value=None,
            required_docs=[], link="https://www.fsa.usda.gov/resources/disaster-recovery/emergency-livestock-relief-program-elrp",
            notes=None, requires_disaster_declaration=False, declaration_confirmed=None,
        ))

    # ELAP
    if profile["has_livestock"] and loss["livestock_loss"]:
        programs.append(EligibleProgram(
            program_id="ELAP",
            name="Emergency Assistance for Livestock, Honeybees & Farm-Raised Fish (ELAP)",
            agency="USDA-FSA",
            category="livestock",
            eligibility_status="confirmed",
            eligibility_reason="Has livestock with confirmed losses from wildfire",
            deadline=_deadline_from_event(event_date, 30),
            deadline_trigger="Notice of Loss must be filed within 30 days of event",
            estimated_value="Cost-based compensation — grazing, feed, water hauling",
            required_docs=["Notice of Loss (within 30 days)", "Feed/water cost receipts", "Livestock inventory"],
            link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/emergency-assist-for-livestock-honey-bees-fish/index",
            notes="30-day Notice of Loss window is hard — do not delay",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # LFP
    if profile["has_livestock"] and loss["forage_loss"]:
        programs.append(EligibleProgram(
            program_id="LFP",
            name="Livestock Forage Disaster Program (LFP)",
            agency="USDA-FSA",
            category="livestock",
            eligibility_status="confirmed",
            eligibility_reason="Has livestock with forage losses from wildfire",
            deadline=deadlines_cache.get("LFP", {}).get("deadline", "Contact local FSA office"),
            deadline_trigger=None,
            estimated_value="Payment rate × number of eligible livestock",
            required_docs=["Livestock inventory", "Grazing lease or deed", "Evidence of forage loss"],
            link="https://www.fsa.usda.gov/resources/disaster-recovery/livestock-forage-disaster-program-lfp",
            notes="Filing LFP is required first — it is the gateway to ELRP auto-payment",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # LIP
    if profile["has_livestock"] and loss["livestock_deaths"]:
        programs.append(EligibleProgram(
            program_id="LIP",
            name="Livestock Indemnity Program (LIP)",
            agency="USDA-FSA",
            category="livestock",
            eligibility_status="confirmed",
            eligibility_reason="Has livestock deaths above normal mortality from wildfire",
            deadline=deadlines_cache.get("LIP", {}).get("deadline", "Mar 1, 2027 for 2026 losses"),
            deadline_trigger="Notice of Loss by Mar 1, 2027 for 2026 calendar year losses",
            estimated_value="75% of market value per head lost above normal mortality",
            required_docs=["Notice of Loss", "Livestock inventory pre-event", "Death records / veterinary documentation"],
            link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/livestock-indemnity/index",
            notes=None,
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # NAP
    if profile["has_crops"] and loss["crop_loss"]:
        if profile["has_federal_crop_insurance"]:
            programs.append(EligibleProgram(
                program_id="NAP",
                name="Noninsured Crop Disaster Assistance Program (NAP)",
                agency="USDA-FSA",
                category="crop",
                eligibility_status="ineligible",
                eligibility_reason="Hard exclusion: farmer has federal crop insurance — NAP and crop insurance are mutually exclusive",
                deadline=None, deadline_trigger=None, estimated_value=None,
                required_docs=[], link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index",
                notes=None, requires_disaster_declaration=False, declaration_confirmed=None,
            ))
        elif not profile["has_nap_coverage"]:
            programs.append(EligibleProgram(
                program_id="NAP",
                name="Noninsured Crop Disaster Assistance Program (NAP)",
                agency="USDA-FSA",
                category="crop",
                eligibility_status="ineligible",
                eligibility_reason="NAP coverage was not elected before the disaster event — coverage must be purchased prior to loss",
                deadline=None, deadline_trigger=None, estimated_value=None,
                required_docs=[], link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index",
                notes=None, requires_disaster_declaration=False, declaration_confirmed=None,
            ))
        else:
            programs.append(EligibleProgram(
                program_id="NAP",
                name="Noninsured Crop Disaster Assistance Program (NAP)",
                agency="USDA-FSA",
                category="crop",
                eligibility_status="confirmed",
                eligibility_reason="Has crops, crop loss confirmed, NAP coverage elected pre-event, no federal crop insurance",
                deadline=deadlines_cache.get("NAP", {}).get("deadline", "Contact local FSA office"),
                deadline_trigger=None,
                estimated_value="55% of average market price for crop losses above 50% threshold",
                required_docs=["NAP coverage certification", "Production records", "Loss documentation"],
                link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index",
                notes=None,
                requires_disaster_declaration=False,
                declaration_confirmed=None,
            ))

    # ECP
    if loss["infrastructure_damage"]:
        programs.append(EligibleProgram(
            program_id="ECP",
            name="Emergency Conservation Program (ECP)",
            agency="USDA-FSA",
            category="conservation",
            eligibility_status="confirmed",
            eligibility_reason=f"Farmland infrastructure damage confirmed; cost-share rate: {ecp_rate}",
            deadline=deadlines_cache.get("ECP", {}).get("deadline", "Contact local FSA office"),
            deadline_trigger=None,
            estimated_value=f"{ecp_rate} cost-share on fencing repair, water restoration, debris removal",
            required_docs=["Damage assessment", "Cost estimates for repairs", "Farm ownership / lease documentation"],
            link="https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-conservation/index",
            notes=f"Cost-share is {ecp_rate} — {'90% applies to underserved producers' if profile['underserved_producer'] else 'to qualify for 90%, producer must be designated underserved'}",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # EFRP
    if profile["has_forested_parcels"] and loss["forested_parcel_damage"]:
        programs.append(EligibleProgram(
            program_id="EFRP",
            name="Emergency Forest Restoration Program (EFRP)",
            agency="USDA-FSA",
            category="conservation",
            eligibility_status="confirmed",
            eligibility_reason="Has non-industrial private forested parcels with wildfire damage",
            deadline=deadlines_cache.get("EFRP", {}).get("deadline", "Contact local FSA office"),
            deadline_trigger=None,
            estimated_value="Up to 75% cost-share on forest restoration practices",
            required_docs=["Forest management plan", "Damage documentation", "Land ownership records"],
            link="https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-forest-restoration/index",
            notes=None,
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))
    else:
        programs.append(EligibleProgram(
            program_id="EFRP",
            name="Emergency Forest Restoration Program (EFRP)",
            agency="USDA-FSA",
            category="conservation",
            eligibility_status="ineligible",
            eligibility_reason="No non-industrial private forested parcels on this farm profile",
            deadline=None, deadline_trigger=None, estimated_value=None,
            required_docs=[], link="https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-forest-restoration/index",
            notes=None, requires_disaster_declaration=False, declaration_confirmed=None,
        ))

    # SDRP 2023/2024
    if profile["has_crops"] and loss["crop_loss"]:
        programs.append(EligibleProgram(
            program_id="SDRP_2324",
            name="Supplemental Disaster Relief Program (SDRP) — 2023/2024",
            agency="USDA-FSA",
            category="crop",
            eligibility_status="check_required",
            eligibility_reason="Has crops with losses — SDRP covers 2023 and 2024 weather events only; verify event year qualifies",
            deadline=deadlines_cache.get("SDRP_2324", {}).get("deadline", "Check FSA office — American Relief Act of 2025 program"),
            deadline_trigger=None,
            estimated_value="Crop revenue loss compensation — amount based on existing crop insurance data",
            required_docs=["Crop insurance records (Stage 1)", "Production records (Stage 2)", "Evidence of revenue loss"],
            link="https://www.fsa.usda.gov/resources/programs/20232024-supplemental-disaster-assistance",
            notes="Program covers 2023 and 2024 losses only — confirm event year is within scope",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # FSA Emergency Farm Loans
    programs.append(EligibleProgram(
        program_id="FSA_LOAN",
        name="FSA Emergency Farm Loans",
        agency="USDA-FSA",
        category="loan",
        eligibility_status=_decl_confirmed_status(),
        eligibility_reason=f"Farm/ranch with production or property losses. {_decl_note('FSA_LOAN')}",
        deadline=_deadline_from_event(event_date, 243),  # ~8 months
        deadline_trigger="Apply within 8 months of disaster declaration date",
        estimated_value="Up to $500,000",
        required_docs=["Federal disaster declaration", "Farm financial records", "Loss documentation", "Tax returns (3 years)"],
        link="https://www.fsa.usda.gov/programs-and-services/farm-loan-programs/emergency-farm-loans/index",
        notes="Requires federal disaster declaration — check FEMA declaration status first",
        requires_disaster_declaration=True,
        declaration_confirmed=decl_status,
    ))

    # --- USDA NRCS ---

    # EQIP Wildfire
    if profile["land_types"]:
        programs.append(EligibleProgram(
            program_id="EQIP_FIRE",
            name="Environmental Quality Incentives Program (EQIP) — Wildfire",
            agency="USDA-NRCS",
            category="conservation",
            eligibility_status="confirmed",
            eligibility_reason=f"Has eligible land types: {', '.join(profile['land_types'])}",
            deadline=deadlines_cache.get("EQIP_FIRE", {}).get("deadline", "Contact local NRCS office — rolling applications"),
            deadline_trigger=None,
            estimated_value="Cost-share payments for approved conservation practices",
            required_docs=["Farm plan", "NRCS application", "Land documentation"],
            link="https://www.nrcs.usda.gov/programs-and-initiatives/eqip-environmental-quality-incentives",
            notes=None,
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))

    # EWP
    if loss["watershed_damage"]:
        programs.append(EligibleProgram(
            program_id="EWP",
            name="Emergency Watershed Protection (EWP) Program",
            agency="USDA-NRCS",
            category="conservation",
            eligibility_status="confirmed",
            eligibility_reason="Watershed damage from wildfire — formal request required within 60 days",
            deadline=_deadline_from_event(event_date, 60),
            deadline_trigger="Formal request to state conservationist within 60 days of disaster",
            estimated_value="Up to 75% of restoration costs (90% for limited-resource/underserved)",
            required_docs=["Formal request to NRCS state conservationist", "Site damage documentation"],
            link="https://www.nrcs.usda.gov/programs-and-initiatives/ewp-emergency-watershed-protection-program",
            notes="Request must be submitted by a project sponsor (local government, tribe, or similar) — individual farmers apply through a sponsor",
            requires_disaster_declaration=False,
            declaration_confirmed=None,
        ))
    else:
        programs.append(EligibleProgram(
            program_id="EWP",
            name="Emergency Watershed Protection (EWP) Program",
            agency="USDA-NRCS",
            category="conservation",
            eligibility_status="ineligible",
            eligibility_reason="No watershed damage recorded in loss summary",
            deadline=None, deadline_trigger=None, estimated_value=None,
            required_docs=[], link="https://www.nrcs.usda.gov/programs-and-initiatives/ewp-emergency-watershed-protection-program",
            notes=None, requires_disaster_declaration=False, declaration_confirmed=None,
        ))

    # --- FEMA ---

    # FEMA IA
    programs.append(EligibleProgram(
        program_id="FEMA_IA",
        name="FEMA Individual Assistance (IA)",
        agency="FEMA",
        category="mitigation",
        eligibility_status=_decl_confirmed_status(),
        eligibility_reason=f"Farmers qualify as individuals/households. {_decl_note('FEMA_IA')}",
        deadline=None,
        deadline_trigger="Apply as soon as Presidential Major Disaster Declaration is issued",
        estimated_value="Grants for home/property repair, essential items, serious disaster needs",
        required_docs=["Disaster declaration number", "Proof of ownership/occupancy", "Insurance documentation"],
        link="https://www.disasterassistance.gov",
        notes="Apply at disasterassistance.gov — requires Presidential Major Disaster Declaration",
        requires_disaster_declaration=True,
        declaration_confirmed=decl_status,
    ))

    # FEMA FMAG
    programs.append(EligibleProgram(
        program_id="FEMA_FMAG",
        name="FEMA Fire Management Assistance Grant (FMAG)",
        agency="FEMA",
        category="mitigation",
        eligibility_status="check_required",
        eligibility_reason="Not direct farmer aid — issued to state/local/tribal governments; activates other programs",
        deadline=None,
        deadline_trigger=None,
        estimated_value="Varies — state-level grant",
        required_docs=[],
        link="https://www.fema.gov/assistance/public/fire-management-assistance",
        notes="FMAG activates additional state-level disaster programs — check if your state has an active FMAG declaration",
        requires_disaster_declaration=False,
        declaration_confirmed=None,
    ))

    # FEMA HMGP
    programs.append(EligibleProgram(
        program_id="FEMA_HMGP",
        name="FEMA Hazard Mitigation Grant Program (HMGP) — Post Fire",
        agency="FEMA",
        category="mitigation",
        eligibility_status=_decl_confirmed_status("check_required"),
        eligibility_reason=f"Long-term mitigation projects (firebreaks, etc.). {_decl_note('FEMA_HMGP')}",
        deadline=_deadline_from_event(event_date, 365),
        deadline_trigger="Available up to 12 months after presidentially-declared major disaster",
        estimated_value="Varies by project scope",
        required_docs=["Disaster declaration number", "Project proposal", "Cost-benefit analysis"],
        link="https://www.fema.gov/grants/mitigation/hazard-mitigation",
        notes=None,
        requires_disaster_declaration=True,
        declaration_confirmed=decl_status,
    ))

    # --- SBA ---

    programs.append(EligibleProgram(
        program_id="SBA_EIDL",
        name="SBA Economic Injury Disaster Loans (EIDL)",
        agency="SBA",
        category="loan",
        eligibility_status=_decl_confirmed_status() if loss["economic_injury"] else "ineligible",
        eligibility_reason=(
            f"Small agricultural operation with economic injury from wildfire. {_decl_note('SBA_EIDL')}"
            if loss["economic_injury"] else "No economic injury recorded"
        ),
        deadline=None,
        deadline_trigger="Apply after SBA disaster declaration for the area",
        estimated_value="Up to $2,000,000",
        required_docs=["SBA disaster declaration", "Business financial statements (3 years)", "Personal financial statement", "Tax returns"],
        link="https://www.sba.gov/funding-programs/disaster-assistance",
        notes="Covers cash flow losses, not physical property — complements FSA Emergency Loans",
        requires_disaster_declaration=True,
        declaration_confirmed=decl_status,
    ))

    # --- UN / International ---

    fao_status = "ineligible" if profile["country"] == "US" else "check_required"
    fao_reason = "FAO Fire Hub is a coordination body for international farmers — US farmers should use USDA programs above" if profile["country"] == "US" else "International farmer — contact FAO national focal point"

    programs.append(EligibleProgram(
        program_id="FAO_FIRE_HUB",
        name="FAO Global Fire Management Hub",
        agency="UN/FAO",
        category="international",
        eligibility_status=fao_status,
        eligibility_reason=fao_reason,
        deadline=None, deadline_trigger=None, estimated_value=None,
        required_docs=[],
        link="https://www.fao.org/partnerships/fire-hub/en",
        notes="Policy/coordination body — not a direct aid program for US farmers",
        requires_disaster_declaration=False,
        declaration_confirmed=None,
    ))

    programs.append(EligibleProgram(
        program_id="GCF_FAO",
        name="Green Climate Fund (GCF) via FAO",
        agency="UN/FAO",
        category="international",
        eligibility_status="ineligible" if profile["country"] == "US" else "check_required",
        eligibility_reason="Access is through national government applications only — not direct farmer enrollment" if profile["country"] == "US" else "International — apply through national government",
        deadline=None, deadline_trigger=None, estimated_value="Tens of millions per project",
        required_docs=[],
        link="https://www.greenclimate.fund/ae/fao",
        notes="Structural constraint: no direct farmer application path regardless of geography",
        requires_disaster_declaration=False,
        declaration_confirmed=None,
    ))

    # --- California state programs (CA only) ---
    if state == "CA":
        programs.extend([
            EligibleProgram(
                program_id="CDFA_ERL",
                name="CA Dept of Food & Agriculture — Emergency Relief Programs",
                agency="CDFA",
                category="state",
                eligibility_status="check_required",
                eligibility_reason="CA farm with wildfire loss — governor's emergency declaration status unknown; verify at CDFA",
                deadline=None,
                deadline_trigger="Check CDFA website — deadlines vary by program cycle",
                estimated_value="Varies by program",
                required_docs=["CA farm registration", "Loss documentation", "Governor's emergency declaration number"],
                link="https://www.cdfa.ca.gov/grants/",
                notes="CA state program deadlines are variable — check CDFA directly after any governor's emergency declaration",
                requires_disaster_declaration=True,
                declaration_confirmed=None,
            ),
            EligibleProgram(
                program_id="CALFIRE_FRAP",
                name="CAL FIRE — Forest Health Grants",
                agency="CAL FIRE",
                category="state",
                eligibility_status="ineligible" if not profile["has_forested_parcels"] else "check_required",
                eligibility_reason="No forested parcels on farm profile" if not profile["has_forested_parcels"] else "Non-industrial private forest landowner in CA — verify current grant cycle",
                deadline=None,
                deadline_trigger="Rolling applications — check CAL FIRE grants portal",
                estimated_value="Varies by project",
                required_docs=["Timber Production Zone designation or equivalent", "Reforestation/fire resilience project plan"],
                link="https://www.fire.ca.gov/grants",
                notes=None,
                requires_disaster_declaration=False,
                declaration_confirmed=None,
            ),
            EligibleProgram(
                program_id="CDFA_OEFI",
                name="CA Office of Emergency Food and Farming Infrastructure (OEFI)",
                agency="CDFA",
                category="state",
                eligibility_status="check_required",
                eligibility_reason="Small/mid-scale CA farm with food system disruption from disaster — verify current funding cycle",
                deadline=None,
                deadline_trigger="Check CDFA OEFI website for open solicitations",
                estimated_value="Varies by project",
                required_docs=["CA farm registration", "Evidence of disaster-related disruption", "Project proposal"],
                link="https://www.cdfa.ca.gov/oefi/",
                notes=None,
                requires_disaster_declaration=False,
                declaration_confirmed=None,
            ),
            EligibleProgram(
                program_id="CA_EDD_DISASTER",
                name="CA EDD Disaster Unemployment Assistance",
                agency="CA EDD",
                category="state",
                eligibility_status="check_required",
                eligibility_reason="Covers self-employed farmers who lost work due to disaster — verify active DUA period for this disaster",
                deadline=None,
                deadline_trigger="Apply within 30 days of DUA announcement",
                estimated_value="Weekly benefit based on prior earnings",
                required_docs=["Proof of self-employment or farm income", "Disaster declaration number", "Evidence of lost work"],
                link="https://edd.ca.gov/en/unemployment/disaster/",
                notes=None,
                requires_disaster_declaration=True,
                declaration_confirmed=decl_status,
            ),
        ])

    return programs


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class PolicyAgent:
    def __init__(self, farm_config_path: str | Path, status_path: str | Path = STATUS_JSON):
        with open(farm_config_path) as f:
            self.farm_config = json.load(f)
        self.status_path = Path(status_path)
        self.declaration: Optional[bool] = None
        self.event_date: Optional[datetime] = None
        self.programs: list[EligibleProgram] = []
        self.report: dict = {}

    def run(self, loss_summary: Optional[dict] = None) -> dict:
        """Run the full policy eligibility pipeline. Returns the report dict."""
        if loss_summary is not None:
            loss = loss_summary
        else:
            loss = _derive_loss_from_jsons()

        state = self.farm_config["location"]["state"]
        county = self.farm_config["location"]["county"]

        self.event_date = _load_event_date(self.status_path)
        logger.info("Event date: %s", self.event_date)

        self.declaration = _check_fema_declaration(state, county)

        deadlines_cache = _load_deadlines_cache()

        self.programs = _build_catalog(
            farm_config=self.farm_config,
            declaration=self.declaration,
            event_date=self.event_date,
            deadlines_cache=deadlines_cache,
            loss=loss,
        )

        grants_gov = _fetch_grants_gov(["wildfire agriculture", "wildfire livestock"])
        self.programs.extend(grants_gov)

        # Tavily enrichment with acceptance scoring
        farm_context = {
            "state": state,
            "county": county,
            "loss": loss,
        }
        self.programs = _enrich_with_tavily(self.programs, farm_context)

        # Sort by eligibility status, then by acceptance chance descending
        self.programs.sort(
            key=lambda p: (
                _STATUS_ORDER.get(p.eligibility_status, 99),
                -(p.acceptance_chance or 0),
            )
        )

        eligible = [p for p in self.programs if p.eligibility_status != "ineligible"]
        ineligible = [p for p in self.programs if p.eligibility_status == "ineligible"]

        tavily_enriched = sum(1 for p in self.programs if p.acceptance_chance is not None)

        self.report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "farm_id": self.farm_config["farm_id"],
            "state": state,
            "county": county,
            "disaster_declaration_confirmed": self.declaration,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "tavily_enrichment": {
                "enabled": True,
                "programs_scored": tavily_enriched,
                "programs_skipped": len(self.programs) - tavily_enriched,
            },
            "data_sources": {
                "loss_summary": "live_jsons",
                "crop_json": max((CROP_AGENT_DIR.glob("crop_agent_output_*.json")), default=None).__str__() if list(CROP_AGENT_DIR.glob("crop_agent_output_*.json")) else None,
                "livestock_json": str(LIVESTOCK_ERPC_MSG) if LIVESTOCK_ERPC_MSG.exists() else None,
            },
            "summary": {
                "total_programs_evaluated": len(self.programs),
                "confirmed": sum(1 for p in self.programs if p.eligibility_status == "confirmed"),
                "likely": sum(1 for p in self.programs if p.eligibility_status == "likely"),
                "check_required": sum(1 for p in self.programs if p.eligibility_status == "check_required"),
                "ineligible": sum(1 for p in self.programs if p.eligibility_status == "ineligible"),
            },
            "eligible_programs": [p.to_dict() for p in eligible],
            "ineligible_programs": [p.to_dict() for p in ineligible],
        }

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / "policy_report.json"
        with open(out_path, "w") as f:
            json.dump(self.report, f, indent=2, default=str)
        logger.info("Wrote %s", out_path)

        return self.report

    def print_summary(self) -> None:
        s = self.report.get("summary", {})
        print("\n--- POLICY REPORT SUMMARY ---")
        print(f"  Declaration confirmed : {self.report.get('disaster_declaration_confirmed')}")
        print(f"  Event date            : {self.report.get('event_date')}")
        print(f"  Confirmed             : {s.get('confirmed')}")
        print(f"  Likely                : {s.get('likely')}")
        print(f"  Check required        : {s.get('check_required')}")
        print(f"  Ineligible            : {s.get('ineligible')}")
        print(f"\n  Eligible programs:")
        for p in self.report.get("eligible_programs", []):
            deadline_str = f" | deadline: {p['deadline']}" if p.get("deadline") else ""
            print(f"  [{p['eligibility_status'].upper():14}] {p['name']}{deadline_str}")
            if p.get("notes"):
                print(f"                   ^ {p['notes']}")
        print(f"\n  output/policy_report.json written.")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Policy Agent — wildfire aid eligibility engine")
    parser.add_argument("--status", default=str(STATUS_JSON), help="Path to forecaster status.json")
    parser.add_argument("--dry-run", action="store_true", help="Skip API calls, use None for declaration")
    args = parser.parse_args()

    farm_config_path = CONFIG_DIR / "farm_config.json"

    agent = PolicyAgent(farm_config_path=farm_config_path, status_path=args.status)

    if args.dry_run:
        logger.info("Dry run — skipping FEMA and Grants.gov API calls, using hardcoded loss")
        agent.declaration = None
        agent.event_date = _load_event_date(Path(args.status))
        deadlines_cache = _load_deadlines_cache()
        agent.programs = _build_catalog(
            farm_config=agent.farm_config,
            declaration=None,
            event_date=agent.event_date,
            deadlines_cache=deadlines_cache,
            loss=HARDCODED_LOSS_SUMMARY,
        )
        agent.programs.sort(
            key=lambda p: (
                _STATUS_ORDER.get(p.eligibility_status, 99),
                -(p.acceptance_chance or 0),
            )
        )
        eligible = [p for p in agent.programs if p.eligibility_status != "ineligible"]
        ineligible = [p for p in agent.programs if p.eligibility_status == "ineligible"]
        agent.report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "farm_id": agent.farm_config["farm_id"],
            "state": agent.farm_config["location"]["state"],
            "county": agent.farm_config["location"]["county"],
            "disaster_declaration_confirmed": None,
            "event_date": agent.event_date.isoformat() if agent.event_date else None,
            "summary": {
                "total_programs_evaluated": len(agent.programs),
                "confirmed": sum(1 for p in agent.programs if p.eligibility_status == "confirmed"),
                "likely": sum(1 for p in agent.programs if p.eligibility_status == "likely"),
                "check_required": sum(1 for p in agent.programs if p.eligibility_status == "check_required"),
                "ineligible": sum(1 for p in agent.programs if p.eligibility_status == "ineligible"),
            },
            "eligible_programs": [p.to_dict() for p in eligible],
            "ineligible_programs": [p.to_dict() for p in ineligible],
        }
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / "policy_report.json"
        with open(out_path, "w") as f:
            json.dump(agent.report, f, indent=2, default=str)
        logger.info("Wrote %s", out_path)
    else:
        agent.run()

    agent.print_summary()


if __name__ == "__main__":
    main()
