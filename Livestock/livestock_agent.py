import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "crop_agent" / ".env")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# USDA NASS QuickStats API prices (2024 average, $/head)
USDA_NASS_PRICES = {
    "cattle": 1450,      # Feeder cattle
    "horse": 3500,       # Horse
    "sheep": 350,        # Lamb/sheep
    "pig": 550,          # Hog
    "goat": 300,         # Goat
}

LIVESTOCK_DIR = Path(__file__).parent
FAIRPLEX_POMONA = {"lat": 34.0564, "lon": -117.7500, "name": "Fairplex Pomona"}
DEL_MAR_FAIRGROUNDS = {"lat": 32.9595, "lon": -117.2653, "name": "Del Mar Fairgrounds"}

FALLBACK_SITES = [FAIRPLEX_POMONA, DEL_MAR_FAIRGROUNDS]

BASE_SPECIES_SCORES = {
    "cattle": 40,
    "horse": 50,
    "sheep": 30,
    "pig": 25,
    "goat": 28,
}

AGE_MODIFIERS = {
    "juvenile": 20,
    "mixed": 10,
    "adult": 0,
}

HEALTH_MODIFIERS = {
    "sick": 20,
    "mixed": 10,
    "healthy": 0,
}


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    y = math.sin(dlon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    bearing_rad = math.atan2(y, x)
    bearing_deg = math.degrees(bearing_rad)
    return (bearing_deg + 360) % 360


def angle_diff(a: float, b: float) -> float:
    diff = (a - b + 180) % 360 - 180
    return abs(diff)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat, dlon = lat2_rad - lat1_rad, lon2_rad - lon1_rad
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def compute_priority_score(pen: Dict[str, Any], time_to_impact_hours: float) -> float:
    base_species = BASE_SPECIES_SCORES.get(pen["species"], 0)
    age_modifier = AGE_MODIFIERS.get(pen["age_distribution"], 0)
    health_modifier = HEALTH_MODIFIERS.get(pen["health_status"], 0)

    avg_value = pen.get("avg_market_value_usd", 0)
    value_score = min(20, (avg_value / 2000) * 20)

    zone_urgency = min(30, max(0, 100 - (time_to_impact_hours * 8)))

    priority = base_species + age_modifier + health_modifier + value_score + zone_urgency
    return priority


def compute_evacuation_optimization(pens_output: List[Dict], farm: Dict[str, Any], time_available_hours: float) -> Dict[str, Any]:
    """
    Minimize financial loss under hard truck capacity.

    Scoring per pen (fractional knapsack with urgency):
      score = 0.6 * (value_per_head / max_value_per_head)
            + 0.4 * urgency
    where urgency = 1 - clamp(route_hours / time_to_impact, 0, 1).
    Pens that fit entirely go first (sorted by score desc), then partial fill
    of the highest-value remaining pen to use every slot.
    """
    vehicle_capacity = farm.get("infrastructure", {}).get("vehicle_capacity_head", 150)
    available_trailers = farm.get("infrastructure", {}).get("available_trailers", 1)

    # Realistic trips: time_available / (2 * avg_route_time), min 1
    evacuable_pens_raw = [p for p in pens_output if p["decision"] == "evacuate"]
    avg_route = (
        sum(p["route_duration_hours"] for p in evacuable_pens_raw) / len(evacuable_pens_raw)
        if evacuable_pens_raw else 1.0
    )
    trips_per_trailer = max(1, int(time_available_hours / (2 * avg_route)))
    total_capacity = vehicle_capacity * available_trailers * trips_per_trailer

    pen_lookup = {pen["pen_id"]: pen for pen in farm["pens"]}

    candidates = []
    for p in evacuable_pens_raw:
        pen_data = pen_lookup.get(p["pen_id"])
        if not pen_data:
            continue
        count = pen_data["count"]
        price = pen_data["avg_market_value_usd"]
        total_value = count * price
        route_hrs = p["route_duration_hours"] or avg_route
        candidates.append({
            "pen_id": p["pen_id"],
            "species": p["species"],
            "count": count,
            "price_per_head": price,
            "total_value": total_value,
            "route_hours": route_hrs,
        })

    if not candidates:
        return {
            "transport_capacity": {"vehicles": available_trailers, "capacity_per_vehicle": vehicle_capacity,
                                   "total_capacity": total_capacity, "trips_per_trailer": trips_per_trailer},
            "evacuation_sequence": [],
            "summary": {"total_animals_at_risk": 0, "animals_can_evacuate": 0,
                        "value_can_save_usd": 0, "potential_loss_usd": 0, "loss_percentage": 0}
        }

    max_price = max(c["price_per_head"] for c in candidates)

    for c in candidates:
        urgency = 1.0 - min(c["route_hours"] / max(time_available_hours, 0.1), 1.0)
        c["score"] = 0.6 * (c["price_per_head"] / max_price) + 0.4 * urgency

    # Sort highest score first — this is the load order for the trucks
    candidates.sort(key=lambda x: x["score"], reverse=True)

    evacuation_plan = []
    remaining_capacity = total_capacity
    value_saved = 0.0

    for pen in candidates:
        if remaining_capacity <= 0:
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": 0,
                "value_saved_usd": 0,
                "status": "MUST_SHELTER",
                "reason": f"No capacity — load higher-value pens first (score {pen['score']:.2f})",
            })
            continue

        if pen["count"] <= remaining_capacity:
            # Full pen fits
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": pen["count"],
                "value_saved_usd": pen["total_value"],
                "status": "EVACUATE",
                "reason": f"score {pen['score']:.2f} | ${pen['price_per_head']:,}/head | {pen['route_hours']:.1f}h route",
            })
            remaining_capacity -= pen["count"]
            value_saved += pen["total_value"]
        else:
            # Partial — fill every remaining slot with the highest-value animals
            partial_value = round(pen["total_value"] * remaining_capacity / pen["count"], 2)
            evacuation_plan.append({
                "sequence": len(evacuation_plan) + 1,
                "pen_id": pen["pen_id"],
                "species": pen["species"],
                "count": pen["count"],
                "value_usd": pen["total_value"],
                "animals_saved": remaining_capacity,
                "value_saved_usd": partial_value,
                "status": "PARTIAL",
                "reason": f"Only {remaining_capacity} slots left of {pen['count']} — take highest-value animals first",
            })
            value_saved += partial_value
            remaining_capacity = 0

    total_value_at_risk = sum(c["total_value"] for c in candidates)
    potential_loss = total_value_at_risk - value_saved
    total_animals = sum(c["count"] for c in candidates)
    animals_saved = sum(e["animals_saved"] for e in evacuation_plan)

    return {
        "transport_capacity": {
            "vehicles": available_trailers,
            "capacity_per_vehicle": vehicle_capacity,
            "trips_per_trailer": trips_per_trailer,
            "total_capacity": total_capacity,
        },
        "evacuation_sequence": evacuation_plan,
        "summary": {
            "total_animals_at_risk": total_animals,
            "animals_can_evacuate": animals_saved,
            "value_can_save_usd": round(value_saved, 2),
            "potential_loss_usd": round(potential_loss, 2),
            "loss_percentage": round(potential_loss / total_value_at_risk * 100, 1) if total_value_at_risk else 0,
        },
    }


