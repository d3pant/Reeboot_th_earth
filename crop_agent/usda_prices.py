"""
USDA price fetching module.
Fetch chain: USDA NASS QuickStats → 2025 historical fallback
"""

import os
import requests
from datetime import datetime

USDA_NASS_BASE = "https://quickstats.nass.usda.gov/api/api_GET/"

# Real-world average yields per acre (USDA 2024-2025)
CROP_YIELDS = {
    "wheat":        {"yield_per_acre": 50.0,    "unit": "bushel"},
    "corn":         {"yield_per_acre": 177.0,   "unit": "bushel"},
    "soybeans":     {"yield_per_acre": 51.0,    "unit": "bushel"},
    "cotton":       {"yield_per_acre": 900.0,   "unit": "lb"},
    "alfalfa":      {"yield_per_acre": 5.5,     "unit": "ton"},
    "grapes":       {"yield_per_acre": 8.2,     "unit": "ton"},
    "almonds":      {"yield_per_acre": 1.6,     "unit": "ton"},
    "tomatoes":     {"yield_per_acre": 40.0,    "unit": "ton"},
    "potatoes":     {"yield_per_acre": 430.0,   "unit": "cwt"},
    "barley":       {"yield_per_acre": 70.0,    "unit": "bushel"},
    "oats":         {"yield_per_acre": 65.0,    "unit": "bushel"},
    "sorghum":      {"yield_per_acre": 70.0,    "unit": "bushel"},
    "cover_crop":   {"yield_per_acre": 2.0,     "unit": "ton"},
    "sunflower":    {"yield_per_acre": 1400.0,  "unit": "lb"},
    "canola":       {"yield_per_acre": 1800.0,  "unit": "lb"},
    "strawberries": {"yield_per_acre": 30000.0, "unit": "lb"},
    "avocado":      {"yield_per_acre": 5.0,     "unit": "ton"},
    "citrus":       {"yield_per_acre": 15.0,    "unit": "ton"},
    "walnuts":      {"yield_per_acre": 1.8,     "unit": "ton"},
    "pistachios":   {"yield_per_acre": 1.2,     "unit": "ton"},
    "oranges":      {"yield_per_acre": 15.0,    "unit": "ton"},
    "lemons":       {"yield_per_acre": 12.0,    "unit": "ton"},
}

# NASS commodity name mapping (exact strings NASS accepts)
NASS_COMMODITY_MAP = {
    "wheat":        "WHEAT",
    "corn":         "CORN",
    "soybeans":     "SOYBEANS",
    "cotton":       "COTTON",
    "alfalfa":      "HAY, ALFALFA",
    "grapes":       "GRAPES",
    "almonds":      "ALMONDS",
    "tomatoes":     "TOMATOES",
    "potatoes":     "POTATOES",
    "barley":       "BARLEY",
    "oats":         "OATS",
    "sorghum":      "SORGHUM",
    "strawberries": "STRAWBERRIES",
    "avocado":      "AVOCADOS",
    "citrus":       "ORANGES",
    "walnuts":      "WALNUTS",
    "pistachios":   "PISTACHIOS",
    "canola":       "CANOLA",
    "sunflower":    "SUNFLOWER",
    "oranges":      "ORANGES",
    "lemons":       "LEMONS",
}

