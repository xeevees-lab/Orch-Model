import { useEffect, useRef, useState } from "react";

export const API = "http://localhost:8000";
export const SIM = "http://localhost:8100";
export const TWIN = "http://localhost:8200";
export const OPT = "http://localhost:8300";

export type Band = "ok" | "warning" | "critical";

export type PressureRow = {
  node_id: number;
  name: string;
  kind: string;
  queue: number;
  inside: number;
  queue_hours: number;
  fill: number;
  pressure_index: number;
  peak_pressure_index: number;
  tts_min: number | null;
  binding: "throughput" | "space";
  inflow_ppm: number;
  service_ppm: number;
  band: Band;
};

export type ZoneRow = {
  zone_id: string;
  occupancy: number;
  rooms_available: number;
  price_inr: number;
  fill_rate_ppm: number;
  tts_min: number | null;
  pressure_index: number;
};

export type SimState = {
  running: boolean;
  speed: number;
  clock: string;
  ticks: number;
  cohorts: number;
  people_in_system: number;
  events_emitted: number;
  kafka: boolean;
  active_effects: { kind: string; target: string | null; remaining_min: number }[];
};

export type Truth = {
  clock: string;
  balked_total: number;
  people_in_system: number;
  cohorts: number;
  by_state: Record<string, number>;
  venues: {
    node_id: number; name: string; kind: string;
    queue: number; inside: number; wait_min: number; balked: number;
  }[];
  zones_top: {
    zone_id: string; occupancy_pct: number;
    price_inr: number; rooms_available: number;
  }[];
};

export type Scenario = { id: string; label: string; description: string };

export type ActionRow = {
  action_id: number;
  created_at: string;
  kind: string;
  target_id: string;
  payload: Record<string, any>;
  trigger_reason: string;
  predicted_delta: Record<string, any>;
  measured_delta: Record<string, any> | null;
  status: string;
  decided_at: string | null;
};

export type ForecastPoint = {
  ts: string; queue: number; inside: number;
  queue_hours: number; fill: number; pressure_index: number;
};

export type Role = "organiser" | "hospitality" | "city";

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url.split("/").slice(3).join("/")}`);
  return r.json();
}
async function post<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) throw new Error(`${r.status} ${url.split("/").slice(3).join("/")}`);
  return r.json();
}

export const api = {
  zones: () => get<GeoJSON.FeatureCollection>(`${API}/zones`),
  venues: () => get<GeoJSON.FeatureCollection>(`${API}/venues`),
  stats: () => get<Record<string, number>>(`${API}/stats`),

  simState: () => get<SimState>(`${SIM}/state`),
  truth: () => get<Truth>(`${SIM}/truth`),
  scenarios: () => get<Scenario[]>(`${SIM}/scenarios`),
  start: () => post(`${SIM}/start`),
  pause: () => post(`${SIM}/pause`),
  reset: () => post(`${SIM}/reset`),
  speed: (x: number) => post(`${SIM}/speed?x=${x}`),
  inject: (id: string) => post(`${SIM}/inject/${id}`),
  clearScenarios: () => post(`${SIM}/clear`),

  pressure: () => get<PressureRow[]>(`${TWIN}/pressure`),
  zonePressure: () => get<ZoneRow[]>(`${TWIN}/pressure/zones`),
  twinHealth: () => get<Record<string, any>>(`${TWIN}/health`),
  forecast: (id: number) =>
    get<{ name: string; series: ForecastPoint[] }>(`${TWIN}/forecast/${id}`),
  profile: () => get<Record<string, number>>(`${TWIN}/profile`),

  proposed: () => get<ActionRow[]>(`${OPT}/actions?status=proposed`),
  allActions: () => get<ActionRow[]>(`${OPT}/actions?limit=60`),
  approve: (id: number) => post<any>(`${OPT}/actions/${id}/approve`),
  dismiss: (id: number) => post<any>(`${OPT}/actions/${id}/dismiss`),
  uptake: () =>
    get<{ estimate: number; history: any[] }>(`${OPT}/uptake`),
  optHealth: () => get<Record<string, any>>(`${OPT}/health`),
};

/** Poll a fetcher on an interval. Pass enabled=false to stand it down. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    if (!enabled) return;
    alive.current = true;
    let timer: number;
    const run = async () => {
      try {
        const d = await fn();
        if (alive.current) { setData(d); setError(null); }
      } catch (e) {
        if (alive.current) setError((e as Error).message);
      }
      if (alive.current) timer = window.setTimeout(run, ms);
    };
    run();
    return () => { alive.current = false; window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled]);

  return { data, error };
}

export function clockLabel(iso?: string): string {
  if (!iso) return "--:--";
  return new Date(iso).toLocaleString("en-IN", {
    day: "2-digit", month: "short", hour: "2-digit",
    minute: "2-digit", hour12: false,
  });
}

export function timeOnly(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function ttsLabel(tts: number | null): string {
  if (tts === null) return "—";
  if (tts === 0) return "now";
  if (tts < 60) return `${tts}m`;
  return `${Math.floor(tts / 60)}h ${tts % 60}m`;
}

export const ROLES: Record<Role, string> = {
  organiser: "Event organiser",
  hospitality: "Hospitality operator",
  city: "City authority",
};
