# Project Hail Miriam

<img width="1914" height="910" alt="image" src="https://github.com/user-attachments/assets/345bf72f-3197-4e51-9fde-a9dd69b3d126" />

An interactive web experience for exploring how Earth's surface changes over time, using NASA-ISRO Synthetic Aperture Radar (NISAR) observations.

This repository is the starting point for Team Project Hail Miriam's 2026 NASA Space Apps Challenge project. The team describes itself as a multidisciplinary group of builders, data enthusiasts, and storytellers. Its stated goal is to make NISAR radar remote sensing accessible through an interactive experience that tracks surface changes and explores the natural and human forces behind them.

## Challenge: Dancing with the SARs

The 2026 NASA Space Apps Challenge asks teams to build an interactive application using NISAR radar remote-sensing data to track and visualize one or more types of Earth's surface change at locations around the world. Example topics include wetland and floodplain change, wildfire and deforestation, earthquakes, agriculture and soil moisture, glacier and sea-ice movement, and coastal or other land subsidence.

SAR is an active microwave sensing technique: the instrument transmits and receives its own signal, so it can collect observations day or night and through clouds. Radar backscatter responds to surface roughness and dielectric properties; it can reveal structure and materials, highlight water and soil moisture, and measure fine surface motion. These characteristics make SAR useful for observing change that can be difficult to see in optical imagery alone.

The challenge encourages a user-friendly, open-source solution that makes complex radar observations understandable to a broad audience. Suggested approaches include change-detection maps, time-series animations, and dashboards. Teams should choose a study area, obtain freely available NISAR data, optionally add complementary imagery or GIS layers, investigate plausible drivers, and present findings in an interactive digital medium. The challenge page suggests considering different polarizations/frequencies, dates, ancillary data, human impacts, local context, and compelling visual storytelling. It does not prescribe a single study area or specific final format.

## Team direction

The team's published description proposes an interactive web experience for tracking Earth's surface changes and explaining the forces behind them. The team page currently does not publish further project specifics. Team listing: Tel Aviv, Israel; languages: English and Hebrew.

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

## Project status

See [Dashboard](./DASHBOARD.md) for the purpose, current implementation, limitations, and development plan.

### Dashboard prototype

Serve `index.html` from a local HTTP server and open it in a browser. The map uses OpenStreetMap tiles; the risk zones and measurements are illustrative, not NISAR observations or operational warnings.

The dashboard requests roads and pumping stations for the Dead Sea study area once per page load. Residential building footprints are requested only when zoomed in, and previously requested areas are cached in memory until the page is reloaded. If the Overpass service fails or times out, use the displayed Retry button; map tiles and illustrative zones remain available. Internet access is required for Leaflet, map tiles, and OpenStreetMap features.

## Sources

- [Official challenge page](https://www.spaceappschallenge.org/2026/challenges/dancing-with-the-sars/)
- [Project Hail Miriam team page](https://www.spaceappschallenge.org/2026/find-a-team/project-hail-miriam/)
