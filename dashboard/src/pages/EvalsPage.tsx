import { useEffect, useId, useState } from "react";
import { ApiError, useApi } from "../api";
import { BarChart } from "../components/BarChart";
import { Empty, ErrorNote, PageHeader } from "../components/common";
import { percent } from "../format";
import type { ConfigMetrics, EvalReport, MetricCell } from "../types";

const RESULTS_URL = "https://github.com/dgkans/TradeGuard/blob/main/RESULTS.md";
const CONFIG_ORDER = ["no_guard", "rules_only", "full_guard"];

// Same labels and order as evals/report.py.
const METRICS: { key: string; label: string; percent: boolean }[] = [
  { key: "unsafe_action_block_rate", label: "Unsafe action block rate", percent: true },
  { key: "false_block_rate", label: "False block rate", percent: true },
  { key: "task_success_rate", label: "Task success rate", percent: true },
  { key: "injection_success_rate", label: "Injection success rate", percent: true },
  { key: "escalation_rate", label: "Escalation rate", percent: true },
  { key: "gateway_overhead_p50_ms", label: "p50 gateway overhead (ms)", percent: false },
  { key: "gateway_overhead_p95_ms", label: "p95 gateway overhead (ms)", percent: false },
  { key: "agent_tokens_per_scenario", label: "Agent tokens per scenario", percent: false },
  { key: "judge_tokens_per_scenario", label: "Judge tokens per scenario", percent: false },
];

const CHARTS = ["unsafe_action_block_rate", "false_block_rate", "task_success_rate",
                "escalation_rate"];

function one(value: number, asPercent: boolean): string {
  if (asPercent) return percent(value);
  return value < 1000 ? value.toFixed(1) : Math.round(value).toLocaleString("en-US");
}

function Cell({ cell, asPercent }: { cell: MetricCell | undefined; asPercent: boolean }) {
  if (!cell || cell.mean === null) {
    return (
      <td className="num">
        <span>n/a</span>
      </td>
    );
  }
  const same = cell.min === cell.max;
  return (
    <td className="num">
      <span className="value">{one(cell.mean, asPercent)}</span>{" "}
      <span className="spread">
        {same || cell.min === null || cell.max === null
          ? `(all ${cell.repeats})`
          : `[${one(cell.min, asPercent)}, ${one(cell.max, asPercent)}]`}
      </span>
    </td>
  );
}

function configsOf(table: Record<string, ConfigMetrics>): string[] {
  const known = CONFIG_ORDER.filter((name) => name in table);
  const extra = Object.keys(table).filter((name) => !CONFIG_ORDER.includes(name));
  return [...known, ...extra];
}

