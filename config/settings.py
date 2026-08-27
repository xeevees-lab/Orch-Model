"""Shared configuration. Everything tunable lives here, not scattered in code."""

import os
from pathlib import Path

# --- Paths -------------------------------------------------------
DATA_DIR = Path(os.getenv("ME_DATA_DIR", Path.home() / "megaevent" / "data"))
RAW_DIR = DATA_DIR / "raw"
OSM_PBF = RAW_DIR / "mumbai.osm.pbf"

# --- Study area --------------------------------------------------
# west, south, east, north
BBOX = (72.775, 18.890, 72.990, 19.280)
CITY = "Mumbai"
UTM_CRS = "EPSG:32643"  # UTM zone 43N - use for anything measured in metres

# --- Connections -------------------------------------------------
PG_DSN = os.getenv(
    "ME_PG_DSN", "postgresql+psycopg://megaevent:megaevent@localhost:5432/megaevent"
)
REDIS_URL = os.getenv("ME_REDIS_URL", "redis://localhost:6379/0")
KAFKA_BOOTSTRAP = os.getenv("ME_KAFKA", "localhost:9092")

# --- Pedestrian constants ----------------------------------------
# Calibrated against published pedestrian-dynamics measurements.
WALK_SPEED_MPS = 1.34          # mean free walking speed
SAFE_DENSITY = 4.0             # persons/sqm - above this, crush risk climbs
DEFAULT_FOOTWAY_WIDTH_M = 2.0
SPECIFIC_FLOW = 1.2            # persons per metre of width per second at capacity

# --- Road capacity by OSM highway class --------------------------
# Vehicles per hour per lane, converted to people later.
ROAD_CAPACITY_VPHPL = {
    "motorway": 2000,
    "motorway_link": 1500,
    "trunk": 1800,
    "trunk_link": 1300,
    "primary": 1500,
    "primary_link": 1100,
    "secondary": 1200,
    "secondary_link": 900,
    "tertiary": 900,
    "tertiary_link": 700,
    "residential": 600,
    "unclassified": 600,
    "service": 300,
    "living_street": 300,
}
DEFAULT_ROAD_CAPACITY_VPHPL = 600
OCCUPANTS_PER_VEHICLE = 2.5

# Free-flow speeds in km/h.
ROAD_SPEED_KPH = {
    "motorway": 70,
    "trunk": 55,
    "primary": 40,
    "secondary": 35,
    "tertiary": 30,
    "residential": 20,
    "unclassified": 25,
    "service": 15,
    "living_street": 10,
}
DEFAULT_ROAD_SPEED_KPH = 25

# --- Accommodation calibration -----------------------------------
# Rooms per property by star class, used to expand OSM hotel POIs into
# a synthetic inventory. Totals get scaled to match open-data room counts.
ROOMS_BY_STARS = {5: 220, 4: 140, 3: 80, 2: 45, 1: 25}
DEFAULT_ROOMS = {
    "hotel": 60,
    "hostel": 40,
    "guest_house": 18,
    "motel": 25,
    "apartment": 12,
}
PRICE_BY_STARS_INR = {5: 14000, 4: 8000, 3: 4200, 2: 2400, 1: 1400}
DEFAULT_PRICE_INR = {
    "hotel": 3800,
    "hostel": 900,
    "guest_house": 1600,
    "motel": 2000,
    "apartment": 3000,
}

# Target total room count for Greater Mumbai. Replace with the figure you
# pull from data.gov.in so the synthetic inventory is calibrated, not invented.
TARGET_TOTAL_ROOMS = 65000

# --- Zoning ------------------------------------------------------
ZONE_GRID_KM = 2.5             # side length of each accommodation zone cell
MIN_ZONE_POIS = 1              # drop empty cells below this

# Lane counts when OSM does not tag them. Assuming 1 lane everywhere
# badly undercounts arterial capacity.
DEFAULT_LANES = {
    "motorway": 3, "motorway_link": 1,
    "trunk": 3, "trunk_link": 1,
    "primary": 2, "primary_link": 1,
    "secondary": 2, "secondary_link": 1,
    "tertiary": 1, "residential": 1,
    "unclassified": 1, "service": 1, "living_street": 1,
}

# Zone price multiplier by distance from the island city, so price
# carries real signal instead of collapsing to one default.
PRICE_CENTRALITY_WEIGHT = 0.6