# Hardcoded 2025 fallback prices (USDA NASS 2024-2025 averages)
FALLBACK_PRICES_2025 = {
    "wheat":        {"price_per_unit": 5.50,    "unit": "bushel"},
    "corn":         {"price_per_unit": 4.35,    "unit": "bushel"},
    "soybeans":     {"price_per_unit": 10.10,   "unit": "bushel"},
    "cotton":       {"price_per_unit": 0.71,    "unit": "lb"},
    "alfalfa":      {"price_per_unit": 185.0,   "unit": "ton"},
    "grapes":       {"price_per_unit": 1100.0,  "unit": "ton"},
    "almonds":      {"price_per_unit": 2800.0,  "unit": "ton"},
    "tomatoes":     {"price_per_unit": 85.0,    "unit": "ton"},
    "potatoes":     {"price_per_unit": 11.0,    "unit": "cwt"},
    "barley":       {"price_per_unit": 5.30,    "unit": "bushel"},
    "oats":         {"price_per_unit": 3.90,    "unit": "bushel"},
    "sorghum":      {"price_per_unit": 4.20,    "unit": "bushel"},
    "cover_crop":   {"price_per_unit": 80.0,    "unit": "ton"},
    "sunflower":    {"price_per_unit": 0.22,    "unit": "lb"},
    "canola":       {"price_per_unit": 0.28,    "unit": "lb"},
    "strawberries": {"price_per_unit": 1.10,    "unit": "lb"},
    "avocado":      {"price_per_unit": 1850.0,  "unit": "ton"},
    "citrus":       {"price_per_unit": 350.0,   "unit": "ton"},
    "walnuts":      {"price_per_unit": 1400.0,  "unit": "ton"},
    "pistachios":   {"price_per_unit": 3200.0,  "unit": "ton"},
    "oranges":      {"price_per_unit": 350.0,   "unit": "ton"},
    "lemons":       {"price_per_unit": 420.0,   "unit": "ton"},
}


def _clean_yield_unit(raw: str) -> str:
    """Normalise NASS yield unit (e.g. 'BU / ACRE' → 'bu', 'TONS / ACRE' → 'ton')."""
    u = raw.lower().replace(" / acre", "").replace("/acre", "").strip()
    return "ton" if u == "tons" else u


