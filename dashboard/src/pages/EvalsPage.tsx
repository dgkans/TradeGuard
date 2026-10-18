import { useEffect, useState } from "react";
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

/** The latest full suite eval: the three config comparison, charts and caveats. */
export function EvalsPage() {
  const api = useApi();
  const [data, setData] = useState<{ name: string; report: EvalReport } | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let live = true;
    api.latestEval().then(
      (result) => live && setData(result),
      (failure: unknown) => live && setError(failure),
    );
    return () => {
      live = false;
    };
  }, [api]);

  if (error instanceof ApiError && error.status === 404) {
    return (
      <section>
        <PageHeader title="Evals" />
        <Empty>
          No full suite report yet. Run <code>make eval-full</code> (hours on a local model), then
          reload.
        </Empty>
      </section>
    );
  }

  const header = data?.report.header ?? {};
  const judge = data?.report.judge;
  const detector = data?.report.detector;
  return (
    <section>
      <PageHeader title="Evals">
        Same scenarios, three configurations: no guard, rules only, and the full guard.
      </PageHeader>
      <ErrorNote error={error} />
      {data ? (
        <>
          <dl className="run-header">
            <div><dt>Report</dt><dd><code>{data.name}</code></dd></div>
            <div><dt>Date</dt><dd>{String(header.date ?? "–")}</dd></div>
            <div><dt>Commit</dt><dd><code>{String(header.commit ?? "–")}</code></dd></div>
            <div><dt>Agent</dt><dd>{String(header.agent_model ?? "–")}</dd></div>
            <div><dt>Judge</dt><dd>{String(header.judge_model ?? "–")}</dd></div>
            <div><dt>Temperature</dt><dd>{String(header.temperature ?? "–")}</dd></div>
            <div><dt>Repeats</dt><dd>{String(header.repeats ?? "–")}</dd></div>
          </dl>
          <aside className="callout">
            Read the caveats in{" "}
            <a href={RESULTS_URL} target="_blank" rel="noreferrer">RESULTS.md</a> before quoting
            these numbers. Values are the mean over repeats, with [min, max] when repeats differed.
          </aside>
          <h2>Tuning set</h2>
          <MetricsTable caption="Tuning set" table={data.report.tuning} />
          <Charts table={data.report.tuning} />
          <h2>Held out set</h2>
          <p className="muted">Never looked at while building the guard. Reported separately.</p>
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
                    {judge.agreement_on_cases_gateway_sends_to_judge.cases} on the ambiguous cases
                    the gateway sends it ({judge.model}, {judge.prompt_version})
                  </li>
                ) : null}
                {detector ? (
                  <li>
                    Injection detector: precision {detector.precision}, recall {detector.recall} on{" "}
                    {detector.snippets} snippets
                  </li>
                ) : null}
              </ul>
            </>
          ) : null}
        </>
      ) : error ? null : (
        <p className="muted">Loading…</p>
      )}
    </section>
  );
}