async def fetch_usda_prices(client: httpx.AsyncClient) -> Dict[str, float]:
    """Fetch live USDA NASS livestock prices from QuickStats API"""
    try:
        logger.info("Fetching live USDA NASS livestock prices")
        # USDA NASS QuickStats commodities
        commodities = {
            "cattle": "CATTLE, FEEDER, PRICE RECEIVED",
            "horse": "HORSES",
            "sheep": "SHEEP & LAMBS, PRICE RECEIVED",
            "pig": "HOGS, PRICE RECEIVED",
            "goat": "GOATS"
        }

        prices = {}
        for species, commodity in commodities.items():
            try:
                url = "https://quickstats.nass.usda.gov/api/api_GET/"
                params = {
                    "key": os.getenv("USDA_NASS_API_KEY", ""),
                    "commodity_desc": commodity,
                    "statisticcat_desc": "PRICE RECEIVED",
                    "year": str(datetime.now().year - 1),
                    "format": "JSON"
                }
                response = await client.get(url, params=params, timeout=5.0)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("data"):
                        # Get most recent price
                        price = float(data["data"][0].get("Value", USDA_NASS_PRICES[species]))
                        prices[species] = price
                        logger.info(f"USDA NASS {species}: ${price:.2f}/head")
                    else:
                        prices[species] = USDA_NASS_PRICES[species]
                else:
                    prices[species] = USDA_NASS_PRICES[species]
            except Exception as e:
                logger.warning(f"USDA NASS {species} error: {e}. Using default: ${USDA_NASS_PRICES[species]}")
                prices[species] = USDA_NASS_PRICES[species]

        return prices
    except Exception as e:
        logger.warning(f"USDA NASS fetch failed: {e}. Using default prices.")
        return USDA_NASS_PRICES


