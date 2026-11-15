This solution was developed as part of NASA Space Apps Tel Aviv 2026. The event was led by the Space Engineering Center (SEC) and SpaceTAU at Tel Aviv University, in partnership with the Rakia Mission and Space Campus IL.

# Dead Sea Alerts

[Visit Dead Sea Alerts](https://deadsea-alerts.earth/)

<img width="1918" height="921" alt="image" src="https://github.com/user-attachments/assets/3ef2dacb-0954-46cb-a59e-2a3e749b3fd2" />

Dead Sea Alerts is an interactive map for exploring ground deformation around the Dead Sea and its relationship to nearby infrastructure. The project aims to use NASA-ISRO Synthetic Aperture Radar (NISAR) observations to help people explore changes over time.

Built by [Team Project Hail Miriam](https://www.spaceappschallenge.org/2026/find-a-team/project-hail-miriam/) for the 2026 NASA Space Apps Challenge, **Dancing with the SARs**.

> **Important:** Dashboard zones and measurements are exploratory. This project is not a validated hazard assessment or a safety warning system.

The team brings together builders, data enthusiasts and storytellers to make NISAR observations accessible. Team listing: Tel Aviv, Israel; languages: English and Hebrew.

## Challenge: Dancing with the SARs

The 2026 NASA Space Apps Challenge asks teams to build an interactive application using NISAR radar remote-sensing data to track and visualize one or more types of Earth's surface change at locations around the world. Example topics include wetland and floodplain change, wildfire and deforestation, earthquakes, agriculture and soil moisture, glacier and sea-ice movement, and coastal or other land subsidence.

SAR is an active microwave sensing technique: the instrument transmits and receives its own signal, so it can collect observations day or night and through clouds. Radar backscatter responds to surface roughness and dielectric properties; it can reveal structure and materials, highlight water and soil moisture, and measure fine surface motion. These characteristics make SAR useful for observing change that can be difficult to see in optical imagery alone.

The challenge encourages a user-friendly, open-source solution that makes complex radar observations understandable to a broad audience. Suggested approaches include change-detection maps, time-series animations, and dashboards. Teams should choose a study area, obtain freely available NISAR data, optionally add complementary imagery or GIS layers, investigate plausible drivers, and present findings in an interactive digital medium. The challenge page suggests considering different polarizations/frequencies, dates, ancillary data, human impacts, local context, and compelling visual storytelling. It does not prescribe a single study area or specific final format.

## How it works

- **Dashboard:** A Leaflet map displays zones, displacement indicators, trends and nearby OpenStreetMap infrastructure.
- **NISAR processing:** Python scripts acquire GUNW products and build a cumulative line-of-sight displacement stack. A separate detector screens for exploratory deformation anomalies.
- **API:** A Python endpoint defines the interface for zone geometry, measurements and displacement histories. See the [API guide](./API.md) and [OpenAPI specification](./openapi.json).

Line-of-sight movement is relative to the processing reference and is not automatically vertical subsidence. The reference, sign convention, classification methods and scientific interpretation require validation. Intersecting mapped infrastructure does not establish damage.

## Official challenge and data resources

### NASA and NISAR
- [Challenge details — Dancing with the SARs](https://www.spaceappschallenge.org/2026/challenges/dancing-with-the-sars/)
- [NASA Earthdata Search](https://search.earthdata.nasa.gov/search) — discover, compare, visualize, and access NASA Earth science data.
- [ASF Data Search Vertex](https://search.asf.alaska.edu/) — Alaska Satellite Facility SAR data search and access.
- [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/) — guidance for finding, accessing, and working with NISAR data.
- [Introduction to Remote Sensing](https://www.earthdata.nasa.gov/learn/earth-observation-data-basics/remote-sensing)
- [Introduction to Synthetic Aperture Radar](https://www.earthdata.nasa.gov/learn/earth-observation-data-basics/sar)
- [NASA ARSET Trainings](https://appliedsciences.nasa.gov/get-involved/training) — remote sensing, SAR, NISAR, and application-area training.
- [NISAR StoryMaps](https://nisar.jpl.nasa.gov/applications/arcgis-storymaps/) — examples covering floods, wildfires, land subsidence, oil slicks, and glacial motion.
- [NISAR Applications White Papers](https://nisar.jpl.nasa.gov/applications/societal-benefit/) — short overviews of societal-benefit applications.
- [UAVSAR Postcards](https://uavsar.jpl.nasa.gov/education/postcards.html) — false-color airborne SAR examples.

### Partner-agency SAR resources
- [RADARSAT Constellation Mission data (CSA)](https://donnees-data.asc-csa.gc.ca/en/dataset/rcm)
- [CSA Synthetic Aperture Radar Python Tutorial](https://github.com/asc-csa/Synthetic_Aperture_Radar_Tutorial) — practical SAR access and visualization steps with Python and QGIS.
- [RADARSAT-1 data (CSA)](https://donnees-data.asc-csa.gc.ca/en/dataset/radarsat1)
- [SAOCOM catalogue (CONAE)](https://catalogos.conae.gov.ar/catalogo/catalogoSatSaocomInfo.html)
- [SAOCOM catalogue registration](https://www.argentina.gob.ar/noticias/la-informacion-saocom-cada-vez-mas-accesible)

NASA notes that it does not endorse non-U.S. Government entities or take responsibility for information on their websites. Follow each non-U.S. resource's own data-use terms.

## Explore the dashboard

Explore [deadsea-alerts.earth](https://deadsea-alerts.earth/) to view the map, select zones and explore nearby infrastructure. See [Dashboard](./DASHBOARD.md) for implementation details and limitations.

### Zones API

The [OpenAPI specification](./openapi.json) describes `GET /api/v1/zones` and `GET /api/v1/osm` and can be opened in Swagger Editor. See the [backend integration guide](./API.md) for the contracts and implementation handoff. The zones endpoint is in `api/v1/zones.py`; its processing integration point is `src/zone_service.py::get_zones()`.

The detector produces exploratory deformation anomalies, review priorities and per-date quality proxies. These are not automatically equivalent to risk levels or latest-pair coherence. Its results can include MultiPolygon geometry and use mm/day and mm/day²; compatible LOS values need unit conversion for the API.

For local endpoint testing use `vercel dev` from the project directory (a plain `python -m http.server` only serves static files). Run the route tests with `python -m unittest discover -s tests`.

### Run locally

Run `vercel dev` from the repository root and open its local URL. A plain static HTTP server serves the frontend but cannot run the Python API. The map uses OpenStreetMap tiles; experimental data is not an operational warning.

Alternatively, run `python scripts/dashboard_server.py` for a localhost-only server. It defaults to the real API; set `ZONE_DATA_SOURCE=demo` before starting it to display synthetic demo zones locally. See [Dashboard](./DASHBOARD.md) for commands.

The dashboard loads precomputed OpenStreetMap infrastructure and area names through `GET /api/v1/osm`. It displays roads, pumping stations, buildings and other mapped features intersecting each zone; stale or missing entries are unavailable rather than fetched live in production. Pipelines are not necessarily water pipelines, and mapped features may be duplicated, incomplete or outdated. Selected ways may extend outside a zone. If the request fails, use the displayed Retry button. Intersections do not establish damage. Internet access is required for Leaflet and map tiles. See [Dashboard](./DASHBOARD.md) for cache maintenance and the local demo's live requests.

## Sources

- [Official challenge page](https://www.spaceappschallenge.org/2026/challenges/dancing-with-the-sars/)
- [Project Hail Miriam team page](https://www.spaceappschallenge.org/2026/find-a-team/project-hail-miriam/)
