"""
sim/engine.py - the world model behind the prototype.

A compartment model at cohort granularity. A cohort is a group of people
sharing an origin zone, a destination venue and a state. Simulating
cohorts rather than individuals keeps this fast enough to run live and
keeps every emitted record non-identifying by construction.

    AT_STAY --> TRAVELLING --> QUEUING --> AT_VENUE --> RETURNING --> done

Three things this model gets right that a naive one does not:

  * cohorts split. A venue serving 70 people a minute cannot admit a
    250-person cohort atomically, so the cohort is cut and the remainder
    stays queued.
  * crowds balk. Once a queue passes a multiple of venue capacity,
    arrivals divert to a less loaded venue of the same kind, or give up.
    Without this, queues grow without bound and every number is fiction.
  * venue state is derived from cohorts, never accumulated alongside
    them. One source of truth means occupancy cannot drift past 100%.

Ground truth lives here. What the platform sees is a noisy, sometimes
missing view of it, so forecast error is measurable.
"""

from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger("sim.engine")

AT_STAY, TRAVELLING, QUEUING, AT_VENUE, RETURNING = (
    "at_stay", "travelling", "queuing", "at_venue", "returning"
)


@dataclass
class Zone:
    zone_id: str
    lat: float
    lon: float
    rooms: int
    base_price: float
    occupied: int = 0

    @property
    def occupancy(self) -> float:
        return min(self.occupied / self.rooms, 1.0) if self.rooms else 1.0


@dataclass
class Venue:
    node_id: int
    name: str
    kind: str
    lat: float
    lon: float
    capacity: int
    service_rate: float
    draw: float = 1.0
    service_factor: float = 1.0
    credit: float = 0.0
    balked: int = 0
    queue: float = 0.0
    inside: float = 0.0

    @property
    def occupancy(self) -> float:
        return (self.inside + self.queue) / self.capacity if self.capacity else 0.0

    @property
    def wait_min(self) -> float:
        rate = max(self.service_rate * self.service_factor, 1.0)
        return self.queue / rate


@dataclass
class Cohort:
    cid: int
    size: int
    origin: str
    dest: int
    state: str
    timer: float
    mode: str = "walk"
    needs_room: bool = False


@dataclass
class Effect:
    kind: str
    params: dict
    remaining_min: float
    target: str | int | None = None


