import { useEffect, useState } from "react";
import { useApi } from "../api";
import { marketTime } from "../format";
import type { MarketItem, MarketOverview as Overview } from "../types";
import { Sparkline } from "./charts";
import { ErrorNote } from "./common";

export function changeText(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${Math.abs(value).toFixed(2)}%`;
}

export function changeTone(value: number): string {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "";
}

/** The market overview, refreshed as the shared replayed clock moves (F10e). */
function useOverview(pollMs = 15_000): { data: Overview | null; error: unknown } {
  const api = useApi();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    const load = () => api.marketOverview().then(
      (result) => live && setData(result),
      (failure: unknown) => live && setError(failure),
    );
    void load();
    const timer = pollMs > 0 ? window.setInterval(() => void load(), pollMs) : null;
    return () => {
      live = false;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [api, pollMs]);
  return { data, error };
}

function Quote({ item, big = false }: { item: MarketItem; big?: boolean }) {
  return (
    <article className={`quote ${big ? "quote-big" : ""}`} aria-label={item.symbol}>
      <div className="quote-head">
        <span className="quote-symbol">{item.symbol}</span>
        <span className={`quote-change ${changeTone(item.day_change_pct)}`}>
          {changeText(item.day_change_pct)}
        </span>
      </div>
      <div className="quote-price">{item.price.toFixed(2)}</div>
      <Sparkline points={item.spark} label={`${item.symbol} price today`}
                 width={big ? 220 : 140} height={big ? 56 : 40} />
    </article>
  );
}

function Movers({ movers }: { movers: Overview["movers"] }) {
  const most = Math.max(0.01, ...movers.map((m) => Math.abs(m.day_change_pct)));
  const row = 26;
  const height = movers.length * row + 6;
  const summary = movers.map((m) => `${m.symbol} ${changeText(m.day_change_pct)}`).join(", ");
  return (
    <figure className="chart movers">
      <figcaption>Top movers today</figcaption>
      <svg viewBox={`0 0 320 ${height}`} role="img" aria-label={`Top movers: ${summary}`}>
        <line x1="160" x2="160" y1="0" y2={height} className="movers-axis" />
        {movers.map((mover, index) => {
          const y = index * row + 4;
          const width = (Math.abs(mover.day_change_pct) / most) * 110;
          const up = mover.day_change_pct >= 0;
          return (
            <g key={mover.symbol}>
              <rect x={up ? 160 : 160 - width} y={y + 4} width={Math.max(width, 1.5)}
                    height={row - 10} rx={3} className={up ? "bar-up" : "bar-down"} />
              <text x={up ? 152 : 168} y={y + row / 2} textAnchor={up ? "end" : "start"}
                    dominantBaseline="middle" className="chart-label">{mover.symbol}</text>
              <text x={up ? 166 + width : 154 - width} y={y + row / 2}
                    textAnchor={up ? "start" : "end"} dominantBaseline="middle"
                    className="chart-value">{changeText(mover.day_change_pct)}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

/** The landing page's market view: indices, a watchlist with sparklines, top movers. */
export function MarketOverview() {
  const { data, error } = useOverview();
  if (!data) return <ErrorNote error={error} />;
  return (
    <section className="market" aria-label="Market today">
      <div className="market-head">
        <h2>Market today</h2>
        <span className="muted small">
          {marketTime(data.as_of)} · {data.market_open ? "market open" : "market closed"} ·
          replayed market, updates live
        </span>
      </div>
      <div className="market-grid">
        <div className="indices">
          {data.indices.map((item) => <Quote key={item.symbol} item={item} big />)}
        </div>
        <Movers movers={data.movers} />
      </div>
      <h3 className="watch-title">Watchlist</h3>
      <div className="watch-grid">
        {data.watchlist.map((item) => <Quote key={item.symbol} item={item} />)}
      </div>
    </section>
  );
}

/** A compact market list for beside the chat. */
export function MarketWatch() {
  const { data } = useOverview();
  if (!data) return null;
  return (
    <aside className="market-watch">
      <h2 id="market-watch-title">Market watch</h2>
      <ul aria-labelledby="market-watch-title">
        {[...data.indices, ...data.watchlist].map((item) => (
          <li key={item.symbol}>
            <span className="quote-symbol">{item.symbol}</span>
            <Sparkline points={item.spark} label={`${item.symbol} price today`} width={70}
                       height={24} />
            <span className="watch-price">{item.price.toFixed(2)}</span>
            <span className={`quote-change ${changeTone(item.day_change_pct)}`}>
              {changeText(item.day_change_pct)}
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
