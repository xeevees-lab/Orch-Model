"""
01_build_graph.py - turn mumbai.osm.pbf into the destination graph in PostGIS.

Produces four tables:
    zones  - grid cells covering the study area, each an accommodation zone
    nodes  - graph junctions plus named POIs promoted to nodes
    edges  - road and pedestrian links with capacity and free-flow time
    pois   - hotels, restaurants and services with synthetic capacity

Run:
    cd ~/megaevent
    source .venv/bin/activate
    python etl/01_build_graph.py
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import box
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("build_graph")

# The database calls the geometry column "geom", geopandas defaults to
# "geometry". Every write has to rename before it hits PostGIS.
GEOM_COL = "geom"


# ---------------------------------------------------------------- helpers

def engine():
    return create_engine(S.PG_DSN, future=True)


def road_capacity_ppm(highway: str, lanes: float | None) -> float:
    """People per minute a road link can carry."""
    vphpl = S.ROAD_CAPACITY_VPHPL.get(highway, S.DEFAULT_ROAD_CAPACITY_VPHPL)
    n_lanes = lanes if lanes and lanes > 0 else 1.0
    vehicles_per_min = (vphpl * n_lanes) / 60.0
    return vehicles_per_min * S.OCCUPANTS_PER_VEHICLE


def walk_capacity_ppm(width_m: float | None) -> float:
    """People per minute a footway can carry, from specific flow."""
    w = width_m if width_m and width_m > 0 else S.DEFAULT_FOOTWAY_WIDTH_M
    return w * S.SPECIFIC_FLOW * 60.0


def free_flow_minutes(length_m: float, mode: str, highway: str | None) -> float:
    if mode == "walk":
        return (length_m / S.WALK_SPEED_MPS) / 60.0
    kph = S.ROAD_SPEED_KPH.get(highway or "", S.DEFAULT_ROAD_SPEED_KPH)
    return length_m / (kph * 1000 / 60.0)


def to_float(series: pd.Series) -> pd.Series:
    """OSM numeric tags arrive as messy strings ('2', '2;3', 'two')."""
    return pd.to_numeric(
        series.astype(str).str.extract(r"(\d+\.?\d*)", expand=False), errors="coerce"
    )


def nullable_int(series: pd.Series) -> pd.Series:
    """Pandas nullable integer, so NaN becomes SQL NULL instead of a bad cast."""
    return pd.to_numeric(series, errors="coerce").round().astype("Int64")


def for_postgis(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Rename the active geometry column to match the schema."""
    if gdf.geometry.name != GEOM_COL:
        gdf = gdf.rename_geometry(GEOM_COL)
    return gdf


# ---------------------------------------------------------------- zones

