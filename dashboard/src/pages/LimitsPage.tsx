import { useEffect, useId, useState, type FormEvent } from "react";
import { useApi } from "../api";
import { Empty, ErrorNote } from "../components/common";
import type { Limits, LimitsView, RuleCount, SessionInfo } from "../types";

type NumberField = Exclude<keyof Limits, "allowed_symbols">;

const FIELDS: { key: NumberField; label: string; hint: string; min: number; max: number;
                step: number }[] = [
  { key: "max_order_usd", label: "Per order ($)", hint: "The most one order can be worth.",
    min: 100, max: 100_000, step: 100 },
  { key: "max_position_usd", label: "Per stock ($)",
    hint: "The most you can hold in any one stock.", min: 100, max: 500_000, step: 100 },
  { key: "max_exposure_pct", label: "Share of account in stocks (%)",
    hint: "Keeps the rest in cash.", min: 5, max: 100, step: 5 },
  { key: "max_orders_per_day", label: "Orders per day", hint: "Stops runaway trading.",
    min: 1, max: 100, step: 1 },
  { key: "approval_over_usd", label: "Approval over ($)",
    hint: "Orders above this wait for you to approve.", min: 0, max: 100_000, step: 50 },
  { key: "approval_sell_pct", label: "Large sale approval (%)",
    hint: "Selling more than this share of a holding waits for you.", min: 1, max: 100,
    step: 1 },
];

const TEMPLATES = [
  { policy: "default", label: "Standard" },
  { policy: "strict", label: "Strict" },
];

function problems(limits: Limits): string[] {
  const out: string[] = [];
  for (const field of FIELDS) {
    const value = limits[field.key];
    if (!Number.isFinite(value) || value < field.min || value > field.max) {
      out.push(`${field.label.replace(/ \(.*\)/, "")} must be between ${field.min.toLocaleString()} and ${field.max.toLocaleString()}.`);
    }
  }
  if (limits.approval_over_usd > limits.max_order_usd) {
    out.push("The approval line can't be more than your per-order limit.");
  }
  if (limits.max_position_usd < limits.max_order_usd) {
    out.push("The per-stock limit must be at least your per-order limit.");
  }
  if (limits.allowed_symbols.length === 0) out.push("Pick at least one stock.");
  return out;
}

/** The trader's own limits: edit, save, start from a template; protections stay on. */
export function LimitsPage({ session }: { session: SessionInfo }) {
  const api = useApi();
  const [view, setView] = useState<LimitsView | null>(null);
  const [draft, setDraft] = useState<Limits | null>(null);
  const [stopped, setStopped] = useState<RuleCount[]>([]);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ids = { locked: useId(), stopped: useId(), symbols: useId() };
  const sessionId = session.session_id;

  useEffect(() => {
    let live = true;
    api.limits().then(
      (data) => {
        if (!live) return;
        setView(data);
        setDraft(data.limits);
      },
      (failure: unknown) => live && setError(failure),
    );
    api.stopped().then((rules) => live && setStopped(rules), () => undefined);
    return () => {
      live = false;
    };
  }, [api, sessionId]);

  if (!view || !draft) {
    return (
      <section>
        <ErrorNote error={error} />
        {error ? null : <p className="muted">Loading your limits…</p>}
      </section>
    );
  }

  const issues = problems(draft);

  function setNumber(key: NumberField, raw: string) {
    setSaved(false);
    setDraft((current) => (current ? { ...current, [key]: Number(raw) } : current));
  }

  function toggle(symbol: string) {
    setSaved(false);
    setDraft((current) => {
      if (!current) return current;
      const has = current.allowed_symbols.includes(symbol);
      return { ...current, allowed_symbols: has
        ? current.allowed_symbols.filter((item) => item !== symbol)
        : [...current.allowed_symbols, symbol] };
    });
  }

  async function applyTemplate(policy: string) {
    try {
      const template = await api.limits(policy);
      setDraft(template.limits);
      setSaved(false);
    } catch (failure) {
      setError(failure);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || issues.length) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api.setLimits(draft);
      setView(result);
      setDraft(result.limits);
      setSaved(true);
    } catch (failure) {
      setError(failure);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="limits-page">
      <p className="lede">
        Set the limits the AI agent has to stay inside. TradeGuard checks every order against
        them before it reaches the broker.
      </p>
      <div className="split">
        <form className="card limits-form" noValidate onSubmit={(event) => void save(event)}>
          <div className="template-row">
            {TEMPLATES.map((template) => (
              <button key={template.policy} type="button" className="button button-quiet"
                      onClick={() => void applyTemplate(template.policy)}>
                Start from {template.label}
              </button>
            ))}
          </div>
          <div className="limit-fields">
            {FIELDS.map((field) => {
              const id = `limit-${field.key}`;
              return (
                <div key={field.key} className="limit-field">
                  <label htmlFor={id} className="field-label">{field.label}</label>
                  <input id={id} className="input" type="number" min={field.min} max={field.max}
                         step={field.step} value={Number.isFinite(draft[field.key]) ? draft[field.key] : ""}
                         onChange={(event) => setNumber(field.key, event.target.value)} />
                  <span className="hint">{field.hint}</span>
                </div>
              );
            })}
          </div>
          <fieldset className="symbols">
            <legend className="field-label" id={ids.symbols}>Stocks the agent may trade</legend>
            <div className="symbol-picks">
              {view.universe.map((symbol) => (
                <label key={symbol} className={`pick ${draft.allowed_symbols.includes(symbol) ? "pick-on" : ""}`}>
                  <input type="checkbox" checked={draft.allowed_symbols.includes(symbol)}
                         onChange={() => toggle(symbol)} />
                  {symbol}
                </label>
              ))}
            </div>
          </fieldset>
          {issues.length ? (
            <ul className="issues">
              {issues.map((issue) => <li key={issue}>{issue}</li>)}
            </ul>
          ) : null}
          <div className="actions">
            <button type="submit" className="button" disabled={saving || issues.length > 0}>
              {saving ? "Saving…" : "Save my limits"}
            </button>
            {saved ? <span className="saved">Saved. Your next orders use these limits.</span> : null}
          </div>
          <ErrorNote error={error} />
        </form>
        <div>
          <section className="card" aria-labelledby={ids.locked}>
            <h2 id={ids.locked}>Always on</h2>
            <p className="muted small">These protect you from the AI and can't be turned off.</p>
            <ul className="rule-list" aria-labelledby={ids.locked}>
              {view.locked.map((rule) => <li key={rule}>{rule}</li>)}
            </ul>
          </section>
          <section className="card stopped-card" aria-labelledby={ids.stopped}>
            <h2 id={ids.stopped}>What the guard stopped for you</h2>
            {stopped.length === 0 ? <Empty>Nothing yet in this session.</Empty> : (
              <ul className="stopped-list">
                {stopped.map((rule) => (
                  <li key={`${rule.rule_id}-${rule.decision}`}>
                    <div className="stopped-row">
                      <span>{rule.title ?? rule.rule_id}</span>
                      <strong>{rule.count}</strong>
                    </div>
                    <span className="muted small">
                      {rule.decision === "ESCALATE" ? "held for your approval" : "blocked"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
