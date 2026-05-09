# Reeboot the Earth — Wildfire Agricultural Advisory System

An AI-powered wildfire monitoring and agricultural advisory system for Southern California farms. Detects fire threats in real time and activates downstream agents (Crop, Livestock, ERPC) when risk thresholds are crossed.

---

## System Components

| Component | Description |
|---|---|
| `forecaster/` | Core threat assessment engine — monitors fire weather, active fires, and vegetation stress |
| `backend/` | FastAPI server + Leaflet map — visualizes live NASA FIRMS fire detections |

---

## Data Sources & API Citations

### 1. NASA FIRMS — Active Fire Detections
**Full name**: Fire Information for Resource Management System  
**Operated by**: NASA Earth Science Data and Information System (ESDIS)  
**URL**: https://firms.modaps.eosdis.nasa.gov  
**API docs**: https://firms.modaps.eosdis.nasa.gov/api/  
**Data used**: VIIRS S-NPP Near Real-Time active fire detections (375m resolution)  
**Update frequency**: ~4x per day (every satellite pass)  
**Access**: Free. Register at https://urs.earthdata.nasa.gov, then request a MAP KEY at https://firms.modaps.eosdis.nasa.gov/api/map_key/  
**License**: NASA Open Data — free for commercial and non-commercial use with attribution  
**Citation**:
> Giglio, L., Schroeder, W., & Justice, C. O. (2016). The collection 6 MODIS active fire detection algorithm and fire characterization study. *Remote Sensing of Environment*, 178, 31–41. https://doi.org/10.1016/j.rse.2016.02.054

---

### 2. Open-Meteo — Fire Weather Index & Weather Forecast
**Full name**: Open-Meteo Weather API  
**Operated by**: Open-Meteo (open-source, non-commercial)  
**URL**: https://open-meteo.com  
**API docs**: https://open-meteo.com/en/docs  
**Data used**:
- Canadian Fire Weather Index (FWI) and components (FFMC, DMC, DC, ISI, BUI)
- Wind speed, wind direction, wind gusts
- Temperature, relative humidity
- Soil moisture (vegetation stress proxy)

