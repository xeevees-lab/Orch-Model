# Orchestration

**Mega-Event Hospitality Orchestration — Intelligent Capacity & Crowd Management**

Problem Statement ID 8 · Mumbai · Ganesh Chaturthi

---

When a city hosts a mega-event, the problem is rarely that resources are
scarce. It is that nobody can see all of them at once.

A hotel knows its own occupancy but not the district's. The event control room
knows gate counts but not where those people are sleeping tonight or which
line they will take home. The transport operator knows ridership but not that
a procession start time just moved. Each party optimises what it can see, and
coordination happens by phone call once the problem is already visible.

The result is the pattern the brief describes: some areas overwhelmed while
accommodation sits empty a few kilometres away, corridors past safe
throughput, prices surging where demand concentrated, and people who came for
darshan and left without getting in.

**This project closes that loop.** It senses the current state of the whole
destination, forecasts where pressure is heading, works out what to do about
it, puts the recommendation in front of an operator in plain language, and —
on approval — applies it and measures what actually happened.

---

## What it does

```
   SENSE ──> FORECAST ──> OPTIMIZE ──> PROPOSE ──> APPLY ──> MEASURE
     ↑                                                          │
     └──────────────── corrects the next cycle ─────────────────┘
```

A worked example, taken from a live run:

> Lalbaugcha Raja's queue is 9 hours deep and **throughput-bound** — the venue
> is not physically full, its darshan rate is the bottleneck. It crosses the
> safe limit in 18 minutes.
>
> **Recommendation:** move 3,361 people to Khetwadi Cha Ganraj, 20 minutes
> away, which has headroom. Saves 4.25 hours per person.
>
> *Approved.* 741 of 3,361 asked actually accepted — a diversion is an offer,
> not an order. Realised uptake of 22% corrects the controller's estimate for
> the next cycle.

Every step of that is real system output, and every step is recorded in an
audit ledger with predicted effect against measured outcome.

### Scale

| | |
|---|---|
| Destination graph | 190,018 nodes · 389,963 edges · 68 accommodation zones |
| Room supply spread | **216×** between the densest and sparsest zone |
| Transit | 5,600 bus stops · 96 rail and metro stations |
| Event | 6 pandals, 6 immersion points, 2 holding areas, 73 schedule items |
| Services | 6 — API, simulator, ingestion, twin, optimizer, console |

That 216× figure is the reason the project exists. When Lalbaug saturates,
there is genuinely underused capacity to send people toward.

---

## What makes it different

**Time-to-saturation, not utilisation.** "Lalbaug is at 82%" is a dashboard.
"Lalbaug saturates in 18 minutes" is a decision. Every venue is ranked by
minutes of headroom remaining, shortest fuse first.

**Two honest pressure metrics.** Venue fill and queue hours are reported
separately, because a venue that is physically full and one that is
throughput-limited need opposite responses — meter entry versus open more
lanes. Conflating them into a single occupancy number hides which you have.

**Hospitality and crowd solved as one problem.** Where people sleep determines
which corridor they use and when. An origin-destination matrix ties room
supply, travel time and crowd load together rather than treating them as three
separate dashboards.

**Incentives with a damped controller.** The system estimates how many people
will accept a diversion offer, measures what it actually got, and corrects.
Without that damping it overshoots and creates the bottleneck it was avoiding.

**Ground truth is separated from observation.** The platform never reads the
simulator's internal state — only noisy, occasionally-missing telemetry. Its
forecast error is therefore a real number that gets reported openly. A model
claiming zero error would be reading the answer key.

**Privacy by construction.** Everything is modelled and transmitted at cohort
level, never individual. No identifying data exists in the pipeline to begin
with, so there is nothing to protect after the fact.

---

## Quickstart

### Prerequisites

- Docker with the WSL2 backend (Windows) or Docker Engine (Linux/macOS)
- Python 3.12 — 3.13+ lacks prebuilt wheels for the GDAL-based geo packages
- Node.js 20+
- Flutter SDK and Android SDK, only for the attendee app
- ~4 GB disk for the source data

### 1 · Clone and install

```bash
git clone https://github.com/xeevees-lab/megaevent.git
cd megaevent

curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate

uv pip install fastapi "uvicorn[standard]" websockets pydantic pydantic-settings
uv pip install "psycopg[binary]" sqlalchemy geoalchemy2 asyncpg redis aiokafka
uv pip install geopandas shapely pyproj rasterio osmnx pyrosm
uv pip install networkx igraph gtfs_kit pyyaml
uv pip install numpy pandas scipy pyarrow scikit-learn lightgbm statsforecast
uv pip install ortools jupedsim pedpy
uv pip install llama-index llama-index-llms-ollama llama-index-embeddings-ollama
uv pip install simpy apscheduler httpx python-dotenv
```

