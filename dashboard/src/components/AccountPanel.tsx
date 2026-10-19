import { useId, useState, type FormEvent } from "react";
import { useApi } from "../api";
import { money } from "../format";
import type { Me } from "../types";
import { ErrorNote } from "./common";

const MIN_CASH = 1000;
const MAX_CASH = 1_000_000;

/** Left panel: log in or sign up when signed out; the account card when signed in. */
export function AccountPanel({ me, onAuthed, onLoggedOut }: {
  me: Me | null; onAuthed: () => void; onLoggedOut: () => void;
}) {
  const api = useApi();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [display, setDisplay] = useState("");
  const [profile, setProfile] = useState("default");
  const [cash, setCash] = useState("10000");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ids = { user: useId(), pass: useId(), display: useId(), profile: useId(), cash: useId(),
                hint: useId() };
  const cashValue = Number(cash);
  const cashOk = Number.isFinite(cashValue) && cashValue >= MIN_CASH && cashValue <= MAX_CASH;

  if (me) {
    const trader = me.trader;
    const state = me.session.waiting_for_approval ? "Waiting for your approval"
      : me.session.busy ? "The agent is working…" : "Ready";
    return (
      <aside className="trader-bar account">
        <h2>Signed in</h2>
        <div className="account-card">
          <strong className="account-name">{trader?.display_name ?? me.session.user}</strong>
          <span className="muted small">@{trader?.username}</span>
          <dl className="facts">
            <dt>Profile</dt>
            <dd>{trader?.profile === "strict" ? "Strict" : "Standard"}</dd>
            <dt>Started with</dt>
            <dd>{trader ? money(trader.starting_cash) : "–"}</dd>
            <dt>Status</dt>
            <dd>{state}</dd>
          </dl>
          <button type="button" className="button button-quiet"
                  onClick={async () => {
                    await api.logout().catch(() => undefined);
                    onLoggedOut();
                  }}>
            Log out
          </button>
        </div>
        <p className="muted small">Your account, positions, limits and chat are saved.</p>
      </aside>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        await api.login(username.trim(), password);
      } else {
        await api.signup({ username: username.trim(), password, starting_cash: cashValue,
                           profile, display_name: display.trim() || undefined });
      }
      setPassword("");
      onAuthed();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  const signup = mode === "signup";
  const ready = username.trim().length >= 3 && password.length >= (signup ? 8 : 1) &&
                (!signup || cashOk);
  return (
    <aside className="trader-bar">
      <div className="tabs" role="tablist" aria-label="Account">
        <button type="button" role="tab" aria-selected={!signup}
                className={`tab ${signup ? "" : "tab-active"}`}
                onClick={() => setMode("login")}>Log in</button>
        <button type="button" role="tab" aria-selected={signup}
                className={`tab ${signup ? "tab-active" : ""}`}
                onClick={() => setMode("signup")}>Sign up</button>
      </div>
      <form className="new-session" onSubmit={(event) => void submit(event)}>
        <label htmlFor={ids.user} className="field-label">Username</label>
        <input id={ids.user} className="input" value={username} maxLength={24}
               autoComplete="username" placeholder="letters, numbers, _"
               onChange={(event) => setUsername(event.target.value)} />
        <label htmlFor={ids.pass} className="field-label">Password</label>
        <input id={ids.pass} className="input" type="password" value={password} maxLength={200}
               autoComplete={signup ? "new-password" : "current-password"}
               placeholder={signup ? "at least 8 characters" : ""}
               onChange={(event) => setPassword(event.target.value)} />
        {signup ? (
          <>
            <label htmlFor={ids.display} className="field-label">Display name (optional)</label>
            <input id={ids.display} className="input" value={display} maxLength={40}
                   onChange={(event) => setDisplay(event.target.value)} />
            <label htmlFor={ids.profile} className="field-label">Risk profile</label>
            <select id={ids.profile} className="input" value={profile}
                    onChange={(event) => setProfile(event.target.value)}>
              <option value="default">Standard</option>
              <option value="strict">Strict</option>
            </select>
            <label htmlFor={ids.cash} className="field-label">Starting cash ($)</label>
            <input id={ids.cash} className="input" type="number" min={MIN_CASH} max={MAX_CASH}
                   step={1000} value={cash} aria-describedby={ids.hint} aria-invalid={!cashOk}
                   onChange={(event) => setCash(event.target.value)} />
            <span id={ids.hint} className={`hint ${cashOk ? "" : "hint-bad"}`}>
              Between $1,000 and $1,000,000. New accounts start with no positions.
            </span>
          </>
        ) : null}
        <button type="submit" className="button" disabled={busy || !ready}>
          {busy ? "One moment…" : signup ? "Create account" : "Log in"}
        </button>
        <ErrorNote error={error} />
      </form>
    </aside>
  );
}
