import { useCallback, useEffect, useState } from "react";
import { ApiError, useApi } from "./api";
import { AccountPanel } from "./components/AccountPanel";
import { HowItWorks } from "./components/HowItWorks";
import { MarketOverview, MarketWatch } from "./components/MarketOverview";
import { statusText } from "./format";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditPage } from "./pages/AuditPage";
import { ChatPage } from "./pages/ChatPage";
import { EvalsPage } from "./pages/EvalsPage";
import { LimitsPage } from "./pages/LimitsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { PositionsPage } from "./pages/PositionsPage";
import { TradersPage } from "./pages/TradersPage";
import type { MarketStatus, Me, Role } from "./types";

// Traders see these; they ask for a log in first.
const TRADER = [
  { path: "chat", label: "Chat & Trade" },
  { path: "positions", label: "Positions" },
  { path: "limits", label: "My limits" },
] as const;

// Only the owner, logged in as `admin` on the same form (F10f).
const ADMIN = [
  { path: "traders", label: "Traders" },
  { path: "approvals", label: "Approvals" },
  { path: "audit", label: "Audit log" },
  { path: "activity", label: "Guard activity" },
  { path: "results", label: "Test results" },
] as const;

type Path = (typeof TRADER)[number]["path"] | (typeof ADMIN)[number]["path"];
const ALL: readonly { path: Path; label: string }[] = [...TRADER, ...ADMIN];

function current(): Path {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const found = ALL.find((page) => page.path === hash);
  return found ? found.path : "chat";
}

/** Hash routing (#/chat, #/positions, ...): works on any static host without rewrites. */
function useRoute(): Path {
  const [path, setPath] = useState<Path>(current);
  useEffect(() => {
    const onChange = () => setPath(current());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

/** The logged-in trader, refreshed in the background (status, waiting for approval). */
function useMe(pollMs: number) {
  const api = useApi();
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const refresh = useCallback(async () => {
    if (!api.isTrader()) {
      setMe(null);
      setChecked(true);
      return;
    }
    try {
      setMe(await api.me());
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) setMe(null);
    } finally {
      setChecked(true);
    }
  }, [api]);
  useEffect(() => {
    let live = true;
    const load = () => {
      if (live) void refresh();
    };
    load();
    const timer = window.setInterval(load, pollMs);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [refresh, pollMs]);
  return { me, checked, refresh, setMe };
}

/** Where prices come from and whether the market is open (F10f). */
function MarketBadge() {
  const api = useApi();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => api.marketStatus().then((data) => live && setStatus(data), () => undefined);
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [api]);
  if (!status) return null;
  const tone = status.stale ? "dot-escalate" : status.live && status.market_open ? "dot-live" : "";
  return (
    <span className="clock-badge"
          title={status.live ? `Prices from ${status.source}; trades are paper only` : undefined}>
      <span className={`dot ${tone}`} aria-hidden="true" />
      {statusText(status)}
    </span>
  );
}

function NavLink({ path, label, active }: { path: Path; label: string; active: boolean }) {
  return (
    <a href={`#/${path}`} className={`nav-link ${active ? "nav-active" : ""}`}
       aria-current={active ? "page" : undefined}>
      {label}
    </a>
  );
}

function Landing({ onHow }: { onHow: () => void }) {
  return (
    <div className="landing">
      <div className="hero">
        <div>
          <h1>Trade in plain words. TradeGuard keeps the AI in check.</h1>
          <p className="lede">
            Create an account on the left, then tell the agent what to trade at live market
            prices. Every order is checked against your limits and your request before it
            reaches the broker.
          </p>
          <div className="hero-steps">
            <span><strong>1</strong> Sign up or log in</span>
            <span><strong>2</strong> Ask or trade in chat</span>
            <span><strong>3</strong> Approve large orders</span>
            <span><strong>4</strong> Watch your positions</span>
          </div>
        </div>
        <button type="button" className="button button-quiet" onClick={onHow}>
          See how it works
        </button>
      </div>
      <MarketOverview />
    </div>
  );
}

export function App() {
  const api = useApi();
  const path = useRoute();
  const { me, checked, refresh, setMe } = useMe(5000);
  const [howOpen, setHowOpen] = useState(false);
  const [admin, setAdmin] = useState(() => api.isAdmin());
  const adminPage = ADMIN.some((page) => page.path === path);

  function authed(role: Role) {
    if (role === "admin") {
      setAdmin(true);
      window.location.hash = "#/traders";
    } else {
      void refresh();
    }
  }

  function loggedOut() {
    setAdmin(false);
    setMe(null);
    if (adminPage) window.location.hash = "#/chat";
  }

  const session = me?.session ?? null;
  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/chat">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
            <path d="M16 2 4 7v8c0 7.5 5.1 13.4 12 15 6.9-1.6 12-7.5 12-15V7z" fill="currentColor" />
          </svg>
          TradeGuard
        </a>
        <MarketBadge />
        <nav aria-label="Pages">
          {(admin ? ADMIN : TRADER).map((page) => (
            <NavLink key={page.path} path={page.path} label={page.label}
                     active={page.path === path} />
          ))}
        </nav>
        <div className="top-actions">
          <button type="button" className="button button-quiet how" onClick={() => setHowOpen(true)}>
            How it works
          </button>
        </div>
      </header>
      <HowItWorks open={howOpen} onClose={() => setHowOpen(false)} />
      {admin ? (
        <div className="desk">
          <AccountPanel me={null} admin onAuthed={authed} onLoggedOut={loggedOut} />
          <main className="desk-main">
            {path === "approvals" ? <ApprovalsPage />
              : path === "audit" ? <AuditPage />
              : path === "activity" ? <PoliciesPage />
              : path === "results" ? <EvalsPage />
              : <TradersPage />}
          </main>
        </div>
      ) : (
        <div className="desk">
          <AccountPanel me={me} onAuthed={authed} onLoggedOut={loggedOut} />
          <main className="desk-main">
            {session ? (
              <>
                <div className="desk-head">
                  <h1>{me?.trader?.display_name ?? session.user}</h1>
                  <span className="muted small">
                    {session.policy === "strict" ? "Strict" : "Standard"} profile · guarded by
                    TradeGuard
                  </span>
                </div>
                {path === "positions" ? (
                  <PositionsPage session={session} />
                ) : path === "limits" ? (
                  <LimitsPage session={session} />
                ) : (
                  <div className="chat-layout">
                    <ChatPage session={session} />
                    <MarketWatch />
                  </div>
                )}
              </>
            ) : checked ? (
              <Landing onHow={() => setHowOpen(true)} />
            ) : null}
          </main>
        </div>
      )}
    </div>
  );
}
