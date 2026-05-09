"""Report Agent — final ERPC PDF.

Combines outputs from the forecaster, crop, livestock, econ, policy, and
insurance agents into a single human-readable action briefing for the farmer.
Includes a static "evacuation go-bag" checklist.

This is a *text-heavy* briefing: prose paragraphs and bulleted lists rather than
tables or KPI grids. That is deliberate — Google Translate produces much better
output when given full sentences with surrounding context than isolated cell
values.

Usage:
    python -m forecaster.agents.report_agent
    python -m forecaster.agents.report_agent --lang es
    python -m forecaster.agents.report_agent --lang vi --output briefing_vi.pdf

Translation uses deep-translator (Google web endpoint, no API key needed).
Output: forecaster/output/action_briefing[_<lang>].pdf
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("report_agent")

REPO_ROOT = Path(__file__).parent.parent.parent
FORECASTER_OUTPUT = REPO_ROOT / "forecaster" / "output"
LIVESTOCK_DIR = REPO_ROOT / "Livestock"
CROP_DIR = REPO_ROOT / "crop_agent"
CONFIG_DIR = REPO_ROOT / "forecaster" / "config"

# Theme — kept minimal, two-color (green headings, near-black body).
GREEN = colors.HexColor("#2d6a4f")
TEXT = colors.HexColor("#1a1a1a")
TEXT2 = colors.HexColor("#5a5a5a")
TEXT3 = colors.HexColor("#9a9a9a")
RULE = colors.HexColor("#d8d6d0")

SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "zh-CN": "Chinese (Simplified)",
    "vi": "Vietnamese",
    "tl": "Tagalog (Filipino)",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "fr": "French",
    "pt": "Portuguese",
}

# Static emergency go-bag checklist.
EMERGENCY_CHECKLIST = [
    ("Personal", [
        "Government ID, passport, driver's license",
        "Insurance cards (health, property, crop, livestock)",
        "Cash and credit or debit cards",
        "Phone and charger, including a car charger or power bank",
        "Prescriptions and a seven-day supply of medication",
        "Eyeglasses or hearing aids",
    ]),
    ("Documents (originals or photocopies)", [
        "Property deeds and mortgage papers",
        "Livestock registration and health or vaccination records",
        "Crop insurance policies and recent receipts",
        "Tax records for the last three years",
        "Birth certificates, marriage certificate, and social security cards",
        "Vehicle titles and registration",
        "USB drive or cloud backup of farm records",
    ]),
    ("Animals", [
        "Halters, leads, and transport crates",
        "Portable water containers and a three-day feed supply",
        "Veterinary records and any required medications",
        "Recent photos of each animal as proof of ownership",
        "Pet carriers and leashes for dogs and cats",
    ]),
    ("Farm records", [
        "Backup of farm management software or spreadsheets",
        "Field maps and irrigation schedules",
        "Equipment inventory and serial numbers",
        "Contact list with veterinarians, suppliers, and neighboring farms",
    ]),
    ("Vehicle preparation", [
        "Fuel tank filled. Do this now, not at evacuation time.",
        "Tire pressure checked, with the spare in good condition",
        "Emergency kit with water, food, and first-aid supplies for three days",
        "Blankets, a change of clothes, and sturdy shoes",
        "Flashlight and extra batteries",
    ]),
    ("Do not forget", [
        "Family photos and irreplaceable items",
        "Comfort items for children",
        "List of evacuation contact addresses",
        "Paper maps of evacuation routes in case GPS fails",
    ]),
]


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _load_latest_crop() -> dict:
    candidates = (
        list(CROP_DIR.glob("output_*.json"))
        + list(CROP_DIR.glob("crop_agent_output_*.json"))
    )
    candidates = [p for p in candidates if "raw" not in p.name and "erpc" not in p.name]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return _load_json(candidates[0])
    return {}


# ── Translator ───────────────────────────────────────────────────────────────

_RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


def _post_process(text: str, target_lang: str) -> str:
    """Apply language-specific post-processing to translated text.
    For RTL scripts, run arabic-reshaper + python-bidi so connected forms and
    visual order are correct in left-to-right PDF rendering. We must strip
    HTML markup first — the bidi algorithm reorders chars across `<b>` tag
    boundaries, producing malformed XML that reportlab's parser rejects."""
    if not text or target_lang not in _RTL_LANGUAGES:
        return text
    try:
        import re
        import arabic_reshaper
        from bidi.algorithm import get_display
        text_plain = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
        reshaped = arabic_reshaper.reshape(text_plain)
        return get_display(reshaped)
    except Exception as e:
        logger.warning("RTL post-processing failed: %s", e)
        return text