async def query_calaes(client: httpx.AsyncClient, lat: float, lon: float) -> List[Dict[str, Any]]:
    url = "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "outSR": 4326,
        "distance": 50000,
        "units": "esriSRUnit_Meter",
        "outFields": "*",
        "f": "geojson",
        "returnGeometry": "true"
    }

    try:
        logger.info(f"Querying CalOES API for farm centroid ({lat}, {lon})")
        response = await client.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"CalOES API raw response: {json.dumps(data)}")

        features = data.get("features", [])
        filtered = [
            f for f in features
            if f.get("properties", {}).get("Status") in ["EVACUATION WARNING", "NORMAL"]
        ]

        sites = []
        for feature in filtered:
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            coords = geom.get("coordinates", [0, 0])

            site = {
                "name": props.get("ZoneName") or props.get("ExternalIdentifier", "Unknown"),
                "lat": coords[1],
                "lon": coords[0],
            }
            sites.append(site)

        return sites if sites else FALLBACK_SITES
    except (httpx.TimeoutException, httpx.HTTPError, Exception) as e:
        logger.warning(f"CalOES API error: {e}. Using fallback sites.")
        return FALLBACK_SITES


async def query_osrm(
    client: httpx.AsyncClient,
    lat1: float, lon1: float,
    lat2: float, lon2: float
) -> Optional[Dict[str, float]]:
    url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    params = {"overview": "false"}

    try:
        logger.info(f"OSRM route query: ({lat1}, {lon1}) -> ({lat2}, {lon2})")
        response = await client.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(f"OSRM error: {data.get('code')}")

        route = data["routes"][0]
        duration_seconds = route.get("duration", 0)
        distance_meters = route.get("distance", 0)

        return {
            "duration_hours": duration_seconds / 3600.0,
            "distance_km": distance_meters / 1000.0,
            "source": "osrm"
        }
    except Exception as e:
        logger.warning(f"OSRM error: {e}. Using fallback calculation.")
        distance_km = haversine(lat1, lon1, lat2, lon2)
        return {
            "duration_hours": (distance_km * 1.35) / 60.0,
            "distance_km": distance_km,
            "source": "fallback"
        }


async def compute_routes_for_pen(
    client: httpx.AsyncClient,
    pen: Dict[str, Any],
    sites: List[Dict[str, Any]],
    pen_routes: Dict[str, List[Dict]]
) -> None:
    pen_id = pen["pen_id"]
    pen_lat, pen_lon = pen["centroid"]["lat"], pen["centroid"]["lon"]

    routes = []
    for i, site in enumerate(sites):
        if i > 0 and i % 5 == 0:
            await asyncio.sleep(0.5)

        result = await query_osrm(client, pen_lat, pen_lon, site["lat"], site["lon"])
        if result:
            routes.append({
                "site": site,
                "duration_hours": result["duration_hours"],
                "distance_km": result["distance_km"],
                "source": result["source"]
            })

    pen_routes[pen_id] = routes


