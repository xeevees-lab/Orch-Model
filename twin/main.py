"""
twin/main.py - the forward-looking half of the platform.

Everything up to now answers "what is happening". This answers
"what is about to happen, and how long have we got".

The model is macroscopic on purpose. A full agent simulation of Mumbai
would not finish between ticks, and would not be more useful: what a
command centre needs is a queue trajectory per venue and a saturation
time per zone, not the path of individual pilgrims.

Per venue, per 5-minute step across a 2-hour horizon:

    admitted(t) = min(queue(t) + arrivals(t), service_rate * step)
    queue(t+1)  = queue(t) + arrivals(t) - admitted(t)
    inside(t+1) = inside(t) + admitted(t) - inside(t) * step / dwell

    arrivals(t) = ewma(observed inflow) * profile(t) / profile(now)

Two headline outputs:

    PRESSURE INDEX      how bad it is, on a comparable scale where 1.0
                        is the line. max(queue_hours/limit, fill/limit),
                        so whichever constraint binds first drives it.
    TIME TO SATURATION  minutes until it crosses 1.0, or 0 if already past.

TTS is what makes the system prescriptive rather than descriptive.
"Lalbaug is at 82%" is a dashboard. "Lalbaug saturates in 18 minutes"
is a decision.

The twin never reads the simulator's internals - only what the ingestion
worker recorded, noise and gaps included. It learns its own hour-of-day
arrival profile from that telemetry rather than being handed the shape.
That is what makes forecast_error_mape a real number.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    uvicorn twin.main:app --port 8200
"""

from __future__ import annotations

import sys
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("twin")

HORIZON_MIN = 120
STEP_MIN = 5
TICK_SECONDS = 8
EWMA_WINDOW_MIN = 30
DWELL_MIN = 25.0

QUEUE_HOURS_LIMIT = 2.0     # a queue longer than this counts as saturated
FILL_LIMIT = 0.90           # ...or a venue this full inside
ZONE_OCCUPANCY_LIMIT = 0.92