class Translator:
    """Lazy on-demand translator with per-string cache. Falls back to English
    text on any failure so the report always renders. For RTL languages
    (Arabic etc.) the result is run through arabic-reshaper + python-bidi so
    connected forms and visual order are correct in the PDF."""

    def __init__(self, target_lang: str = "en"):
        self.target = target_lang
        self.cache: dict[str, str] = {}
        self._gt = None
        if target_lang and target_lang != "en":
            try:
                from deep_translator import GoogleTranslator
                self._gt = GoogleTranslator(source="en", target=target_lang)
            except Exception as e:
                logger.warning("Translator unavailable for %s: %s", target_lang, e)

    def t(self, text: str) -> str:
        if not text or self.target == "en" or self._gt is None:
            return text
        if text in self.cache:
            return self.cache[text]
        try:
            translated = self._gt.translate(text) or text
        except Exception:
            translated = text
        translated = _post_process(translated, self.target)
        self.cache[text] = translated
        return translated


# ── Formatting helpers ──────────────────────────────────────────────────────

def _fmt_money(n) -> str:
    """Inline money string. Numbers are kept as digits — translators preserve
    these for most languages; that's intentional so amounts stay unambiguous."""
    if not isinstance(n, (int, float)) or n == 0:
        return "$0"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f} million"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f} thousand"
    return f"${n:,.0f}"


def _fmt_hours(h) -> str:
    if not isinstance(h, (int, float)):
        return "—"
    if h < 1:
        return f"{int(h * 60)} minutes"
    if h < 48:
        return f"{h:.1f} hours"
    return f"{h / 24:.1f} days"


# ── Font registration per language ──────────────────────────────────────────
#
# Reportlab's default Helvetica is a Type 1 font with WinAnsi encoding only —
# it has no glyphs for CJK, Arabic, Devanagari, or even the full Vietnamese
# Latin Extended Additional set. Without registering a Unicode-capable font,
# translated text renders as boxes (■). Strategy:
#
#   - English and Latin-1 languages (es, pt, fr, tl):  Helvetica (default)
#   - Simplified Chinese:  STSong-Light (built-in CID font, no external file)
#   - Korean:              HYSMyeongJo-Medium (built-in CID font)
#   - Vietnamese, Arabic, Hindi, and any unhandled non-Latin language:
#                          Arial Unicode TTF (covers ~50,000 glyphs incl. CJK,
#                          Devanagari, Arabic, Latin Extended Additional)
#
# Bold variants for non-Latin fonts: most CJK/Unicode fonts on macOS don't ship
# a separate bold weight. We register the same font as both regular and bold so
# `<b>` markup in Paragraphs doesn't fail, even though it won't render bolder.

_LATIN1_LANGUAGES = {"en", "es", "pt", "fr", "tl"}
_FONT_REGISTRY: dict[str, str] = {}

_ARIAL_UNICODE_PATHS = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def _register_arial_unicode() -> Optional[str]:
    if "ArialUnicode" in _FONT_REGISTRY:
        return _FONT_REGISTRY["ArialUnicode"]
    for path in _ARIAL_UNICODE_PATHS:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("ArialUnicode", path))
                pdfmetrics.registerFontFamily(
                    "ArialUnicode",
                    normal="ArialUnicode", bold="ArialUnicode",
                    italic="ArialUnicode", boldItalic="ArialUnicode",
                )
                _FONT_REGISTRY["ArialUnicode"] = "ArialUnicode"
                return "ArialUnicode"
            except Exception as e:
                logger.warning("Failed to register Arial Unicode at %s: %s", path, e)
    return None