async def compute_pool_route(
    client: httpx.AsyncClient,
    farm_lat: float, farm_lon: float,
    neighbor_lat: float, neighbor_lon: float,
    site_lat: float, site_lon: float
) -> Optional[Dict[str, float]]:
    leg1 = await query_osrm(client, farm_lat, farm_lon, neighbor_lat, neighbor_lon)
    if not leg1:
        return None

    leg2 = await query_osrm(client, neighbor_lat, neighbor_lon, site_lat, site_lon)
    if not leg2:
        return None

    return {
        "total_duration_hours": leg1["duration_hours"] + leg2["duration_hours"],
        "total_distance_km": leg1["distance_km"] + leg2["distance_km"]
    }


async def compute_return_trip(
    client: httpx.AsyncClient,
    site_lat: float, site_lon: float,
    neighbor_lat: float, neighbor_lon: float,
    neighbor_site_lat: float, neighbor_site_lon: float
) -> Optional[Dict[str, float]]:
    leg1 = await query_osrm(client, site_lat, site_lon, neighbor_lat, neighbor_lon)
    if not leg1:
        return None

    leg2 = await query_osrm(client, neighbor_lat, neighbor_lon, neighbor_site_lat, neighbor_site_lon)
    if not leg2:
        return None

    return {
        "total_duration_hours": leg1["duration_hours"] + leg2["duration_hours"],
        "total_distance_km": leg1["distance_km"] + leg2["distance_km"]
    }


