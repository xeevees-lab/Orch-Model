"""
04_load_rail.py - add Mumbai suburban rail and metro to the graph.

The GTFS feed is bus-only, but rail carries the bulk of visarjan crowd
movement. This script closes that gap.

Station coordinates come from OSM (railway=station / halt, and metro
stations), matched by name against config/rail_lines.yaml. Nothing is
hand-typed, so nothing is silently in the wrong place. Unmatched
stations are reported - add the OSM spelling to that line's `aliases`.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    python etl/04_load_rail.py
"""

from __future__ import annotations

import re
import sys
import logging
from pathlib import Path

import yaml
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("load_rail")

RAIL_YAML = Path(__file__).resolve().parents[1] / "config" / "rail_lines.yaml"
RAIL_ID_BASE = 9_200_000_000

# Average dwell at a station, added to every inter-station leg.
DWELL_MIN = 0.5
# Assumed average speed between stations, km/h.
LINE_SPEED_KPH = {"rail": 42.0, "metro": 33.0}


def engine():
    return create_engine(S.PG_DSN, future=True)


def norm(s: str) -> str:
    """Loose key for name matching: lowercase, alphanumerics only."""
    s = re.sub(r"\b(railway|metro)?\s*station\b", " ", str(s), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- osm

def osm_stations() -> pd.DataFrame:
    """Every railway/metro station point in the extract, with a match key."""
    from pyrosm import OSM

    log.info("reading stations from %s", S.OSM_PBF.name)
    osm = OSM(str(S.OSM_PBF))

    frames = []
    for flt in (
        {"railway": ["station", "halt"]},
        {"public_transport": ["station"]},
        {"station": ["subway", "light_rail"]},
    ):
        try:
            g = osm.get_pois(custom_filter=flt)
        except Exception:
            g = None
        if g is not None and not g.empty:
            frames.append(g)

    if not frames:
        log.error("no stations found in the OSM extract")
        sys.exit(1)

    df = pd.concat(frames, ignore_index=True)
    df = df[df.geometry.notna()].copy()
    if "name" not in df.columns:
        log.error("OSM station records have no name field")
        sys.exit(1)
    df = df[df["name"].notna()].copy()

    proj = gpd.GeoSeries(df.geometry.values, crs="EPSG:4326").to_crs(S.UTM_CRS)
    pts = proj.centroid.to_crs("EPSG:4326")
    df["lon"] = pts.x.values
    df["lat"] = pts.y.values
    df["key"] = df["name"].map(norm)

    df = df.drop_duplicates("key").reset_index(drop=True)
    log.info("OSM stations available: %d", len(df))
    return df[["name", "key", "lon", "lat"]]


def resolve(line: dict, stations: pd.DataFrame) -> list[dict]:
    """Match each configured station name to an OSM point."""
    by_key = {r.key: r for r in stations.itertuples(index=False)}
    aliases = {k: v for k, v in (line.get("aliases") or {}).items()}

    resolved, missing = [], []
    for name in line["stations"]:
        candidates = [name] + list(aliases.get(name, []))
        hit = None
        for c in candidates:
            hit = by_key.get(norm(c))
            if hit:
                break
        if hit is None:
            # Last resort: substring match, longest OSM name wins.
            k = norm(name)
            subs = [r for key, r in by_key.items() if k and k in key]
            hit = max(subs, key=lambda r: len(r.key)) if subs else None
        if hit is None:
            missing.append(name)
        else:
            resolved.append(
                {"cfg_name": name, "osm_name": hit.name, "lon": hit.lon,
                 "lat": hit.lat}
            )

    if missing:
        log.warning(
            "%s: %d unmatched -> %s",
            line["name"],
            len(missing),
            ", ".join(missing),
        )
    log.info("%s: matched %d of %d stations", line["name"],
             len(resolved), len(line["stations"]))
    return resolved


# ---------------------------------------------------------------- write

def load(cfg: dict):
    stations = osm_stations()
    eng = engine()

    with eng.begin() as conn:
        conn.execute(text("DELETE FROM edges WHERE highway IN ('rail','interchange')"))
        conn.execute(
            text("DELETE FROM nodes WHERE node_id BETWEEN :a AND :b"),
            {"a": RAIL_ID_BASE, "b": RAIL_ID_BASE + 99_999},
        )

        next_id = RAIL_ID_BASE
        # cfg_name -> [node_id, ...] so interchanges can find both platforms
        by_name: dict[str, list[int]] = {}
        n_edges = 0

        for line in cfg["lines"]:
            pts = resolve(line, stations)
            if len(pts) < 2:
                log.warning("%s: too few stations, skipped", line["name"])
                continue

            mode = line["mode"]
            headway = float(line["headway_peak_min"])
            cap_ppm = float(line["capacity_per_vehicle"]) / max(headway, 0.5)
            speed = LINE_SPEED_KPH.get(mode, 35.0)

            ids = []
            for p in pts:
                nid = next_id
                next_id += 1
                conn.execute(
                    text(
                        """
                        INSERT INTO nodes
                            (node_id, kind, name, zone_id, geom,
                             capacity, safe_density, service_rate)
                        VALUES (:nid, 'station', :name,
                                (SELECT zone_id FROM zones
                                 WHERE ST_Contains(geom,
                                    ST_SetSRID(ST_MakePoint(:lon,:lat),4326))
                                 LIMIT 1),
                                ST_SetSRID(ST_MakePoint(:lon,:lat),4326),
                                :cap, :dens, :rate)
                        """
                    ),
                    {
                        "nid": nid,
                        "name": f"{p['osm_name']} ({line['name']})",
                        "lon": p["lon"],
                        "lat": p["lat"],
                        "cap": 4000 if mode == "rail" else 2000,
                        "dens": S.SAFE_DENSITY,
                        "rate": round(cap_ppm, 2),
                    },
                )
                ids.append(nid)
                by_name.setdefault(p["cfg_name"], []).append(nid)

            # Consecutive stations, both directions.
            for a, b in zip(ids, ids[1:]):
                for u, v in ((a, b), (b, a)):
                    conn.execute(
                        text(
                            """
                            INSERT INTO edges
                                (u, v, mode, highway, name, length_m,
                                 capacity_ppm, free_flow_min, oneway, geom)
                            SELECT :u, :v, :mode, 'rail', :name,
                                   ST_Distance(x.geom::geography,
                                               y.geom::geography),
                                   :cap,
                                   ST_Distance(x.geom::geography,
                                               y.geom::geography)
                                     / (:speed * 1000 / 60.0) + :dwell,
                                   true,
                                   ST_MakeLine(x.geom, y.geom)
                            FROM nodes x, nodes y
                            WHERE x.node_id = :u AND y.node_id = :v
                            """
                        ),
                        {
                            "u": u,
                            "v": v,
                            "mode": mode,
                            "name": line["name"],
                            "cap": round(cap_ppm, 2),
                            "speed": speed,
                            "dwell": DWELL_MIN,
                        },
                    )
                    n_edges += 1

        # Interchange links between platforms sharing a station name.
        n_ix = 0
        for name, ids in by_name.items():
            if len(ids) < 2:
                continue
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    for u, v in ((a, b), (b, a)):
                        conn.execute(
                            text(
                                """
                                INSERT INTO edges
                                    (u, v, mode, highway, name, length_m,
                                     capacity_ppm, free_flow_min, oneway, geom)
                                SELECT :u, :v, 'walk', 'interchange',
                                       :name, 120, 400, 3.0, false,
                                       ST_MakeLine(x.geom, y.geom)
                                FROM nodes x, nodes y
                                WHERE x.node_id = :u AND y.node_id = :v
                                """
                            ),
                            {"u": u, "v": v, "name": f"{name} interchange"},
                        )
                        n_ix += 1

        # Wire every station into the walk network, both ways.
        conn.execute(
            text(
                """
                INSERT INTO edges
                    (u, v, mode, highway, name, length_m,
                     capacity_ppm, free_flow_min, oneway, geom)
                SELECT s.node_id, j.node_id, 'walk', 'transit_access',
                       'station access', j.dist, 600,
                       j.dist / :speed / 60.0, false,
                       ST_MakeLine(s.geom, j.geom)
                FROM nodes s
                CROSS JOIN LATERAL (
                    SELECT n.node_id, n.geom,
                           ST_Distance(n.geom::geography,
                                       s.geom::geography) AS dist
                    FROM nodes n
                    WHERE n.kind = 'junction'
                    ORDER BY n.geom <-> s.geom
                    LIMIT 1
                ) j
                WHERE s.kind = 'station'
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
                SELECT e.v, e.u, 'walk', 'transit_access', 'station access',
                       e.length_m, e.capacity_ppm, e.free_flow_min, false,
                       ST_Reverse(e.geom)
                FROM edges e
                WHERE e.name = 'station access'
                """
            )
        )

        log.info("rail: %d line edges, %d interchange edges", n_edges, n_ix)

    with eng.connect() as conn:
        for mode, n, cap in conn.execute(
            text(
                """
                SELECT mode, count(*), round(avg(capacity_ppm))
                FROM edges GROUP BY mode ORDER BY mode
                """
            )
        ):
            log.info("  %-7s %7d edges, avg %s ppm", mode, n, cap)
        n = conn.execute(
            text("SELECT count(*) FROM nodes WHERE kind='station'")
        ).scalar()
        log.info("  stations %d", n)


def main():
    if not RAIL_YAML.exists():
        log.error("missing %s", RAIL_YAML)
        sys.exit(1)
    with open(RAIL_YAML) as f:
        cfg = yaml.safe_load(f)
    load(cfg)
    log.info("done - rail and metro loaded")


if __name__ == "__main__":
    main()