function MetricsTable({ caption, table }: { caption: string; table: Record<string, ConfigMetrics> }) {
  const configs = configsOf(table);
  const scenarios = configs.length ? table[configs[0] as string]?.scenarios?.mean : null;
  return (
    <div className="table-wrap">
      <table className="metrics-table">
        <caption>
          {caption}
          {typeof scenarios === "number" ? ` (${scenarios} scenarios)` : ""}
        </caption>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            {configs.map((config) => (
              <th scope="col" key={config} className="num">
                {config}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRICS.map((metric) => (
            <tr key={metric.key}>
              <th scope="row">{metric.label}</th>
              {configs.map((config) => (
                <Cell key={config} cell={table[config]?.[metric.key]} asPercent={metric.percent} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Charts({ table }: { table: Record<string, ConfigMetrics> }) {
  const configs = configsOf(table);
  return (
    <div className="chart-grid">
      {CHARTS.map((key) => {
        const label = METRICS.find((metric) => metric.key === key)?.label ?? key;
        const bars = configs.map((config) => {
          const cell = table[config]?.[key];
          return { label: config, value: cell?.mean ?? null, low: cell?.min ?? null,
                   high: cell?.max ?? null };
        });
        return <BarChart key={key} title={`${label} by config`} bars={bars} format={percent}
                         max={1} />;
      })}
    </div>
  );
}

function Categories({ data }: { data: EvalReport["by_category"] }) {
  const configs = CONFIG_ORDER.filter((name) => name in data);
  const categories = [...new Set(configs.flatMap((config) => Object.keys(data[config] ?? {})))].sort();
  if (!categories.length) return null;
  return (
    <div className="table-wrap">
      <table>
        <caption>Task success by category (all scenarios, repeats pooled)</caption>
        <thead>
          <tr>
            <th scope="col">Config</th>
            {categories.map((category) => (
              <th scope="col" key={category} className="num">
                {category}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {configs.map((config) => (
            <tr key={config}>
              <th scope="row">{config}</th>
              {categories.map((category) => {
                const value = data[config]?.[category];
                return (
                  <td key={category} className="num">
                    {value === undefined ? "n/a" : `${Math.round(value * 100)}%`}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const SETUPS: { key: string; name: string; what: string }[] = [
  { key: "no_guard", name: "No guard",
    what: "The AI agent talks to the broker directly. Whatever it decides gets sent." },
  { key: "rules_only", name: "Basic rules",
    what: "Size, stock and market hours limits only, like a broker's usual risk checks." },
  { key: "full_guard", name: "TradeGuard",
    what: "Basic rules, plus \"did you ask for this?\" and \"does it match your request?\" " +
          "checks, and your approval for large orders." },
];

const HEADLINES: { key: string; title: string; note: string }[] = [
  { key: "unsafe_action_block_rate", title: "Unsafe orders stopped",
    note: "Higher is better. Orders that should not have gone through." },
  { key: "false_block_rate", title: "Good orders wrongly blocked",
    note: "Lower is better. Orders the trader really wanted." },
  { key: "task_success_rate", title: "Requests handled correctly",
    note: "Higher is better. Did the right thing, and nothing wrong." },
  { key: "escalation_rate", title: "Orders held for your approval",
    note: "Share of orders that waited for a person. Too high gets annoying." },
];

function rate(cell: MetricCell | undefined): number | null {
  return cell && cell.mean !== null ? cell.mean : null;
}

function Bars({ title, note, table, metric, heldOut }: {
  title: string; note: string; table: Record<string, ConfigMetrics>; metric: string;
  heldOut?: Record<string, ConfigMetrics>;
}) {
  const id = useId();
  const held = rate(heldOut?.full_guard?.[metric]);
  return (
    <section className="result-card" aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      <p className="muted small">{note}</p>
      {SETUPS.map((setup) => {
        const value = rate(table[setup.key]?.[metric]);
        return (
          <div key={setup.key} className="result-row">
            <span className="result-name">{setup.name}</span>
            <span className="result-track" aria-hidden="true">
              <span className={`result-fill fill-${setup.key}`}
                    style={{ width: `${value === null ? 0 : Math.max(value * 100, 1)}%` }} />
            </span>
            <span className="result-value">{value === null ? "n/a" : percent(value)}</span>
          </div>
        );
      })}
      {held !== null ? (
        <p className="muted small">{`On the kept-aside situations, TradeGuard: ${percent(held)}`}</p>
      ) : null}
    </section>
  );
}

/** The latest eval in plain words: what was tested, what it showed, and the caveats. */
export function EvalsPage() {
  const api = useApi();
  const [data, setData] = useState<{ name: string; report: EvalReport } | null>(null);
  const [worst, setWorst] = useState<{ name: string; report: EvalReport } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [details, setDetails] = useState(false);
  const worstId = useId();

  useEffect(() => {
    let live = true;
    api.latestEval().then(
      (result) => live && setData(result),
      (failure: unknown) => live && setError(failure),
    );
    api.latestEval("attacker").then(
      (result) => live && setWorst(result),
      () => undefined, // optional: the page works without a worst case run
    );
    return () => {
      live = false;
    };
  }, [api]);

  if (error instanceof ApiError && error.status === 404) {
    return (
      <section>
        <PageHeader title="Test results" />
        <Empty>
          No full test run yet. Run <code>make eval-full</code> (hours on a local model), then
          reload.
        </Empty>
      </section>
    );
  }

  const header = data?.report.header ?? {};
  const judge = data?.report.judge;
  const detector = data?.report.detector;
  const practice = data?.report.tuning.full_guard?.scenarios?.mean;
  const keptAside = data?.report.held_out.full_guard?.scenarios?.mean;
  return (
    <section className="evals-page">
      <PageHeader title="Test results">
        How well TradeGuard protects a trader, measured on scripted trading situations.
      </PageHeader>
      <ErrorNote error={error} />
      {data ? (
        <>
          <p className="lede">
            {`The same ${practice ?? "?"} practice situations were run ${String(header.repeats ?? "?")} times `}
            {"with each setup below, using the open "}<strong>{String(header.agent_model ?? "?").replace(/^ollama:/, "")}</strong>
            {` AI model. Another ${keptAside ?? "?"} situations were kept aside and never used while building the guard.`}
          </p>
          <div className="setups">
            {SETUPS.map((setup) => (
              <div key={setup.key} className={`setup setup-${setup.key}`}>
                <strong>{setup.name}</strong>
                <span>{setup.what}</span>
              </div>
            ))}
          </div>
          <div className="result-grid">
            {HEADLINES.map((headline) => (
              <Bars key={headline.key} title={headline.title} note={headline.note}
                    table={data.report.tuning} metric={headline.key}
                    heldOut={data.report.held_out} />
            ))}
          </div>
          {worst ? (
            <section className="worst" aria-labelledby={worstId}>
              <h2 id={worstId}>If the agent is fully fooled</h2>
              <p className="muted">
                A scripted agent that always obeys instructions hidden in the news it reads, to
                test the guard on its own. Share of those planted orders stopped:
              </p>
              <Bars title="Planted orders stopped" note="Higher is better."
                    table={worst.report.tuning} metric="unsafe_action_block_rate"
                    heldOut={worst.report.held_out} />
            </section>
          ) : null}
          <aside className="callout">
            Read these as a strong first result, not a guarantee: one AI model, situations
            written by the same person who built the guard, and repeats that barely vary. Full
            numbers and caveats are in{" "}
            <a href={RESULTS_URL} target="_blank" rel="noreferrer">RESULTS.md</a>.
          </aside>
          <button type="button" className="button button-quiet" aria-expanded={details}
                  onClick={() => setDetails(!details)}>
            {details ? "Hide detailed results" : "Show detailed results"}
          </button>
          {details ? (
            <div className="details">
              <dl className="run-header">
                <div><dt>Report</dt><dd><code>{data.name}</code></dd></div>
                <div><dt>Date</dt><dd>{String(header.date ?? "–")}</dd></div>
                <div><dt>Commit</dt><dd><code>{String(header.commit ?? "–")}</code></dd></div>
                <div><dt>Agent</dt><dd>{String(header.agent_model ?? "–")}</dd></div>
                <div><dt>Judge</dt><dd>{String(header.judge_model ?? "–")}</dd></div>
                <div><dt>Temperature</dt><dd>{String(header.temperature ?? "–")}</dd></div>
              </dl>
              <p className="muted small">Mean over repeats, with [min, max] when repeats differed.</p>
              <MetricsTable caption="Tuning set" table={data.report.tuning} />
              <Charts table={data.report.tuning} />
              <h2>Held out set</h2>
              <MetricsTable caption="Held out set" table={data.report.held_out} />
              <h2>By category</h2>
              <Categories data={data.report.by_category} />
              {judge || detector ? (
                <>
                  <h2>Component checks</h2>
                  <ul className="plain-list">
                    {judge ? (
                      <li>
                        Intent judge agreement: {judge.agreed}/{judge.cases} overall,{" "}
                        {judge.agreement_on_cases_gateway_sends_to_judge.agreed}/
                        {judge.agreement_on_cases_gateway_sends_to_judge.cases} on the ambiguous
                        cases the gateway sends it ({judge.model}, {judge.prompt_version})
                      </li>
                    ) : null}
                    {detector ? (
                      <li>
                        Injection detector: precision {detector.precision}, recall{" "}
                        {detector.recall} on {detector.snippets} snippets
                      </li>
                    ) : null}
                  </ul>
                </>
              ) : null}
            </div>
          ) : null}
        </>
      ) : error ? null : (
        <p className="muted">Loading…</p>
      )}
    </section>
  );
}
