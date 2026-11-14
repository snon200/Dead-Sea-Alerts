# Product Requirements Document (PRD): Dead Sea Macro-Subsidence & Infrastructure Risk Monitor
**Team:** Project Hail Miriam | **Event:** 2026 NASA Space Apps Challenge  
**Challenge:** Dancing with the SARs  
**Repository:** `github.com/snon200/project-hail-miriam`

---

## 1. Executive Summary & Product Framing
### 1.1 The Problem
The Dead Sea water level drops by $>1.1\text{ m/year}$, triggering the dissolution of a subsurface salt (halite) layer buried $15\text{--}65\text{ m}$ underground. This causes catastrophic sinkhole collapses that threaten **Route 90** (the sole north-south highway corridor), **Dead Sea Works (DSW)** industrial pumping stations and evaporation dikes, and local agricultural/tourism hubs.

### 1.2 Why Macro-Scale Framing? (Scientific Alignment with NISAR)
Individual sinkholes are small ($1\text{--}40\text{ m}$), which requires very high-resolution X-band radar + LiDAR to spot individually. However, geological research (Geological Survey of Israel - Nof et al., 2019; Baer et al., 2002) proves that sinkhole clusters are preceded and surrounded by **gradually subsiding macro-zones ("Subsidence Bowls") ranging from $100\text{ m}$ to $>1,000\text{ m}$ in diameter**.
* **NISAR's Sweet Spot:** NISAR's **L-Band ($\lambda = 24\text{ cm}$) Geocoded Unwrapped Interferogram (`GUNW`)** product has an $80\text{ m}$ pixel spacing and a $12\text{-day}$ orbital repeat cycle. It is purpose-built to detect **macro-scale regional subsidence bowls** with high coherence (even across vegetated date palms and alluvial fans where C/X-band radars decorrelate).
* **Value Proposition:** Instead of trying to spot single $5\text{ m}$ holes, our system acts as a **Macro-Scale Regional Susceptibility & Infrastructure Risk Dashboard**—identifying hundreds-of-meters-wide subsiding zones ("Disaster-Prone Corridors") months before surface collapse occurs.

---

## 2. Target Audience & Use Cases
1. **National Transport Infrastructure Planners (Netivei Israel - Route 90):** Identify which highway segments intersect active macro-subsidence bowls to prioritize geosynthetic reinforcement or bypass construction.
2. **Industrial Infrastructure Engineers (Dead Sea Works - DSW):** Monitor ground stability around critical pumping stations (e.g., Ze'elim Fan P88/P9) and supply pipelines.
3. **Regional Council & Civil Defense (Tamar / Megillot):** Update statutory Sinkhole Susceptibility Maps from static buffer distances to dynamic, InSAR-driven active/inactive hazard zones.

---

## 3. Core Product Principles (What Changed from V0)
To keep the dashboard clean, actionable, and hackathon-ready, we are enforcing **strict minimalism**:
* **NO clutter:** Removed dual optical/SAR split-screen canvas sliders, what-if lake drop simulators, and multi-band toggles.
* **English-Only UI (LTR):** Standard aerospace/GIS interface suitable for global NASA Space Apps judges.
* **3-Component Focused Layout:**
  1. **Left Sidebar:** Macro Hazard Corridors list (5 regional polygons) + GIS Infrastructure Layer toggles.
  2. **Center Interactive Map:** Clean satellite map displaying **Macro-Subsidence Polygons ($300\text{--}1,200\text{ m}$ bowls)** color-coded by GSI Alert Level (Red/Orange/Yellow/Green) + Route 90 & DSW pipelines + 12-day NISAR Timeline Scrubber.
  3. **Right Analytics Panel:** Regional Velocity KPI, Cumulative Subsidence Time-Series Chart (LOS displacement in $\text{mm}$), and Infrastructure Exposure Card.

---

## 4. Alert Level Taxonomy (GSI-Aligned Macro Thresholds)
| Alert Level | Color | NISAR Macro Criteria | Operational Meaning |
| :--- | :--- | :--- | :--- |
| **CRITICAL (Level 4)** | Red (`#ef4444`) | $>100\text{ mm/yr}$ LOS subsidence OR coherence loss inside active bowl intersecting infrastructure | Immediate threat to Route 90 / DSW asset; field inspection & traffic/site restriction required. |
| **ACCELERATING (Level 3)** | Orange (`#f97316`) | $40\text{--}100\text{ mm/yr}$ LOS subsidence with increasing velocity over $3+$ consecutive 12-day passes | Precursory cavity expansion; deploy ground geophysics (GPR/microgravity) & engineering review. |
| **WATCH (Level 2)** | Yellow (`#eab308`) | $15\text{--}40\text{ mm/yr}$ steady macro-subsidence over $>200\text{ m}$ footprint | Susceptible zone flagged; prohibit new unlicensed construction. |
| **STABLE / INACTIVE (Level 1)** | Emerald (`#10b981`) | $<10\text{ mm/yr}$ displacement over $12+\text{ months}$ | Historically active or stable zone (e.g., Northern P9 Pumping Station alternative). |

---

## 5. System Architecture & Data Pipeline
### 5.1 Data Sources
* **Primary SAR Data:** NASA-ISRO SAR (**NISAR**) Level-2 **GUNW** (Geocoded Unwrapped Interferogram, $80\text{ m}$ resolution) & **GCOV** (Geocoded Polarimetric Covariance, $20\text{ m}$ resolution) accessed via **NASA Earthdata / ASF Vertex API**.
* **Historical / Proxy SAR Data (for multi-year baseline):** Sentinel-1 / ALOS-2 L-band & GSI published InSAR velocity benchmarks along the Dead Sea.
* **Ancillary GIS Layers (GeoJSON):**
  * Route 90 highway centerline & kilometer markers.
  * Dead Sea Works (DSW) pumping stations (P88, P9) and brine conduit corridors.
  * Subsurface Halite (Salt) Layer Western Pinch-Out Boundary (the geological boundary controlling sinkhole susceptibility).

### 5.2 Lightweight Hackathon Tech Stack
* **Frontend:** Single-Page Web Application (HTML5 / Tailwind CSS / Vanilla JS or React, Leaflet.js for GIS mapping, Chart.js for time-series).
* **Data Processing (Offline / Pre-computed Python Pipeline):**
  * Use `earthaccess` + `asf_search` + `xarray` / `rasterio` in **Google Colab** to crop NISAR/proxy `GUNW` HDF5/NetCDF granules to the Dead Sea bounding box (`[35.32, 31.15, 35.58, 31.65]`).
  * Export lightweight static **GeoJSON polygons** (`macro_zones.geojson`) and time-series JSON (`timeseries.json`) directly into the Git repository so the web app loads instantaneously without heavy backend servers.

---

## 6. Recommended Google Development & Git Workflow
Since the team is collaborating via Git (`github.com/snon200/project-hail-miriam`), we recommend:
1. **Firebase Studio (formerly Google Project IDX - `idx.dev`):**
   * Import your GitHub repo directly into Firebase Studio.
   * Gives every developer a cloud VS Code environment with built-in Gemini AI assistance, instant live web preview, and 1-click deployment to **Firebase Hosting**.
2. **Google Colab (linked to GitHub):**
   * Use Colab for Python SAR data exploration (`notebooks/nisar_gunw_processing.ipynb`) and commit the resulting GeoJSON files straight to the `data/` folder in GitHub.