app = FastAPI(title="Mega-Event Digital Twin", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = create_engine(S.PG_DSN, future=True, pool_pre_ping=True)


class Twin:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.nodes: dict[int, dict] = {}
        self.zones: dict[str, dict] = {}
        self.hour_profile: dict[int, float] = {}
        self.forecasts: dict[int, list[dict]] = {}
        self.zone_forecasts: dict[str, dict] = {}
        self.board: list[dict] = []
        self.last_run: str | None = None
        self.runs = 0
        self.error_mape: float | None = None
        self.scored = 0
        # node_id -> inflow rate we predicted for the step just gone.
        # Same units as the observation it is scored against.
        self._predicted_inflow: dict[int, float] = {}
        self.corridors: dict[str, dict] = {}
        self.paths: dict[int, list] = {}
        self.zone_share: dict[str, float] = {}
        self.corridor_board: list[dict] = []
        self.board_rate: dict[int, float] = {}
        self._corridor_prev: dict[str, float] = {}

    # ---------------------------------------------------------- setup

    def load_reference(self):
        with db.connect() as conn:
            for r in conn.execute(
                text(
                    """
                    SELECT node_id, name, kind,
                           COALESCE(capacity, 0) AS capacity,
                           COALESCE(service_rate, 0) AS service_rate
                    FROM nodes
                    WHERE kind IN ('pandal','immersion','holding')
                    """
                )
            ).mappings():
                self.nodes[r["node_id"]] = dict(r)
            for r in conn.execute(
                text(
                    "SELECT zone_id, rooms_total, median_price "
                    "FROM zones WHERE rooms_total > 0"
                )
            ).mappings():
                self.zones[r["zone_id"]] = dict(r)
        log.info("reference: %d venues, %d zones", len(self.nodes), len(self.zones))

    def learn_hour_profile(self):
        """Hour-of-day arrival shape, learned from observed telemetry."""
        with db.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT EXTRACT(hour FROM ts)::int AS h,
                           AVG(inflow_ppm) AS rate
                    FROM node_state
                    WHERE is_forecast = false AND inflow_ppm IS NOT NULL
                    GROUP BY 1 ORDER BY 1
                    """
                )
            ).all()
        if not rows:
            self.hour_profile = {h: 1.0 for h in range(24)}
            return
        vals = {int(h): float(r or 0) for h, r in rows}
        mean = (sum(vals.values()) / max(len(vals), 1)) or 1.0
        self.hour_profile = {
            h: max(vals.get(h, mean) / mean, 0.05) for h in range(24)
        }

    # ---------------------------------------------------------- inputs

    def recent(self, now: datetime) -> tuple[datetime | None, dict[int, dict]]:
        with db.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT node_id,
                           AVG(inflow_ppm)  AS inflow,
                           AVG(outflow_ppm) AS outflow
                    FROM node_state
                    WHERE is_forecast = false AND ts > :cut
                    GROUP BY node_id
                    """
                ),
                {"cut": now - timedelta(minutes=EWMA_WINDOW_MIN)},
            ).mappings().all()
        return now, {r["node_id"]: dict(r) for r in rows}

    async def live(self, node_id: int) -> dict:
        return await self.redis.hgetall(f"node:{node_id}") or {}


    # ---------------------------------------------------------- corridors

    def load_corridors(self):
        """Corridor definitions and the zone -> venue lookup, read once."""
        with db.connect() as conn:
            for r in conn.execute(
                text(
                    """
                    SELECT corridor_id, name, mode, kind, capacity_ppm
                    FROM corridors
                    """
                )
            ).mappings():
                self.corridors[r["corridor_id"]] = dict(r)

            for r in conn.execute(
                text(
                    "SELECT zone_id, node_id, corridor_id, share "
                    "FROM corridor_paths"
                )
            ).mappings():
                self.paths.setdefault(r["node_id"], []).append(
                    (r["zone_id"], r["corridor_id"], float(r["share"]))
                )

            # How demand for a venue splits across origin zones, by room
            # supply. People come from where the beds are.
            rows = conn.execute(
                text(
                    "SELECT zone_id, rooms_total FROM zones "
                    "WHERE rooms_total > 0"
                )
            ).all()
        total = sum(r[1] for r in rows) or 1
        self.zone_share = {r[0]: r[1] / total for r in rows}
        log.info(
            "corridors: %d defined, %d venues with paths",
            len(self.corridors), len(self.paths),
        )

    async def project_corridors(self, now):
        """Push forecast venue demand back onto the network.

        A corridor's load is the sum, across every venue it serves, of
        that venue's inbound rate multiplied by the share of journeys
        that use this corridor. Crude compared with a full assignment,
        but it is the right order of magnitude and it runs in
        milliseconds.
        """
        if not self.corridors:
            return

        load: dict[str, float] = {}
        for nid, series in self.forecasts.items():
            if not series:
                continue
            # Inbound rate over the next half hour, people per minute.
            window = series[: max(1, 30 // STEP_MIN)]
            arrivals = sum(
                max(s["queue"] - (series[0]["queue"] if i else 0), 0)
                for i, s in enumerate(window)
            )
            rate = max(
                float(self.board_rate.get(nid, 0)),
                arrivals / max(len(window) * STEP_MIN, 1),
            )
            if rate <= 0:
                continue
            for zone_id, cid, share in self.paths.get(nid, []):
                zshare = self.zone_share.get(zone_id, 0)
                if zshare <= 0:
                    continue
                load[cid] = load.get(cid, 0.0) + rate * zshare * share

        rows, board = [], []
        for cid, meta in self.corridors.items():
            flow = load.get(cid, 0.0)
            cap = float(meta["capacity_ppm"]) or 1.0
            pi = flow / cap

            # How long until it saturates, if the trend continues.
            prev = self._corridor_prev.get(cid)
            tts = None
            if pi >= 1.0:
                tts = 0
            elif prev is not None and flow > prev:
                growth = (flow - prev) / max(TICK_SECONDS / 60.0, 0.01)
                if growth > 0:
                    tts = round((cap - flow) / growth)
            self._corridor_prev[cid] = flow

            board.append({
                "corridor_id": cid,
                "name": meta["name"],
                "mode": meta["mode"],
                "kind": meta["kind"],
                "flow_ppm": round(flow, 1),
                "capacity_ppm": round(cap, 1),
                "pressure_index": round(pi, 3),
                "tts_min": tts,
                "band": ("critical" if pi >= 1.0
                         else "warning" if pi >= 0.7 else "ok"),
            })
            rows.append({
                "ts": now, "corridor_id": cid, "flow": round(flow, 2),
                "pi": round(pi, 3), "tts": tts,
            })

        board.sort(key=lambda r: (
            r["tts_min"] if r["tts_min"] is not None else 10_000,
            -r["pressure_index"],
        ))
        self.corridor_board = board

        if rows:
            with db.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO corridor_state
                            (ts, corridor_id, flow_ppm, pressure_index,
                             tts_min, is_forecast)
                        VALUES (:ts, :corridor_id, :flow, :pi, :tts, false)
                        """
                    ),
                    rows,
                )

    # ---------------------------------------------------------- scoring

    def _score_previous(self, window: dict):
        """Score the last prediction against what actually arrived.

        Both sides are inflow in people per minute. Comparing a queue
        against a rate, as an earlier version did, produces a number in
        the hundreds of thousands and means nothing.
        """
        if not self._predicted_inflow:
            return
        errs = []
        for nid, predicted in self._predicted_inflow.items():
            actual = window.get(nid, {}).get("inflow")
            if actual is None:
                continue
            actual = float(actual)
            if actual < 1.0 and predicted < 1.0:
                continue                      # both idle, nothing to score
            errs.append(abs(predicted - actual))
        if errs:
            mape = sum(errs) / len(errs)
            # Smooth it so one bad step does not dominate the headline.
            self.error_mape = (
                round(mape, 1) if self.error_mape is None
                else round(0.7 * self.error_mape + 0.3 * mape, 1)
            )
            self.scored += len(errs)

    # ---------------------------------------------------------- project

    async def project(self):
        raw = await self.redis.get('sys:last_observation')
        if not raw:
            return
        now = datetime.fromisoformat(raw)
        _, window = self.recent(now)

        self._score_previous(window)
        predicted_next: dict[int, float] = {}
        board = []

        for nid, meta in self.nodes.items():
            live = await self.live(nid)
            if not live:
                continue

            queue = float(live.get("queue") or 0)
            # The venue reports how many are inside. Deriving it as
            # occupancy minus queue subtracts two noisy numbers and can
            # exceed capacity, which is impossible.
            inside = float(live.get("inside") or 0)
            cap = float(meta["capacity"]) or 1.0
            rate = float(meta["service_rate"]) or 1.0
            inside = min(inside, cap)

            base_inflow = float(window.get(nid, {}).get("inflow") or 0)
            cur_shape = self.hour_profile.get(now.hour, 1.0) or 1.0

            series, tts = [], None
            q, ins = queue, inside
            first_rate = None

            for i in range(1, HORIZON_MIN // STEP_MIN + 1):
                t = now + timedelta(minutes=i * STEP_MIN)
                shape = self.hour_profile.get(t.hour, 1.0)
                rate_now = base_inflow * (shape / cur_shape)
                if first_rate is None:
                    first_rate = rate_now
                arrivals = rate_now * STEP_MIN
                served = rate * STEP_MIN

                admitted = min(q + arrivals, served)
                q = max(0.0, q + arrivals - admitted)
                ins = max(0.0, min(cap, ins + admitted
                                   - ins * (STEP_MIN / DWELL_MIN)))

                queue_h = q / (rate * 60.0)
                fill = ins / cap
                pi = max(queue_h / QUEUE_HOURS_LIMIT, fill / FILL_LIMIT)
                series.append({
                    "ts": t.isoformat(),
                    "queue": round(q),
                    "inside": round(ins),
                    "queue_hours": round(queue_h, 2),
                    "fill": round(fill, 3),
                    "pressure_index": round(pi, 3),
                })
                if tts is None and pi >= 1.0:
                    tts = i * STEP_MIN

            predicted_next[nid] = first_rate or 0.0
            self.board_rate[nid] = base_inflow
            self.forecasts[nid] = series

            queue_h_now = queue / (rate * 60.0)
            fill_now = inside / cap
            pi_now = max(queue_h_now / QUEUE_HOURS_LIMIT, fill_now / FILL_LIMIT)
            # Already over the line means zero minutes left, not five.
            if pi_now >= 1.0:
                tts = 0
            peak = max((s["pressure_index"] for s in series), default=pi_now)

            board.append({
                "node_id": nid,
                "name": meta["name"],
                "kind": meta["kind"],
                "queue": round(queue),
                "inside": round(inside),
                "queue_hours": round(queue_h_now, 2),
                "fill": round(fill_now, 3),
                "pressure_index": round(pi_now, 3),
                "peak_pressure_index": round(peak, 3),
                "tts_min": tts,
                "binding": ("throughput"
                            if queue_h_now / QUEUE_HOURS_LIMIT
                            >= fill_now / FILL_LIMIT else "space"),
                "inflow_ppm": round(base_inflow, 1),
                "service_ppm": round(rate, 1),
                "band": self._band(tts, pi_now),
            })

        self._predicted_inflow = predicted_next
        await self.project_zones(now)
        await self.project_corridors(now)

        # Shortest fuse first; among equals, worst pressure first.
        board.sort(key=lambda r: (
            r["tts_min"] if r["tts_min"] is not None else 10_000,
            -r["pressure_index"],
        ))
        self.board = board
        self.last_run = now.isoformat()
        self.runs += 1
        self._persist(now)

    @staticmethod
    def _band(tts, pi) -> str:
        if pi >= 1.0:
            return "critical"
        if tts is not None and tts <= 30:
            return "critical"
        if tts is not None and tts <= 90:
            return "warning"
        if pi >= 0.7:
            return "warning"
        return "ok"

    async def project_zones(self, now: datetime):
        out = {}
        for zid, meta in self.zones.items():
            h = await self.redis.hgetall(f"zone:{zid}")
            if not h:
                continue
            total = float(h.get("rooms_total") or meta["rooms_total"] or 1)
            occupied = float(h.get("rooms_occupied") or 0)
            occ = occupied / total if total else 0.0

            with db.connect() as conn:
                prev = conn.execute(
                    text(
                        """
                        SELECT rooms_occupied FROM zone_state
                        WHERE zone_id = :z AND is_forecast = false
                          AND ts <= :cut
                        ORDER BY ts DESC LIMIT 1
                        """
                    ),
                    {"z": zid, "cut": now - timedelta(minutes=EWMA_WINDOW_MIN)},
                ).scalar()
            rate = (((occupied - float(prev)) / EWMA_WINDOW_MIN)
                    if prev is not None else 0.0)

            tts = None
            if occ >= ZONE_OCCUPANCY_LIMIT:
                tts = 0
            elif rate > 0:
                tts = round((total * ZONE_OCCUPANCY_LIMIT - occupied) / rate)

            out[zid] = {
                "zone_id": zid,
                "occupancy": round(occ, 3),
                "rooms_available": int(float(h.get("rooms_available") or 0)),
                "price_inr": float(h.get("price_inr") or 0),
                "fill_rate_ppm": round(rate, 2),
                "tts_min": tts,
                "pressure_index": round(occ / ZONE_OCCUPANCY_LIMIT, 3),
            }
        self.zone_forecasts = out

    def _persist(self, now: datetime):
        rows = [
            {"ts": s["ts"], "node_id": nid,
             "occupancy": s["queue"] + s["inside"],
             "pi": s["pressure_index"], "tts": None}
            for nid, series in self.forecasts.items() for s in series
        ]
        if not rows:
            return
        with db.begin() as conn:
            conn.execute(
                text("DELETE FROM node_state "
                     "WHERE is_forecast = true AND ts > :t"),
                {"t": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO node_state
                        (ts, node_id, occupancy, pressure_index, tts_min,
                         is_forecast)
                    VALUES (:ts, :node_id, :occupancy, :pi, :tts, true)
                    """
                ),
                rows,
            )