async def main():
    with open(LIVESTOCK_DIR / "status.json") as f:
        status = json.load(f)

    with open(LIVESTOCK_DIR / "wake_up_packet.json") as f:
        wake_up = json.load(f)

    with open(LIVESTOCK_DIR / "farm_profile.json") as f:
        farm = json.load(f)

    # Fetch live USDA NASS prices
    async with httpx.AsyncClient() as price_client:
        usda_prices = await fetch_usda_prices(price_client)
        logger.info(f"Using USDA NASS prices: {usda_prices}")

    with open(LIVESTOCK_DIR / "neighboring_farms.json") as f:
        neighbors_data = json.load(f)

    async with httpx.AsyncClient() as client:
        farm_centroid = farm["centroid"]
        evac_sites = await query_calaes(client, farm_centroid["lat"], farm_centroid["lon"])

        # Update pen values with USDA prices
        for pen in farm["pens"]:
            species = pen["species"]
            pen["avg_market_value_usd"] = usda_prices.get(species, USDA_NASS_PRICES.get(species, 1000))
            logger.info(f"Pen {pen['pen_id']} ({species}): ${pen['avg_market_value_usd']}/head")

        pen_routes: Dict[str, List[Dict]] = {}
        for pen in farm["pens"]:
            await compute_routes_for_pen(client, pen, evac_sites, pen_routes)

        threat_level = status["threat_level"]
        if threat_level == "WATCH":
            phase = "pre_stage"
        elif threat_level == "WARNING":
            phase = "movement_orders"
        elif threat_level == "CRITICAL":
            phase = "execute_movement"
        elif threat_level == "EMERGENCY":
            phase = "real_time_tracking"
        else:
            phase = "unknown"

        affected_zones = {zone["zone_id"]: zone["time_to_impact_hours"] for zone in wake_up["affected_zones"]}

        pens_output = []
        total_animals_at_risk = 0
        animals_moved = 0
        animals_remaining = 0

        for pen in farm["pens"]:
            pen_id = pen["pen_id"]
            time_to_impact = affected_zones.get(pen["zone"], 11.0)
            priority_score = compute_priority_score(pen, time_to_impact)

            if pen_id not in pen_routes or not pen_routes[pen_id]:
                pens_output.append({
                    "pen_id": pen_id,
                    "lat": pen["centroid"]["lat"],
                    "lon": pen["centroid"]["lon"],
                    "species": pen["species"],
                    "priority_score": priority_score,
                    "decision": "cannot_move",
                    "decision_reason": "No evacuation routes available",
                    "assigned_evac_site": None,
                    "route_duration_hours": None,
                    "route_source": None,
                    "smoke_penalty_applied": False,
                    "assigned_vehicle": None,
                    "movement_status": "cannot_move"
                })
                animals_remaining += pen["count"]
                continue

            routes = pen_routes[pen_id]
            smoke_dir = wake_up.get("smoke_trajectory", {}).get("direction_degrees", 0)

            best_site = None
            best_duration = float("inf")
            smoke_penalty_applied = False

            for route in routes:
                site = route["site"]
                duration = route["duration_hours"]

                bearing = bearing_between(farm_centroid["lat"], farm_centroid["lon"], site["lat"], site["lon"])
                if angle_diff(bearing, smoke_dir) <= 45:
                    duration += 2.0
                    smoke_penalty_applied = True

                if duration < best_duration:
                    best_duration = duration
                    best_site = {
                        "name": site["name"],
                        "lat": site["lat"],
                        "lon": site["lon"],
                        "distance_km": route["distance_km"]
                    }

            if best_duration > time_to_impact * 0.8:
                decision = "shelter_in_place"
                decision_reason = f"Evacuation time ({best_duration:.1f}h) exceeds safe threshold ({time_to_impact * 0.8:.1f}h)"
                movement_status = "cannot_move"
                animals_remaining += pen["count"]
            else:
                decision = "evacuate"
                decision_reason = f"Safe evacuation possible within {best_duration:.1f}h"
                movement_status = "pending"
                animals_moved += pen["count"]

            total_animals_at_risk += pen["count"] * pen.get("avg_market_value_usd", 0)

            pens_output.append({
                "pen_id": pen_id,
                "lat": pen["centroid"]["lat"],
                "lon": pen["centroid"]["lon"],
                "species": pen["species"],
                "priority_score": priority_score,
                "decision": decision,
                "decision_reason": decision_reason,
                "assigned_evac_site": best_site,
                "route_duration_hours": best_duration,
                "route_source": routes[0].get("source") if routes else None,
                "smoke_penalty_applied": smoke_penalty_applied,
                "assigned_vehicle": None,
                "movement_status": movement_status
            })

        pens_output.sort(key=lambda p: p["priority_score"], reverse=True)

        # Compute evacuation optimization
        evac_optimization = compute_evacuation_optimization(pens_output, farm, affected_zones.get("Z1", 11.0))
        logger.info(f"Evacuation Optimization: Can save ${evac_optimization['summary']['value_can_save_usd']:,.0f} of ${evac_optimization['summary']['value_can_save_usd'] + evac_optimization['summary']['potential_loss_usd']:,.0f}")

        pool_blocks = []
        neighbors = neighbors_data.get("farms", [])

        for neighbor in neighbors:
            neighbor_id = neighbor["farm_id"]
            neighbor_centroid = neighbor["centroid"]
            neighbor_species = set(neighbor.get("species", []))

            our_species = {pen["species"] for pen in farm["pens"]}
            if not our_species & neighbor_species:
                continue

            our_best_sites = {
                p["assigned_evac_site"]["name"]: p["assigned_evac_site"]
                for p in pens_output
                if p["assigned_evac_site"]
            }

            neighbor_best_sites = set()
            for neighbor_pen in neighbor.get("pens", []):
                n_pen_id = neighbor_pen["pen_id"]
                n_species = neighbor_pen["species"]
                n_lat, n_lon = neighbor_pen["centroid"]["lat"], neighbor_pen["centroid"]["lon"]

                best_neighbor_site = None
                best_neighbor_duration = float("inf")

                for site in evac_sites:
                    result = await query_osrm(client, n_lat, n_lon, site["lat"], site["lon"])
                    if result and result["duration_hours"] < best_neighbor_duration:
                        best_neighbor_duration = result["duration_hours"]
                        best_neighbor_site = site["name"]

                if best_neighbor_site:
                    neighbor_best_sites.add(best_neighbor_site)

            common_sites = set(our_best_sites.keys()) & neighbor_best_sites
            if not common_sites:
                continue

            for common_site in common_sites:
                site_obj = our_best_sites[common_site]
                site_lat, site_lon = site_obj["lat"], site_obj["lon"]
                distance_km = haversine(farm_centroid["lat"], farm_centroid["lon"], neighbor_centroid["lat"], neighbor_centroid["lon"])

                if distance_km > 20:
                    continue

                pool_route = await compute_pool_route(
                    client,
                    farm_centroid["lat"], farm_centroid["lon"],
                    neighbor_centroid["lat"], neighbor_centroid["lon"],
                    site_lat, site_lon
                )

                if not pool_route:
                    continue

                our_total = 0
                for pen in pens_output:
                    if pen["assigned_evac_site"] and pen["assigned_evac_site"]["name"] == common_site:
                        our_total += pen["route_duration_hours"]

                neighbor_total = 0
                for n_pen in neighbor.get("pens", []):
                    n_lat, n_lon = n_pen["centroid"]["lat"], n_pen["centroid"]["lon"]
                    result = await query_osrm(client, n_lat, n_lon, site_lat, site_lon)
                    if result:
                        neighbor_total += result["duration_hours"]

                time_saved = (our_total + neighbor_total) - pool_route["total_duration_hours"]
                time_saved_minutes = time_saved * 60

                if time_saved_minutes < 15:
                    continue

                return_trip = await compute_return_trip(
                    client,
                    site_lat, site_lon,
                    neighbor_centroid["lat"], neighbor_centroid["lon"],
                    site_lat, site_lon
                )

                return_assist = False
                if return_trip:
                    neighbor_standalone = neighbor_total
                    if (return_trip["total_duration_hours"] * 60) < (neighbor_standalone * 60 - 20):
                        return_assist = True

                pool_blocks.append({
                    "pool_id": f"pool_{farm['farm_id']}_{neighbor_id}_{common_site.replace(' ', '_')}",
                    "farms_involved": [farm["farm_id"], neighbor_id],
                    "shared_route_summary": f"{farm['farm_id']} -> {neighbor_id} -> {common_site}",
                    "time_saved_minutes": time_saved_minutes,
                    "return_trip_assist": return_assist,
                    "estimated_cost_sharing_usd": round(time_saved_minutes * 5, 2)
                })

        livestock_status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "threat_level": threat_level,
            "phase": phase,
            "pens": pens_output,
            "transport_pool": pool_blocks,
            "evacuation_optimization": evac_optimization,
            "blockers": [],
            "animals_at_risk_usd": total_animals_at_risk,
            "animals_moved_count": animals_moved,
            "animals_remaining_count": animals_remaining
        }

        erpc_message = {
            "sender": "livestock_agent",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "evacuation_status": "in_progress" if animals_moved > 0 else "pending",
            "animal_valuation_at_risk": total_animals_at_risk,
            "transport_costs_usd": round(animals_moved * 15, 2),
            "blockers": [],
            "transport_pool": pool_blocks,
            "cost_optimization": evac_optimization["summary"],
            "forecasting_requests": [],
            "crop_messages": []
        }

        with open(LIVESTOCK_DIR / "livestock_status.json", "w") as f:
            json.dump(livestock_status, f, indent=2)

        with open(LIVESTOCK_DIR / "erpc_message.json", "w") as f:
            json.dump(erpc_message, f, indent=2)

        logger.info(f"Cycle complete. Animals at risk: {total_animals_at_risk} USD. Moved: {animals_moved}, Remaining: {animals_remaining}")


if __name__ == "__main__":
    asyncio.run(main())
