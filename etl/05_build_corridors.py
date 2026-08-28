"""
05_build_corridors.py - make the edge network usable.

The graph holds ~390,000 edges. Projecting each one every tick is
pointless: an operator cannot act on a single 40-metre link, and the
twin cannot finish the arithmetic between ticks.

So edges are grouped into CORRIDORS - things a person would name in a
radio call. A rail line. A named arterial. The walking approach to a
venue. That gives ~40-60 objects instead of 390,000, and each one has:

    capacity_ppm   the bottleneck along it, not the average. A corridor
                   is only as good as its narrowest link.
    length_m       for travel time
    mode           rail | metro | bus | road | walk

Then, for every (accommodation zone -> venue) pair, the shortest path is
computed once and the corridors it traverses are recorded. At run time
the twin only has to push forecast demand through that lookup table.

Run:
    cd ~/megaevent (or the repo root)
    source .venv/bin/activate
    python etl/05_build_corridors.py
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path
from collections import defaultdict

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("corridors")

# A named road needs at least this many edges to count as an arterial.
MIN_ARTERIAL_EDGES = 8
# Walk edges within this distance of a venue form its approach corridor.
APPROACH_M = 700
# Road classes that carry enough to be worth naming.
ARTERIAL_CLASSES = ("motorway", "trunk", "primary", "secondary")


DDL = """
CREATE TABLE IF NOT EXISTS corridors (
    corridor_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- line | arterial | approach
    capacity_ppm  NUMERIC(10,2) NOT NULL,
    length_m      NUMERIC(12,2),
    n_edges       INTEGER,
    geom          GEOMETRY(MultiLineString, 4326)
);
CREATE INDEX IF NOT EXISTS corridors_geom_idx ON corridors USING GIST (geom);
CREATE INDEX IF NOT EXISTS corridors_mode_idx ON corridors (mode);

CREATE TABLE IF NOT EXISTS corridor_edges (
    corridor_id TEXT REFERENCES corridors(corridor_id) ON DELETE CASCADE,
    edge_id     BIGINT,
    PRIMARY KEY (corridor_id, edge_id)
);

-- Which corridors a journey from a zone to a venue passes through.
CREATE TABLE IF NOT EXISTS corridor_paths (
    zone_id     TEXT NOT NULL,
    node_id     BIGINT NOT NULL,
    corridor_id TEXT NOT NULL,
    share       NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    PRIMARY KEY (zone_id, node_id, corridor_id)
);
CREATE INDEX IF NOT EXISTS corridor_paths_zv_idx
    ON corridor_paths (zone_id, node_id);