twin = Twin()


@app.on_event("startup")
async def startup():
    twin.redis = aioredis.from_url(S.REDIS_URL, decode_responses=True)
    twin.load_reference()
    twin.learn_hour_profile()
    twin.load_corridors()

    async def loop():
        while True:
            try:
                await twin.project()
            except Exception as e:
                log.warning("projection failed: %s", e)
            if twin.runs % 20 == 0:
                twin.learn_hour_profile()
            await asyncio.sleep(TICK_SECONDS)

    asyncio.create_task(loop())


@app.get("/health")
def health():
    return {
        "runs": twin.runs,
        "last_run": twin.last_run,
        "venues": len(twin.nodes),
        "forecast_mae_ppm": twin.error_mape,
        "samples_scored": twin.scored,
    }


@app.get("/pressure")
def pressure(band: str | None = Query(None)):
    """The pressure board. Shortest fuse first."""
    rows = twin.board
    return [r for r in rows if r["band"] == band] if band else rows


@app.get("/pressure/zones")
def zone_pressure():
    return sorted(
        twin.zone_forecasts.values(),
        key=lambda r: (r["tts_min"] if r["tts_min"] is not None else 10_000),
    )


@app.get("/pressure/corridors")
def corridor_pressure(band: str | None = Query(None), mode: str | None = None):
    """Which routes into the event are filling up."""
    rows = twin.corridor_board
    if band:
        rows = [r for r in rows if r["band"] == band]
    if mode:
        rows = [r for r in rows if r["mode"] == mode]
    return rows


@app.get("/forecast/{node_id}")
def forecast(node_id: int):
    if node_id not in twin.forecasts:
        raise HTTPException(404, "no forecast for that node yet")
    return {
        "node_id": node_id,
        "name": twin.nodes.get(node_id, {}).get("name"),
        "horizon_min": HORIZON_MIN,
        "step_min": STEP_MIN,
        "series": twin.forecasts[node_id],
    }


@app.get("/profile")
def profile():
    """The hour-of-day arrival shape the twin has learned so far."""
    return twin.hour_profile
