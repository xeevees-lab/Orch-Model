import { useState } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, ReferenceLine,
} from "recharts";
import MapView from "./MapView";
import {
  api, usePoll, ttsLabel, timeOnly, clockLabel,
  type PressureRow, type ZoneRow, type ActionRow, type Scenario,
} from "./api";

const CHART = {
  grid: "#e8e5de",
  axis: "#8a8f98",
  marigold: "#c8890f",
  teal: "#0f8b7e",
  coral: "#cf4429",
};

// ================================================================ bits

export function Panel({
  title, note, error, children,
}: {
  title: string; note?: string;
  error?: string | null | false; children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <header>
        <h2>{title}</h2>
        {note && <span>{note}</span>}
      </header>
      {error ? <div className="panel-error">{error}</div> : children}
    </section>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Stat({
  label, value, tone, hint,
}: {
  label: string; value: React.ReactNode;
  tone?: "good" | "bad"; hint?: string;
}) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <strong>{value}</strong>
      <span>{label}</span>
      {hint && <em>{hint}</em>}
    </div>
  );
}

// ================================================================ panels

export function PressureBoard({
  rows, error, selected, onSelect, full,
}: {
  rows: PressureRow[]; error: string | null;
  selected: number | null; onSelect: (id: number) => void; full?: boolean;
}) {
  const shown = full
    ? rows
    : rows.filter((r) => r.queue > 0 || r.band !== "ok").slice(0, 8);
  return (
    <Panel
      title="Pressure board"
      note="Shortest fuse first"
      error={error && "Twin unreachable on :8200"}
    >
      {shown.length === 0 && <Empty>Nothing under pressure.</Empty>}
      <table className="board">
        <tbody>
          {shown.map((r) => (
            <tr
              key={r.node_id}
              className={`${r.band} ${selected === r.node_id ? "sel" : ""}`}
              onClick={() => onSelect(r.node_id)}
            >
              <td className="nm">
                {r.name}
                <em>{r.binding === "throughput"
                  ? "throughput-bound" : "space-bound"}</em>
              </td>
              <td className="num">{r.queue.toLocaleString()}<em>queue</em></td>
              <td className="num">{r.queue_hours}h<em>wait</em></td>
              <td className="num tts">{ttsLabel(r.tts_min)}<em>to limit</em></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

export function ActionPanel({
  rows, error, onDone,
}: {
  rows: ActionRow[]; error: string | null; onDone?: () => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const act = async (id: number, kind: "approve" | "dismiss") => {
    setBusy(id);
    try {
      const r = kind === "approve" ? await api.approve(id) : await api.dismiss(id);
      const moved = r?.result?.moved;
      setResult(
        kind === "dismiss" ? "Dismissed"
          : moved !== undefined
            ? `Moved ${Number(moved).toLocaleString()} of ${Number(r.result.asked).toLocaleString()} asked`
            : "Approved, but the simulator did not apply it"
      );
      onDone?.();
    } catch (e) {
      setResult((e as Error).message);
    }
    setBusy(null);
  };

  return (
    <Panel
      title="Recommended actions"
      note="Approve to apply"
      error={error && "Optimizer unreachable on :8300"}
    >
      {rows.length === 0 && <Empty>No action needed right now.</Empty>}
      {result && <div className="result">{result}</div>}
      {rows.slice(0, 5).map((a) => {
        const p = a.payload ?? {};
        const d = a.predicted_delta ?? {};
        return (
          <div className="action" key={a.action_id}>
            <div className="what">
              {p.kind === "divert" ? (
                <>Move <b>{Number(p.people).toLocaleString()}</b> from{" "}
                  <b>{p.from_name}</b> to <b>{p.to_name}</b>
                  <span className="meta">{p.detour_min} min away</span></>
              ) : p.kind === "zone_shift" ? (
                <>Route <b>{Number(p.people).toLocaleString()}</b> arrivals to{" "}
                  <b>zone {p.zone_id}</b>
                  <span className="meta">
                    ₹{Number(p.price_inr).toLocaleString()} · {p.travel_min} min
                  </span></>
              ) : (
                <>Offer to <b>{Number(p.push_to).toLocaleString()}</b> people
                  <span className="meta">
                    for {Number(p.divert_target).toLocaleString()} diversions
                  </span></>
              )}
            </div>
            <div className="why">{a.trigger_reason}</div>
            {d?.hours_saved_per_person !== undefined && (
              <div className="effect">
                Saves <b>{d.hours_saved_per_person}h</b> per person
              </div>
            )}
            <div className="buttons">
              <button className="approve" disabled={busy === a.action_id}
                onClick={() => act(a.action_id, "approve")}>
                {busy === a.action_id ? "Applying…" : "Approve"}
              </button>
              <button className="ghost" disabled={busy === a.action_id}
                onClick={() => act(a.action_id, "dismiss")}>Dismiss</button>
            </div>
          </div>
        );
      })}
    </Panel>
  );
}

export function ZonePanel({ rows }: { rows: ZoneRow[] }) {
  const tight = rows.filter((r) => r.occupancy > 0).slice(0, 6);
  const spare = [...rows]
    .filter((r) => r.rooms_available > 0)
    .sort((a, b) => b.rooms_available - a.rooms_available)
    .slice(0, 3);
  return (
    <Panel title="Accommodation" note="Where the rooms are">
      {tight.length === 0 && <Empty>No occupancy recorded yet.</Empty>}
      {tight.map((z) => (
        <div className="zrow" key={z.zone_id}>
          <span className="zid">{z.zone_id}</span>
          <div className="zbar">
            <i style={{ width: `${Math.min(z.occupancy * 100, 100)}%` }} />
          </div>
          <span className="znum">{Math.round(z.occupancy * 100)}%</span>
          <span className="zprice">₹{Math.round(z.price_inr).toLocaleString()}</span>
        </div>
      ))}
      {spare.length > 0 && (
        <div className="spare">
          Most headroom:{" "}
          {spare.map((z) =>
            `${z.zone_id} (${z.rooms_available.toLocaleString()})`).join(", ")}
        </div>
      )}
    </Panel>
  );
}

// ================================================================ overview

export function Overview({
  board, boardError, actions, actionsError, zones, selected, onSelect,
}: {
  board: PressureRow[]; boardError: string | null;
  actions: ActionRow[]; actionsError: string | null;
  zones: ZoneRow[]; selected: number | null; onSelect: (id: number) => void;
}) {
  return (
    <div className="split">
      <MapView board={board} selected={selected} onSelect={onSelect} />
      <aside className="rail">
        <PressureBoard rows={board} error={boardError}
          selected={selected} onSelect={onSelect} />
        <ActionPanel rows={actions} error={actionsError} />
        <ZonePanel rows={zones} />
      </aside>
    </div>
  );
}

// ================================================================ analysis

export function Analysis({
  board, selected, onSelect,
}: {
  board: PressureRow[]; selected: number | null; onSelect: (id: number) => void;
}) {
  const target = selected ?? board[0]?.node_id ?? null;
  const forecast = usePoll(
    () => api.forecast(target as number), 5000, target !== null);
  const profile = usePoll(api.profile, 30000);
  const health = usePoll(api.twinHealth, 5000);
  const uptake = usePoll(api.uptake, 5000);
  const zones = usePoll(api.zonePressure, 6000);

  const series = (forecast.data?.series ?? []).map((s) => ({
    t: timeOnly(s.ts),
    queue: s.queue,
    pi: s.pressure_index,
  }));
  const hours = Object.entries(profile.data ?? {})
    .map(([h, v]) => ({ h: `${h}:00`, v: Number(v.toFixed(2)) }));
  const learn = (uptake.data?.history ?? []).map((r, i) => ({
    n: i + 1,
    observed: r.observed,
    estimate: r.estimate,
  }));
  const zoneBars = (zones.data ?? [])
    .filter((z) => z.occupancy > 0)
    .slice(0, 12)
    .map((z) => ({ z: z.zone_id, occ: Math.round(z.occupancy * 100) }));

  return (
    <div className="page">
      <div className="stats">
        <Stat label="Forecast error" hint="mean absolute, people/min"
          value={health.data?.forecast_mae_ppm ?? "—"} />
        <Stat label="Samples scored" value={health.data?.samples_scored ?? 0} />
        <Stat label="Twin runs" value={health.data?.runs ?? 0} />
        <Stat label="Uptake estimate"
          value={`${Math.round((uptake.data?.estimate ?? 0) * 100)}%`}
          hint="learned from approvals" />
      </div>

      <div className="grid2">
        <Panel
          title={`Queue forecast — ${forecast.data?.name ?? "select a venue"}`}
          note="120 min ahead"
        >
          {series.length === 0 ? (
            <Empty>No forecast yet. Run the simulator for a minute.</Empty>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={series}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="t" stroke={CHART.axis} fontSize={11}
                  interval={4} tickLine={false} />
                <YAxis stroke={CHART.axis} fontSize={11} tickLine={false}
                  width={54} />
                <Tooltip />
                <Line type="monotone" dataKey="queue" dot={false}
                  stroke={CHART.marigold} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          )}
          <div className="chips">
            {board.slice(0, 6).map((r) => (
              <button key={r.node_id}
                className={`chip ${target === r.node_id ? "on" : ""}`}
                onClick={() => onSelect(r.node_id)}>
                {r.name.split(" (")[0]}
              </button>
            ))}
          </div>
        </Panel>

        <Panel title="Pressure trajectory" note="1.0 is the line">
          {series.length === 0 ? <Empty>No data yet.</Empty> : (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={series}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="t" stroke={CHART.axis} fontSize={11}
                  interval={4} tickLine={false} />
                <YAxis stroke={CHART.axis} fontSize={11} tickLine={false}
                  width={40} />
                <Tooltip />
                <ReferenceLine y={1} stroke={CHART.coral} strokeDasharray="4 3" />
                <Line type="monotone" dataKey="pi" dot={false}
                  stroke={CHART.coral} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </Panel>

        <Panel title="Learned arrival profile" note="hour of day, from telemetry">
          {hours.length === 0 ? <Empty>Not learned yet.</Empty> : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={hours}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="h" stroke={CHART.axis} fontSize={10}
                  interval={2} tickLine={false} />
                <YAxis stroke={CHART.axis} fontSize={11} tickLine={false}
                  width={36} />
                <Tooltip />
                <Bar dataKey="v" fill={CHART.marigold} radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Panel>

        <Panel title="Incentive controller" note="asked vs accepted">
          {learn.length === 0 ? (
            <Empty>Approve a diversion to start the controller learning.</Empty>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={learn}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="n" stroke={CHART.axis} fontSize={11}
                  tickLine={false} />
                <YAxis stroke={CHART.axis} fontSize={11} tickLine={false}
                  width={40} />
                <Tooltip />
                <Line dataKey="observed" dot stroke={CHART.axis}
                  strokeWidth={1} strokeDasharray="3 3" />
                <Line dataKey="estimate" dot={false} stroke={CHART.teal}
                  strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </Panel>

        <Panel title="Zone occupancy" note="top zones by fill" >
          {zoneBars.length === 0 ? <Empty>No occupancy yet.</Empty> : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={zoneBars}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="z" stroke={CHART.axis} fontSize={10}
                  tickLine={false} />
                <YAxis stroke={CHART.axis} fontSize={11} tickLine={false}
                  width={36} unit="%" />
                <Tooltip />
                <Bar dataKey="occ" fill={CHART.teal} radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </Panel>

        <PressureBoard rows={board} error={null} selected={selected}
          onSelect={onSelect} full />
      </div>
    </div>
  );
}

// ================================================================ simulator

export function Simulator({ scenarios }: { scenarios: Scenario[] }) {
  const state = usePoll(api.simState, 1200);
  const truth = usePoll(api.truth, 1500);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>, note?: string) => {
    setBusy(true);
    try { await fn(); if (note) setMsg(note); }
    catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  };

  const s = state.data;
  const t = truth.data;
  const states = Object.entries(t?.by_state ?? {});
  const totalMoving = states.reduce((a, [, v]) => a + v, 0) || 1;

  return (
    <div className="page">
      <div className="stats">
        <Stat label="Event clock" value={clockLabel(s?.clock)} />
        <Stat label="In system"
          value={(s?.people_in_system ?? 0).toLocaleString()} />
        <Stat label="Gave up" tone={t?.balked_total ? "bad" : undefined}
          value={(t?.balked_total ?? 0).toLocaleString()}
          hint="wanted darshan, left" />
        <Stat label="Events emitted"
          value={(s?.events_emitted ?? 0).toLocaleString()} />
        <Stat label="Stream" tone={s?.kafka ? "good" : "bad"}
          value={s?.kafka ? "connected" : "offline"} />
      </div>

      <div className="grid2">
        <Panel title="Transport" note="drive the clock">
          {msg && <div className="result">{msg}</div>}
          <div className="transport">
            <button className={s?.running ? "ghost" : "approve"} disabled={busy}
              onClick={() => run(s?.running ? api.pause : api.start)}>
              {s?.running ? "Pause" : "Run"}
            </button>
            {[1, 4, 15, 60].map((x) => (
              <button key={x} disabled={busy}
                className={s?.speed === x ? "speed on" : "speed"}
                onClick={() => run(() => api.speed(x))}>{x}×</button>
            ))}
            <button className="ghost" disabled={busy}
              onClick={() => run(api.reset, "World reset and Redis flushed")}>
              Reset
            </button>
          </div>
          <p className="note">
            One tick is one minute of event time. At 1× the clock advances
            about four minutes per second, so a full festival day takes
            roughly six minutes to watch.
          </p>
        </Panel>

        <Panel title="Where everyone is" note="ground truth">
          {states.length === 0 ? <Empty>Nothing in the system.</Empty> : (
            <>
              <div className="statebar">
                {states.map(([k, v]) => (
                  <i key={k} className={`seg ${k}`}
                    style={{ width: `${(v / totalMoving) * 100}%` }}
                    title={`${k}: ${v.toLocaleString()}`} />
                ))}
              </div>
              {states.map(([k, v]) => (
                <div className="zrow" key={k}>
                  <span className="zid wide">{k.replace(/_/g, " ")}</span>
                  <span className="znum wide">{v.toLocaleString()}</span>
                </div>
              ))}
              <p className="note">
                This is the simulator's own record. The platform never reads
                it — it only sees the noisy telemetry the venues publish,
                which is why its forecast error is a real number.
              </p>
            </>
          )}
        </Panel>

        <Panel title="Inject a scenario" note="the seven the brief names">
          <div className="scenarios">
            {scenarios.map((sc) => (
              <button key={sc.id} className="scenario" disabled={busy}
                onClick={() => run(() => api.inject(sc.id), `Injected ${sc.label}`)}>
                <b>{sc.label}</b>
                <em>{sc.description}</em>
              </button>
            ))}
          </div>
          {(s?.active_effects?.length ?? 0) > 0 && (
            <>
              <div className="legend-title spaced">Running now</div>
              {s!.active_effects.map((e, i) => (
                <div className="effect-row" key={i}>
                  <span>{e.kind.replace(/_/g, " ")}</span>
                  <b>{e.remaining_min}m left</b>
                </div>
              ))}
              <button className="ghost wide" disabled={busy}
                onClick={() => run(api.clearScenarios, "Scenarios cleared")}>
                Clear all
              </button>
            </>
          )}
        </Panel>

        <Panel title="Venues" note="truth, not estimate">
          <table className="board">
            <tbody>
              {(t?.venues ?? []).slice(0, 8).map((v) => (
                <tr key={v.node_id}>
                  <td className="nm">{v.name}<em>{v.kind}</em></td>
                  <td className="num">{v.queue.toLocaleString()}<em>queue</em></td>
                  <td className="num">{v.wait_min}m<em>wait</em></td>
                  <td className="num">{v.balked.toLocaleString()}<em>left</em></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}

// ================================================================ ledger

export function Ledger() {
  const actions = usePoll(api.allActions, 4000);
  const rows = actions.data ?? [];
  const done = rows.filter((r) => r.status === "executed");
  const moved = done.reduce(
    (a, r) => a + Number(r.measured_delta?.moved ?? 0), 0);
  const asked = done.reduce(
    (a, r) => a + Number(r.measured_delta?.asked ?? 0), 0);

  return (
    <div className="page">
      <div className="stats">
        <Stat label="Proposed" value={rows.length} />
        <Stat label="Executed" value={done.length} />
        <Stat label="People moved" value={moved.toLocaleString()} tone="good" />
        <Stat label="Realised uptake" hint="moved ÷ asked"
          value={asked ? `${Math.round((moved / asked) * 100)}%` : "—"} />
      </div>

      <Panel title="Action ledger"
        note="audit trail, evaluation set, after-action report"
        error={actions.error && "Optimizer unreachable on :8300"}>
        {rows.length === 0 && <Empty>No actions recorded yet.</Empty>}
        <table className="ledger">
          <thead>
            <tr>
              <th>#</th><th>Action</th><th>Why</th>
              <th>Predicted</th><th>Measured</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const p = r.payload ?? {};
              const d = r.predicted_delta ?? {};
              const m = r.measured_delta;
              return (
                <tr key={r.action_id} className={r.status}>
                  <td className="dim">{r.action_id}</td>
                  <td>
                    {p.kind === "divert"
                      ? `Move ${Number(p.people).toLocaleString()} to ${p.to_name}`
                      : p.kind === "zone_shift"
                        ? `Route ${Number(p.people).toLocaleString()} to ${p.zone_id}`
                        : `Offer to ${Number(p.push_to ?? 0).toLocaleString()}`}
                  </td>
                  <td className="dim small">{r.trigger_reason}</td>
                  <td className="num">
                    {d.hours_saved_per_person !== undefined
                      ? `${d.hours_saved_per_person}h saved` : "—"}
                  </td>
                  <td className="num">
                    {m?.moved !== undefined
                      ? `${Number(m.moved).toLocaleString()} moved`
                      : m ? "not applied" : "—"}
                  </td>
                  <td><span className={`tag ${r.status}`}>{r.status}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