def _fetch_nass_yield(crop: str) -> dict | None:
    """Fetch yield per acre from USDA NASS QuickStats API."""
    api_key = os.getenv("USDA_NASS_API_KEY", "")
    if not api_key:
        return None
    commodity = NASS_COMMODITY_MAP.get(crop)
    if not commodity:
        return None
    try:
        r = requests.get(
            USDA_NASS_BASE,
            params={
                "key":               api_key,
                "commodity_desc":    commodity,
                "statisticcat_desc": "YIELD",
                "year":              str(datetime.now().year - 1),
                "format":            "JSON",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("data", [])
        for item in reversed(items):
            val = item.get("Value", "").strip().replace(",", "")
            if val and val not in ("(D)", "(Z)", "(NA)"):
                try:
                    unit = _clean_yield_unit(item.get("unit_desc", ""))
                    return {
                        "yield_per_acre": float(val),
                        "unit":           unit,
                        "source":         "USDA NASS",
                    }
                except (ValueError, TypeError):
                    continue
        return None
    except Exception as e:
        print(f"    NASS yield error ({crop}): {e}")
        return None


def _fetch_nass(crop: str) -> dict | None:
    """Fetch price from USDA NASS QuickStats API."""
    api_key = os.getenv("USDA_NASS_API_KEY", "")
    if not api_key:
        return None
    commodity = NASS_COMMODITY_MAP.get(crop)
    if not commodity:
        return None
    try:
        r = requests.get(
            USDA_NASS_BASE,
            params={
                "key": api_key,
                "commodity_desc": commodity,
                "statisticcat_desc": "PRICE RECEIVED",
                "year": str(datetime.now().year - 1),
                "format": "JSON",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("data", [])
        for item in reversed(items):
            val = item.get("Value", "").strip().replace(",", "")
            if val and val not in ("(D)", "(Z)", "(NA)"):
                try:
                    return {
                        "price_per_unit_usd": float(val),
                        "unit": item.get("unit_desc", "unit").lower(),
                        "report_date": f"{item.get('year')}-{item.get('reference_period_desc', '')}",
                        "source": "USDA NASS",
                    }
                except (ValueError, TypeError):
                    continue
        return None
    except Exception as e:
        print(f"    NASS API error ({crop}): {e}")
        return None


# Conversion factors: (from_unit, to_unit) -> multiply_by
_UNIT_CONVERSIONS = {
    ("lb",  "cwt"):    0.01,
    ("cwt", "lb"):   100.0,
    ("lb",  "ton"):    0.0005,
    ("ton", "lb"):  2000.0,
    ("ton", "cwt"):   20.0,
    ("cwt", "ton"):    0.05,
}

def _clean_unit(raw: str) -> str:
    """Strip NASS '$ / ' prefix and normalise to lowercase."""
    return raw.lower().replace("$ / ", "").replace("$/", "").strip()


def _align_yield(yield_per_acre: float, yield_unit: str, price_unit: str) -> float:
    """Convert yield so its unit matches the price unit."""
    if yield_unit == price_unit:
        return yield_per_acre
    factor = _UNIT_CONVERSIONS.get((yield_unit, price_unit))
    return round(yield_per_acre * factor, 4) if factor else yield_per_acre


def fetch_live_prices(crop_categories: list) -> dict:
    """
    Fetch live prices for all crops.
    Chain: USDA NASS -> 2025 historical fallback
    Returns dict with 'live_prices' list ready to pass to the LLM.
    """
    live_prices = []
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    for crop in crop_categories:
        key = crop.lower().replace(" ", "_").replace("-", "_")

        print(f"  Fetching: {crop}")
        nass_yield = _fetch_nass_yield(key)
        if nass_yield:
            crop_info = {"yield_per_acre": nass_yield["yield_per_acre"], "unit": nass_yield["unit"]}
            print(f"    NASS yield: {nass_yield['yield_per_acre']} {nass_yield['unit']}/acre")
        else:
            crop_info = CROP_YIELDS.get(key, {"yield_per_acre": 1.0, "unit": "unit"})
            print(f"    yield fallback: {crop_info['yield_per_acre']} {crop_info['unit']}/acre")

        price_data = _fetch_nass(key)

        if not price_data:
            fallback = FALLBACK_PRICES_2025.get(key)
            if fallback:
                print(f"    NASS unavailable -> using 2025 historical fallback")
                price_data = {
                    "price_per_unit_usd": fallback["price_per_unit"],
                    "unit": fallback["unit"],
                    "report_date": "USDA 2025 Historical Average",
                    "source": "USDA 2025 Historical (fallback)",
                }
            else:
                print(f"    WARNING: No price data found for '{crop}' — skipping")
                continue

        # Clean unit string and align yield to price unit
        price_unit  = _clean_unit(price_data["unit"])
        yield_unit  = crop_info["unit"]
        yield_value = _align_yield(crop_info["yield_per_acre"], yield_unit, price_unit)

        # If units don't match and no conversion exists, the price_per_acre will be wrong
        # Fall back to hardcoded price in that case
        if yield_unit != price_unit and (yield_unit, price_unit) not in _UNIT_CONVERSIONS:
            fallback = FALLBACK_PRICES_2025.get(key)
            if fallback:
                print(f"    Unit mismatch ({yield_unit} vs {price_unit}) -> using 2025 historical fallback")
                price_data = {
                    "price_per_unit_usd": fallback["price_per_unit"],
                    "unit": fallback["unit"],
                    "report_date": "USDA 2025 Historical Average",
                    "source": "USDA 2025 Historical (fallback)",
                }
                price_unit  = fallback["unit"]
                yield_value = crop_info["yield_per_acre"]

        price_per_acre = round(price_data["price_per_unit_usd"] * yield_value, 2)
        print(
            f"    ${price_data['price_per_unit_usd']:.2f}/{price_unit} "
            f"x {yield_value} {price_unit}/acre "
            f"= ${price_per_acre:.2f}/acre  [{price_data['source']}]"
        )

        live_prices.append({
            "crop_category": crop,
            "price_per_unit_usd": price_data["price_per_unit_usd"],
            "unit": price_unit,
            "yield_per_acre": yield_value,
            "price_per_acre_usd": price_per_acre,
            "fetched_at": now,
            "usda_report_date": price_data.get("report_date", "N/A"),
            "source": price_data.get("source", "unknown"),
        })

    return {"live_prices": live_prices}
