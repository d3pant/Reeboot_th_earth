"""
API endpoint constants for all no-key-required data sources.
All of these are open government APIs — no signup or token needed.
Pass APP_USER_AGENT in request headers so agencies can identify traffic.
"""

APP_USER_AGENT = "reboot-the-earth-crop-agent/1.0"

# ── Water Availability ────────────────────────────────────────────────────────

USGS_WATER = {
    "base": "https://waterservices.usgs.gov/nwis/iv/",
    "docs": "https://waterservices.usgs.gov/rest/IV-Service.html",
    # Key params: sites=<gauge_id>, parameterCd=00060 (streamflow) or 72019 (groundwater depth)
    # Find nearest gauge: https://maps.waterdata.usgs.gov
    "streamflow_param": "00060",
    "groundwater_param": "72019",
    "format": "json",
}

USBR_WATERSMART = {
    "base": "https://water.usbr.gov/api/web/app.php/api",
    "docs": "https://water.usbr.gov/api",
    # Endpoint: /datacatalog — lists available reservoirs and delivery points
    # Endpoint: /data — returns time series for a given site and parameter
}

# ── Fire & Fuel Data ──────────────────────────────────────────────────────────

LANDFIRE = {
    "base": "https://landfire.gov/arcgis/rest/services",
    "docs": "https://landfire.gov/data.php",
    # Endpoint: /LANDFIRE_Vegetation/MapServer — vegetation type + canopy cover per lat/lon
    # Endpoint: /LANDFIRE_Fire/MapServer   — fuel model (Scott & Burgan 40) per lat/lon
    # Use: identify/json?geometry=<lon,lat>&geometryType=esriGeometryPoint to query a point
    "identify_endpoint": "https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer/identify",
    # Pass geometry as JSON with spatialReference wkid=4326 (WGS84 lat/lon)
    # Returns numeric FBFM40 code (e.g. 186 = TL6, 101 = GR1) in response "value" field
}

WFAS_FUEL_MOISTURE = {
    "base": "https://wfas.net",
    "docs": "https://wfas.net/nfdrs/data-downloads.html",
    # Downloads national fuel moisture observations as CSV
    # Use: filter by state/region for live dead fuel moisture %
    # High dead fuel moisture (>15%) = lower fire risk
    # Low dead fuel moisture (<8%) = extreme fire spread risk
    "download_url": "https://wfas.net/nfdrs/dead-fm.html",
}

# ── Crop Biology ──────────────────────────────────────────────────────────────

USDA_PLANTS = {
    "base": "https://plants.usda.gov/api",
    "docs": "https://plants.usda.gov/api",
    # Endpoint: /plants?filter[Growth_Habit]=<value> — annual vs perennial
    # Endpoint: /plants?filter[symbol]=<USDA_symbol> — full plant profile
    # Key fields returned: Growth_Habit, Duration (Annual/Perennial/Biennial),
    #                      Root_Depth_Minimum_inches, Lifespan
    "plant_detail_endpoint": "https://plants.usda.gov/api/plants",
}

# ── Soil Data ─────────────────────────────────────────────────────────────────

USDA_SOILDATA = {
    "base": "https://sdmdataaccess.nrcs.usda.gov/tabular/post.rest",
    "docs": "https://sdmdataaccess.nrcs.usda.gov",
    # POST a SQL query to get soil data for a lat/lon
    # Key tables: mapunit, component, chorizon
    # Key columns: awc_r (available water capacity), drainagecl (drainage class),
    #              om_r (organic matter %), ksat_r (saturated hydraulic conductivity)
    # Example query to get soil at a point:
    # SELECT mapunit.muname, component.drainagecl, chorizon.awc_r
    # FROM ... WHERE ...
    "query_format": "JSON",
}

# ── Crop Growth Stages ────────────────────────────────────────────────────────

USDA_CROP_PROGRESS = {
    "base": "https://quickstats.nass.usda.gov/api/api_GET/",
    "docs": "https://www.nass.usda.gov/Statistics_by_Subject/index.php?sector=CROPS",
    # Uses same USDA_NASS_API_KEY from .env
    # Key params:
    #   source_desc=SURVEY
    #   sector_desc=CROPS
    #   statisticcat_desc=PROGRESS
    #   short_desc=<CROP> - PCT HARVESTED
    #   state_alpha=<CA/TX/etc>
    #   year=2026
    # Returns weekly % of crop at each growth stage in that state
}
