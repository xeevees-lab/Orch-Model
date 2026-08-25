import { useState } from "react";
import "./console.css";
import {
  api, usePoll, clockLabel, ROLES, type Role,
} from "./api";
import { Overview, Analysis, Simulator, Ledger, Stat } from "./views";

type Page = "overview" | "analysis" | "simulator" | "ledger";

const PAGES: { id: Page; label: string; hint: string }[] = [
  { id: "overview", label: "Live map", hint: "where pressure is building" },
  { id: "analysis", label: "Analysis", hint: "forecasts and model accuracy" },
  { id: "simulator", label: "Simulator", hint: "drive the event" },
  { id: "ledger", label: "Ledger", hint: "what was decided, and what happened" },
];

export default function App() {
  const [user, setUser] = useState<{ name: string; role: Role } | null>(null);
  const [page, setPage] = useState<Page>("overview");
  const [selected, setSelected] = useState<number | null>(null);

  const on = user !== null;
  const sim = usePoll(api.simState, 1500, on);
  const truth = usePoll(api.truth, 2000, on);
  const board = usePoll(api.pressure, 2000, on);
  const zones = usePoll(api.zonePressure, 4000, on);
  const actions = usePoll(api.proposed, 3000, on);
  const scenarios = usePoll(api.scenarios, 60000, on);

  if (!user) return <Login onSignIn={setUser} />;

  const s = sim.data;
  const rows = board.data ?? [];
  const critical = rows.filter((r) => r.band === "critical").length;

  return (
    <div className="console">
      <header className="bar">
        <div className="mark">
          <span className={`dot ${s?.running ? "live" : ""}`} />
          <span className="title">Visarjan Orchestration</span>
          <span className="sub">Mumbai · Ganesh Chaturthi</span>
        </div>

        <nav className="nav">
          {PAGES.map((p) => (
            <button
              key={p.id}
              className={page === p.id ? "tab on" : "tab"}
              title={p.hint}
              onClick={() => setPage(p.id)}
            >
              {p.label}
            </button>
          ))}
        </nav>

        <div className="who">
          <div className="figures">
            <Stat label="Clock" value={clockLabel(s?.clock)} />
            <Stat label="Critical" value={critical}
              tone={critical ? "bad" : "good"} />
            <Stat label="Gave up"
              value={(truth.data?.balked_total ?? 0).toLocaleString()}
              tone={truth.data?.balked_total ? "bad" : undefined} />
          </div>
          <button className="user" onClick={() => setUser(null)}>
            <b>{user.name}</b>
            <em>{ROLES[user.role]}</em>
          </button>
        </div>
      </header>

      <main className="content">
        {page === "overview" && (
          <Overview
            board={rows} boardError={board.error}
            actions={actions.data ?? []} actionsError={actions.error}
            zones={zones.data ?? []}
            selected={selected} onSelect={setSelected}
          />
        )}
        {page === "analysis" && (
          <Analysis board={rows} selected={selected} onSelect={setSelected} />
        )}
        {page === "simulator" && (
          <Simulator scenarios={scenarios.data ?? []} />
        )}
        {page === "ledger" && <Ledger />}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------- login

function Login({
  onSignIn,
}: {
  onSignIn: (u: { name: string; role: Role }) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("organiser");
  const [err, setErr] = useState<string | null>(null);

  const submit = () => {
    if (!name.trim()) {
      setErr("Enter your name to continue");
      return;
    }
    onSignIn({ name: name.trim(), role });
  };

  return (
    <div className="login">
      <div className="card">
        <div className="brand">
          <span className="dot live" />
          <div>
            <h1>Visarjan Orchestration</h1>
            <p>Mumbai · Ganesh Chaturthi · command and decision support</p>
          </div>
        </div>

        <label className="field">
          <span>Name</span>
          <input
            value={name}
            placeholder="Who is on shift?"
            onChange={(e) => { setName(e.target.value); setErr(null); }}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        </label>
        {err && <div className="field-error">{err}</div>}

        <label className="field">
          <span>Role</span>
          <div className="roles">
            {(Object.keys(ROLES) as Role[]).map((r) => (
              <button
                key={r}
                className={role === r ? "role on" : "role"}
                onClick={() => setRole(r)}
              >
                {ROLES[r]}
              </button>
            ))}
          </div>
        </label>

        <button className="approve wide" onClick={submit}>
          Open the console
        </button>

        <p className="disclaimer">
          Prototype sign-in. No accounts or permissions are enforced — the
          role sets the label on your session, nothing more.
        </p>
      </div>
    </div>
  );
}
