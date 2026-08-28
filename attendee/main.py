"""
attendee/main.py - the visitor-facing API.

Deliberately a separate service with a separate vocabulary. An attendee
never sees a pressure index, a time-to-saturation, an action ledger or
anything else that belongs to whoever is running the event. They see
words: "very busy", "about 40 minutes", "rooms available 20 minutes
away".

That translation happens here, once, so the client cannot leak operator
concepts by accident. Nothing in this file returns an internal metric
unrendered.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    uvicorn attendee.main:app --reload --port 8400 --host 0.0.0.0
"""

from __future__ import annotations

import sys
import random
from pathlib import Path
from datetime import datetime, timedelta

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402

TWIN = "http://localhost:8200"
SIM = "http://localhost:8100"

app = FastAPI(title="Festival Companion API", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)

db = create_engine(S.PG_DSN, future=True, pool_pre_ping=True)
rds: aioredis.Redis | None = None


@app.on_event("startup")
async def startup():
    global rds
    rds = aioredis.from_url(S.REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------- language

def crowd_words(queue_hours: float | None, fill: float | None) -> tuple[str, str]:
    """Turn two operational numbers into one word a visitor understands."""
    q = queue_hours or 0
    f = fill or 0
    if q >= 3 or f >= 1.0:
        return "packed", "Very long wait right now"
    if q >= 1.5 or f >= 0.85:
        return "busy", "Building up quickly"
    if q >= 0.5 or f >= 0.6:
        return "moderate", "Steady, moving well"
    return "clear", "Walk straight in"


def wait_words(minutes: float) -> str:
    m = int(round(minutes))
    if m < 5:
        return "No queue"
    if m < 60:
        return f"About {m} min"
    hours = m / 60
    if hours < 2:
        return f"About {hours:.1f} hours"
    return f"Over {int(hours)} hours"


def travel_words(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m} min away" if m < 60 else f"{m // 60}h {m % 60}m away"


# ---------------------------------------------------------------- models

class TripSetup(BaseModel):
    party_size: int = 2
    budget: str = "mid"          # low | mid | high
    arriving_from: str | None = None
    needs_step_free: bool = False


# ---------------------------------------------------------------- helpers

async def _venue_state() -> dict[int, dict]:
    """Live venue state, read from the cache the operator side also writes.

    Only the fields a visitor is allowed to see leave this function.
    """
    out = {}
    with db.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT node_id, name, kind,
                       ST_Y(geom) AS lat, ST_X(geom) AS lon,
                       COALESCE(service_rate, 30) AS rate
                FROM nodes
                WHERE kind IN ('pandal', 'immersion')
                ORDER BY kind, name
                """
            )
        ).mappings().all()

    for r in rows:
        h = await rds.hgetall(f"node:{r['node_id']}") if rds else {}
        queue = float(h.get("queue") or 0)
        qh = float(h.get("queue_hours") or 0)
        fill = float(h.get("fill") or 0)
        level, note = crowd_words(qh, fill)
        out[r["node_id"]] = {
            "id": r["node_id"],
            "name": r["name"],
            "kind": "darshan" if r["kind"] == "pandal" else "immersion",
            "lat": r["lat"],
            "lon": r["lon"],
            "crowd": level,
            "note": note,
            "wait": wait_words(queue / max(float(r["rate"]), 1)),
            "wait_minutes": int(queue / max(float(r["rate"]), 1)),
        }
    return out


# ---------------------------------------------------------------- routes

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/now")
async def now():
    """Festival clock plus one plain-language headline."""
    clock, day = None, None
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            st = (await c.get(f"{SIM}/state")).json()
            clock = st.get("clock")
    except Exception:
        pass

    with db.connect() as conn:
        first = conn.execute(text("SELECT min(day) FROM schedule")).scalar()
    if clock and first:
        day = (datetime.fromisoformat(clock).date() - first).days + 1

    venues = await _venue_state()
    packed = [v for v in venues.values() if v["crowd"] == "packed"]
    clearest = sorted(venues.values(), key=lambda v: v["wait_minutes"])

    if packed:
        headline = (
            f"{packed[0]['name']} is very busy. "
            f"{clearest[0]['name']} is much quieter right now."
        )
    else:
        headline = "Everywhere is moving well at the moment."

    return {
        "clock": clock,
        "festival_day": day,
        "headline": headline,
        "advisory": (
            "Evenings are the busiest time. Mornings before 10 are calmest."
        ),
    }


@app.get("/venues")
async def venues(sort: str = Query("quietest", pattern="^(quietest|nearest|name)$")):
    """Everywhere you might go, with how busy it is."""
    vs = list((await _venue_state()).values())
    if sort == "quietest":
        vs.sort(key=lambda v: v["wait_minutes"])
    elif sort == "name":
        vs.sort(key=lambda v: v["name"])
    return vs


@app.get("/venues/{venue_id}")
async def venue(venue_id: int):
    vs = await _venue_state()
    if venue_id not in vs:
        raise HTTPException(404, "not found")
    v = dict(vs[venue_id])

    # A gentle suggestion of when to come instead, from the schedule.
    with db.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT starts_at, ends_at FROM schedule
                WHERE dest_node = :v ORDER BY starts_at LIMIT 1
                """
            ),
            {"v": venue_id},
        ).mappings().first()
    if row:
        v["open_from"] = row["starts_at"].strftime("%H:%M")
        v["open_until"] = row["ends_at"].strftime("%H:%M")
    v["best_time"] = "Before 10:00 or after 22:00"
    return v


