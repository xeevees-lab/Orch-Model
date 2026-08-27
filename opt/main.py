"""
opt/main.py - the half that decides.

Everything upstream detects pressure. This proposes what to do about it,
predicts the effect, and - when an operator approves - applies it.

Three solvers, all OR-Tools min-cost flow. Assignment under capacity is
exactly what min-cost flow is for, and it runs in milliseconds at this
scale, which matters when the console recomputes every few seconds.

  1. QUEUE DIVERSION
     Sources are venues past the line, supplying their excess queue.
     Sinks are peer venues with headroom. Arc cost is travel time
     between them. The solve answers: move how many people, where.

  2. ZONE ASSIGNMENT
     Arriving demand distributed across accommodation zones. Cost blends
     price, travel time to the venues under pressure, and how full the
     zone already is. A per-zone cap stops the solver dumping everyone
     into the cheapest zone - that would just recreate the problem
     somewhere else, which is PS issue #2.

  3. INCENTIVE SIZING
     A diversion of N people needs an offer pushed to N / uptake people.
     Uptake is not known in advance, so it is estimated and corrected
     from what actually happened. Without that damping the system
     overshoots and creates the bottleneck it was avoiding.

Every proposal is written to the actions ledger with its predicted
delta. When it is approved and applied, the measured delta goes back
into the same row. That table is the audit trail, the evaluation
dataset and the after-action report.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    uvicorn opt.main:app --port 8300
"""

from __future__ import annotations

import sys
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ortools.graph.python import min_cost_flow
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("opt")

TWIN = "http://localhost:8200"
SIM = "http://localhost:8100"
TICK_SECONDS = 10

QUEUE_HOURS_TARGET = 2.0     # what we are trying to pull venues back to
MIN_DIVERSION = 500          # do not bother proposing smaller moves
MAX_DETOUR_MIN = 45          # nobody accepts a diversion further than this

# Zone assignment cost weights. They must sum to something sensible;
# relative size is what matters.
W_PRICE = 0.30
W_TRAVEL = 0.45
W_CROWDING = 0.25
ZONE_FAIR_CAP = 0.28         # no zone absorbs more than this share

