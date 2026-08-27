-- Mega-Event Hospitality Orchestration - core schema
-- One database, three extensions.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;


-- ---------------------------------------------------------------
-- STATIC WORLD  (built once by the ETL, never changes during a run)
-- ---------------------------------------------------------------

-- Accommodation / activity zones. The unit the optimizer assigns demand to.
CREATE TABLE IF NOT EXISTS zones (
    zone_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- accommodation | mixed | immersion | transit
    geom            GEOMETRY(Polygon, 4326) NOT NULL,
    centroid        GEOMETRY(Point, 4326),
    rooms_total     INTEGER DEFAULT 0,      -- synthetic, calibrated to open data
    beds_total      INTEGER DEFAULT 0,
    median_price    NUMERIC(10, 2),
    area_sqkm       NUMERIC(10, 4)
);
CREATE INDEX IF NOT EXISTS zones_geom_idx ON zones USING GIST (geom);


-- Graph nodes: junctions, gates, pandals, immersion points, transit stops.
CREATE TABLE IF NOT EXISTS nodes (
    node_id         BIGINT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- junction | pandal | immersion | station | stop | holding | gate
    name            TEXT,
    zone_id         TEXT REFERENCES zones(zone_id),
    geom            GEOMETRY(Point, 4326) NOT NULL,
    capacity        INTEGER,                -- people it can hold at once (NULL for plain junctions)
    safe_density    NUMERIC(4, 2) DEFAULT 4.0,   -- persons per sqm before it is unsafe
    service_rate    NUMERIC(10, 2)          -- people per minute it can process (queues, gates)
);
CREATE INDEX IF NOT EXISTS nodes_geom_idx ON nodes USING GIST (geom);
CREATE INDEX IF NOT EXISTS nodes_kind_idx ON nodes (kind);
CREATE INDEX IF NOT EXISTS nodes_zone_idx ON nodes (zone_id);


-- Graph edges: roads, footways, transit links, shuttle routes.
CREATE TABLE IF NOT EXISTS edges (
    edge_id         BIGSERIAL PRIMARY KEY,
    u               BIGINT NOT NULL,
    v               BIGINT NOT NULL,
    mode            TEXT NOT NULL,          -- walk | road | rail | metro | bus | shuttle
    highway         TEXT,
    name            TEXT,
    length_m        NUMERIC(10, 2) NOT NULL,
    lanes           NUMERIC(4, 1),
    width_m         NUMERIC(6, 2),
    capacity_ppm    NUMERIC(10, 2) NOT NULL,   -- people per minute at free flow
    free_flow_min   NUMERIC(10, 3) NOT NULL,   -- traversal time with no congestion
    oneway          BOOLEAN DEFAULT FALSE,
    geom            GEOMETRY(LineString, 4326)
);
CREATE INDEX IF NOT EXISTS edges_geom_idx ON edges USING GIST (geom);
CREATE INDEX IF NOT EXISTS edges_u_idx ON edges (u);
CREATE INDEX IF NOT EXISTS edges_v_idx ON edges (v);
CREATE INDEX IF NOT EXISTS edges_mode_idx ON edges (mode);


-- Points of interest: hotels, restaurants, services. Capacity holders.
CREATE TABLE IF NOT EXISTS pois (
    poi_id          BIGINT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- hotel | hostel | guest_house | restaurant | toilet | water | medical
    name            TEXT,
    zone_id         TEXT REFERENCES zones(zone_id),
    geom            GEOMETRY(Point, 4326) NOT NULL,
    stars           SMALLINT,
    rooms           INTEGER,                -- synthetic for accommodation
    capacity        INTEGER,                -- covers for restaurants, throughput for services
    price_inr       NUMERIC(10, 2),
    tags            JSONB
);
CREATE INDEX IF NOT EXISTS pois_geom_idx ON pois USING GIST (geom);
CREATE INDEX IF NOT EXISTS pois_kind_idx ON pois (kind);
CREATE INDEX IF NOT EXISTS pois_zone_idx ON pois (zone_id);


-- The event schedule. Treated as a decision variable, not read-only input.
CREATE TABLE IF NOT EXISTS schedule (
    item_id         TEXT PRIMARY KEY,
    day             DATE NOT NULL,
    starts_at       TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ NOT NULL,
    kind            TEXT NOT NULL,          -- darshan | procession | immersion | aarti
    origin_node     BIGINT REFERENCES nodes(node_id),
    dest_node       BIGINT REFERENCES nodes(node_id),
    expected_crowd  INTEGER,
    is_movable      BOOLEAN DEFAULT TRUE    -- can the optimizer suggest shifting it?
);
CREATE INDEX IF NOT EXISTS schedule_day_idx ON schedule (day);


-- ---------------------------------------------------------------
-- LIVE STATE  (time-series, written by the ingestion worker)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS node_state (
    ts              TIMESTAMPTZ NOT NULL,
    node_id         BIGINT NOT NULL,
    occupancy       INTEGER NOT NULL,
    inflow_ppm      NUMERIC(10, 2),
    outflow_ppm     NUMERIC(10, 2),
    density         NUMERIC(6, 3),
    pressure_index  NUMERIC(6, 3),
    tts_min         NUMERIC(8, 2),          -- minutes to saturation, NULL if not saturating
    is_forecast     BOOLEAN DEFAULT FALSE
);
SELECT create_hypertable('node_state', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS node_state_node_ts_idx ON node_state (node_id, ts DESC);


CREATE TABLE IF NOT EXISTS edge_state (
    ts              TIMESTAMPTZ NOT NULL,
    edge_id         BIGINT NOT NULL,
    flow_ppm        NUMERIC(10, 2) NOT NULL,
    travel_time_min NUMERIC(10, 3),
    pressure_index  NUMERIC(6, 3),
    tts_min         NUMERIC(8, 2),
    is_forecast     BOOLEAN DEFAULT FALSE
);
SELECT create_hypertable('edge_state', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS edge_state_edge_ts_idx ON edge_state (edge_id, ts DESC);


CREATE TABLE IF NOT EXISTS zone_state (
    ts              TIMESTAMPTZ NOT NULL,
    zone_id         TEXT NOT NULL,
    rooms_occupied  INTEGER,
    rooms_available INTEGER,
    median_price    NUMERIC(10, 2),
    visitors        INTEGER,
    pressure_index  NUMERIC(6, 3),
    is_forecast     BOOLEAN DEFAULT FALSE
);
SELECT create_hypertable('zone_state', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS zone_state_zone_ts_idx ON zone_state (zone_id, ts DESC);


-- ---------------------------------------------------------------
-- DECISIONS  (the action ledger: audit trail + evaluation dataset)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS actions (
    action_id       BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,          -- reroute | zone_shift | shuttle | incentive | schedule_shift | gate
    target_id       TEXT,
    payload         JSONB NOT NULL,
    trigger_reason  TEXT,
    predicted_delta JSONB,                  -- what the twin said would happen
    status          TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | dismissed | executed
    decided_at      TIMESTAMPTZ,
    measured_delta  JSONB                   -- what actually happened
);
CREATE INDEX IF NOT EXISTS actions_status_idx ON actions (status, created_at DESC);


-- RAG corpus for the command-centre copilot.
CREATE TABLE IF NOT EXISTS docs (
    doc_id          BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    chunk           TEXT NOT NULL,
    embedding       VECTOR(768)             -- nomic-embed-text dimensionality
);
CREATE INDEX IF NOT EXISTS docs_embedding_idx
    ON docs USING hnsw (embedding vector_cosine_ops);
