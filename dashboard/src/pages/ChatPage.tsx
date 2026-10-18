import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useApi } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { ErrorNote, PageHeader, StatusBadge } from "../components/common";
import { compactJson } from "../format";
import { reviewerName, useReviewer } from "../reviewer";
import type {
  PendingApproval,
  Scenario,
  SessionInfo,
  TranscriptEntry,
  TurnResult,
} from "../types";

const SESSION_KEY = "tradeguard.chat.session";
const PAPER = "alpaca_paper"; // a real paper account: always guarded, real clock

function rememberSession(id: string | null) {
  try {
    if (id) window.sessionStorage.setItem(SESSION_KEY, id);
    else window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage blocked: the session just isn't restored after a reload.
  }
}

function storedSession(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

function turnEntries(turn: TurnResult): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [...turn.tool_calls];
  if (turn.outcome) entries.push({ role: "decision", outcome: turn.outcome });
  if (turn.pending_approval) entries.push({ role: "approval", ...turn.pending_approval });
  else entries.push({ role: "agent", text: turn.answer ?? "" });
  return entries;
}

function Entry({ entry }: { entry: TranscriptEntry }) {
  switch (entry.role) {
    case "user":
      return <div className="bubble bubble-user">{entry.text}</div>;
    case "agent":
      return <div className="bubble bubble-agent">{entry.text || "(no answer)"}</div>;
    case "tool":
      return (
        <div className={`tool-line ${entry.error ? "tool-error" : ""}`}>
          <code>{entry.tool}</code>
          <span className="args">{compactJson(entry.args)}</span>
          <span className="muted nowrap">{Math.round(entry.latency_ms)} ms</span>
          {entry.error ? <div className="tool-result">{entry.error}</div> : null}
        </div>
      );
    case "approval":
      return (
        <div className="system-line">
          Held for approval: <code>{entry.rule_id}</code>
        </div>
      );
    case "decision":
      return (
        <div className="system-line">
          Approval <StatusBadge status={entry.outcome.status} />
          {entry.outcome.decided_by ? ` · decided by ${entry.outcome.decided_by}` : ""}
          {entry.outcome.note ? ` · ${entry.outcome.note}` : ""}
        </div>
      );
    case "clock":
      return (
        <div className="system-line">
          Clock moved {entry.minutes} min → {String(entry.clock.now ?? "")}
        </div>
      );
  }
}

