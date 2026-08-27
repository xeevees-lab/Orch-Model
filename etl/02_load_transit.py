"""
02_load_transit.py - fold the GTFS feed into the destination graph.

Creates:
  * transit stop nodes inside the study bbox
  * inter-stop edges per route with a headway-derived capacity and a
    median observed travel time
  * two-way walk access edges linking each stop into the street network

Headway maths: a line running every H minutes with vehicles holding C
people can move C/H people per minute. That is the capacity number the
twin needs, and it is why the GTFS timetable matters at all.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    python etl/02_load_transit.py
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("load_transit")

TRANSIT_ID_BASE = 9_100_000_000

# GTFS route_type -> (our mode, people per vehicle, fallback headway minutes)
ROUTE_TYPES = {
    0: ("metro", 250, 10),    # tram / light rail
    1: ("metro", 1100, 5),    # subway / metro
    2: ("rail", 2500, 4),     # rail - Mumbai locals run at crush load
    3: ("bus", 60, 12),       # bus
    4: ("bus", 300, 30),      # ferry
    5: ("bus", 60, 15),
    6: ("bus", 60, 15),
    7: ("bus", 60, 15),
    11: ("bus", 60, 12),
    12: ("rail", 800, 20),
}
DEFAULT_ROUTE_TYPE = (3, ("bus", 60, 12))

# Peak window used to measure headway, in seconds past midnight.
PEAK_START_S = 7 * 3600
PEAK_END_S = 11 * 3600


def engine():
    return create_engine(S.PG_DSN, future=True)


def find_gtfs() -> Path:
    zips = sorted(S.RAW_DIR.glob("*.zip"))
    if not zips:
        log.error("no GTFS zip found in %s", S.RAW_DIR)
        sys.exit(1)
    if len(zips) > 1:
        log.info("multiple zips found, using %s", zips[0].name)
    return zips[0]


def secs(series: pd.Series) -> pd.Series:
    """GTFS times can exceed 24h ('25:10:00'), so parse them by hand."""
    parts = series.astype(str).str.split(":", expand=True)
    if parts.shape[1] < 3:
        return pd.Series(np.nan, index=series.index)
    return (
        pd.to_numeric(parts[0], errors="coerce") * 3600
        + pd.to_numeric(parts[1], errors="coerce") * 60
        + pd.to_numeric(parts[2], errors="coerce")
    )


# ---------------------------------------------------------------- load

def read_feed(path: Path) -> dict[str, pd.DataFrame]:
    import zipfile

    wanted = ["stops.txt", "routes.txt", "trips.txt", "stop_times.txt"]
    out = {}
    with zipfile.ZipFile(path) as z:
        names = {Path(n).name: n for n in z.namelist()}
        for w in wanted:
            if w not in names:
                log.error("feed is missing %s - cannot build transit layer", w)
                sys.exit(1)
            with z.open(names[w]) as f:
                out[w[:-4]] = pd.read_csv(f, low_memory=False)
            log.info("  %-12s %d rows", w, len(out[w[:-4]]))
    return out


def filter_stops(stops: pd.DataFrame) -> pd.DataFrame:
    w, s, e, n = S.BBOX
    stops = stops.copy()
    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    inside = (
        stops["stop_lon"].between(w, e)
        & stops["stop_lat"].between(s, n)
    )
    kept = stops[inside].dropna(subset=["stop_lat", "stop_lon"]).copy()
    log.info("stops: %d of %d inside the bbox", len(kept), len(stops))
    if kept.empty:
        log.error("no stops inside the study area - wrong city feed?")
        sys.exit(1)
    kept = kept.reset_index(drop=True)
    kept["node_id"] = TRANSIT_ID_BASE + kept.index
    return kept


# ---------------------------------------------------------------- headway

def route_headways(feed: dict, stops: pd.DataFrame) -> pd.DataFrame:
    """Peak-window headway per route, in minutes."""
    st = feed["stop_times"].copy()
    st["dep_s"] = secs(st["departure_time"])
    st = st.merge(feed["trips"][["trip_id", "route_id"]], on="trip_id", how="left")

    first = (
        st.sort_values("stop_sequence")
        .groupby("trip_id", as_index=False)
        .first()[["trip_id", "route_id", "dep_s"]]
    )
    peak = first[first["dep_s"].between(PEAK_START_S, PEAK_END_S)]

    window_min = (PEAK_END_S - PEAK_START_S) / 60.0
    hw = (
        peak.groupby("route_id")
        .size()
        .rename("trips")
        .reset_index()
        .assign(headway_min=lambda d: window_min / d["trips"].clip(lower=1))
    )
    log.info("headways computed for %d routes", len(hw))
    return hw


def segment_times(feed: dict) -> pd.DataFrame:
    """Median observed travel time between consecutive stops, per route."""
    st = feed["stop_times"].copy()
    st["arr_s"] = secs(st["arrival_time"])
    st["dep_s"] = secs(st["departure_time"])
    st = st.merge(feed["trips"][["trip_id", "route_id"]], on="trip_id", how="left")
    st = st.sort_values(["trip_id", "stop_sequence"])

    st["next_stop"] = st.groupby("trip_id")["stop_id"].shift(-1)
    st["next_arr"] = st.groupby("trip_id")["arr_s"].shift(-1)
    st["run_min"] = (st["next_arr"] - st["dep_s"]) / 60.0

    seg = st.dropna(subset=["next_stop", "run_min"])
    seg = seg[(seg["run_min"] > 0) & (seg["run_min"] < 90)]
    out = (
        seg.groupby(["route_id", "stop_id", "next_stop"], as_index=False)["run_min"]
        .median()
        .rename(columns={"stop_id": "u_stop", "next_stop": "v_stop"})
    )
    log.info("segments: %d unique route legs", len(out))
    return out


# ---------------------------------------------------------------- write

def write_transit(feed, stops, segs, hw):
    routes = feed["routes"].copy()
    if "route_type" not in routes.columns:
        routes["route_type"] = DEFAULT_ROUTE_TYPE[0]
    routes["route_type"] = pd.to_numeric(
        routes["route_type"], errors="coerce"
    ).fillna(DEFAULT_ROUTE_TYPE[0]).astype(int)

    segs = segs.merge(
        routes[["route_id", "route_type", "route_short_name"]]
        if "route_short_name" in routes.columns
        else routes[["route_id", "route_type"]].assign(route_short_name=None),
        on="route_id",
        how="left",
    ).merge(hw, on="route_id", how="left")

    id_of = dict(zip(stops["stop_id"].astype(str), stops["node_id"]))
    segs["u"] = segs["u_stop"].astype(str).map(id_of)
    segs["v"] = segs["v_stop"].astype(str).map(id_of)
    before = len(segs)
    segs = segs.dropna(subset=["u", "v"])
    log.info("segments inside bbox: %d of %d", len(segs), before)

    eng = engine()
    with eng.begin() as conn:
        conn.execute(
            text("DELETE FROM edges WHERE mode IN ('rail','metro','bus')")
        )
        conn.execute(
            text("DELETE FROM edges WHERE highway = 'transit_access'")
        )
        conn.execute(
            text("DELETE FROM nodes WHERE node_id BETWEEN :a AND :b"),
            {"a": TRANSIT_ID_BASE, "b": TRANSIT_ID_BASE + 999_999},
        )

        log.info("writing %d stop nodes ...", len(stops))
        for r in stops.itertuples(index=False):
            conn.execute(
                text(
                    """
                    INSERT INTO nodes
                        (node_id, kind, name, zone_id, geom,
                         capacity, safe_density, service_rate)
                    VALUES (:nid, 'stop', :name,
                            (SELECT zone_id FROM zones
                             WHERE ST_Contains(geom,
                                   ST_SetSRID(ST_MakePoint(:lon,:lat),4326))
                             LIMIT 1),
                            ST_SetSRID(ST_MakePoint(:lon,:lat),4326),
                            NULL, :dens, NULL)
                    ON CONFLICT (node_id) DO NOTHING
                    """
                ),
                {
                    "nid": int(r.node_id),
                    "name": str(getattr(r, "stop_name", "") or "")[:200],
                    "lon": float(r.stop_lon),
                    "lat": float(r.stop_lat),
                    "dens": S.SAFE_DENSITY,
                },
            )

        log.info("writing %d transit edges ...", len(segs))
        n_edges = 0
        for r in segs.itertuples(index=False):
            mode, per_veh, fallback_hw = ROUTE_TYPES.get(
                int(r.route_type), DEFAULT_ROUTE_TYPE[1]
            )
            headway = r.headway_min if pd.notna(r.headway_min) else fallback_hw
            headway = max(float(headway), 0.5)
            cap_ppm = per_veh / headway

            conn.execute(
                text(
                    """
                    INSERT INTO edges
                        (u, v, mode, highway, name, length_m,
                         capacity_ppm, free_flow_min, oneway, geom)
                    SELECT :u, :v, :mode, 'transit', :name,
                           ST_Distance(a.geom::geography, b.geom::geography),
                           :cap, :tmin, true,
                           ST_MakeLine(a.geom, b.geom)
                    FROM nodes a, nodes b
                    WHERE a.node_id = :u AND b.node_id = :v
                    """
                ),
                {
                    "u": int(r.u),
                    "v": int(r.v),
                    "mode": mode,
                    "name": str(r.route_short_name or r.route_id)[:200],
                    "cap": round(cap_ppm, 2),
                    "tmin": round(float(r.run_min), 3),
                },
            )
            n_edges += 1

        # Walk access from each stop into the street network.
        log.info("wiring stops into the walk network ...")
        conn.execute(
            text(
                """
                INSERT INTO edges
                    (u, v, mode, highway, name, length_m,
                     capacity_ppm, free_flow_min, oneway, geom)
                SELECT s.node_id, j.node_id, 'walk', 'transit_access',
                       'stop access', j.dist,
                       240, j.dist / :speed / 60.0, false,
                       ST_MakeLine(s.geom, j.geom)
                FROM nodes s
                CROSS JOIN LATERAL (
                    SELECT n.node_id, n.geom,
                           ST_Distance(n.geom::geography, s.geom::geography) AS dist
                    FROM nodes n
                    WHERE n.kind = 'junction'
                    ORDER BY n.geom <-> s.geom
                    LIMIT 1
                ) j
                WHERE s.kind = 'stop'
                """
            ),
            {"speed": S.WALK_SPEED_MPS},
        )
        conn.execute(
            text(
                """
                INSERT INTO edges
                    (u, v, mode, highway, name, length_m,
                     capacity_ppm, free_flow_min, oneway, geom)
                SELECT e.v, e.u, 'walk', 'transit_access', 'stop access',
                       e.length_m, e.capacity_ppm, e.free_flow_min, false,
                       ST_Reverse(e.geom)
                FROM edges e
                WHERE e.highway = 'transit_access'
                """
            )
        )

    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT mode, count(*) AS n, round(avg(capacity_ppm)) AS cap
                FROM edges GROUP BY mode ORDER BY mode
                """
            )
        ).fetchall()
        for mode, n, cap in rows:
            log.info("  %-7s %7d edges, avg %s ppm", mode, n, cap)
        n_stops = conn.execute(
            text("SELECT count(*) FROM nodes WHERE kind = 'stop'")
        ).scalar()
        log.info("  stops   %d", n_stops)


def main():
    path = find_gtfs()
    log.info("reading %s", path.name)
    feed = read_feed(path)

    stops = filter_stops(feed["stops"])
    hw = route_headways(feed, stops)
    segs = segment_times(feed)

    write_transit(feed, stops, segs, hw)
    log.info("done - transit layer loaded")


if __name__ == "__main__":
    main()