@app.post("/stays")
async def stays(setup: TripSetup, limit: int = 6):
    """Where to stay. Ranked by what actually matters to a visitor.

    Price, how long it takes to reach the places they came for, and
    whether there is space. The operator side calls this load balancing;
    a visitor just wants somewhere decent that is not miles away.
    """
    budget_ceiling = {"low": 2500, "mid": 6000, "high": 100000}
    ceiling = budget_ceiling.get(setup.budget, 6000)

    vs = await _venue_state()
    busiest = sorted(vs.values(), key=lambda v: -v["wait_minutes"])[:3]
    anchor_ids = [v["id"] for v in busiest] or list(vs)[:1]

    with db.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT z.zone_id, z.rooms_total, z.median_price,
                       AVG(ST_Distance(z.centroid::geography,
                                       n.geom::geography)) / 1000.0 AS km
                FROM zones z, nodes n
                WHERE n.node_id = ANY(:ids) AND z.rooms_total > 0
                GROUP BY z.zone_id, z.rooms_total, z.median_price
                """
            ),
            {"ids": anchor_ids},
        ).mappings().all()

    out = []
    for r in rows:
        live = await rds.hgetall(f"zone:{r['zone_id']}") if rds else {}
        available = int(float(live.get("rooms_available") or r["rooms_total"]))
        price = float(live.get("price_inr") or r["median_price"] or 3000)
        if available < 5:
            continue
        travel = (float(r["km"]) * 1.35) / 22.0 * 60.0

        # Score: cheaper, closer and emptier is better. A visitor never
        # sees this number - only the order it produces.
        over_budget = max(price - ceiling, 0) / max(ceiling, 1)
        score = travel / 60.0 + over_budget * 1.5

        out.append({
            "zone_id": r["zone_id"],
            "area": f"Area {r['zone_id'][1:]}",
            "price_per_night": int(price),
            "rooms_available": available,
            "travel": travel_words(travel),
            "travel_minutes": int(travel),
            "why": _stay_reason(price, ceiling, travel, available),
            "fits_budget": price <= ceiling,
            "_score": score,
        })

    out.sort(key=lambda r: r["_score"])
    for r in out:
        r.pop("_score")
    return out[:limit]


def _stay_reason(price: float, ceiling: float, travel: float,
                 available: int) -> str:
    if price <= ceiling * 0.7 and travel < 30:
        return "Good value and close to the main venues"
    if price <= ceiling * 0.7:
        return "Noticeably cheaper, a longer ride in"
    if travel < 20:
        return "Very close, so you can come and go easily"
    if available > 800:
        return "Plenty of space here while central areas fill up"
    return "A reasonable balance of price and distance"


@app.get("/journey")
async def journey(from_zone: str, to_venue: int):
    """A door-to-door plan in steps a person can follow."""
    vs = await _venue_state()
    if to_venue not in vs:
        raise HTTPException(404, "unknown destination")
    v = vs[to_venue]

    with db.connect() as conn:
        km = conn.execute(
            text(
                """
                SELECT ST_Distance(z.centroid::geography, n.geom::geography)
                       / 1000.0
                FROM zones z, nodes n
                WHERE z.zone_id = :z AND n.node_id = :v
                """
            ),
            {"z": from_zone, "v": to_venue},
        ).scalar()
    if km is None:
        raise HTTPException(404, "unknown starting point")

    km = float(km) * 1.35
    if km <= 1.6:
        steps = [{"mode": "walk", "detail": f"Walk {km:.1f} km",
                  "minutes": int(km / 4.2 * 60)}]
    elif km > 8:
        steps = [
            {"mode": "walk", "detail": "Walk to the station", "minutes": 8},
            {"mode": "train", "detail": "Take the local train",
             "minutes": int(km / 28 * 60)},
            {"mode": "walk", "detail": "Walk from the station", "minutes": 12},
        ]
    else:
        steps = [
            {"mode": "walk", "detail": "Walk to the stop", "minutes": 6},
            {"mode": "metro", "detail": "Metro towards the venue",
             "minutes": int(km / 24 * 60)},
            {"mode": "walk", "detail": "Walk the last stretch", "minutes": 10},
        ]

    total = sum(s["minutes"] for s in steps)
    return {
        "destination": v["name"],
        "crowd": v["crowd"],
        "wait": v["wait"],
        "steps": steps,
        "total_minutes": total,
        "total": travel_words(total),
        "leave_by": (
            datetime.now() + timedelta(minutes=5)
        ).strftime("%H:%M"),
        "tip": (
            "Trains are busiest between 18:00 and 21:00. Leaving an hour "
            "earlier usually means a seat."
        ),
    }


@app.get("/alternatives/{venue_id}")
async def alternatives(venue_id: int, limit: int = 3):
    """Quieter places of the same kind, when somewhere is packed."""
    vs = await _venue_state()
    if venue_id not in vs:
        raise HTTPException(404, "not found")
    target = vs[venue_id]
    peers = [
        v for v in vs.values()
        if v["kind"] == target["kind"] and v["id"] != venue_id
        and v["wait_minutes"] < target["wait_minutes"]
    ]
    peers.sort(key=lambda v: v["wait_minutes"])
    return peers[:limit]


@app.get("/offers")
async def offers():
    """Perks for travelling at quieter times or to quieter places.

    The reason these exist is never framed as crowd management. From a
    visitor's side it is simply a better deal for being flexible.
    """
    vs = await _venue_state()
    quiet = sorted(vs.values(), key=lambda v: v["wait_minutes"])[:2]
    out = []
    for i, v in enumerate(quiet):
        out.append({
            "id": f"offer_{v['id']}",
            "title": f"20% off refreshments at {v['name']}",
            "detail": "Quieter right now, and you get a discount for going.",
            "expires_in_min": 90 - i * 20,
            "kind": "venue",
        })
    out.append({
        "id": "offer_offpeak",
        "title": "Free travel pass before 10:00",
        "detail": "Start early and your journey in is on us.",
        "expires_in_min": 240,
        "kind": "travel",
    })
    return out


@app.post("/report")
async def report(venue_id: int, level: str = Query(pattern="^(ok|busy|unsafe)$")):
    """Let visitors tell us what it is actually like where they are."""
    if rds:
        await rds.hincrby(f"reports:{venue_id}", level, 1)
    return {"thanks": True}


@app.get("/help")
def help_points():
    """Medical, water and assistance points."""
    with db.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT poi_id, kind, name, ST_Y(geom) AS lat, ST_X(geom) AS lon
                FROM pois
                WHERE kind IN ('hospital','clinic','drinking_water','toilets')
                LIMIT 60
                """
            )
        ).mappings().all()
    label = {
        "hospital": "Medical", "clinic": "Medical",
        "drinking_water": "Drinking water", "toilets": "Toilets",
    }
    return [
        {"id": r["poi_id"], "name": r["name"] or label[r["kind"]],
         "type": label[r["kind"]], "lat": r["lat"], "lon": r["lon"]}
        for r in rows
    ]