def _register_cid(name: str) -> Optional[str]:
    if name in _FONT_REGISTRY:
        return _FONT_REGISTRY[name]
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(name))
        pdfmetrics.registerFontFamily(
            name, normal=name, bold=name, italic=name, boldItalic=name,
        )
        _FONT_REGISTRY[name] = name
        return name
    except Exception as e:
        logger.warning("Failed to register CID font %s: %s", name, e)
        return None


def _setup_font_for_lang(target_lang: str) -> str:
    """Register a Unicode-capable font for the language; return the font name."""
    if target_lang in _LATIN1_LANGUAGES:
        return "Helvetica"

    if target_lang == "zh-CN":
        cid = _register_cid("STSong-Light")
        if cid:
            return cid
    elif target_lang == "ko":
        cid = _register_cid("HYSMyeongJo-Medium")
        if cid:
            return cid

    # vi, hi, ar, and any CJK fallback: Arial Unicode
    arial = _register_arial_unicode()
    if arial:
        return arial

    logger.warning("No Unicode font available; falling back to Helvetica — non-Latin glyphs will render as boxes")
    return "Helvetica"


def _build_styles(font_name: str = "Helvetica"):
    """Build paragraph styles using the supplied font. For Helvetica we use
    Helvetica-Bold for headings; for any other font we reuse the same name
    (no separate bold weight available in CID/Arial Unicode)."""
    bold_name = "Helvetica-Bold" if font_name == "Helvetica" else font_name
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Heading1"], fontSize=22, leading=28, textColor=GREEN,
            spaceAfter=2, fontName=bold_name,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=11, leading=15, textColor=TEXT2,
            spaceAfter=14, fontName=font_name,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading2"], fontSize=15, leading=20, textColor=GREEN,
            spaceBefore=18, spaceAfter=8, fontName=bold_name,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading3"], fontSize=12, leading=16, textColor=TEXT,
            spaceBefore=10, spaceAfter=4, fontName=bold_name,
        ),
        "lead": ParagraphStyle(
            "lead", parent=base["Normal"], fontSize=11, leading=16, textColor=TEXT,
            spaceAfter=8, fontName=bold_name,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=10.5, leading=15, textColor=TEXT,
            spaceAfter=8, fontName=font_name,
        ),
        "bullet": ParagraphStyle(
            "bullet", parent=base["Normal"], fontSize=10.5, leading=15, textColor=TEXT,
            leftIndent=18, bulletIndent=4, spaceAfter=3, fontName=font_name,
        ),
        "checkitem": ParagraphStyle(
            "checkitem", parent=base["Normal"], fontSize=10.5, leading=15, textColor=TEXT,
            leftIndent=22, spaceAfter=2, fontName=font_name,
        ),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontSize=9, leading=12, textColor=TEXT2,
            spaceAfter=8, fontName=font_name,
        ),
    }