def build_zones(bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Cover the study area with a square grid. Each cell is a candidate zone."""
    west, south, east, north = bounds
    mid_lat = (south + north) / 2
    deg_per_km_lat = 1 / 110.574
    deg_per_km_lon = 1 / (111.320 * np.cos(np.radians(mid_lat)))
    dy = S.ZONE_GRID_KM * deg_per_km_lat
    dx = S.ZONE_GRID_KM * deg_per_km_lon

    cells, names = [], []
    row = 0
    y = south
    while y < north:
        col = 0
        x = west
        while x < east:
            cells.append(box(x, y, min(x + dx, east), min(y + dy, north)))
            names.append(f"Z{row:02d}{col:02d}")
            x += dx
            col += 1
        y += dy
        row += 1

    gdf = gpd.GeoDataFrame(
        {"zone_id": names, "name": names, "kind": "accommodation"},
        geometry=cells,
        crs="EPSG:4326",
    )
    # Area in a projected CRS - doing this in degrees gives nonsense.
    gdf["area_sqkm"] = (gdf.to_crs(S.UTM_CRS).geometry.area / 1e6).round(4)
    log.info("built %d candidate zones at %.1f km grid", len(gdf), S.ZONE_GRID_KM)
    return gdf


# ---------------------------------------------------------------- network

def build_network(osm) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Extract driving and walking networks, normalised into one edge set."""
    frames_nodes, frames_edges = [], []

    for mode, network_type in (("road", "driving"), ("walk", "walking")):
        log.info("extracting %s network ...", network_type)
        nodes, edges = osm.get_network(nodes=True, network_type=network_type)
        if edges is None or edges.empty:
            log.warning("no %s edges found", network_type)
            continue

        edges = edges.copy()
        edges["mode"] = mode
        for col in ("highway", "name", "width", "lanes", "oneway"):
            if col not in edges.columns:
                edges[col] = None

        edges["lanes_n"] = to_float(edges["lanes"])
        edges["width_n"] = to_float(edges["width"])

        if "length" in edges.columns:
            edges["length_m"] = to_float(edges["length"])
        else:
            edges["length_m"] = np.nan
        missing = edges["length_m"].isna()
        if missing.any():
            edges.loc[missing, "length_m"] = (
                edges.loc[missing].to_crs(S.UTM_CRS).geometry.length
            )

        if mode == "road":
            edges["capacity_ppm"] = [
                road_capacity_ppm(h, l)
                for h, l in zip(edges["highway"], edges["lanes_n"])
            ]
        else:
            edges["capacity_ppm"] = [walk_capacity_ppm(w) for w in edges["width_n"]]

        edges["free_flow_min"] = [
            free_flow_minutes(lm, m, h)
            for lm, m, h in zip(edges["length_m"], edges["mode"], edges["highway"])
        ]
        edges["oneway_b"] = (
            edges["oneway"].astype(str).str.lower().isin(["yes", "true", "1", "-1"])
        )

        frames_nodes.append(nodes)
        frames_edges.append(edges)

    if not frames_edges:
        raise RuntimeError("no network extracted - check the .osm.pbf file")

    all_nodes = pd.concat(frames_nodes, ignore_index=True).drop_duplicates("id").reset_index(drop=True)
    all_edges = pd.concat(frames_edges, ignore_index=True)

    nodes_out = gpd.GeoDataFrame(
        {
            "node_id": all_nodes["id"].astype("int64"),
            "kind": "junction",
            "name": None,
            "capacity": pd.Series([pd.NA] * len(all_nodes), dtype="Int64"),
            "safe_density": S.SAFE_DENSITY,
            "service_rate": np.nan,
        },
        geometry=all_nodes.geometry.values,
        crs="EPSG:4326",
    )

    edges_out = gpd.GeoDataFrame(
        {
            "u": all_edges["u"].astype("int64"),
            "v": all_edges["v"].astype("int64"),
            "mode": all_edges["mode"],
            "highway": all_edges["highway"].astype(str),
            "name": all_edges["name"],
            "length_m": all_edges["length_m"].round(2),
            "lanes": all_edges["lanes_n"],
            "width_m": all_edges["width_n"],
            "capacity_ppm": all_edges["capacity_ppm"].round(2),
            "free_flow_min": all_edges["free_flow_min"].round(3),
            "oneway": all_edges["oneway_b"],
        },
        geometry=all_edges.geometry.values,
        crs="EPSG:4326",
    )

    known = set(nodes_out["node_id"])
    before = len(edges_out)
    edges_out = edges_out[edges_out["u"].isin(known) & edges_out["v"].isin(known)]
    log.info(
        "network: %d nodes, %d edges (dropped %d dangling)",
        len(nodes_out),
        len(edges_out),
        before - len(edges_out),
    )
    return nodes_out, edges_out


# ---------------------------------------------------------------- pois

ACCOM_TAGS = ["hotel", "hostel", "guest_house", "motel", "apartment"]


def build_pois(osm) -> gpd.GeoDataFrame:
    """Accommodation, food and service POIs, with synthetic capacity attached."""
    log.info("extracting POIs ...")
    frames = []

    accom = osm.get_pois(custom_filter={"tourism": ACCOM_TAGS})
    if accom is not None and not accom.empty:
        accom = accom.copy()
        accom["kind"] = accom["tourism"]
        frames.append(accom)

    food = osm.get_pois(custom_filter={"amenity": ["restaurant", "fast_food", "cafe"]})
    if food is not None and not food.empty:
        food = food.copy()
        food["kind"] = food["amenity"]
        frames.append(food)

    svc = osm.get_pois(
        custom_filter={"amenity": ["toilets", "drinking_water", "hospital", "clinic"]}
    )
    if svc is not None and not svc.empty:
        svc = svc.copy()
        svc["kind"] = svc["amenity"]
        frames.append(svc)

    if not frames:
        log.warning("no POIs found")
        return gpd.GeoDataFrame(
            columns=["poi_id", "kind", "name", "stars", "rooms", "capacity",
                     "price_inr"],
            geometry=[],
            crs="EPSG:4326",
        )

    pois = pd.concat(frames, ignore_index=True)
    pois = pois[pois.geometry.notna()].copy()
    # Polygonal POIs (hotel buildings) become their centroid, computed
    # in a projected CRS so it is not silently wrong.
    proj = gpd.GeoSeries(pois.geometry.values, crs="EPSG:4326").to_crs(S.UTM_CRS)
    pois["geometry"] = proj.centroid.to_crs("EPSG:4326").values

    if "stars" in pois.columns:
        stars = to_float(pois["stars"])
    else:
        stars = pd.Series(np.nan, index=pois.index)
    pois["stars_n"] = stars.where(stars.between(1, 5))

    def rooms_for(kind, star):
        if kind not in ACCOM_TAGS:
            return np.nan
        if pd.notna(star):
            return S.ROOMS_BY_STARS.get(int(star), S.DEFAULT_ROOMS.get(kind, 40))
        return S.DEFAULT_ROOMS.get(kind, 40)

    def price_for(kind, star):
        if kind not in ACCOM_TAGS:
            return np.nan
        if pd.notna(star):
            return S.PRICE_BY_STARS_INR.get(int(star), S.DEFAULT_PRICE_INR.get(kind))
        return S.DEFAULT_PRICE_INR.get(kind)

    pois["rooms"] = [rooms_for(k, s) for k, s in zip(pois["kind"], pois["stars_n"])]
    pois["price_inr"] = [price_for(k, s) for k, s in zip(pois["kind"], pois["stars_n"])]
    pois["capacity"] = np.where(
        pois["kind"].isin(["restaurant", "cafe", "fast_food"]), 60.0, np.nan
    )

    # Calibrate the synthetic room total to the open-data figure.
    total = pd.to_numeric(pois["rooms"], errors="coerce").sum()
    if total and total > 0:
        scale = S.TARGET_TOTAL_ROOMS / total
        pois["rooms"] = pd.to_numeric(pois["rooms"], errors="coerce") * scale
        log.info(
            "room calibration: raw %d -> target %d (x%.2f)",
            int(total),
            S.TARGET_TOTAL_ROOMS,
            scale,
        )

    out = gpd.GeoDataFrame(
        {
            "poi_id": pois["id"].astype("int64"),
            "kind": pois["kind"].astype(str),
            "name": pois["name"] if "name" in pois.columns else None,
            "stars": nullable_int(pois["stars_n"]),
            "rooms": nullable_int(pois["rooms"]),
            "capacity": nullable_int(pois["capacity"]),
            "price_inr": pd.to_numeric(pois["price_inr"], errors="coerce").round(2),
        },
        geometry=pois.geometry.values,
        crs="EPSG:4326",
    ).drop_duplicates("poi_id")

    log.info(
        "POIs: %d total, %d accommodation",
        len(out),
        int(out["kind"].isin(ACCOM_TAGS).sum()),
    )
    return out


# ---------------------------------------------------------------- assembly

def attach_zones(gdf: gpd.GeoDataFrame, zones: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    joined = gpd.sjoin(
        gdf, zones[["zone_id", "geometry"]], how="left", predicate="within"
    )
    return joined.drop(columns=["index_right"], errors="ignore")


def summarise_zones(zones: gpd.GeoDataFrame, pois: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    accom = pois[pois["kind"].isin(ACCOM_TAGS)]
    agg = (
        accom.groupby("zone_id")
        .agg(
            rooms_total=("rooms", "sum"),
            median_price=("price_inr", "median"),
            n_props=("poi_id", "count"),
        )
        .reset_index()
    )
    zones = zones.merge(agg, on="zone_id", how="left")
    zones["rooms_total"] = nullable_int(zones["rooms_total"]).fillna(0).astype(int)
    zones["beds_total"] = (zones["rooms_total"] * 2.2).round().astype(int)
    zones["median_price"] = pd.to_numeric(
        zones["median_price"], errors="coerce"
    ).round(2)
    zones["n_props"] = zones["n_props"].fillna(0).astype(int)
    keep = zones["n_props"] >= S.MIN_ZONE_POIS
    log.info("keeping %d of %d zones with accommodation", int(keep.sum()), len(zones))
    return zones[keep].drop(columns=["n_props"]).reset_index(drop=True)


def write_postgis(zones, nodes, edges, pois):
    eng = engine()
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE pois, edges, nodes, zones CASCADE"))

    log.info("writing zones ...")
    for_postgis(zones).to_postgis("zones", eng, if_exists="append", index=False)
    with eng.begin() as conn:
        conn.execute(text("UPDATE zones SET centroid = ST_Centroid(geom)"))

    log.info("writing nodes ...")
    for_postgis(nodes).to_postgis("nodes", eng, if_exists="append", index=False)

    log.info("writing edges ...")
    for_postgis(edges).to_postgis("edges", eng, if_exists="append", index=False)

    log.info("writing pois ...")
    for_postgis(pois).to_postgis("pois", eng, if_exists="append", index=False)

    with eng.connect() as conn:
        for tbl in ("zones", "nodes", "edges", "pois"):
            n = conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar()
            log.info("  %-7s %d rows", tbl, n)


def main():
    if not S.OSM_PBF.exists():
        log.error("missing %s", S.OSM_PBF)
        sys.exit(1)

    from pyrosm import OSM  # imported late: it is slow to load

    log.info("reading %s", S.OSM_PBF)
    osm = OSM(str(S.OSM_PBF))

    zones = build_zones(S.BBOX)
    nodes, edges = build_network(osm)
    pois = build_pois(osm)

    pois = attach_zones(pois, zones)
    nodes = attach_zones(nodes, zones)
    zones = summarise_zones(zones, pois)

    live = set(zones["zone_id"])
    pois.loc[~pois["zone_id"].isin(live), "zone_id"] = None
    nodes.loc[~nodes["zone_id"].isin(live), "zone_id"] = None

    write_postgis(zones, nodes, edges, pois)
    log.info("done - destination graph built")


if __name__ == "__main__":
    main()