**Update frequency**: Hourly forecasts, updated 4x daily  
**Access**: Free, no API key required  
**License**: CC BY 4.0  
**Citation**:
> Zippenfenig, P. (2023). Open-Meteo.com Weather API [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.7970649

---

### 3. SDG&E Fire Potential Index (FPI)
**Full name**: San Diego Gas & Electric Fire Potential Index  
**Operated by**: San Diego Gas & Electric (Sempra Energy)  
**URL**: https://www.sdge.com/wildfire-safety  
**Data used**: Localized Fire Weather Index calibrated for San Diego County  
**Update frequency**: 2x daily  
**Access**: Utility partner API — requires SDG&E partnership agreement  
**License**: Proprietary — SDG&E  
**Note**: Currently stubbed in code; Open-Meteo FWI used as free alternative

---

### 4. NASA NDVI — Vegetation Stress
**Full name**: Normalized Difference Vegetation Index via NASA AppEEARS  
**Operated by**: NASA Land Processes Distributed Active Archive Center (LP DAAC)  
**URL**: https://appeears.earthdatacloud.nasa.gov  
**API docs**: https://appeears.earthdatacloud.nasa.gov/api/  
**Data used**: MOD13Q1 — MODIS 16-day NDVI composite at 250m resolution; z-score anomaly computed against seasonal baseline  
**Update frequency**: Every 16 days  
**Access**: Free. Requires NASA Earthdata account at https://urs.earthdata.nasa.gov  
**License**: NASA Open Data  
**Citation**:
> Didan, K. (2021). *MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061* [Data set]. NASA EOSDIS Land Processes DAAC. https://doi.org/10.5067/MODIS/MOD13Q1.061

---

### 5. NOAA Weather API
**Full name**: National Weather Service Web API  
**Operated by**: National Oceanic and Atmospheric Administration (NOAA)  
**URL**: https://www.weather.gov  
**API docs**: https://www.weather.gov/documentation/services-web-api  
**Data used**: Hourly weather forecast — wind, temperature, humidity  
**Update frequency**: Hourly  
**Access**: Free, no API key required for US locations  
**License**: U.S. Government Open Data — public domain  

---

### 6. WIFIRE Firemap — Real-Time Fire Spread
**Full name**: WIFIRE Firemap  
**Operated by**: UC San Diego / Halıcıoğlu Data Science Institute  
**URL**: https://wifire.ucsd.edu  
**Data used**: Real-time fire spread direction, speed, affected roads, community proximity  
**Update frequency**: Minutes  
**Access**: Academic/research API — requires WIFIRE partnership or research affiliation  
**License**: Research use only  
**Note**: Currently stubbed in code; mock spread model used as fallback  
**Citation**:
> Altintas, I., Block, J., de Callafon, R., Crawl, D., Cowart, C., Gupta, A., ... & Nguyen, M. H. (2015). Towards integrated cyberinfrastructure for data, analysis and modeling for the WIFIRE project. *Proceedings of the Workshop on Big Data from Stream to Knowledge*. https://doi.org/10.1145/2834976.2834985

---

### 7. Pyrecast — 14-Day Ensemble Fire Spread Forecast
**Full name**: Pyrecast Fire Spread Prediction API  
**Operated by**: UC San Diego / WIFIRE Lab  
**URL**: https://pyrecast.org  
**Data used**: Probabilistic 14-day fire spread forecast using ensemble simulation (200 members)  
**Update frequency**: On-demand async simulation (10–30 min compute time)  
**Access**: Academic/research API — requires approval from WIFIRE Lab  
**License**: Research use only  
**Note**: Currently stubbed in code; async UID placeholder returned in wake-up packet

---

### 8. Copernicus EFFIS — European Fire Danger Forecast
**Full name**: European Forest Fire Information System  
**Operated by**: European Commission Joint Research Centre (JRC) / Copernicus Emergency Management Service  
**URL**: https://effis.jrc.ec.europa.eu  
**API docs**: https://effis.jrc.ec.europa.eu/apps/effis.global.viewer/  
**Data used**: Fire danger forecast index, fire probability maps (global coverage)  
**Update frequency**: Daily  
**Access**: Free with EU Copernicus account — https://emergency.copernicus.eu  
**License**: Copernicus open access  
**Note**: Identified as a recommended alternative to WIFIRE for fire probability data

---

## Map & Visualization

### Leaflet.js
**URL**: https://leafletjs.com  
**Version**: 1.9.4  
**License**: BSD 2-Clause  

### CartoDB Dark Matter Tiles
**URL**: https://carto.com/basemaps/  
**License**: CC BY 3.0 — requires attribution to © OpenStreetMap contributors © CARTO  

---

## Fire Weather Science

### Canadian Forest Fire Weather Index (FWI) System
The FWI system is the international standard for quantifying fire weather conditions. It combines temperature, humidity, wind speed, and 24-hour rainfall into six components:

| Component | Measures |
|---|---|
| FFMC (Fine Fuel Moisture Code) | Moisture of fine fuels (litter, grass) |
| DMC (Duff Moisture Code) | Moisture of loosely compacted organic layers |
| DC (Drought Code) | Seasonal drought effect on deep organic layers |
| ISI (Initial Spread Index) | Expected rate of fire spread |
| BUI (Build-Up Index) | Total fuel available for combustion |
| FWI (Fire Weather Index) | Overall fire intensity potential (0–180) |

**Citation**:
> Van Wagner, C. E. (1987). *Development and structure of the Canadian Forest Fire Weather Index System* (Forestry Technical Report 35). Canadian Forestry Service. https://cfs.nrcan.gc.ca/pubwarehouse/pdfs/19927.pdf

---

## Threshold References

Fire distance and FWI thresholds in `forecaster/config/farm_config.json` are derived from:

- SDG&E Wildfire Mitigation Plan 2023–2025
- CAL FIRE Fire Hazard Severity Zone classifications
- USDA Forest Service Fire Danger Rating System (FDRS)

---

## License

Code: MIT  
Data: Subject to individual data provider licenses listed above. NASA and NOAA data are U.S. Government public domain. Open-Meteo data is CC BY 4.0.