def _para(text: str, S, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def _bullets(items: list[str], S, style: str = "bullet") -> list:
    return [Paragraph("• " + item, S[style]) for item in items]


# ── Section builders ─────────────────────────────────────────────────────────

def _section_summary(status, t, S) -> list:
    # Use title case for the threat level word inside prose. All-caps tokens
    # like "CRITICAL" sometimes confuse the translator — Vietnamese, for
    # example, maps the all-caps form to "great/wonderful" instead of
    # "critical/serious". Title case keeps the emphasis but reads as a normal
    # English adjective.
    threat_raw = status.get("threat_level") or "Unknown"
    threat = threat_raw.title()
    fire = status.get("nearest_fire") or {}
    impact = (status.get("spread_prediction") or {}).get("time_to_farm") or {}
    fwi = status.get("fwi_index")
    wind = status.get("wind_speed_kmh")
    wind_dir = status.get("wind_direction_degrees")
    temp = status.get("temperature_c")
    humidity = status.get("humidity_percent")
    gate_reason = status.get("gate_condition_reason")

    elems = [Paragraph(t("Current Threat Snapshot"), S["h1"])]

    fire_name = fire.get("name") or "no active fire detected nearby"
    fire_dist = fire.get("distance_km")
    impact_hours = impact.get("hours") if isinstance(impact, dict) else None

    if fire_dist is not None and impact_hours is not None:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"The nearest detected wildfire is {fire_name}, located {fire_dist:.1f} kilometers away. "
            f"Based on current wind and fuel conditions, fire could reach your farm in {_fmt_hours(impact_hours)}."
        )
    elif fire_dist is not None:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"The nearest detected wildfire is {fire_name}, located {fire_dist:.1f} kilometers away. "
            f"No precise time-to-impact estimate is available at the moment."
        )
    else:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"There is {fire_name} at the moment."
        )
    elems.append(_para(t(lead), S, "lead"))

    weather_parts = []
    if isinstance(wind, (int, float)) and isinstance(wind_dir, (int, float)):
        weather_parts.append(f"Wind is blowing at {wind:.0f} kilometers per hour from {wind_dir:.0f} degrees")
    if isinstance(temp, (int, float)):
        weather_parts.append(f"the temperature is {temp:.0f} degrees Celsius")
    if isinstance(humidity, (int, float)):
        weather_parts.append(f"relative humidity is {humidity:.0f} percent")
    if isinstance(fwi, (int, float)):
        weather_parts.append(f"the Fire Weather Index is {fwi:.1f}")
    if weather_parts:
        weather = "Weather conditions: " + ", ".join(weather_parts) + "."
        elems.append(_para(t(weather), S))

    if gate_reason:
        elems.append(_para(t(f"Gate condition: {gate_reason}"), S, "small"))

    return elems


def _section_livestock(livestock_status, erpc, t, S) -> list:
    elems = [Paragraph(t("Livestock Plan"), S["h1"])]
    pens = (livestock_status or {}).get("pens", []) or []
    cost_opt = (erpc or {}).get("cost_optimization", {}) or {}

    total = cost_opt.get("total_animals_at_risk", 0)
    can_evac = cost_opt.get("animals_can_evacuate", 0)
    save_value = cost_opt.get("value_can_save_usd", 0) or 0
    potential_loss = cost_opt.get("potential_loss_usd", 0) or 0

    if total == 0 and not pens:
        elems.append(_para(t("No livestock evacuation plan has been generated yet."), S))
        return elems

    intro = (
        f"You have {total} animals at risk across all pens, with an estimated total value "
        f"of {_fmt_money(save_value)}. Of those, {can_evac} can be safely evacuated using "
        f"available trailers and routes."
    )
    if potential_loss > 0:
        intro += (
            f" If evacuation cannot be fully completed, the potential loss is "
            f"{_fmt_money(potential_loss)}."
        )
    elems.append(_para(t(intro), S, "lead"))

    if pens:
        elems.append(Paragraph(t("Per-pen evacuation plan"), S["h2"]))
        bullets = []
        for p in pens[:20]:
            site = (p.get("assigned_evac_site") or {}).get("name") or "an unspecified evacuation site"
            decision = (p.get("decision") or "monitor").replace("_", " ")
            reason = (p.get("decision_reason") or "").strip()
            species = p.get("species") or "livestock"
            pen_id = p.get("pen_id") or "pen"
            sentence = f"<b>{pen_id}</b> ({species}): plan is to {decision} to {site}."
            if reason:
                sentence += f" {reason}."
            bullets.append(t(sentence))
        elems += _bullets(bullets, S)

    return elems


