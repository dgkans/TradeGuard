import { useEffect, useState } from "react";
import { useApi } from "../api";
import { Empty, ErrorNote, PageHeader } from "../components/common";
import { money } from "../format";
import type { AdminTrader } from "../types";

/** Admin: every trader account, and who is active right now. */
export function TradersPage({ pollMs = 5000 }: { pollMs?: number }) {
  const api = useApi();
  const [traders, setTraders] = useState<AdminTrader[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let live = true;
    const load = () => api.traders().then(
      (list) => live && setTraders(list),
      (failure: unknown) => live && setError(failure),
    );
    void load();
    const timer = pollMs > 0 ? window.setInterval(() => void load(), pollMs) : null;
    return () => {
      live = false;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [api, pollMs]);

  return (
    <section>
      <PageHeader title="Traders">Every account. Passwords are stored only as scrypt hashes.</PageHeader>
      <ErrorNote error={error} />
      {traders !== null && traders.length === 0 ? <Empty>No accounts yet.</Empty> : null}
      {traders && traders.length ? (
        <div className="table-wrap">
          <table aria-label="Traders">
            <thead>
              <tr>
                <th scope="col">Trader</th>
                <th scope="col">Profile</th>
                <th scope="col" className="num">Started with</th>
                <th scope="col" className="num">Orders</th>
                <th scope="col">Now</th>
              </tr>
            </thead>
            <tbody>
              {traders.map((trader) => (
                <tr key={trader.id}>
                  <th scope="row">
                    {trader.display_name} <span className="muted small">@{trader.username}</span>
                  </th>
                  <td>{trader.profile === "strict" ? "Strict" : "Standard"}</td>
                  <td className="num">{money(trader.starting_cash)}</td>
                  <td className="num">{trader.orders}</td>
                  <td>
                    {trader.waiting_for_approval ? "Waiting for approval"
                      : trader.active ? "Online" : "Offline"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