class Engine:
    def __init__(self, cfg: dict, zones: list[Zone], venues: list[Venue],
                 schedule: list[dict], seed: int = 7):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.zones = {z.zone_id: z for z in zones}
        self.venues = {v.node_id: v for v in venues}
        self.schedule = schedule

        self.tick_min = cfg["clock"]["tick_minutes"]
        self.cohort_size = cfg["cohort"]["size"]
        venue_cfg = cfg.get("venue", {})
        self.balk_mult = venue_cfg.get("balk_at_queue_multiple", 1.5)
        self.abandon_share = venue_cfg.get("abandon_share", 0.35)

        self.clock = self._start_clock()
        self.ticks = 0
        self.cohorts: dict[int, Cohort] = {}
        self._next_cid = 1
        self.effects: list[Effect] = []
        self.arrivals_at: dict[int, int] = {}
        self.departures_at: dict[int, int] = {}
        self.balked_total = 0

        self._travel_cache: dict[tuple[str, int], tuple[float, str]] = {}
        self._zone_weights = self._compute_zone_weights()
        self._recompute_venue_state()

        log.info("engine ready: %d zones, %d venues, %d schedule items",
                 len(self.zones), len(self.venues), len(self.schedule))

    # -------------------------------------------------- setup

    def _start_clock(self) -> datetime:
        base = min((s["starts_at"] for s in self.schedule), default=None)
        if base is None:
            return datetime(2026, 9, 14, 6, 0)
        day = self.cfg["clock"]["start_day"] - 1
        return (base + timedelta(days=day)).replace(
            hour=self.cfg["clock"]["start_hour"], minute=0, second=0,
            microsecond=0)

    def _compute_zone_weights(self) -> dict[str, float]:
        total = sum(z.rooms for z in self.zones.values()) or 1
        return {zid: z.rooms / total for zid, z in self.zones.items()}

    @staticmethod
    def _km(a_lat, a_lon, b_lat, b_lon) -> float:
        r = 6371.0
        p1, p2 = math.radians(a_lat), math.radians(b_lat)
        dp, dl = p2 - p1, math.radians(b_lon - a_lon)
        h = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        return 2 * r * math.asin(math.sqrt(h))

    def travel(self, zone_id: str, venue_id: int) -> tuple[float, str]:
        key = (zone_id, venue_id)
        if key in self._travel_cache:
            return self._travel_cache[key]
        z, v = self.zones[zone_id], self.venues[venue_id]
        km = self._km(z.lat, z.lon, v.lat, v.lon)
        sp = self.cfg["cohort"]["speed_kph"]
        if km <= self.cfg["cohort"]["walk_threshold_km"]:
            mode = "walk"
        elif km > 8:
            mode = "rail"
        elif km > 4:
            mode = "metro"
        else:
            mode = "bus"
        minutes = (km * 1.35) / sp[mode] * 60.0
        out = (max(minutes, 2.0), mode)
        self._travel_cache[key] = out
        return out

    # -------------------------------------------------- effects

    def inject(self, kind: str, target=None, params: dict | None = None) -> dict:
        spec = self.cfg["scenarios"].get(kind)
        if spec is None:
            raise ValueError(f"unknown scenario {kind}")
        p = dict(spec["params"])
        p.update(params or {})
        dur = float(p.get("duration_min", p.get("over_min", 120)))

        if kind == "demand_spike":
            self._spike(int(p["extra_visitors"]), float(p["over_min"]), target)
        elif kind == "schedule_change":
            self._shift_schedule(float(p["shift_min"]), target)

        self.effects.append(
            Effect(kind=kind, params=p, remaining_min=dur, target=target))
        log.info("injected %s target=%s for %.0f min", kind, target, dur)
        return {"scenario": kind, "target": target, "params": p,
                "duration_min": dur}

    def clear_effects(self) -> int:
        n = len(self.effects)
        self.effects.clear()
        for v in self.venues.values():
            v.service_factor = 1.0
        return n

    def _active(self, kind: str) -> Effect | None:
        return next((e for e in self.effects if e.kind == kind), None)

    def _spike(self, extra: int, over_min: float, target=None):
        pandals = [v for v in self.venues.values() if v.kind == "pandal"]
        if target and int(target) in self.venues:
            pandals = [self.venues[int(target)]]
        if not pandals:
            return
        for _ in range(max(1, extra // self.cohort_size)):
            v = self.rng.choices(pandals, weights=[p.draw for p in pandals])[0]
            self._spawn(self._pick_zone(), v.node_id, self.cohort_size,
                        delay=self.rng.uniform(0, over_min))

    def _shift_schedule(self, minutes: float, target=None):
        for s in self.schedule:
            if s["kind"] != "procession":
                continue
            if target and s["item_id"] != target:
                continue
            s["starts_at"] += timedelta(minutes=minutes)
            s["ends_at"] += timedelta(minutes=minutes)
            s["shifted_min"] = s.get("shifted_min", 0) + minutes

    # -------------------------------------------------- cohorts

    def _pick_zone(self) -> str:
        weights = dict(self._zone_weights)
        eff = self._active("hotel_saturation")
        if eff:
            bias = float(eff.params.get("zone_bias", 3.0))
            busiest = max((v for v in self.venues.values()
                           if v.kind == "pandal"),
                          key=lambda v: v.draw, default=None)
            if busiest:
                for zid, z in self.zones.items():
                    if self._km(z.lat, z.lon, busiest.lat, busiest.lon) < 4:
                        weights[zid] *= bias
        ids = list(weights)
        return self.rng.choices(ids, weights=[weights[i] for i in ids])[0]

    def _spawn(self, zone_id: str, venue_id: int, size: int, delay: float = 0.0):
        minutes, mode = self.travel(zone_id, venue_id)
        needs_room = self.rng.random() < self.cfg["cohort"]["out_of_town_share"]
        self.cohorts[self._next_cid] = Cohort(
            cid=self._next_cid, size=size, origin=zone_id, dest=venue_id,
            state=TRAVELLING, timer=minutes + delay, mode=mode,
            needs_room=needs_room)
        self._next_cid += 1
        if needs_room:
            z = self.zones[zone_id]
            z.occupied = min(z.rooms, z.occupied + max(1, size // 3))

    def _alternative(self, v: Venue) -> Venue | None:
        peers = [
            p for p in self.venues.values()
            if p.kind == v.kind and p.node_id != v.node_id
            and self._km(v.lat, v.lon, p.lat, p.lon) < 12
        ]
        if not peers:
            return None
        best = min(peers, key=lambda p: p.occupancy)
        return best if best.occupancy < self.balk_mult * 0.6 else None

    # -------------------------------------------------- demand

    def _demand_this_tick(self) -> list[tuple[int, int]]:
        hour = self.clock.hour
        hw = self.cfg["demand"]["hourly_weights"].get(hour, 0.3)
        n = self.cfg["demand"]["noise"]
        noise = 1.0 + self.rng.uniform(-n, n)
        day = self.clock.date()
        focus = self._active("uneven_distribution")
        out = []

        for item in self.schedule:
            if item["day"] != day:
                continue
            if not (item["starts_at"] <= self.clock <= item["ends_at"]):
                continue
            dest = item.get("dest_node")
            if dest not in self.venues:
                continue
            span_h = max((item["ends_at"] - item["starts_at"]).total_seconds()
                         / 3600.0, 1.0)
            people = ((item["expected_crowd"] / span_h)
                      * (self.tick_min / 60.0) * hw * noise)

            if focus and self.venues[dest].kind == "immersion":
                share = float(focus.params.get("focus_share", 0.6))
                busiest = max((v for v in self.venues.values()
                               if v.kind == "immersion"),
                              key=lambda v: v.capacity)
                people *= share * 2 if dest == busiest.node_id else (1 - share)

            if people > 0:
                out.append((dest, int(people)))
        return out

    # -------------------------------------------------- tick

    def _recompute_venue_state(self):
        for v in self.venues.values():
            v.queue = 0.0
            v.inside = 0.0
        for c in self.cohorts.values():
            v = self.venues.get(c.dest)
            if v is None:
                continue
            if c.state == QUEUING:
                v.queue += c.size
            elif c.state == AT_VENUE:
                v.inside += c.size

    def step(self) -> None:
        self.arrivals_at.clear()
        self.departures_at.clear()

        cong = self._active("transport_congestion")
        last_mile = self._active("last_mile")
        vcap = self._active("venue_capacity")
        for v in self.venues.values():
            v.service_factor = 1.0
        if vcap:
            if vcap.target and int(vcap.target) in self.venues:
                targets = [self.venues[int(vcap.target)]]
            else:
                targets = sorted(
                    (v for v in self.venues.values() if v.kind == "pandal"),
                    key=lambda v: -v.draw)[:1]
            for v in targets:
                v.service_factor = float(vcap.params.get("service_factor", 0.5))

        for venue_id, people in self._demand_this_tick():
            whole, rem = divmod(int(people), self.cohort_size)
            for _ in range(whole):
                self._spawn(self._pick_zone(), venue_id, self.cohort_size)
            if rem and self.rng.random() < rem / self.cohort_size:
                self._spawn(self._pick_zone(), venue_id, self.cohort_size)

        finished = []
        for c in list(self.cohorts.values()):
            slow = 1.0
            if c.state == TRAVELLING:
                if cong and c.mode == cong.params.get("mode", "rail"):
                    slow /= max(float(cong.params["capacity_factor"]), 0.05)
                if last_mile and c.mode != "walk":
                    slow /= max(float(last_mile.params["access_factor"]), 0.1)
            c.timer -= self.tick_min / slow
            if c.timer > 0:
                continue

            if c.state == TRAVELLING:
                v = self.venues[c.dest]
                if v.queue > self.balk_mult * v.capacity:
                    alt = self._alternative(v)
                    if alt is not None:
                        mins, mode = self.travel(c.origin, alt.node_id)
                        c.dest, c.mode = alt.node_id, mode
                        c.timer = max(mins * 0.35, 5.0)
                        continue
                    if self.rng.random() < self.abandon_share:
                        v.balked += c.size
                        self.balked_total += c.size
                        finished.append(c.cid)
                        continue
                c.state = QUEUING
                c.timer = 0.0
                self.arrivals_at[c.dest] = (
                    self.arrivals_at.get(c.dest, 0) + c.size)

            elif c.state == AT_VENUE:
                c.state = RETURNING
                mins, _ = self.travel(c.origin, c.dest)
                c.timer = mins
                self.departures_at[c.dest] = (
                    self.departures_at.get(c.dest, 0) + c.size)

            elif c.state == RETURNING:
                finished.append(c.cid)

        for cid in finished:
            self.cohorts.pop(cid, None)

        dwell = self.cfg["cohort"]["dwell_min"]
        spread = self.cfg["cohort"]["dwell_spread"]
        for v in self.venues.values():
            v.credit += v.service_rate * v.service_factor * self.tick_min
            if v.credit < 1:
                continue
            waiting = [c for c in self.cohorts.values()
                       if c.dest == v.node_id and c.state == QUEUING]
            for c in waiting:
                if v.credit < 1:
                    break
                if c.size <= v.credit:
                    v.credit -= c.size
                    c.state = AT_VENUE
                    c.timer = max(5.0, self.rng.gauss(dwell, spread))
                else:
                    take = int(v.credit)
                    if take < 1:
                        break
                    c.size -= take
                    v.credit -= take
                    self.cohorts[self._next_cid] = Cohort(
                        cid=self._next_cid, size=take, origin=c.origin,
                        dest=c.dest, state=AT_VENUE,
                        timer=max(5.0, self.rng.gauss(dwell, spread)),
                        mode=c.mode, needs_room=c.needs_room)
                    self._next_cid += 1
                    break

        for e in self.effects:
            e.remaining_min -= self.tick_min
        self.effects = [e for e in self.effects if e.remaining_min > 0]

        self._recompute_venue_state()
        self.clock += timedelta(minutes=self.tick_min)
        self.ticks += 1


    # -------------------------------------------------- intervention

    def divert(self, from_node: int, to_node: int, people: int,
               compliance: float | None = None) -> dict:
        """Offer a diversion to people queuing at from_node.

        Returns how many actually moved. Not everyone accepts - people
        have queued for hours and are invested in where they are. The
        gap between people asked and people moved is exactly what the
        optimizer's uptake controller needs to see.
        """
        if from_node not in self.venues or to_node not in self.venues:
            return {"applied": False, "error": "unknown venue"}

        share = (compliance if compliance is not None
                 else self.cfg.get("incentives", {}).get("base_uptake", 0.22))
        want = int(people * share)
        if want < 1:
            return {"applied": True, "asked": people, "moved": 0}

        queued = [c for c in self.cohorts.values()
                  if c.dest == from_node and c.state == QUEUING]
        # Take from the back of the queue first: those people have
        # invested the least time and are the likeliest to accept.
        queued.reverse()

        moved = 0
        for c in queued:
            if moved >= want:
                break
            remaining = want - moved
            mins, mode = self.travel(c.origin, to_node)
            if c.size <= remaining:
                c.dest = to_node
                c.state = TRAVELLING
                c.mode = mode
                c.timer = max(mins * 0.4, 5.0)
                moved += c.size
            else:
                c.size -= remaining
                self.cohorts[self._next_cid] = Cohort(
                    cid=self._next_cid, size=remaining, origin=c.origin,
                    dest=to_node, state=TRAVELLING,
                    timer=max(mins * 0.4, 5.0), mode=mode,
                    needs_room=c.needs_room)
                self._next_cid += 1
                moved += remaining
                break

        self._recompute_venue_state()
        log.info("diverted %d of %d asked from %s to %s",
                 moved, people, from_node, to_node)
        return {
            "applied": True,
            "asked": people,
            "moved": moved,
            "uptake": round(moved / people, 3) if people else 0,
        }

    # -------------------------------------------------- observation

    def _noisy(self, value: float) -> int | None:
        o = self.cfg["observation"]
        if self.rng.random() < o["missing_rate"]:
            return None
        return max(0, int(value * (1 + self.rng.gauss(0, o["count_noise"]))))

    def zone_price(self, z: Zone) -> float:
        p = self.cfg["pricing"]
        occ = z.occupancy
        if occ <= p["surge_at_occupancy"]:
            return round(z.base_price * p["base_multiplier"], 2)
        over = (occ - p["surge_at_occupancy"]) / max(
            1 - p["surge_at_occupancy"], 0.01)
        return round(z.base_price * (1 + over * (p["max_multiplier"] - 1)), 2)

    def observations(self) -> list[dict]:
        ts = self.clock.isoformat()
        out = []

        for v in self.venues.values():
            occ = self._noisy(v.inside + v.queue)
            if occ is None:
                continue
            out.append({
                "event_type": "venue_state", "ts": ts,
                "source_id": str(v.node_id),
                "payload": {
                    "occupancy": occ,
                    "queue": self._noisy(v.queue) or 0,
                    "inside": self._noisy(v.inside) or 0,
                    "capacity": v.capacity,
                    "inflow_ppm": round(
                        self.arrivals_at.get(v.node_id, 0) / self.tick_min, 1),
                    "outflow_ppm": round(
                        self.departures_at.get(v.node_id, 0) / self.tick_min, 1),
                },
            })

        if self.ticks % self.cfg["observation"]["inventory_every_ticks"] == 0:
            for z in self.zones.values():
                out.append({
                    "event_type": "inventory_snapshot", "ts": ts,
                    "source_id": z.zone_id,
                    "payload": {
                        "rooms_total": z.rooms,
                        "rooms_occupied": min(z.occupied, z.rooms),
                        "rooms_available": max(0, z.rooms - z.occupied),
                        "price_inr": self.zone_price(z),
                    },
                })

        by_mode: dict[str, int] = {}
        for c in self.cohorts.values():
            if c.state == TRAVELLING:
                by_mode[c.mode] = by_mode.get(c.mode, 0) + c.size
        for mode, people in by_mode.items():
            out.append({
                "event_type": "corridor_load", "ts": ts, "source_id": mode,
                "payload": {"in_transit": self._noisy(people) or 0},
            })

        return out

    def truth(self) -> dict:
        return {
            "clock": self.clock.isoformat(),
            "ticks": self.ticks,
            "cohorts": len(self.cohorts),
            "people_in_system": sum(c.size for c in self.cohorts.values()),
            "balked_total": self.balked_total,
            "by_state": {
                st: sum(c.size for c in self.cohorts.values() if c.state == st)
                for st in (TRAVELLING, QUEUING, AT_VENUE, RETURNING)
            },
            "venues": [
                {
                    "node_id": v.node_id, "name": v.name, "kind": v.kind,
                    "queue": round(v.queue), "inside": round(v.inside),
                    "occupancy_pct": round(v.occupancy * 100, 1),
                    "wait_min": round(v.wait_min, 1),
                    "balked": v.balked,
                }
                for v in sorted(self.venues.values(), key=lambda x: -x.occupancy)
            ],
            "zones_top": [
                {
                    "zone_id": z.zone_id,
                    "occupancy_pct": round(z.occupancy * 100, 1),
                    "price_inr": self.zone_price(z),
                    "rooms_available": max(0, z.rooms - z.occupied),
                }
                for z in sorted(self.zones.values(),
                                key=lambda x: -x.occupancy)[:10]
            ],
            "active_effects": [
                {"kind": e.kind, "target": e.target,
                 "remaining_min": round(e.remaining_min)}
                for e in self.effects
            ],
        }