def _section_crop(crop, t, S) -> list:
    elems = [Paragraph(t("Crop Plan"), S["h1"])]
    decisions = crop.get("field_decisions") or crop.get("task4") or []
    impacts = (crop.get("economic_impact") or crop.get("task2") or {}).get("crop_destructions", []) or []
    hydration = crop.get("hydration_strategy") or crop.get("task3") or []

    if not (decisions or impacts or hydration):
        elems.append(_para(
            t("No crop actions are required at the current threat level. The crop agent did not flag any field as economically impacted."),
            S,
        ))
        return elems

    # Decisions paragraph
    if decisions:
        elems.append(Paragraph(t("Field decisions"), S["h2"]))
        intro = f"The crop agent reviewed {len(decisions)} field" + ("s" if len(decisions) != 1 else "") + " and recommends the following actions."
        elems.append(_para(t(intro), S))
        bullets = []
        for d in decisions:
            fid = d.get("field_id", "field")
            crop_cat = d.get("crop_category", "crop")
            maturity = d.get("maturity_pct", 0)
            arrival = d.get("fire_arrival_hours", 0)
            decision = d.get("decision", "monitor")
            reason = (d.get("reason") or "").strip()
            sent = (
                f"<b>{fid}</b> ({crop_cat}, {maturity}% mature): {decision}. "
                f"Fire is estimated to arrive in {_fmt_hours(arrival)}."
            )
            if reason:
                sent += f" {reason}."
            bullets.append(t(sent))
        elems += _bullets(bullets, S)

    # Economic impact
    if impacts:
        elems.append(Paragraph(t("Economic impact per field"), S["h2"]))
        bullets = []
        for d in impacts:
            fid = d.get("field_id", "field")
            crop_cat = d.get("crop_category", "crop")
            acres = d.get("size_acres", 0)
            adj_loss = d.get("confidence_adjusted_loss_usd", 0)
            decision = (d.get("task4_decision") or "no action").lower()
            sent = (
                f"<b>{fid}</b> ({crop_cat}, {acres:.1f} acres): estimated loss is "
                f"{_fmt_money(adj_loss)} if the field is left unprotected. "
                f"Recommended action: {decision}."
            )
            bullets.append(t(sent))
        elems += _bullets(bullets, S)

    # Hydration / firebreaks
    if hydration:
        elems.append(Paragraph(t("Hydration and firebreak schedule"), S["h2"]))
        bullets = []
        for h in hydration:
            fid = h.get("field_id", "field")
            tech = h.get("technique", "monitor")
            urgency = (h.get("urgency") or "scheduled").lower()
            arr = h.get("hours_to_arrival", 0)
            sent = (
                f"<b>{fid}</b>: apply {tech}. Urgency is {urgency}. "
                f"Fire is estimated to arrive in {_fmt_hours(arr)}."
            )
            bullets.append(t(sent))
        elems += _bullets(bullets, S)

    return elems


