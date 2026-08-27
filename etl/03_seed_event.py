"""
03_seed_event.py - lay the Ganesh Chaturthi event onto the destination graph.

Reads config/event_ganesh.yaml and:
  * inserts pandals, immersion points and holding areas as graph nodes,
    each snapped to the nearest existing junction so they are routable
  * writes the multi-day schedule: darshan windows and processions
  * fixes zone prices so they carry spatial signal instead of one default

Run:
    cd ~/megaevent
    source .venv/bin/activate
    python etl/03_seed_event.py
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import yaml
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("seed_event")

EVENT_YAML = Path(__file__).resolve().parents[1] / "config" / "event_ganesh.yaml"
IST = timezone(timedelta(hours=5, minutes=30))

# Synthetic node ids for event venues, kept well clear of OSM ids.
EVENT_ID_BASE = 9_000_000_000


def engine():
    return create_engine(S.PG_DSN, future=True)


def load_event() -> dict:
    with open(EVENT_YAML) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- venues

def snap_and_insert(conn, venues: list[dict], kind: str, start_id: int) -> dict:
    """Insert each venue as a node, wired to the nearest junction.

    Returns {yaml_id: node_id}. The snap distance is logged - anything
    over ~500 m means the coordinate is probably in the wrong place.
    """
    mapping = {}
    for i, v in enumerate(venues):
        node_id = start_id + i
        row = conn.execute(
            text(
                """
                SELECT node_id,
                       ST_Distance(
                           geom::geography,
                           ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                       ) AS dist_m
                FROM nodes
                WHERE kind = 'junction'
                ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                LIMIT 1
                """
            ),
            {"lon": v["lon"], "lat": v["lat"]},
        ).fetchone()

        if row is None:
            log.warning("no junction found near %s - skipping", v["name"])
            continue

        nearest_id, dist_m = row
        if dist_m > 500:
            log.warning(
                "%s snapped %.0f m to the nearest junction - check its coordinates",
                v["name"],
                dist_m,
            )

        conn.execute(
            text(
                """
                INSERT INTO nodes
                    (node_id, kind, name, zone_id, geom, capacity,
                     safe_density, service_rate)
                VALUES
                    (:nid, :kind, :name,
                     (SELECT zone_id FROM zones
                      WHERE ST_Contains(geom,
                            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
                      LIMIT 1),
                     ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                     :cap, :dens, :rate)
                ON CONFLICT (node_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    capacity = EXCLUDED.capacity,
                    service_rate = EXCLUDED.service_rate,
                    geom = EXCLUDED.geom
                """
            ),
            {
                "nid": node_id,
                "kind": kind,
                "name": v["name"],
                "lon": v["lon"],
                "lat": v["lat"],
                "cap": v.get("capacity"),
                "dens": S.SAFE_DENSITY,
                "rate": v.get("service_rate"),
            },
        )

        # Short access edge linking the venue into the walk network,
        # both directions, so routing can reach it.
        for u, w in ((node_id, nearest_id), (nearest_id, node_id)):
            conn.execute(
                text(
                    """
                    INSERT INTO edges
                        (u, v, mode, highway, name, length_m,
                         capacity_ppm, free_flow_min, oneway, geom)
                    SELECT :u, :v, 'walk', 'access', :name, :len,
                           :cap, :tmin, false,
                           ST_MakeLine(a.geom, b.geom)
                    FROM nodes a, nodes b
                    WHERE a.node_id = :u AND b.node_id = :v
                    """
                ),
                {
                    "u": u,
                    "v": w,
                    "name": f"access to {v['name']}",
                    "len": round(max(dist_m, 1.0), 2),
                    "cap": round(v.get("service_rate") or 200, 2),
                    "tmin": round(max(dist_m, 1.0) / S.WALK_SPEED_MPS / 60.0, 3),
                },
            )

        mapping[v["id"]] = node_id
        log.info("  %-32s -> node %d (snap %.0f m)", v["name"], node_id, dist_m)

    return mapping


# ---------------------------------------------------------------- schedule

def seed_schedule(conn, ev: dict, nodes: dict) -> None:
    fest = ev["festival"]
    start = datetime.fromisoformat(fest["start_date"]).replace(tzinfo=IST)
    darshan = ev["darshan"]
    weights = {int(k): v for k, v in darshan["day_weights"].items()}

    conn.execute(text("DELETE FROM schedule"))
    n_items = 0

    # Daily darshan windows, one per pandal per day.
    total_weight = sum(p["draw_weight"] for p in ev["pandals"])
    for day in range(1, fest["days"] + 1):
        day_date = start + timedelta(days=day - 1)
        w = weights.get(day, 1.0)
        for p in ev["pandals"]:
            nid = nodes.get(p["id"])
            if nid is None:
                continue
            share = p["draw_weight"] / total_weight
            expected = int(darshan["baseline_daily_visitors"] * w * share)
            conn.execute(
                text(
                    """
                    INSERT INTO schedule
                        (item_id, day, starts_at, ends_at, kind,
                         origin_node, dest_node, expected_crowd, is_movable)
                    VALUES (:iid, :day, :st, :en, 'darshan',
                            NULL, :dest, :crowd, false)
                    """
                ),
                {
                    "iid": f"darshan_{p['id']}_d{day}",
                    "day": day_date.date(),
                    "st": day_date.replace(hour=darshan["open_hour"], minute=0),
                    "en": day_date.replace(hour=0, minute=0)
                    + timedelta(hours=darshan["close_hour"]),
                    "dest": nid,
                    "crowd": expected,
                },
            )
            n_items += 1

    # Processions on their immersion days.
    for pr in ev["processions"]:
        o = nodes.get(pr["pandal"])
        d = nodes.get(pr["immersion"])
        if o is None or d is None:
            log.warning("procession %s -> %s missing a node, skipped",
                        pr["pandal"], pr["immersion"])
            continue
        for day in pr["days"]:
            day_date = start + timedelta(days=day - 1)
            st = day_date.replace(hour=pr["start_hour"], minute=0)
            conn.execute(
                text(
                    """
                    INSERT INTO schedule
                        (item_id, day, starts_at, ends_at, kind,
                         origin_node, dest_node, expected_crowd, is_movable)
                    VALUES (:iid, :day, :st, :en, 'procession',
                            :o, :d, :crowd, true)
                    """
                ),
                {
                    "iid": f"proc_{pr['pandal']}_{pr['immersion']}_d{day}",
                    "day": day_date.date(),
                    "st": st,
                    "en": st + timedelta(hours=pr["duration_h"]),
                    "o": o,
                    "d": d,
                    "crowd": pr["crowd"],
                },
            )
            n_items += 1

    log.info("schedule: %d items across %d days", n_items, fest["days"])


# ---------------------------------------------------------------- prices

def fix_zone_prices(conn) -> None:
    """Give zone prices spatial signal.

    OSM rarely tags hotel stars, so nearly every property inherits one
    default price and the optimizer's price term becomes meaningless.
    Apply a centrality premium: closer to the island city, more expensive.
    """
    conn.execute(
        text(
            """
            WITH ref AS (
                SELECT ST_SetSRID(ST_MakePoint(72.8330, 18.9400), 4326) AS g
            ),
            d AS (
                SELECT z.zone_id,
                       ST_Distance(z.centroid::geography, ref.g::geography) / 1000.0
                           AS km
                FROM zones z, ref
            ),
            scaled AS (
                SELECT zone_id,
                       1.0 + :w * (1.0 - (km - MIN(km) OVER ())
                             / NULLIF(MAX(km) OVER () - MIN(km) OVER (), 0))
                       AS mult
                FROM d
            )
            UPDATE zones z
            SET median_price = ROUND(z.median_price * s.mult)
            FROM scaled s
            WHERE z.zone_id = s.zone_id
              AND z.median_price IS NOT NULL
            """
        ),
        {"w": S.PRICE_CENTRALITY_WEIGHT},
    )
    conn.execute(
        text(
            """
            UPDATE pois p
            SET price_inr = ROUND(p.price_inr * z.median_price
                                  / NULLIF(base.avg_price, 0))
            FROM zones z,
                 (SELECT AVG(median_price) AS avg_price FROM zones) base
            WHERE p.zone_id = z.zone_id
              AND p.price_inr IS NOT NULL
            """
        )
    )
    log.info("zone prices rescaled by centrality")


# ---------------------------------------------------------------- main

def main():
    if not EVENT_YAML.exists():
        log.error("missing %s", EVENT_YAML)
        sys.exit(1)

    ev = load_event()
    eng = engine()

    with eng.begin() as conn:
        conn.execute(text("DELETE FROM schedule"))
        # Clear any previous event seed.
        conn.execute(
            text("DELETE FROM edges WHERE highway = 'access'")
        )
        conn.execute(
            text("DELETE FROM nodes WHERE node_id >= :base"),
            {"base": EVENT_ID_BASE},
        )

        log.info("seeding pandals ...")
        nodes = snap_and_insert(conn, ev["pandals"], "pandal", EVENT_ID_BASE)

        log.info("seeding immersion points ...")
        nodes |= snap_and_insert(
            conn, ev["immersion_points"], "immersion", EVENT_ID_BASE + 1000
        )

        log.info("seeding holding areas ...")
        nodes |= snap_and_insert(
            conn, ev["holding_areas"], "holding", EVENT_ID_BASE + 2000
        )

        seed_schedule(conn, ev, nodes)
        fix_zone_prices(conn)

    with eng.connect() as conn:
        for kind in ("pandal", "immersion", "holding"):
            n = conn.execute(
                text("SELECT count(*) FROM nodes WHERE kind = :k"), {"k": kind}
            ).scalar()
            log.info("  %-10s %d nodes", kind, n)
        n = conn.execute(text("SELECT count(*) FROM schedule")).scalar()
        log.info("  schedule   %d items", n)

    log.info("done - event layer seeded")


if __name__ == "__main__":
    main()