CREATE TABLE IF NOT EXISTS corridor_state (
    ts             TIMESTAMPTZ NOT NULL,
    corridor_id    TEXT NOT NULL,
    flow_ppm       NUMERIC(10,2) NOT NULL,
    pressure_index NUMERIC(6,3),
    tts_min        NUMERIC(8,2),
    is_forecast    BOOLEAN DEFAULT FALSE
);
SELECT create_hypertable('corridor_state', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS corridor_state_cid_idx
    ON corridor_state (corridor_id, ts DESC);
"""


def engine():
    return create_engine(S.PG_DSN, future=True)


# ---------------------------------------------------------------- group

def build_corridors(conn) -> pd.DataFrame:
    """Group edges into named corridors."""
    log.info("reading edges ...")
    edges = pd.read_sql(
        """
            SELECT edge_id, u, v, mode, highway, name, length_m,
                   capacity_ppm
            FROM edges
            """,
        conn,
    )
    log.info("  %d edges", len(edges))

    groups: dict[str, dict] = {}
    member: dict[str, list[int]] = defaultdict(list)

    # --- transit lines: group by line name -----------------------
    transit = edges[edges["mode"].isin(["rail", "metro", "bus"])]
    for (mode, name), grp in transit.groupby(["mode", "name"]):
        if not name or len(grp) < 2:
            continue
        cid = f"{mode}:{name}".replace(" ", "_").lower()[:60]
        groups[cid] = {
            "corridor_id": cid,
            "name": str(name),
            "mode": mode,
            "kind": "line",
            # A corridor is only as good as its narrowest link.
            "capacity_ppm": float(grp["capacity_ppm"].min()),
            "length_m": float(grp["length_m"].sum()),
            "n_edges": len(grp),
        }
        member[cid] = grp["edge_id"].tolist()

    log.info("  transit lines: %d",
             sum(1 for g in groups.values() if g["kind"] == "line"))

    # --- road arterials: named roads of a decent class -----------
    roads = edges[
        (edges["mode"] == "road")
        & (edges["highway"].isin(ARTERIAL_CLASSES))
        & (edges["name"].notna())
        & (edges["name"] != "None")
    ]
    for name, grp in roads.groupby("name"):
        if len(grp) < MIN_ARTERIAL_EDGES:
            continue
        cid = f"road:{name}".replace(" ", "_").lower()[:60]
        if cid in groups:
            continue
        groups[cid] = {
            "corridor_id": cid,
            "name": str(name),
            "mode": "road",
            "kind": "arterial",
            "capacity_ppm": float(grp["capacity_ppm"].min()),
            "length_m": float(grp["length_m"].sum()),
            "n_edges": len(grp),
        }
        member[cid] = grp["edge_id"].tolist()

    log.info("  road arterials: %d",
             sum(1 for g in groups.values() if g["kind"] == "arterial"))

    # --- venue approaches: the last walk in ----------------------
    approaches = pd.read_sql(
        """
            SELECT n.node_id, n.name AS venue, e.edge_id, e.length_m,
                   e.capacity_ppm
            FROM nodes n
            JOIN edges e
              ON ST_DWithin(e.geom::geography, n.geom::geography, :r)
            WHERE n.kind IN ('pandal', 'immersion')
              AND e.mode = 'walk'
            """,
        conn,
        params={"r": APPROACH_M},
    )
    for node_id, grp in approaches.groupby("node_id"):
        cid = f"approach:{node_id}"
        groups[cid] = {
            "corridor_id": cid,
            "name": f"Approach to {grp['venue'].iloc[0]}",
            "mode": "walk",
            "kind": "approach",
            "capacity_ppm": float(grp["capacity_ppm"].min()),
            "length_m": float(grp["length_m"].sum()),
            "n_edges": len(grp),
        }
        member[cid] = grp["edge_id"].tolist()

    log.info("  venue approaches: %d",
             sum(1 for g in groups.values() if g["kind"] == "approach"))

    df = pd.DataFrame(groups.values())
    log.info("corridors: %d total", len(df))

    # write
    conn.execute(text("TRUNCATE corridor_edges, corridors CASCADE"))
    for r in df.to_dict("records"):
        conn.execute(
            text(
                """
                INSERT INTO corridors
                    (corridor_id, name, mode, kind, capacity_ppm,
                     length_m, n_edges)
                VALUES (:corridor_id, :name, :mode, :kind, :capacity_ppm,
                        :length_m, :n_edges)
                """
            ),
            r,
        )
    for cid, eids in member.items():
        conn.execute(
            text(
                "INSERT INTO corridor_edges (corridor_id, edge_id) "
                "SELECT :c, unnest(CAST(:e AS bigint[])) "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": cid, "e": eids},
        )

    # Geometry, for drawing them on a map.
    conn.execute(
        text(
            """
            UPDATE corridors c
            SET geom = sub.g
            FROM (
                SELECT ce.corridor_id,
                       ST_Multi(ST_Union(e.geom)) AS g
                FROM corridor_edges ce JOIN edges e USING (edge_id)
                GROUP BY ce.corridor_id
            ) sub
            WHERE c.corridor_id = sub.corridor_id
            """
        )
    )
    return df


# ---------------------------------------------------------------- paths

def build_paths(conn):
    """For each zone -> venue journey, record the corridors it uses.

    Done once here so the twin does no routing at run time. Uses igraph
    for the shortest paths: 68 single-source runs over ~390k edges takes
    seconds, where doing it per tick would not.
    """
    import igraph as ig

    log.info("loading graph for path finding ...")
    edges = pd.read_sql(
        "SELECT edge_id, u, v, free_flow_min FROM edges",
        conn,
    )
    edge_corr = pd.read_sql(
        "SELECT edge_id, corridor_id FROM corridor_edges",
        conn,
    )
    corr_of = dict(zip(edge_corr["edge_id"], edge_corr["corridor_id"]))

    nodes = pd.unique(pd.concat([edges["u"], edges["v"]]))
    idx = {n: i for i, n in enumerate(nodes)}
    g = ig.Graph(directed=True)
    g.add_vertices(len(nodes))
    g.add_edges([(idx[u], idx[v]) for u, v in zip(edges["u"], edges["v"])])
    g.es["w"] = edges["free_flow_min"].fillna(1.0).tolist()
    g.es["eid"] = edges["edge_id"].tolist()
    log.info("  graph: %d vertices, %d edges", g.vcount(), g.ecount())

    # Zone entry points: nearest junction to each zone centroid.
    zones = pd.read_sql(
        """
            SELECT z.zone_id, (
                SELECT n.node_id FROM nodes n
                WHERE n.kind = 'junction'
                ORDER BY n.geom <-> z.centroid LIMIT 1
            ) AS node_id
            FROM zones z WHERE z.rooms_total > 0
            """,
        conn,
    )
    venues = pd.read_sql(
        "SELECT node_id FROM nodes WHERE kind IN ('pandal','immersion')",
        conn,
    )
    log.info("  %d zones x %d venues", len(zones), len(venues))

    targets = [idx[n] for n in venues["node_id"] if n in idx]
    target_ids = [n for n in venues["node_id"] if n in idx]

    conn.execute(text("TRUNCATE corridor_paths"))
    written = 0

    for _, z in zones.iterrows():
        src = idx.get(z["node_id"])
        if src is None:
            continue
        paths = g.get_shortest_paths(
            src, to=targets, weights="w", output="epath"
        )
        for tgt_node, epath in zip(target_ids, paths):
            if not epath:
                continue
            # Which corridors does this route touch, and how much of it
            # is spent on each? Share is by number of edges, which is a
            # reasonable proxy for exposure to that corridor.
            counts: dict[str, int] = defaultdict(int)
            for e in epath:
                cid = corr_of.get(g.es[e]["eid"])
                if cid:
                    counts[cid] += 1
            total = sum(counts.values())
            if not total:
                continue
            rows = [
                {"z": z["zone_id"], "n": int(tgt_node), "c": cid,
                 "s": round(n / total, 4)}
                for cid, n in counts.items() if n / total >= 0.02
            ]
            if rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO corridor_paths
                            (zone_id, node_id, corridor_id, share)
                        VALUES (:z, :n, :c, :s)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    rows,
                )
                written += len(rows)

    log.info("corridor_paths: %d rows", written)


def main():
    eng = engine()
    with eng.begin() as conn:
        for stmt in DDL.split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        log.info("schema ready")

    with eng.begin() as conn:
        build_corridors(conn)

    with eng.begin() as conn:
        build_paths(conn)

    with eng.connect() as conn:
        for kind, n, cap in conn.execute(
            text(
                """
                SELECT kind, count(*), round(avg(capacity_ppm))
                FROM corridors GROUP BY kind ORDER BY kind
                """
            )
        ):
            log.info("  %-10s %3d corridors, avg %s ppm", kind, n, cap)

    log.info("done - corridors built")


if __name__ == "__main__":
    main()