/** Talk to the agent inside a replay scenario, guarded by the gateway or not. */
export function ChatPage() {
  const api = useApi();
  const [reviewer] = useReviewer();
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [policies, setPolicies] = useState<string[]>(["default"]);
  const [scenario, setScenario] = useState("demo");
  const [guarded, setGuarded] = useState(true);
  const [policy, setPolicy] = useState("default");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [pending, setPending] = useState<PendingApproval | null>(null);
  const [text, setText] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [cardError, setCardError] = useState<unknown>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const ids = { scenario: useId(), policy: useId(), message: useId() };

  useEffect(() => {
    api.scenarios().then(setScenarios, setError);
    api.policies().then(
      (list) => list.length && setPolicies(list.map((item) => item.name)),
      () => undefined, // keep "default"
    );
    const saved = storedSession();
    if (saved) {
      api.session(saved).then(
        (state) => {
          setSession(state);
          setTranscript(state.transcript);
          setPending(state.pending_approval);
        },
        () => rememberSession(null),
      );
    }
  }, [api]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [transcript, pending]);

  const selected = scenarios.find((item) => item.id === scenario);
  const paperSelected = scenario === PAPER;
  const paperSession = session?.scenario === PAPER;

  function chooseScenario(id: string) {
    setScenario(id);
    if (id === PAPER) setGuarded(true);
  }

  async function start() {
    setError(null);
    setWorking(true);
    try {
      const info = await api.createSession(scenario, guarded, policy);
      setSession(info);
      setTranscript([]);
      setPending(null);
      rememberSession(info.session_id);
    } catch (failure) {
      setError(failure);
    } finally {
      setWorking(false);
    }
  }

  async function end() {
    if (!session) return;
    try {
      await api.closeSession(session.session_id);
    } catch {
      // Already gone (API restarted): nothing to close.
    }
    rememberSession(null);
    setSession(null);
    setTranscript([]);
    setPending(null);
  }

  async function waitForDecision(sessionId: string) {
    try {
      const turn = await api.resume(sessionId);
      setPending(null);
      setTranscript((current) => [...current, ...turnEntries(turn)]);
    } catch (failure) {
      setError(failure);
    } finally {
      setWorking(false);
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const message = text.trim();
    if (!session || !message || working) return;
    setText("");
    setError(null);
    setWorking(true);
    setTranscript((current) => [...current, { role: "user", text: message }]);
    try {
      const turn = await api.send(session.session_id, message);
      setTranscript((current) => [...current, ...turnEntries(turn)]);
      if (turn.pending_approval) {
        setPending(turn.pending_approval);
        setCardError(null);
        void waitForDecision(session.session_id); // returns once a human decides
        return;
      }
    } catch (failure) {
      setError(failure);
    }
    setWorking(false);
  }

  async function decide(approve: boolean, note: string) {
    if (!pending) return;
    try {
      const name = reviewerName(reviewer);
      if (approve) await api.approve(pending.approval_id, name, note);
      else await api.reject(pending.approval_id, name, note);
      setCardError(null);
    } catch (failure) {
      setCardError(failure);
    }
  }

  async function advance(minutes: number) {
    if (!session) return;
    try {
      const { clock } = await api.advance(session.session_id, minutes);
      setTranscript((current) => [...current, { role: "clock", minutes, clock }]);
    } catch (failure) {
      setError(failure);
    }
  }

  return (
    <section className="chat-page">
      <PageHeader title="Chat">
        Talk to the agent inside a replayed market day. Paper and replay only: no real orders.
      </PageHeader>
      {!session ? (
        <form className="card setup" onSubmit={(event) => { event.preventDefault(); void start(); }}>
          <label htmlFor={ids.scenario} className="field-label">Scenario</label>
          <select id={ids.scenario} className="input" value={scenario}
                  onChange={(event) => chooseScenario(event.target.value)}>
            {scenarios.length === 0 ? <option value="demo">demo</option> : null}
            {scenarios.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id} · {item.category}
              </option>
            ))}
          </select>
          {selected ? (
            <p className="muted small">
              {[selected.description, `watchlist ${selected.watchlist.join(", ")}`,
                `starts ${selected.start_time}`].filter(Boolean).join(" · ")}
            </p>
          ) : null}
          <fieldset className="mode">
            <legend className="field-label">Mode</legend>
            <label>
              <input type="radio" name="mode" checked={guarded} onChange={() => setGuarded(true)} />{" "}
              Guarded (through the gateway)
            </label>
            <label>
              <input type="radio" name="mode" checked={!guarded} disabled={paperSelected}
                     onChange={() => setGuarded(false)} />{" "}
              No guard (direct, the baseline){paperSelected ? " · not for a real account" : ""}
            </label>
          </fieldset>
          {guarded ? (
            <>
              <label htmlFor={ids.policy} className="field-label">Policy</label>
              <select id={ids.policy} className="input" value={policy}
                      onChange={(event) => setPolicy(event.target.value)}>
                {policies.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </>
          ) : null}
          <div className="actions">
            <button type="submit" className="button" disabled={working}>Start session</button>
          </div>
        </form>
      ) : (
        <>
          <div className="session-bar">
            <span>
              <code>{session.scenario}</code>{" "}
              <span className="session-mode">
                {paperSession ? "Alpaca paper account · " : ""}
                {session.guarded ? `Guarded · ${session.policy}` : "No guard"}
              </span>{" "}
              <span className="muted small">{session.model}</span>
            </span>
            <span className="actions">
              {paperSession ? null : (
                <>
                  <button type="button" className="button button-quiet" disabled={working}
                          onClick={() => void advance(5)}>+5 min</button>
                  <button type="button" className="button button-quiet" disabled={working}
                          onClick={() => void advance(60)}>+1 hour</button>
                </>
              )}
              <button type="button" className="button button-quiet" onClick={() => void end()}>
                End session
              </button>
            </span>
          </div>
          <div className="transcript" aria-live="polite">
            {transcript.length === 0 ? (
              <p className="muted">
                Try "What is NVDA at?", "Buy $500 of AMD", or "Buy $1500 of NVDA" (that one needs
                your approval).
              </p>
            ) : null}
            {transcript.map((entry, index) => (
              <Entry key={index} entry={entry} />
            ))}
            {pending ? (
              <ApprovalCard
                approval={{ id: pending.approval_id, tool: pending.tool, args: pending.args,
                            rule_id: pending.rule_id, reason: pending.reason,
                            expires_at: pending.expires_at }}
                title={`Approval needed: ${pending.tool}`}
                error={cardError}
                onDecide={decide}
              />
            ) : null}
            {working && !pending ? <p className="muted">The agent is working…</p> : null}
            <div ref={endRef} />
          </div>
          <form className="composer" onSubmit={(event) => void send(event)}>
            <label htmlFor={ids.message} className="visually-hidden">Message</label>
            <textarea id={ids.message} className="input" rows={2} maxLength={2000} value={text}
                      placeholder={pending ? "Waiting for the approval decision…" : "Ask or instruct the agent"}
                      disabled={working}
                      onChange={(event) => setText(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey) {
                          event.preventDefault();
                          event.currentTarget.form?.requestSubmit();
                        }
                      }} />
            <button type="submit" className="button" disabled={working || !text.trim()}>Send</button>
          </form>
        </>
      )}
      <ErrorNote error={error} />
    </section>
  );
}
