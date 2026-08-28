#!/usr/bin/env python3
"""
add_corridors_to_twin.py - teach the twin about the network.

Until now the twin projected venues and zones. It could tell you a venue
was saturating but not that the line feeding it was about to fail, which
left transport congestion with no readout at all.

This adds a third projection. Forecast demand for each venue is pushed
back along the corridors that carry it, using the lookup table
05_build_corridors.py precomputed. Corridor pressure is then the same
shape as venue pressure - load against capacity, with a time to
saturation - so the console can rank all of it together.

Run once from the repo root:
    python add_corridors_to_twin.py
"""

from pathlib import Path

TWIN = Path("twin/main.py")

CORRIDOR_CODE = '''
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
'''

INIT_FIELDS = '''        self.corridors: dict[str, dict] = {}
        self.paths: dict[int, list] = {}
        self.zone_share: dict[str, float] = {}
        self.corridor_board: list[dict] = []
        self.board_rate: dict[int, float] = {}
        self._corridor_prev: dict[str, float] = {}
'''

ENDPOINTS = '''

@app.get("/pressure/corridors")
def corridor_pressure(band: str | None = Query(None), mode: str | None = None):
    """Which routes into the event are filling up."""
    rows = twin.corridor_board
    if band:
        rows = [r for r in rows if r["band"] == band]
    if mode:
        rows = [r for r in rows if r["mode"] == mode]
    return rows
'''


def main():
    if not TWIN.exists():
        raise SystemExit(f"not found: {TWIN} - run from the repo root")
    s = TWIN.read_text(encoding="utf-8")

    if "project_corridors" in s:
        print("already patched")
        return

    # 1. extra state on the Twin object
    anchor = "        self._predicted_inflow: dict[int, float] = {}\n"
    if anchor not in s:
        raise SystemExit("could not find the Twin __init__ anchor")
    s = s.replace(anchor, anchor + INIT_FIELDS, 1)

    # 2. the corridor methods, inserted before the scoring section
    anchor2 = "    # ---------------------------------------------------------- scoring"
    if anchor2 not in s:
        raise SystemExit("could not find the scoring section")
    s = s.replace(anchor2, CORRIDOR_CODE + "\n" + anchor2, 1)

    # 3. remember each venue's observed inflow so corridors can use it
    anchor3 = "            predicted_next[nid] = first_rate or 0.0"
    if anchor3 not in s:
        raise SystemExit("could not find the projection loop anchor")
    s = s.replace(
        anchor3,
        anchor3 + "\n            self.board_rate[nid] = base_inflow",
        1,
    )

    # 4. call it each tick, after zones
    anchor4 = "        await self.project_zones(now)"
    if anchor4 not in s:
        raise SystemExit("could not find the project_zones call")
    s = s.replace(anchor4, anchor4 + "\n        await self.project_corridors(now)", 1)

    # 5. load definitions at startup
    anchor5 = "    twin.learn_hour_profile()"
    if anchor5 not in s:
        raise SystemExit("could not find the startup hook")
    s = s.replace(anchor5, anchor5 + "\n    twin.load_corridors()", 1)

    # 6. the endpoint
    anchor6 = '@app.get("/forecast/{node_id}")'
    if anchor6 not in s:
        raise SystemExit("could not find the forecast endpoint")
    s = s.replace(anchor6, ENDPOINTS.strip() + "\n\n\n" + anchor6, 1)

    TWIN.write_text(s, encoding="utf-8")
    print("patched twin/main.py - restart it to pick this up")


if __name__ == "__main__":
    main()
