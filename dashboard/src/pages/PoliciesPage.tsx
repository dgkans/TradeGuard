import { useEffect, useId, useState } from "react";
import { useApi } from "../api";
import { Empty, ErrorNote, PageHeader } from "../components/common";
import { money } from "../format";
import type { Json, JsonObject, PolicyDetail, PolicyRef, RuleCount } from "../types";

// Risk profiles a trader can pick. Other policy files (e.g. rules_only) exist for the evals.
const PROFILES: Record<string, string> = { default: "Standard", strict: "Strict" };
const TOP = 10;

function num(value: Json | undefined): number | null {
  return typeof value === "number" ? value : null;
}

function obj(value: Json | undefined): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : {};
}

function list(value: Json | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string")
                              : [];
}

interface Rule {
  group: string;
  text: string;
}

/** Plain-language rules from a policy file. Only what the file says; nothing invented. */
export function describePolicy(policy: JsonObject): Rule[] {
  const rules: Rule[] = [];
  const order = num(policy.max_order_notional_usd);
  const position = num(policy.max_position_notional_usd);
  const exposure = num(policy.max_portfolio_exposure_pct);
  const perDay = num(policy.max_orders_per_day);
  const approval = obj(policy.require_approval);
  const approveOver = num(approval.notional_over_usd);
  const approveSell = num(approval.any_sell_over_pct_of_position);
  const types = list(policy.allowed_order_types);
  const intent = obj(policy.intent);
  const tolerance = num(intent.amount_tolerance_pct);

  if (order !== null) rules.push({ group: "Limits", text: `Up to ${money(order)} per order` });
  if (position !== null) {
    rules.push({ group: "Limits", text: `Up to ${money(position)} in any one stock` });
  }
  if (exposure !== null) {
    rules.push({ group: "Limits", text: `At most ${exposure}% of your account in stocks` });
  }
  if (perDay !== null) rules.push({ group: "Limits", text: `Up to ${perDay} orders a day` });
  if (policy.market_hours_only === true) {
    rules.push({ group: "Limits", text: "Only while the market is open" });
  }
  if (policy.allow_short === false) {
    rules.push({ group: "Limits", text: "No short selling (you can only sell what you own)" });
  }
  if (types.length === 1 && types[0] === "limit") {
    rules.push({ group: "Limits", text: "Limit orders only (you set the price)" });
  } else if (types.length) {
    rules.push({ group: "Limits", text: `Order types: ${types.join(" and ")}` });
  }
  if (approveOver !== null) {
    rules.push({ group: "Your approval", text: `Orders over ${money(approveOver)} need your approval` });
  }
  if (approveSell !== null) {
    rules.push({ group: "Your approval",
                 text: `Selling more than ${approveSell}% of a holding needs your approval` });
  }
  if (obj(policy.taint).enabled === true) {
    rules.push({ group: "Checks on the AI",
                 text: "Orders you didn't ask for are blocked, e.g. ones prompted by news text" });
  }
  if (intent.enabled === true) {
    const within = tolerance !== null ? ` (amount within ${tolerance}%)` : "";
    rules.push({ group: "Checks on the AI",
                 text: `Each order must match what you asked: stock, buy or sell, amount${within}` });
    rules.push({ group: "Checks on the AI",
                 text: "Unclear requests (shares or dollars?) come to you to decide" });
  }
  return rules;
}

/** Risk profiles in plain words, and what the guard has stopped so far. */
export function PoliciesPage() {
  const api = useApi();
  const [policies, setPolicies] = useState<PolicyRef[]>([]);
  const [name, setName] = useState("default");
  const [detail, setDetail] = useState<PolicyDetail | null>(null);
  const [rules, setRules] = useState<RuleCount[] | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ids = { rules: useId(), stopped: useId() };

  useEffect(() => {
    api.policies().then(setPolicies, setError);
    api.rulesFired("traders").then(setRules, setError);
  }, [api]);

  useEffect(() => {
    let live = true;
    api.policy(name).then(
      (data) => live && setDetail(data),
      (failure: unknown) => live && setError(failure),
    );
    return () => {
      live = false;
    };
  }, [api, name]);

  const profiles = policies.filter((policy) => policy.name in PROFILES);
  const shown = detail && detail.name === name ? detail : null;
  const described = shown ? describePolicy(shown.policy) : [];
  const groups = [...new Set(described.map((rule) => rule.group))];
  const allowed = shown ? list(shown.policy.allowed_symbols) : [];
  const top = (rules ?? []).slice(0, TOP);
  const most = Math.max(1, ...top.map((rule) => rule.count));

  return (
    <section>
      <PageHeader title="Risk profiles">
        The rules TradeGuard enforces for each trader. A trader picks a profile when they start
        a session; every order the AI agent tries is checked against it.
      </PageHeader>
      <ErrorNote error={error} />
      <div className="split">
        <div>
          <div className="tabs" role="tablist" aria-label="Risk profiles">
            {profiles.map((policy) => (
              <button key={policy.name} type="button" role="tab"
                      aria-selected={policy.name === name}
                      className={`tab ${policy.name === name ? "tab-active" : ""}`}
                      onClick={() => {
                        setName(policy.name);
                        setShowRaw(false);
                      }}>
                {PROFILES[policy.name]}
              </button>
            ))}
          </div>
          {shown ? (
            <div className="card">
              <ul className="rule-groups" aria-labelledby={ids.rules}>
                <li className="visually-hidden" id={ids.rules}>Rules</li>
                {groups.map((group) => (
                  <li key={group}>
                    <h3>{group}</h3>
                    <ul className="rule-list">
                      {described.filter((rule) => rule.group === group).map((rule) => (
                        <li key={rule.text}>{rule.text}</li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
              {allowed.length ? (
                <>
                  <h3>Stocks you can trade</h3>
                  <div className="symbol-chips">
                    {allowed.map((symbol) => (
                      <span key={symbol} className="symbol-chip">{symbol}</span>
                    ))}
                  </div>
                </>
              ) : null}
              <button type="button" className="link-button raw-toggle"
                      aria-expanded={showRaw} onClick={() => setShowRaw(!showRaw)}>
                {showRaw ? "Hide the policy file" : "Show the policy file"}
              </button>
              {showRaw ? (
                <>
                  <p className="muted small"><code>{shown.path}</code></p>
                  <pre className="yaml" tabIndex={0}>{shown.yaml}</pre>
                </>
              ) : null}
            </div>
          ) : (
            <p className="muted">Loading…</p>
          )}
        </div>
        <section aria-labelledby={ids.stopped}>
          <h2 id={ids.stopped}>What the guard stopped</h2>
          <p className="muted small">Blocked or held orders so far, across all traders.</p>
          {rules !== null && top.length === 0 ? <Empty>Nothing has been stopped yet.</Empty> : null}
          {top.length > 0 ? (
            <ul className="stopped-list">
              {top.map((rule) => (
                <li key={`${rule.rule_id}-${rule.decision}`}>
                  <div className="stopped-row">
                    <span>{rule.title ?? rule.rule_id}</span>
                    <strong>{rule.count}</strong>
                  </div>
                  <div className="stopped-bar" aria-hidden="true">
                    <span className={rule.decision === "ESCALATE" ? "bar-held" : "bar-blocked"}
                          style={{ width: `${(rule.count / most) * 100}%` }} />
                  </div>
                  <span className="muted small">
                    {rule.decision === "ESCALATE" ? "held for approval" : "blocked"}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </div>
    </section>
  );
}
