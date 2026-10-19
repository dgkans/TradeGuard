import { useId, useState, type FormEvent } from "react";
import { useApi } from "../api";
import type { SessionInfo } from "../types";
import { ErrorNote } from "./common";

const MIN_CASH = 1000;
const MAX_CASH = 1_000_000;

const PROFILES = [
  { policy: "default", label: "Standard" },
  { policy: "strict", label: "Strict" },
];

interface Props {
  sessions: SessionInfo[];
  activeId: string | null;
  onSelect: (sessionId: string) => void;
  onStarted: (session: SessionInfo) => void;
  onEnded: (sessionId: string) => void;
}

function status(session: SessionInfo): { text: string; tone: string } {
  if (session.waiting_for_approval) return { text: "Waiting for approval", tone: "escalate" };
  if (session.busy) return { text: "Working…", tone: "busy" };
  return { text: "Ready", tone: "allow" };
}

/** Traders with open sessions (select one, end any), and a form to start a new one. */
export function TraderBar({ sessions, activeId, onSelect, onStarted, onEnded }: Props) {
  const api = useApi();
  const [name, setName] = useState("");
  const [policy, setPolicy] = useState("default");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [showForm, setShowForm] = useState(false);
  const [cash, setCash] = useState("10000");
  const ids = { name: useId(), policy: useId(), list: useId(), cash: useId(), cashHint: useId() };
  const cashValue = Number(cash);
  const cashOk = Number.isFinite(cashValue) && cashValue >= MIN_CASH && cashValue <= MAX_CASH;
  const formOpen = sessions.length === 0 || showForm;

  async function start(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || starting || !cashOk) return;
    setStarting(true);
    setError(null);
    try {
      onStarted(await api.createSession(name.trim(), policy, cashValue));
      setName("");
      setShowForm(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setStarting(false);
    }
  }

  async function end(session: SessionInfo) {
    try {
      await api.closeSession(session.session_id);
    } catch {
      // Already gone (API restarted): drop it from the list anyway.
    }
    onEnded(session.session_id);
  }

  return (
    <aside className="trader-bar">
      {formOpen ? null : (
        <button type="button" className="button button-quiet new-toggle" aria-expanded={false}
                onClick={() => setShowForm(true)}>
          + New session
        </button>
      )}
      {formOpen ? (
      <form className="new-session" onSubmit={(event) => void start(event)}>
        <h2>New session</h2>
        <label htmlFor={ids.name} className="field-label">Trader name</label>
        <input id={ids.name} className="input" value={name} maxLength={40}
               placeholder="e.g. Asha" onChange={(event) => setName(event.target.value)} />
        <label htmlFor={ids.policy} className="field-label">Risk profile</label>
        <select id={ids.policy} className="input" value={policy}
                onChange={(event) => setPolicy(event.target.value)}>
          {PROFILES.map((profile) => (
            <option key={profile.policy} value={profile.policy}>{profile.label}</option>
          ))}
        </select>
        <label htmlFor={ids.cash} className="field-label">Starting cash ($)</label>
        <input id={ids.cash} className="input" type="number" inputMode="numeric"
               min={MIN_CASH} max={MAX_CASH} step={1000} value={cash}
               aria-describedby={ids.cashHint} aria-invalid={!cashOk}
               onChange={(event) => setCash(event.target.value)} />
        <span id={ids.cashHint} className={`hint ${cashOk ? "" : "hint-bad"}`}>
          Between $1,000 and $1,000,000. New accounts start with no positions.
        </span>
        <button type="submit" className="button" disabled={starting || !name.trim() || !cashOk}>
          {starting ? "Starting…" : "Start session"}
        </button>
        <ErrorNote error={error} />
      </form>
      ) : null}
      <h2 id={ids.list}>Traders</h2>
      {sessions.length === 0 ? (
        <p className="muted small">No one is trading yet. Start a session above.</p>
      ) : null}
      <ul className="trader-list" aria-labelledby={ids.list}>
        {sessions.map((session) => {
          const state = status(session);
          const active = session.session_id === activeId;
          return (
            <li key={session.session_id} className={`trader ${active ? "trader-active" : ""}`}>
              <button type="button" className="trader-name" aria-pressed={active}
                      onClick={() => onSelect(session.session_id)}>
                {session.user}
              </button>
              <span className={`dot dot-${state.tone}`} aria-hidden="true" />
              <span className="trader-status">{state.text}</span>
              <button type="button" className="link-button end"
                      aria-label={`End ${session.user}'s session`}
                      onClick={() => void end(session)}>
                End
              </button>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
