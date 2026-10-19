import { useEffect, useId, useRef } from "react";

const FLOW = [
  { who: "You", what: "Ask in plain words: \"Buy $1500 of NVDA\"." },
  { who: "AI agent", what: "Reads prices, works out the order, and asks the broker to place it." },
  {
    who: "TradeGuard",
    what:
      "Checks every action before it reaches the broker: your limits, allowed stocks, " +
      "market hours, that it matches what you asked, and whether you need to approve it.",
  },
  { who: "Broker", what: "Gets only the orders that passed, exactly once." },
];

const EXAMPLE = [
  ["You", "Buy $1500 of NVDA"],
  ["Agent", "Checks the NVDA price, then prepares a $1,500 market order."],
  ["TradeGuard", "Over your $1,000 approval line, so it holds the order and asks you."],
  ["You", "Tap Approve (or Reject, and nothing is sent)."],
  ["TradeGuard", "Checks the limits once more, then sends the order to the broker."],
  ["Positions", "NVDA shows the new shares, with live P&L."],
];

/** "How it works": the flow, a worked example, and how a broker plugs it in. */
export function HowItWorks({ open, onClose }: { open: boolean; onClose: () => void }) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
           onClick={(event) => event.stopPropagation()}>
        <div className="dialog-head">
          <h2 id={titleId}>How TradeGuard works</h2>
          <button ref={closeRef} type="button" className="button button-quiet" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="lede">
          An AI agent can trade for you. TradeGuard sits between the agent and the broker and
          checks every action, so the agent can only do what you asked, within your limits.
        </p>
        <ol className="flow" aria-label="Flow">
          {FLOW.map((step) => (
            <li key={step.who} className="flow-step">
              <strong>{step.who}</strong>
              <span>{step.what}</span>
            </li>
          ))}
        </ol>
        <h3>Example</h3>
        <ol className="example" aria-label="Example">
          {EXAMPLE.map(([who, what], index) => (
            <li key={index}>
              <span className={`who who-${(who ?? "").toLowerCase()}`}>{who}</span>
              <span>{what}</span>
            </li>
          ))}
        </ol>
        <h3>What it stops</h3>
        <ul className="plain-list">
          <li>Orders over your size limits, or stocks you haven't allowed.</li>
          <li>Orders that don't match what you asked (wrong stock, side or amount).</li>
          <li>Orders nobody asked for, for example ones prompted by text the agent read.</li>
          <li>Anything while the market is closed. Large orders wait for your approval.</li>
        </ul>
        <h3>Plug it into any broker</h3>
        <p>
          TradeGuard speaks MCP, the standard way AI agents call tools. A broker connects by
          exposing its trading tools (quote, account, positions, orders, place and cancel) to
          TradeGuard instead of directly to the agent. This demo trades a paper account at
          live market prices; the same guard runs in front of Alpaca's paper trading API.
        </p>
      </div>
    </div>
  );
}
