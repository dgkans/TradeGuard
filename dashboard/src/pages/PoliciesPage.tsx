import { useEffect, useState } from "react";
import { useApi } from "../api";
import { BarChart } from "../components/BarChart";
import { Empty, ErrorNote, PageHeader } from "../components/common";
import type { PolicyDetail, PolicyRef, RuleCount } from "../types";

const TOP = 12;

/** The policy files the gateway can run, and which rules have fired most. */
export function PoliciesPage() {
  const api = useApi();
  const [policies, setPolicies] = useState<PolicyRef[]>([]);
  const [name, setName] = useState("default");
  const [detail, setDetail] = useState<PolicyDetail | null>(null);
  const [rules, setRules] = useState<RuleCount[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.policies().then(setPolicies, setError);
    api.rulesFired().then(setRules, setError);
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

  const top = (rules ?? []).slice(0, TOP);
  return (
    <section>
      <PageHeader title="Policies">
        Rules are checked in order: schema, allowlist, policy rules, taint, intent, then approval.
      </PageHeader>
      <ErrorNote error={error} />
      <div className="split">
        <div>
          <div className="tabs" role="tablist" aria-label="Policy files">
            {policies.map((policy) => (
              <button key={policy.name} type="button" role="tab" aria-selected={policy.name === name}
                      className={`tab ${policy.name === name ? "tab-active" : ""}`}
                      onClick={() => setName(policy.name)}>
                {policy.name}
              </button>
            ))}
          </div>
          {detail && detail.name === name ? (
            <div className="card">
              <p className="muted small">
                <code>{detail.path}</code>
              </p>
              <pre className="yaml" tabIndex={0}>{detail.yaml}</pre>
            </div>
          ) : (
            <p className="muted">Loading…</p>
          )}
        </div>
        <div>
          <h2>Rules that fired most</h2>
          <p className="muted small">DENY and ESCALATE decisions in this database, all sessions.</p>
          {rules !== null && top.length === 0 ? (
            <Empty>No rule has fired yet.</Empty>
          ) : null}
          {top.length > 0 ? (
            <BarChart title="Times fired" labelWidth={170} format={(value) => String(Math.round(value))}
                      bars={top.map((rule) => ({ label: rule.rule_id, value: rule.count }))} />
          ) : null}
          {top.length > 0 ? (
            <ul className="plain-list small">
              {top.map((rule) => (
                <li key={`${rule.rule_id}-${rule.decision}`}>
                  <code>{rule.rule_id}</code> · {rule.decision} · {rule.count}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
    </section>
  );
}
