import type { Json, JsonObject } from "./types";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD",
                                             maximumFractionDigits: 2 });

export function money(value: number): string {
  return usd.format(value).replace(/\.00$/, "");
}

export function ms(value: number | null): string {
  return value === null ? "–" : `${value.toFixed(2)} ms`;
}

export function time(iso: string | null): string {
  if (!iso) return "–";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit",
                                          minute: "2-digit", second: "2-digit" });
}

export function compactJson(value: Json | JsonObject): string {
  return JSON.stringify(value);
}

function num(value: Json | undefined): number | null {
  return typeof value === "number" ? value : null;
}

/** "buy $1,500 of NVDA", "sell 3 shares of AMD", or the tool name for non orders. */
export function orderSummary(tool: string, args: JsonObject | undefined): string {
  if (!args) return tool;
  const side = typeof args.side === "string" ? args.side : null;
  const symbol = typeof args.symbol === "string" ? args.symbol : "";
  const notional = num(args.notional);
  const qty = num(args.qty);
  if (side === null) return symbol ? `${tool} ${symbol}` : tool;
  let size = "";
  if (notional !== null) size = money(notional);
  else if (qty !== null) size = `${qty} share${qty === 1 ? "" : "s"}`;
  const limit = num(args.limit_price);
  const at = limit !== null ? ` at ${money(limit)} limit` : "";
  return `${side} ${size} of ${symbol}${at}`.replace(/\s+/g, " ");
}

/** Arguments that `orderSummary` does not already say. */
export function otherArgs(args: JsonObject | undefined): JsonObject {
  const shown = new Set(["symbol", "side", "notional", "qty", "limit_price", "order_type"]);
  const out: JsonObject = {};
  if (!args) return out;
  for (const [key, value] of Object.entries(args)) {
    if (!shown.has(key)) out[key] = value;
  }
  return out;
}

export function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}