def _section_financial(econ, t, S) -> list:
    elems = [Paragraph(t("Financial Snapshot"), S["h1"])]
    exp = (econ or {}).get("financial_exposure", {}) or {}
    actions = (econ or {}).get("action_queue", []) or []
    infeasible = (econ or {}).get("infeasible_actions", []) or []

    total = exp.get("total_exposure_usd", 0) or 0
    crop_loss = exp.get("crop_loss_total_usd", 0) or 0
    livestock = exp.get("livestock_at_risk_usd", 0) or 0
    opportunity = exp.get("opportunity_cost_usd", 0) or 0

    intro = (
        f"Your total estimated financial exposure if no protective action is taken is "
        f"{_fmt_money(total)}. This breaks down as {_fmt_money(crop_loss)} in potential crop losses, "
        f"{_fmt_money(livestock)} in livestock value at risk, and {_fmt_money(opportunity)} in "
        f"opportunity cost from disrupted operations."
    )
    elems.append(_para(t(intro), S, "lead"))

    if actions:
        elems.append(Paragraph(t("Recommended actions, ranked by return on investment"), S["h2"]))
        bullets = []
        for i, a in enumerate(actions[:8], 1):
            roi = a.get("roi", 0)
            roi_str = f"{roi:.1f} times the cost" if isinstance(roi, (int, float)) and roi > 0 else "value to be confirmed"
            urgency = (a.get("urgency") or "scheduled").lower()
            avoided = a.get("confidence_adjusted_loss_avoided_usd", 0)
            cost = a.get("estimated_action_cost_usd", 0)
            desc = (a.get("action_description") or "").strip()
            sent = (
                f"<b>Action {i}</b> ({urgency}): {desc}. "
                f"This protects roughly {_fmt_money(avoided)} at an estimated cost of "
                f"{_fmt_money(cost)} — a return of {roi_str}."
            )
            bullets.append(t(sent))
        elems += _bullets(bullets, S)

    if infeasible:
        elems.append(Paragraph(t("Blocked actions"), S["h2"]))
        elems.append(_para(
            t("These actions cannot be completed under current conditions or with available resources:"),
            S,
        ))
        bullets = []
        for a in infeasible[:5]:
            aid = a.get("action_id") or "action"
            reason = (a.get("infeasibility_reason") or "blocker not specified").strip()
            bullets.append(t(f"<b>{aid}</b>: {reason}."))
        elems += _bullets(bullets, S)

    return elems


def _section_aid(policy, insurance_filled: bool, t, S) -> list:
    elems = [Paragraph(t("Aid and Insurance"), S["h1"])]

    if insurance_filled:
        elems.append(_para(
            t(
                "Your USDA CCC-576 Notice of Loss has been pre-filled with farm and disaster data. "
                "You must file it within 30 days of the loss event at your local Farm Service Agency office. "
                "The pre-filled PDF is available in the Insurance section of your dashboard."
            ),
            S,
        ))

    eligible = (policy or {}).get("eligible_programs", []) or []
    if eligible:
        elems.append(Paragraph(t("Eligible aid programs"), S["h2"]))
        elems.append(_para(
            t(f"The policy agent identified {len(eligible)} program" + ("s" if len(eligible) != 1 else "") + " for which your farm is likely eligible. The most relevant are listed below."),
            S,
        ))
        bullets = []
        for p in eligible[:6]:
            name = p.get("name") or "Program"
            agency = p.get("agency") or "Agency"
            deadline = p.get("deadline") or "ongoing"
            status = (p.get("eligibility_status") or "likely").lower()
            sent = (
                f"<b>{name}</b> ({agency}): eligibility status is {status}. "
                f"Filing deadline: {deadline}."
            )
            bullets.append(t(sent))
        elems += _bullets(bullets, S)
    else:
        elems.append(_para(
            t("No policy data is available yet. Run the policy agent to identify federal, state, and local aid programs you may qualify for."),
            S,
        ))

    return elems


def _section_checklist(t, S) -> list:
    elems = [PageBreak(), Paragraph(t("Evacuation Go-Bag Checklist"), S["h1"])]
    elems.append(_para(
        t(
            "Pack these items now so you can leave the property within fifteen minutes if a critical "
            "threat is declared. Keep the bag near your primary exit, and rotate perishable items "
            "(food, medications, water) every six months."
        ),
        S, "lead",
    ))
    for cat_title, items in EMERGENCY_CHECKLIST:
        elems.append(Paragraph(t(cat_title), S["h2"]))
        for item in items:
            elems.append(Paragraph("☐  " + t(item), S["checkitem"]))
        elems.append(Spacer(1, 4))
    return elems


