"""
sim/main.py - the simulator as a service.

Loads the world from PostGIS, runs a clock, publishes observations to
Redpanda, and exposes the controls the console's simulation director
panel will drive.

Run:
    cd ~/megaevent
    source .venv/bin/activate
    uvicorn sim.main:app --port 8100

Controls:
    GET  /state                 clock, run status, active effects
    GET  /truth                 ground truth (demo only)
    GET  /scenarios             what can be injected
    POST /start  /pause  /reset
    POST /speed?x=10            wall-clock speed multiplier
    POST /inject/{scenario}     ?target=<venue node id or item id>
    POST /clear                 cancel all running scenarios
"""

from __future__ import annotations

import sys
import json
import asyncio
import logging
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as S  # noqa: E402
from sim.engine import Engine, Zone, Venue  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("sim")

SIM_YAML = Path(__file__).resolve().parents[1] / "config" / "sim.yaml"
TOPIC = "me.observations"

app = FastAPI(title="Mega-Event Simulator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = create_engine(S.PG_DSN, future=True, pool_pre_ping=True)


class Runner:
    """Owns the engine, the clock task and the Kafka producer."""

    def __init__(self):
        self.cfg = yaml.safe_load(open(SIM_YAML))
        self.engine: Engine | None = None
        self.producer = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.speed = 1.0
        self.emitted = 0
        self.kafka_ok = False

    # ---------------------------------------------------------- world

    def load_world(self):
        with db.connect() as conn:
            zrows = conn.execute(
                text(
                    """
                    SELECT zone_id, ST_Y(centroid) AS lat, ST_X(centroid) AS lon,
                           rooms_total, COALESCE(median_price, 3000) AS price
                    FROM zones WHERE rooms_total > 0
                    """
                )
            ).mappings().all()
            vrows = conn.execute(
                text(
                    """
                    SELECT node_id, name, kind, ST_Y(geom) AS lat,
                           ST_X(geom) AS lon,
                           COALESCE(capacity, 5000) AS capacity,
                           COALESCE(service_rate, 30) AS service_rate
                    FROM nodes
                    WHERE kind IN ('pandal','immersion','holding')
                    """
                )
            ).mappings().all()
            srows = conn.execute(
                text(
                    """
                    SELECT item_id, day, starts_at, ends_at, kind,
                           origin_node, dest_node, expected_crowd, is_movable
                    FROM schedule ORDER BY starts_at
                    """
                )
            ).mappings().all()

        if not zrows or not vrows:
            raise RuntimeError(
                "world is empty - run the ETL scripts before starting the sim"
            )

        zones = [
            Zone(zone_id=r["zone_id"], lat=r["lat"], lon=r["lon"],
                 rooms=int(r["rooms_total"]), base_price=float(r["price"]))
            for r in zrows
        ]
        venues = [
            Venue(node_id=r["node_id"], name=r["name"], kind=r["kind"],
                  lat=r["lat"], lon=r["lon"], capacity=int(r["capacity"]),
                  service_rate=float(r["service_rate"]))
            for r in vrows
        ]
        # Pandal draw weight from expected crowd in the schedule.
        draw = {}
        for s in srows:
            if s["kind"] == "darshan" and s["dest_node"]:
                draw[s["dest_node"]] = draw.get(s["dest_node"], 0) + (
                    s["expected_crowd"] or 0
                )
        top = max(draw.values(), default=1) or 1
        for v in venues:
            v.draw = draw.get(v.node_id, top * 0.2) / top

        schedule = [dict(r) for r in srows]
        self.engine = Engine(self.cfg, zones, venues, schedule)
        log.info("world loaded")

    # ---------------------------------------------------------- kafka

    async def connect_kafka(self):
        try:
            from aiokafka import AIOKafkaProducer

            self.producer = AIOKafkaProducer(
                bootstrap_servers=S.KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
                linger_ms=50,
            )
            await self.producer.start()
            self.kafka_ok = True
            log.info("kafka connected at %s", S.KAFKA_BOOTSTRAP)
        except Exception as e:
            self.producer = None
            self.kafka_ok = False
            log.warning("kafka unavailable (%s) - running without it", e)

    async def publish(self, events: list[dict]):
        if not self.producer:
            return
        for e in events:
            try:
                await self.producer.send(TOPIC, e)
                self.emitted += 1
            except Exception as ex:
                log.warning("publish failed: %s", ex)
                self.kafka_ok = False
                return

    # ---------------------------------------------------------- loop

    async def loop(self):
        base = self.cfg["clock"]["speed"]  # ticks per real second at 1x
        while True:
            if self.running and self.engine:
                self.engine.step()
                await self.publish(self.engine.observations())
            await asyncio.sleep(1.0 / max(base * self.speed, 0.1))


runner = Runner()


@app.on_event("startup")
async def startup():
    try:
        runner.load_world()
    except Exception as e:
        log.error("startup failed: %s", e)
    await runner.connect_kafka()
    runner.task = asyncio.create_task(runner.loop())


@app.on_event("shutdown")
async def shutdown():
    if runner.task:
        runner.task.cancel()
    if runner.producer:
        await runner.producer.stop()


def need_engine() -> Engine:
    if runner.engine is None:
        raise HTTPException(503, "world not loaded - run the ETL scripts")
    return runner.engine


@app.get("/state")
def state():
    e = need_engine()
    return {
        "running": runner.running,
        "speed": runner.speed,
        "clock": e.clock.isoformat(),
        "ticks": e.ticks,
        "cohorts": len(e.cohorts),
        "people_in_system": sum(c.size for c in e.cohorts.values()),
        "events_emitted": runner.emitted,
        "kafka": runner.kafka_ok,
        "active_effects": [
            {"kind": x.kind, "target": x.target,
             "remaining_min": round(x.remaining_min)}
            for x in e.effects
        ],
    }


@app.get("/truth")
def truth():
    """Ground truth, for the console's truth-vs-estimate toggle."""
    return need_engine().truth()


@app.get("/scenarios")
def scenarios():
    return [
        {"id": k, "label": v["label"], "description": v["description"],
         "params": v["params"]}
        for k, v in runner.cfg["scenarios"].items()
    ]


@app.post("/start")
def start():
    need_engine()
    runner.running = True
    return {"running": True}


@app.post("/pause")
def pause():
    runner.running = False
    return {"running": False}


@app.post("/reset")
async def reset():
    runner.running = False
    runner.emitted = 0
    # Redis holds live state from the previous run. Leaving it there
    # makes the twin report queues that no longer exist, and the
    # optimizer then proposes diverting people who are not there.
    import redis.asyncio as _r
    rd = _r.from_url(S.REDIS_URL, decode_responses=True)
    await rd.flushdb()
    await rd.aclose()
    runner.load_world()
    return {"reset": True, "clock": need_engine().clock.isoformat()}


@app.post("/speed")
def speed(x: float = Query(1.0, gt=0, le=120)):
    runner.speed = x
    return {"speed": x}


@app.post("/inject/{scenario}")
def inject(scenario: str, target: str | None = None):
    e = need_engine()
    tgt: str | int | None = target
    if target and target.isdigit():
        tgt = int(target)
    try:
        return e.inject(scenario, target=tgt)
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.post("/clear")
def clear():
    return {"cleared": need_engine().clear_effects()}


@app.post("/divert")
def divert(from_node: int, to_node: int, people: int,
           compliance: float | None = None):
    """Apply an approved diversion to the running world."""
    e = need_engine()
    return e.divert(from_node, to_node, people, compliance)
