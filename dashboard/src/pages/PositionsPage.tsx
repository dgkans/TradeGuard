import { useEffect, useState } from "react";
import { useApi } from "../api";
import { Donut } from "../components/charts";
import { Empty, ErrorNote } from "../components/common";
import { marketTime, money } from "../format";
import type { OrderRow, Portfolio, SessionInfo } from "../types";

function fixed(value: number): string {
  return value.toFixed(2);
}

/** Rounded to cents first, so -0.004 reads as a neutral 0.00, never "-0.00". */
function cents(value: number): number {
  return Math.round(value * 100) / 100;
}

function signed(value: number, suffix = ""): string {
  const rounded = cents(value);
  const text = `${Math.abs(rounded).toFixed(2)}${suffix}`;
  if (rounded > 0) return `+${text}`;
  if (rounded < 0) return `-${text}`;
  return `0.00${suffix}`;
}

function tone(value: number | null): string {
  if (value === null) return "";
  const rounded = cents(value);
  if (rounded > 0) return "up";
  if (rounded < 0) return "down";
  return "";
}

function qty(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function orderSize(order: OrderRow): string {
  if (order.notional !== null) return money(order.notional);
  return order.qty !== null ? `${qty(order.qty)} sh` : "–";
}

/** The trader's positions (Kite style), account summary and today's orders. */
export function PositionsPage({ session, pollMs = 3000 }: { session: SessionInfo;
                                                           pollMs?: number }) {
  const api = useApi();
  const [book, setBook] = useState<Portfolio | null>(null);
  const [error, setError] = useState<unknown>(null);
  const sessionId = session.session_id;

  useEffect(() => {
    let live = true;
    const load = () =>
      api.portfolio().then(
        (data) => {
          if (!live) return;
          setBook(data);
          setError(null);
        },
        (failure: unknown) => live && setError(failure),
      );
    void load();
    const timer = pollMs > 0 ? window.setInterval(() => void load(), pollMs) : null;
    return () => {
      live = false;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [api, sessionId, pollMs]);

  if (!book) {
    return (
      <section>
        <ErrorNote error={error} />
        {error ? null : <p className="muted">Loading positions…</p>}
      </section>
    );
  }

  return (
    <section className="positions-page">
      <div className="summary">
        <div className="stat">
          <span className="stat-label">Cash</span>
          <span className="stat-value">{money(book.cash)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Holdings value</span>
          <span className="stat-value">{money(book.positions_value)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Total P&amp;L</span>
          <span className={`stat-value ${tone(book.total_pnl)}`}>{signed(book.total_pnl)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Day change</span>
          <span className={`stat-value ${tone(book.total_day_change)}`}>
            {signed(book.total_day_change)}
          </span>
        </div>
      </div>
      <p className="muted small">
        Demo market {book.market_open ? "open" : "closed"} · prices as of {marketTime(book.as_of)}
      </p>
      <ErrorNote error={error} />

      <div className="allocation">
        <Donut title="Allocation" slices={[
          { label: "Cash", value: book.cash },
          ...book.positions.map((row) => ({ label: row.symbol, value: row.value })),
        ]} />
      </div>

      <h2>Positions ({book.positions.length})</h2>
      {book.positions.length === 0 ? (
        <Empty>No open positions yet. Ask the agent to buy something in Chat.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="positions-table" aria-label="Positions">
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col" className="num">Qty.</th>
                <th scope="col" className="num">Avg.</th>
                <th scope="col" className="num">LTP</th>
                <th scope="col" className="num">P&amp;L</th>
                <th scope="col" className="num">Chg.</th>
              </tr>
            </thead>
            <tbody>
              {book.positions.map((row) => (
                <tr key={row.symbol}>
                  <th scope="row">
                    <span className="instrument">{row.symbol}</span>
                    <span className="exchange">US</span>
                  </th>
                  <td className="num">{qty(row.qty)}</td>
                  <td className="num">{fixed(row.avg)}</td>
                  <td className="num">{fixed(row.ltp)}</td>
                  <td className="num"><span className={tone(row.pnl)}>{signed(row.pnl)}</span></td>
                  <td className="num">
                    {row.day_change_pct === null ? "–" : (
                      <span className={tone(row.day_change_pct)}>
                        {signed(row.day_change_pct, "%")}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={4}>Total</th>
                <td className="num">
                  <span className={tone(book.total_pnl)}>{signed(book.total_pnl)}</span>
                </td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      )}

      <h2>Orders today ({book.orders.length})</h2>
      {book.orders.length === 0 ? (
        <Empty>No orders yet.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="orders-table" aria-label="Orders">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Type</th>
                <th scope="col">Instrument</th>
                <th scope="col" className="num">Size</th>
                <th scope="col" className="num">Avg. price</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {book.orders.map((order) => (
                <tr key={order.id}>
                  <td className="nowrap muted">{marketTime(order.submitted_at)}</td>
                  <td>
                    <span className={`side side-${order.side}`}>{order.side.toUpperCase()}</span>
                  </td>
                  <td>
                    {order.symbol}
                    <span className="muted small">
                      {" "}{order.type}{order.limit_price !== null
                        ? ` @ ${money(order.limit_price)}` : ""}
                    </span>
                  </td>
                  <td className="num">{orderSize(order)}</td>
                  <td className="num">
                    {order.filled_avg_price !== null ? fixed(order.filled_avg_price) : "–"}
                  </td>
                  <td><span className={`order-status status-${order.status}`}>{order.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