> On Windows, run all of this inside WSL. GDAL on native Windows is a bad
> afternoon.

### 2 · Fetch the source data

**The repository does not include data** — it is regenerable and would add
hundreds of megabytes to the history. This step is required before anything
runs.

```bash
mkdir -p data/raw && cd data/raw

# Street network. Geofabrik splits India by ZONE, not state —
# Maharashtra lives in western-zone.
wget https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf
sudo apt install -y osmium-tool
osmium extract -b 72.775,18.890,72.990,19.280 \
  western-zone-latest.osm.pbf -o mumbai.osm.pbf

# Map tiles. Check maps.protomaps.com/builds for the current date.
wget https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz
tar -xzf go-pmtiles_*.tar.gz && sudo mv pmtiles /usr/local/bin/
pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles mumbai.pmtiles \
  --bbox=72.775,18.890,72.990,19.280 --minzoom=9 --maxzoom=15
cp mumbai.pmtiles ../../manager-console/public/

cd ../..
```

Also download a Mumbai GTFS feed from
[mobilitydatabase.org](https://mobilitydatabase.org) into `data/raw/`. It is
bus-only; rail and metro are hand-authored in `config/rail_lines.yaml`.

**Study area bounding box:** `72.775, 18.890, 72.990, 19.280`
(west, south, east, north)

### 3 · Build the world

```bash
cd infra && docker compose up -d && cd ..
docker compose -f infra/docker-compose.yml ps    # wait for all healthy

docker exec -i me-postgres psql -U megaevent -d megaevent < db/schema.sql

python etl/01_build_graph.py      # OSM  -> zones, nodes, edges, POIs
python etl/02_load_transit.py     # GTFS -> stops, headway capacities
python etl/04_load_rail.py        # rail and metro, matched from OSM by name
python etl/03_seed_event.py       # venues, schedule, zone prices — run last
```

`01` takes a few minutes. `04` reports any station it could not match by name;
fix those by adding the OSM spelling to that line's `aliases` and re-running.

### 4 · Start the services

Five terminals. **Start the ingest worker before the simulator** — it consumes
from `latest`, so anything published while it is down is lost.

```bash
source .venv/bin/activate

uvicorn api.main:app  --reload --port 8000    # 1  zones, venues, stats
python -m ingest.worker                        # 2  stream -> stores
uvicorn sim.main:app  --reload --port 8100    # 3  simulator
uvicorn twin.main:app --reload --port 8200    # 4  forecast
uvicorn opt.main:app  --reload --port 8300    # 5  optimizer
```

Verify:

```bash
for p in 8000 8100 8200 8300; do
  echo -n "$p "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/health
done
```

### 5 · Start the console

```bash
cd manager-console
npm install
npm run dev
```

Open `http://localhost:5173`, sign in, go to **Simulator**, press **Run**.

### 6 · Attendee app (optional)

```bash
cd attendee_app
flutter pub get
flutter run          # with a device connected
```

Currently a scaffold with dependencies installed — see *Roadmap*.

---

## Repository layout

```
megaevent/
├── config/
│   ├── settings.py           paths, bbox, DSNs, capacity constants
│   ├── event_ganesh.yaml     pandals, immersion points, processions, schedule
│   ├── rail_lines.yaml       rail and metro line order, headways, capacities
│   └── sim.yaml              simulator parameters and the seven scenarios
├── db/schema.sql             tables, hypertables, extensions
├── etl/
│   ├── 01_build_graph.py     OSM extract  -> destination graph in PostGIS
│   ├── 02_load_transit.py    GTFS         -> stops and headway capacities
│   ├── 03_seed_event.py      venues snapped to graph, schedule, prices
│   └── 04_load_rail.py       rail/metro from OSM, matched by station name
├── sim/
│   ├── engine.py             cohort compartment model
│   └── main.py               simulator service, control and scenario API
├── ingest/worker.py          validated stream -> Redis + TimescaleDB
├── twin/main.py              arrival profile, projection, pressure, TTS
├── opt/main.py               OR-Tools solvers, action ledger, apply path
├── api/main.py               zones, venues, stops, schedule, stats
├── infra/docker-compose.yml  Postgres, Redis, Redpanda
├── manager-console/          React + TypeScript + Vite
│   ├── public/mumbai.pmtiles local basemap (not in git)
│   └── src/{App,views,MapView,api}.tsx, console.css
├── attendee_app/             Flutter (scaffold)
└── data/raw/                 source data (not in git — see Quickstart)
```

---

## Architecture

```
 ┌──────────────┐        ┌───────────────┐
 │  Simulator   │───────>│  Redpanda     │
 │  (or real    │ publish│  me.observations
 │   feeds)     │        └───────┬───────┘
 └──────▲───────┘                │
        │                        ▼
        │                ┌───────────────┐
        │                │ Ingest worker │  validate · anonymise
        │                │               │  roll up · fan out
        │                └───┬───────┬───┘
        │                    │       │
        │          ┌─────────▼──┐ ┌──▼──────────┐
        │          │   Redis    │ │ TimescaleDB │
        │          │   "now"    │ │  "always"   │
        │          └─────┬──────┘ └──────┬──────┘
        │                └───────┬───────┘
        │                        ▼
        │                ┌───────────────┐
        │                │  Digital twin │  learned arrival profile
        │                │               │  120-min projection
        │                └───────┬───────┘  pressure index · TTS
        │                        ▼
        │                ┌───────────────┐
        │                │   Optimizer   │  min-cost flow × 3
        │                └───────┬───────┘  action ledger
        │                        ▼
        │                ┌───────────────┐
        └────apply───────│    Console    │
             measured    └───────────────┘
```

**One database, three extensions.** PostGIS, TimescaleDB and pgvector run in a
single PostgreSQL container, so spatial queries, time-series history and
vector search can be joined in one statement. Three separate databases would
be three things to break at 3am.

**Redis is a cache; Postgres is the truth.** Redis holds current state for the
twin to read in microseconds each tick. Every delta is also appended to a
Timescale hypertable, which is what the forecaster learns from and the console
charts. Redis can be rebuilt from Postgres; the reverse is not true.

### The twin

Learns an hour-of-day arrival profile **from observed telemetry** — it is
never handed the shape — and applies it to a moving average of current inflow.
Per venue, per 5-minute step, across a 2-hour horizon:

```
admitted(t) = min(queue(t) + arrivals(t), service_rate × step)
queue(t+1)  = queue(t) + arrivals(t) − admitted(t)
inside(t+1) = inside(t) + admitted(t) − inside(t) × step / dwell
```

- `pressure_index = max(queue_hours / 2h, fill / 0.90)` — whichever constraint
  binds first drives the number
- `tts_min` — minutes until the index crosses 1.0; `0` if already past
- `binding` — `throughput` or `space`, which tells the operator which fix applies

### The optimizer

Three OR-Tools min-cost-flow solves, because assignment under capacity is
exactly what min-cost flow is for and it runs in milliseconds at this scale.

| Solver | Sources | Sinks | Arc cost |
|---|---|---|---|
| Diversion | Venues above their fair share of total queue | Venues below it | Travel time, capped at 45 min detour |
| Zone assignment | Arriving demand | Zones with rooms | 30% price + 45% travel to venues under pressure + 25% crowding |
| Incentive sizing | — | — | Diversion ÷ estimated uptake, corrected from measurement |

Fair share is proportional to a venue's own service rate — Lalbaug at 70/min
legitimately carries more queue than Khetwadi at 35/min. The zone solver caps
any single zone at 28% of demand, so it cannot solve crowding by creating it
somewhere else.

---

## The console

| Page | Shows |
|---|---|
| **Sign-in** | Role picker — event organiser, hospitality operator, city authority. Cosmetic; no accounts or permissions |
| **Live map** | Zones shaded by room supply, venues coloured by pressure and sized by queue, pressure board, recommended actions, accommodation |
| **Analysis** | Queue forecast, pressure trajectory against the 1.0 line, learned arrival profile, incentive controller learning curve, zone occupancy, forecast error |
| **Simulator** | Clock control to 60×, all seven scenario injections, ground-truth state breakdown, venue truth table |
| **Ledger** | Every action with reasoning, predicted effect, measured outcome, status, realised uptake |

Map tiles are served from a single local file, so the console works with no
internet — which matters, because a mega-event is exactly when connectivity
fails.

---

## Scenarios

All seven the brief names, injectable from the Simulator page or by API:

```bash
curl -sX POST localhost:8100/inject/hotel_saturation      # demand floods the near zones
curl -sX POST localhost:8100/inject/transport_congestion  # a rail corridor loses capacity
curl -sX POST localhost:8100/inject/demand_spike          # unregistered walk-ins
curl -sX POST localhost:8100/inject/venue_capacity        # darshan throughput drops
curl -sX POST localhost:8100/inject/last_mile             # station access cut
curl -sX POST localhost:8100/inject/uneven_distribution   # everyone to one immersion point
curl -sX POST localhost:8100/inject/schedule_change       # procession time moves
curl -sX POST localhost:8100/clear
```

### Demo sequence

Freeze the clock around the approval, or the audience cannot distinguish your
intervention from the passage of time.

```bash
curl -sX POST localhost:8100/reset
curl -sX POST localhost:8100/start
curl -sX POST "localhost:8100/speed?x=15"
# ... wait for the pressure board to turn red ...
curl -sX POST localhost:8100/pause
curl -s localhost:8100/truth | python -m json.tool | head -20   # before
# approve an action in the console
curl -s localhost:8100/truth | python -m json.tool | head -20   # after
curl -sX POST localhost:8100/start
```

---

## API reference

**API — :8000** · `/stats` `/zones` `/venues` `/stops` `/schedule`
`/venues/{id}/nearby-supply`

**Simulator — :8100** · `/state` `/truth` `/scenarios` `/start` `/pause`
`/reset` `/speed?x=` `/inject/{scenario}` `/clear` `/divert`

**Twin — :8200** · `/health` `/pressure` `/pressure/zones` `/forecast/{node_id}`
`/profile`

**Optimizer — :8300** · `/health` `/proposals` `/actions`
`/actions/{id}/approve` `/actions/{id}/dismiss` `/uptake`

Interactive docs at `/docs` on each service.

---

## Data

**Real geography, synthetic people.** Every static thing is real and free.
Every dynamic thing is generated, because live hotel occupancy, ticketing and
telco feeds are commercially closed or legally restricted — and saying so
plainly is more defensible than implying access we do not have.

| Real | Source |
|---|---|
| Street and footpath network | OpenStreetMap via Geofabrik |
| Accommodation and service POIs | OpenStreetMap — 4,407 POIs |
| Rail and metro stations | OpenStreetMap, matched by name |
| Bus network and timetable | Mobility Database GTFS |
| Map tiles | Protomaps |
| Weather | Open-Meteo |
| Hotel and tourism baselines | data.gov.in, Ministry of Tourism |

| Generated | Why |
|---|---|
| Accommodation inventory, occupancy, dynamic pricing | OTA data is closed |
| Attendee cohorts and arrival curves | No ticketing access |
| Venue occupancy and queue telemetry | No CCTV or gate feeds |
| Corridor loads | No AFC or telco data |
| Incentive responses | Needs an elasticity model |

Synthetic supply is anchored to real geography: room counts expand from actual
OSM hotel locations and are scaled to match the published city total, so the
inventory is a calibrated expansion of real points rather than an invention.

---

## Known issues

**Fix before demonstrating**

- Venue coordinates in `config/event_ganesh.yaml` are approximate and the
  festival `start_date` is unverified. `03_seed_event.py` logs a snap distance
  per venue and warns above 500 m — read those warnings.
- Immersion-point `service_rate` was authored as *idols per hour* but every
  consumer treats `service_rate` as *people per minute*. Pandal rates are
  correct.
- `TARGET_TOTAL_ROOMS` in `config/settings.py` is still a placeholder.
- Capacities and service rates throughout are modelling assumptions, not
  measured figures. Say so if asked.

**Operational gotchas**

- Redis survives simulator restarts, so a fresh simulator reads phantom queues
  from a previous session. `/reset` flushes it; manually
  `docker exec -it me-redis redis-cli FLUSHDB`.
- Services load code once at startup. Use `--reload`; the ingest worker has no
  equivalent and always needs a manual restart.
- MapLibre v5 has no default export — `import * as maplibregl`.
- Vite's dependency optimizer breaks MapLibre's worker;
  `vite.config.ts` needs `optimizeDeps: { exclude: ['maplibre-gl'] }`.

---

## Roadmap

| Priority | Work | Why |
|---|---|---|
| 1 | Attendee application | Half the brief by definition, and the delivery channel the zone optimizer needs |
| 2 | Corridor pressure over the edge network | 389,963 edges carry capacity attributes and the twin projects venues and zones only — the largest correctness gap |
| 3 | Counterfactual evaluator | Turns "we moved 3,361 people" into "which spared 40,000 from giving up" |
| 4 | OSRM preprocessing | Real routing instead of straight-line distance × 1.35 |
| 5 | Retrieval copilot | Grounds recommendations in the event's own permits, site plan and standard procedures |

---

## Acknowledgements

Built on OpenStreetMap contributors' data, the Mobility Database, Protomaps,
Open-Meteo, and India's open government data portals. Pedestrian dynamics
constants are drawn from published measurement literature rather than invented.
