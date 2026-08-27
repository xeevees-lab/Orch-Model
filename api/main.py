"""
api/main.py - read-only API over the destination graph.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    uvicorn api.main:app --reload --port 8000

Then open http://localhost:8000/docs
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

app = FastAPI(title="Mega-Event Orchestration API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_engine(S.PG_DSN, future=True, pool_pre_ping=True)


def geojson(sql: str, params: dict | None = None) -> dict:
    """Run a query whose rows are (geometry, properties json) and wrap it."""
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params or {}).fetchall()
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": g, "properties": p} for g, p in rows
        ],
    }


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/stats")
def stats():
    """Headline numbers for the console header."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM zones)                     AS zones,
                  (SELECT sum(rooms_total) FROM zones)             AS rooms,
                  (SELECT max(rooms_total) FROM zones)             AS rooms_max,
                  (SELECT min(rooms_total) FROM zones)             AS rooms_min,
                  (SELECT count(*) FROM nodes WHERE kind='pandal') AS pandals,
                  (SELECT count(*) FROM nodes
                     WHERE kind='immersion')                       AS immersion,
                  (SELECT count(*) FROM nodes WHERE kind='stop')   AS stops,
                  (SELECT count(*) FROM edges)                     AS edges,
                  (SELECT count(*) FROM schedule)                  AS schedule_items
                """
            )
        ).mappings().one()
    d = dict(row)
    lo = d["rooms_min"] or 1
    d["imbalance"] = round((d["rooms_max"] or 0) / lo, 1)
    return d


@app.get("/zones")
def zones():
    """Accommodation zones with room supply. The console's base data layer."""
    return geojson(
        """
        SELECT ST_AsGeoJSON(geom)::json,
               json_build_object(
                 'zone_id', zone_id,
                 'rooms_total', rooms_total,
                 'beds_total', beds_total,
                 'median_price', median_price,
                 'area_sqkm', area_sqkm
               )
        FROM zones
        ORDER BY rooms_total DESC
        """
    )


@app.get("/venues")
def venues():
    """Event venues: pandals, immersion points, holding areas."""
    return geojson(
        """
        SELECT ST_AsGeoJSON(geom)::json,
               json_build_object(
                 'node_id', node_id,
                 'name', name,
                 'kind', kind,
                 'capacity', capacity,
                 'service_rate', service_rate,
                 'zone_id', zone_id
               )
        FROM nodes
        WHERE kind IN ('pandal', 'immersion', 'holding')
        ORDER BY kind, name
        """
    )


@app.get("/stops")
def stops(limit: int = Query(2000, le=6000)):
    """Transit stops. Capped, because there are thousands."""
    return geojson(
        """
        SELECT ST_AsGeoJSON(geom)::json,
               json_build_object('node_id', node_id, 'name', name)
        FROM nodes
        WHERE kind = 'stop'
        LIMIT :lim
        """,
        {"lim": limit},
    )


@app.get("/schedule")
def schedule(day: int | None = None):
    """Festival schedule. Optionally one day, counted from day 1."""
    sql = """
        SELECT s.item_id, s.day, s.starts_at, s.ends_at, s.kind,
               s.expected_crowd, s.is_movable,
               o.name AS origin, d.name AS destination
        FROM schedule s
        LEFT JOIN nodes o ON o.node_id = s.origin_node
        LEFT JOIN nodes d ON d.node_id = s.dest_node
        {where}
        ORDER BY s.starts_at
    """
    params = {}
    where = ""
    if day is not None:
        where = """
            WHERE s.day = (SELECT min(day) FROM schedule)
                          + (:d - 1) * INTERVAL '1 day'
        """
        params["d"] = day
    with engine.connect() as conn:
        rows = conn.execute(text(sql.format(where=where)), params).mappings().all()
    return [dict(r) for r in rows]


@app.get("/venues/{node_id}/nearby-supply")
def nearby_supply(node_id: int, radius_km: float = 5.0):
    """Rooms available within a radius of a venue.

    This is the seed of the zone-assignment problem: which zones could
    absorb demand for this venue, and how far away are they.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT z.zone_id, z.rooms_total, z.median_price,
                       round((ST_Distance(z.centroid::geography,
                              n.geom::geography) / 1000.0)::numeric, 2) AS km
                FROM zones z, nodes n
                WHERE n.node_id = :nid
                  AND ST_DWithin(z.centroid::geography,
                                 n.geom::geography, :r)
                ORDER BY km
                """
            ),
            {"nid": node_id, "r": radius_km * 1000},
        ).mappings().all()
    return [dict(r) for r in rows]