app = FastAPI(title="Mega-Event Optimizer", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = create_engine(S.PG_DSN, future=True, pool_pre_ping=True)


class Optimizer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.venue_dist: dict[tuple[int, int], float] = {}
        self.zone_dist: dict[tuple[str, int], float] = {}
        self.proposals: list[dict] = []
        self.runs = 0
        self.last_run: str | None = None
        # Damped estimate of how many people accept an offer.
        self.uptake = 0.22
        self.uptake_history: list[dict] = []

    # ---------------------------------------------------------- geometry

    def load_distances(self):
        """Travel minutes between venues, and from every zone to every venue.

        Straight-line distance with a detour factor. Honest approximation:
        real road routing comes from OSRM once it is preprocessed, and
        swapping it in changes only this method.
        """
        with db.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT a.node_id AS a, b.node_id AS b,
                           ST_Distance(a.geom::geography,
                                       b.geom::geography) / 1000.0 AS km
                    FROM nodes a, nodes b
                    WHERE a.kind IN ('pandal','immersion','holding')
                      AND b.kind IN ('pandal','immersion','holding')
                      AND a.node_id <> b.node_id
                    """
                )
            ).all()
            for a, b, km in rows:
                self.venue_dist[(a, b)] = (float(km) * 1.35) / 18.0 * 60.0

            rows = conn.execute(
                text(
                    """
                    SELECT z.zone_id, n.node_id,
                           ST_Distance(z.centroid::geography,
                                       n.geom::geography) / 1000.0 AS km
                    FROM zones z, nodes n
                    WHERE n.kind IN ('pandal','immersion')
                      AND z.rooms_total > 0
                    """
                )
            ).all()
            for zid, nid, km in rows:
                self.zone_dist[(zid, nid)] = (float(km) * 1.35) / 22.0 * 60.0
        log.info("distances: %d venue pairs, %d zone-venue pairs",
                 len(self.venue_dist), len(self.zone_dist))

    # ---------------------------------------------------------- inputs

    async def board(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{TWIN}/pressure")
            r.raise_for_status()
            return r.json()

    async def zones(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{TWIN}/pressure/zones")
            r.raise_for_status()
            return r.json()

    # ---------------------------------------------------------- solver 1

    def solve_diversion(self, board: list[dict]) -> list[dict]:
        """Move excess queue from saturated venues to peers with headroom."""
        by_kind: dict[str, list[dict]] = {}
        for row in board:
            by_kind.setdefault(row["kind"], []).append(row)

        out = []
        for kind, rows in by_kind.items():
            if len(rows) < 2:
                continue

            # Balance queues against each other, not against an
            # absolute target. At peak every venue is over target, so a
            # target-based split finds no sinks at all - even though
            # moving people from a 22h queue to a 6h queue is a real win.
            total_q = sum(r["queue"] for r in rows)
            total_rate = sum(max(r["service_ppm"], 1) for r in rows)
            level_h = total_q / (total_rate * 60.0)

            sources, sinks = [], []
            for r in rows:
                target_queue = level_h * max(r["service_ppm"], 1) * 60.0
                excess = r["queue"] - target_queue
                if excess > MIN_DIVERSION:
                    sources.append((r, int(excess)))
                elif excess < -MIN_DIVERSION:
                    sinks.append((r, int(-excess)))

            if not sources or not sinks:
                continue

            mcf = min_cost_flow.SimpleMinCostFlow()
            n_src = len(sources)
            arcs = []
            for i, (src, supply) in enumerate(sources):
                for j, (dst, cap) in enumerate(sinks):
                    mins = self.venue_dist.get(
                        (src["node_id"], dst["node_id"]), 999)
                    if mins > MAX_DETOUR_MIN:
                        continue
                    arcs.append((i, n_src + j, int(mins * 10), min(supply, cap),
                                 src, dst, mins))
            if not arcs:
                continue

            for u, v, cost, cap, *_ in arcs:
                mcf.add_arc_with_capacity_and_unit_cost(u, v, cap, cost)

            # Only nodes an arc actually touches may carry supply.
            # A sink beyond MAX_DETOUR_MIN has no incoming arc, so giving
            # it demand makes the whole problem infeasible and the solver
            # returns nothing at all - for every venue, not just that one.
            live_src = {a[0] for a in arcs}
            live_snk = {a[1] for a in arcs}
            total_supply = sum(sup for i, (_, sup) in enumerate(sources)
                               if i in live_src)
            total_cap = sum(cap for j, (_, cap) in enumerate(sinks)
                            if n_src + j in live_snk)
            movable = min(total_supply, total_cap)
            if movable < MIN_DIVERSION:
                continue

            assigned = 0
            for i, (_, supply) in enumerate(sources):
                if i not in live_src:
                    continue
                take = int(supply * movable / max(total_supply, 1))
                mcf.set_node_supply(i, take)
                assigned += take

            snk_ids = [j for j in range(len(sinks)) if n_src + j in live_snk]
            drained = 0
            for k, j in enumerate(snk_ids):
                cap = sinks[j][1]
                give = (assigned - drained if k == len(snk_ids) - 1
                        else int(cap * assigned / max(total_cap, 1)))
                mcf.set_node_supply(n_src + j, -give)
                drained += give

            if mcf.solve() != mcf.OPTIMAL:
                log.info("diversion for %s: no optimal solution", kind)
                continue

            for idx in range(mcf.num_arcs()):
                flow = mcf.flow(idx)
                if flow < MIN_DIVERSION:
                    continue
                _, _, _, _, src, dst, mins = arcs[idx]
                out.append({
                    "kind": "divert",
                    "from_node": src["node_id"],
                    "from_name": src["name"],
                    "to_node": dst["node_id"],
                    "to_name": dst["name"],
                    "people": int(flow),
                    "detour_min": round(mins),
                    "reason": (
                        f"{src['name']} is {src['queue_hours']}h deep and "
                        f"{src['binding']}-bound; {dst['name']} has headroom "
                        f"{round(mins)} min away"
                    ),
                    "predicted": self._predict_diversion(src, dst, int(flow)),
                })
        return out

    @staticmethod
    def _predict_diversion(src: dict, dst: dict, people: int) -> dict:
        """What the twin says happens if this move is made."""
        src_rate = max(src["service_ppm"], 1)
        dst_rate = max(dst["service_ppm"], 1)
        q_before = src["queue"] / (src_rate * 60.0)
        q_after = max(src["queue"] - people, 0) / (src_rate * 60.0)
        d_before = dst["queue"] / (dst_rate * 60.0)
        d_after = (dst["queue"] + people) / (dst_rate * 60.0)
        return {
            "source_queue_hours": [round(q_before, 2), round(q_after, 2)],
            "dest_queue_hours": [round(d_before, 2), round(d_after, 2)],
            "hours_saved_per_person": round(q_before - d_after, 2),
            "people_spared_from_balking": people,
        }

    # ---------------------------------------------------------- solver 2

    def solve_zone_assignment(self, zones: list[dict], board: list[dict],
                              demand: int) -> list[dict]:
        """Spread arriving demand across zones by price, travel and crowding."""
        hot = [r for r in board if r["band"] in ("critical", "warning")]
        if not hot:
            hot = board[:3]
        hot_ids = [r["node_id"] for r in hot]
        if not zones or demand < 100:
            return []

        avail = [z for z in zones if z["rooms_available"] > 20]
        if not avail:
            return []

        prices = [z["price_inr"] for z in avail] or [1]
        p_lo, p_hi = min(prices), max(prices)
        travels = []
        for z in avail:
            ts = [self.zone_dist.get((z["zone_id"], n), 90) for n in hot_ids]
            travels.append(sum(ts) / len(ts))
        t_lo, t_hi = min(travels), max(travels)

        def scale(v, lo, hi):
            return 0.0 if hi <= lo else (v - lo) / (hi - lo)

        mcf = min_cost_flow.SimpleMinCostFlow()
        SRC = 0
        arcs = []
        for j, (z, travel) in enumerate(zip(avail, travels)):
            cost = (
                W_PRICE * scale(z["price_inr"], p_lo, p_hi)
                + W_TRAVEL * scale(travel, t_lo, t_hi)
                + W_CROWDING * z["pressure_index"]
            )
            # Rooms hold more than one person; assume a party of 2.2.
            cap = int(min(z["rooms_available"] * 2.2,
                          demand * ZONE_FAIR_CAP))
            if cap < 50:
                continue
            arcs.append((z, travel, cap, int(cost * 1000)))
            mcf.add_arc_with_capacity_and_unit_cost(
                SRC, j + 1, cap, int(cost * 1000))

        if not arcs:
            return []

        placeable = min(demand, sum(a[2] for a in arcs))
        mcf.set_node_supply(SRC, placeable)
        # Distribute the sink demand proportionally to capacity.
        total_cap = sum(a[2] for a in arcs)
        for j, (_, _, cap, _) in enumerate(arcs):
            mcf.set_node_supply(j + 1, -int(placeable * cap / total_cap))

        if mcf.solve() != mcf.OPTIMAL:
            return []

        out = []
        for idx in range(mcf.num_arcs()):
            flow = mcf.flow(idx)
            if flow < 100:
                continue
            z, travel, _, _ = arcs[idx]
            out.append({
                "kind": "zone_shift",
                "zone_id": z["zone_id"],
                "people": int(flow),
                "price_inr": z["price_inr"],
                "travel_min": round(travel),
                "rooms_available": z["rooms_available"],
                "reason": (
                    f"{z['zone_id']} has {z['rooms_available']} rooms at "
                    f"Rs {int(z['price_inr'])}, {round(travel)} min from the "
                    f"venues under pressure"
                ),
            })
        out.sort(key=lambda r: -r["people"])
        return out[:8]

    # ---------------------------------------------------------- solver 3

    def size_incentive(self, people: int) -> dict:
        """How many people to make an offer to, given estimated uptake."""
        push_to = int(people / max(self.uptake, 0.05))
        return {
            "kind": "incentive",
            "divert_target": people,
            "push_to": push_to,
            "assumed_uptake": round(self.uptake, 3),
            "reason": (
                f"reaching {people} diversions needs an offer to about "
                f"{push_to} people at the current {round(self.uptake * 100)}% "
                f"uptake"
            ),
        }

    def record_uptake(self, pushed: int, accepted: int):
        """Correct the uptake estimate from what actually happened.

        A proportional step, deliberately damped. Snapping straight to
        the observed value makes the controller oscillate: overshoot,
        overcorrect, undershoot, repeat.
        """
        if pushed <= 0:
            return
        observed = accepted / pushed
        self.uptake = round(0.7 * self.uptake + 0.3 * observed, 4)
        self.uptake_history.append({
            "ts": datetime.utcnow().isoformat(),
            "pushed": pushed, "accepted": accepted,
            "observed": round(observed, 3), "estimate": self.uptake,
        })
        self.uptake_history = self.uptake_history[-40:]

    # ---------------------------------------------------------- ledger

    def persist(self, proposals: list[dict]):
        if not proposals:
            return
        with db.begin() as conn:
            # Retire only stale proposals. Deleting the whole set
            # every cycle re-issues new ids every 10 seconds, so an
            # operator reading a recommendation cannot approve it -
            # the row is gone by the time they click.
            conn.execute(
                text(
                    """
                    UPDATE actions SET status = 'superseded'
                    WHERE status = 'proposed'
                      AND created_at < now() - INTERVAL '90 seconds'
                    """
                )
            )
            fresh = conn.execute(
                text(
                    "SELECT count(*) FROM actions WHERE status = 'proposed'"
                )
            ).scalar()
            if fresh:
                return
            for p in proposals:
                conn.execute(
                    text(
                        """
                        INSERT INTO actions
                            (kind, target_id, payload, trigger_reason,
                             predicted_delta, status)
                        VALUES (:kind, :target, :payload, :reason,
                                :predicted, 'proposed')
                        """
                    ),
                    {
                        "kind": p["kind"],
                        "target": str(p.get("from_node")
                                      or p.get("zone_id") or ""),
                        "payload": json.dumps(p),
                        "reason": p.get("reason"),
                        "predicted": json.dumps(p.get("predicted", {})),
                    },
                )

    # ---------------------------------------------------------- cycle

    async def run_once(self):
        board = await self.board()
        if not board:
            return
        zones = await self.zones()

        diversions = self.solve_diversion(board)
        total_divert = sum(d["people"] for d in diversions)

        # Demand needing a room, taken from what is currently in flight.
        arriving = sum(max(r["queue"], 0) for r in board) * 0.18
        zone_shifts = self.solve_zone_assignment(zones, board, int(arriving))

        proposals = diversions + zone_shifts
        if total_divert > 0:
            proposals.append(self.size_incentive(total_divert))

        self.proposals = proposals
        self.persist(proposals)
        self.runs += 1
        self.last_run = datetime.utcnow().isoformat()


opt = Optimizer()


@app.on_event("startup")
async def startup():
    opt.redis = aioredis.from_url(S.REDIS_URL, decode_responses=True)
    opt.load_distances()

    async def loop():
        while True:
            try:
                await opt.run_once()
            except Exception as e:
                log.warning("optimize failed: %s", e)
            await asyncio.sleep(TICK_SECONDS)

    asyncio.create_task(loop())


@app.get("/health")
def health():
    return {
        "runs": opt.runs,
        "last_run": opt.last_run,
        "proposals": len(opt.proposals),
        "uptake_estimate": opt.uptake,
    }


@app.get("/proposals")
def proposals(kind: str | None = None):
    rows = opt.proposals
    return [r for r in rows if r["kind"] == kind] if kind else rows


@app.get("/actions")
def actions(status: str | None = None, limit: int = 50):
    sql = "SELECT * FROM actions {w} ORDER BY created_at DESC LIMIT :lim"
    where = "WHERE status = :s" if status else ""
    params = {"lim": limit}
    if status:
        params["s"] = status
    with db.connect() as conn:
        rows = conn.execute(text(sql.format(w=where)), params).mappings().all()
    return [dict(r) for r in rows]


@app.post("/actions/{action_id}/approve")
async def approve(action_id: int):
    """Approve an action and apply it to the world.

    This is the point of the whole system: the recommendation becomes a
    change, and the measured effect comes back into the same ledger row.
    """
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM actions WHERE action_id = :i"),
            {"i": action_id},
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "no such action")
    if row["status"] != "proposed":
        raise HTTPException(400, f"action is already {row['status']}")

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    applied = {"applied": False}
    if payload.get("kind") == "divert":
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                f"{SIM}/divert",
                params={
                    "from_node": payload["from_node"],
                    "to_node": payload["to_node"],
                    "people": payload["people"],
                },
            )
            applied = r.json() if r.status_code == 200 else {
                "applied": False, "error": r.text}
        if applied.get("moved"):
            opt.record_uptake(payload["people"], applied["moved"])

    with db.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE actions
                SET status = 'executed', decided_at = now(),
                    measured_delta = :m
                WHERE action_id = :i
                """
            ),
            {"i": action_id, "m": json.dumps(applied)},
        )
    return {"action_id": action_id, "status": "executed", "result": applied}


@app.post("/actions/{action_id}/dismiss")
def dismiss(action_id: int):
    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE actions SET status='dismissed', decided_at=now() "
                "WHERE action_id = :i AND status = 'proposed'"
            ),
            {"i": action_id},
        )
    return {"action_id": action_id, "status": "dismissed"}


@app.get("/uptake")
def uptake():
    """The incentive controller's learning curve."""
    return {"estimate": opt.uptake, "history": opt.uptake_history}