def _section_contacts(livestock_status, t, S) -> list:
    elems = [Paragraph(t("Emergency Contacts"), S["h1"])]
    elems.append(_para(
        t("Save these numbers in your phone now. Print this page and keep a paper copy in your go-bag in case your phone dies during evacuation."),
        S,
    ))

    items = [
        "Emergency services: 911",
        "CAL FIRE information line: 1-800-540-2722",
        "San Diego County Farm Service Agency: (760) 745-3061",
        "California wildfire updates: https://www.fire.ca.gov/incidents/",
    ]

    pens = (livestock_status or {}).get("pens", []) or []
    sites_seen = set()
    for p in pens[:10]:
        site = p.get("assigned_evac_site") or {}
        name = site.get("name")
        if name and name not in sites_seen:
            sites_seen.add(name)
            lat = site.get("lat")
            lon = site.get("lon")
            coords = f" (located at {lat}, {lon})" if lat is not None and lon is not None else ""
            items.append(f"Assigned evacuation site: {name}{coords}")

    elems += _bullets([t(s) for s in items], S)
    return elems


# ── Page furniture ───────────────────────────────────────────────────────────

def _on_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(TEXT3)
    page = canvas.getPageNumber()
    canvas.drawString(0.6 * inch, 0.4 * inch, "Reeboot the Earth — Action Briefing")
    canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f"Page {page}")
    canvas.restoreState()


# ── Build report ─────────────────────────────────────────────────────────────

def build_report(target_lang: str = "en", output_path: Optional[Path] = None) -> Path:
    farm_config = _load_json(CONFIG_DIR / "farm_config.json")
    status = _load_json(FORECASTER_OUTPUT / "status.json")
    livestock_status = _load_json(LIVESTOCK_DIR / "livestock_status.json")
    erpc = _load_json(LIVESTOCK_DIR / "erpc_message.json")
    crop = _load_latest_crop()
    econ = _load_json(FORECASTER_OUTPUT / "econ_report.json")
    policy = _load_json(FORECASTER_OUTPUT / "policy_report.json")
    insurance_filled = (FORECASTER_OUTPUT / "ccc_576_filled.pdf").exists()

    farm_name = farm_config.get("farm_name") or status.get("farm_name") or "Farm"
    timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    if target_lang not in SUPPORTED_LANGUAGES:
        logger.warning("Unsupported language %s — falling back to English", target_lang)
        target_lang = "en"
    translator = Translator(target_lang)
    t = translator.t

    if output_path is None:
        suffix = "" if target_lang == "en" else f"_{target_lang}"
        output_path = FORECASTER_OUTPUT / f"action_briefing{suffix}.pdf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    font_name = _setup_font_for_lang(target_lang)
    logger.info("Using font %s for language %s", font_name, target_lang)
    S = _build_styles(font_name)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="Action Briefing",
    )

    flow = []
    flow.append(Paragraph(t("Wildfire Action Briefing"), S["title"]))
    subtitle = (
        f"Prepared for <b>{farm_name}</b>. Generated on {timestamp}."
    )
    if target_lang != "en":
        subtitle += f" Language: <b>{SUPPORTED_LANGUAGES[target_lang]}</b>."
    flow.append(_para(t(subtitle), S, "subtitle"))

    flow += _section_summary(status, t, S)
    flow += _section_livestock(livestock_status, erpc, t, S)
    flow += _section_crop(crop, t, S)
    flow += _section_financial(econ, t, S)
    flow += _section_aid(policy, insurance_filled, t, S)
    flow += _section_checklist(t, S)
    flow += _section_contacts(livestock_status, t, S)

    doc.build(flow, onFirstPage=_on_page, onLaterPages=_on_page)
    logger.info("Wrote %s (%d translated strings cached)", output_path, len(translator.cache))
    return output_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate the comprehensive action briefing PDF")
    parser.add_argument("--lang", default="en", choices=list(SUPPORTED_LANGUAGES.keys()),
                        help="Target language (default: en)")
    parser.add_argument("--output", help="Output PDF path")
    args = parser.parse_args()

    out = build_report(target_lang=args.lang, output_path=args.output)
    print(f"\n  Action briefing written: {out}")
    print(f"  Language: {SUPPORTED_LANGUAGES[args.lang]}")


if __name__ == "__main__":
    main()
