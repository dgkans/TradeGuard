import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useApi } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { ErrorNote } from "../components/common";
import { RichText } from "../components/RichText";
import type {
  ApprovalCardData,
  SessionInfo,
  StepEntry,
  TranscriptEntry,
  TurnResult,
} from "../types";

const SUGGESTIONS = ["What is NVDA at?", "Buy $500 of AMD", "Buy $1500 of NVDA",
                     "What are my positions?"];

const ICONS: Record<StepEntry["status"], string> = {
  ok: "✓",
  blocked: "⛔",
  held: "⏸",
  failed: "!",
};

function turnEntries(turn: TurnResult): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [...turn.steps];
  if (turn.decision) entries.push(turn.decision);
  if (turn.pending_approval) entries.push({ role: "approval", ...turn.pending_approval });
  else entries.push({ role: "agent", text: turn.answer ?? "" });
  return entries;
}

function Step({ step }: { step: StepEntry }) {
  if (step.status === "blocked") {
    return (
      <div className="notice notice-blocked" role="status" aria-label={step.text}>
        <div className="notice-title">
          <span aria-hidden="true">{ICONS.blocked}</span> {step.title ?? "Stopped by your guard"}
        </div>
        <div>{step.text}</div>
        {step.reason ? <div className="muted small">{step.reason}</div> : null}
      </div>
    );
  }
  return (
    <div className={`step step-${step.status}`}>
      <span className="step-icon" aria-hidden="true">{ICONS[step.status]}</span>
      <span>{step.text}</span>
    </div>
  );
}

function Entry({ entry }: { entry: TranscriptEntry }) {
  switch (entry.role) {
    case "user":
      return <div className="bubble bubble-user">{entry.text}</div>;
    case "agent":
      return (
        <div className="bubble bubble-agent">
          {entry.text ? <RichText text={entry.text} /> : <p>Done.</p>}
        </div>
      );
    case "step":
      return <Step step={entry} />;
    case "approval":
      return (
        <div className="step step-held">
          <span className="step-icon" aria-hidden="true">{ICONS.held}</span>
          <span>{entry.title}</span>
        </div>
      );
    case "decision":
      return <div className={`decision decision-${entry.status}`}>{entry.text}</div>;
  }
}

/** One trader's conversation with the guarded agent. */
export function ChatPage({ session }: { session: SessionInfo }) {
  const api = useApi();
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [pending, setPending] = useState<ApprovalCardData | null>(null);
  const [text, setText] = useState("");
  const [working, setWorking] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [cardError, setCardError] = useState<unknown>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const messageId = useId();
  const sessionId = session.session_id;

  useEffect(() => {
    let live = true;
    api.session().then(
      (state) => {
        if (!live) return;
        setTranscript(state.transcript);
        setPending(state.pending_approval);
        setLoaded(true);
        if (state.pending_approval) {
          setWorking(true);
          void waitForDecision();
        }
      },
      (failure: unknown) => {
        if (!live) return;
        setError(failure);
        setLoaded(true);
      },
    );
    return () => {
      live = false;
    };
    // waitForDecision only uses stable setters and the api from context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [transcript, pending]);

  async function waitForDecision() {
    try {
      const turn = await api.resume();
      setPending(null);
      setTranscript((current) => [...current, ...turnEntries(turn)]);
    } catch (failure) {
      setError(failure);
    } finally {
      setWorking(false);
    }
  }

  async function send(message: string) {
    if (!message.trim() || working) return;
    setText("");
    setError(null);
    setWorking(true);
    setTranscript((current) => [...current, { role: "user", text: message }]);
    try {
      const turn = await api.send(message);
      setTranscript((current) => [...current, ...turnEntries(turn)]);
      if (turn.pending_approval) {
        setPending(turn.pending_approval);
        setCardError(null);
        void waitForDecision(); // returns once someone approves or rejects
        return;
      }
    } catch (failure) {
      setError(failure);
    }
    setWorking(false);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(text.trim());
  }

  async function decide(approve: boolean, note: string) {
    if (!pending) return;
    try {
      if (approve) await api.approveOwn(pending.approval_id, note);
      else await api.rejectOwn(pending.approval_id, note);
      setCardError(null);
    } catch (failure) {
      setCardError(failure);
    }
  }

  return (
    <section className="chat-page">
      <div className="transcript" aria-live="polite">
        {loaded && transcript.length === 0 ? (
          <div className="welcome">
            <p>
              Hi {session.user}. Ask the agent about a stock or tell it what to trade. Every
              order goes through TradeGuard first.
            </p>
            <div className="chips">
              {SUGGESTIONS.map((suggestion) => (
                <button key={suggestion} type="button" className="chip" disabled={working}
                        onClick={() => void send(suggestion)}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {transcript.map((entry, index) => (
          <Entry key={index} entry={entry} />
        ))}
        {pending ? (
          <ApprovalCard
            trader
            approval={{ id: pending.approval_id, tool: pending.tool, args: pending.args,
                        rule_id: pending.rule_id, reason: pending.reason,
                        expires_at: pending.expires_at ?? undefined }}
            title={pending.title}
            error={cardError}
            onDecide={decide}
          />
        ) : null}
        {working && !pending ? (
          <div className="typing" aria-label="The agent is working">
            <span /> <span /> <span />
          </div>
        ) : null}
        <div ref={endRef} />
      </div>
      <form className="composer" onSubmit={submit}>
        <label htmlFor={messageId} className="visually-hidden">Message</label>
        <textarea id={messageId} className="input" rows={2} maxLength={2000} value={text}
                  placeholder={pending ? "Approve or reject the order above first"
                                       : "Ask or tell the agent what to trade"}
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
      <ErrorNote error={error} />
    </section>
  );
}
