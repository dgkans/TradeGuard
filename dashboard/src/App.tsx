import { useCallback, useEffect, useState } from "react";
import { useApi } from "./api";
import { AdminLogin } from "./components/AdminLogin";
import { HowItWorks } from "./components/HowItWorks";
import { MarketOverview, MarketWatch } from "./components/MarketOverview";
import { TraderBar } from "./components/TraderBar";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditPage } from "./pages/AuditPage";
import { ChatPage } from "./pages/ChatPage";
import { EvalsPage } from "./pages/EvalsPage";
import { LimitsPage } from "./pages/LimitsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { PositionsPage } from "./pages/PositionsPage";
import type { SessionInfo } from "./types";

// Every visitor (a trader) sees these.
const TRADER = [
  { path: "chat", label: "Chat & Trade" },
  { path: "positions", label: "Positions" },
  { path: "limits", label: "My limits" },
] as const;

// Only the owner, after the admin login.
const ADMIN = [
  { path: "approvals", label: "Approvals" },
  { path: "audit", label: "Audit log" },
  { path: "activity", label: "Guard activity" },
  { path: "results", label: "Test results" },
] as const;

type Path = (typeof TRADER)[number]["path"] | (typeof ADMIN)[number]["path"];
const ALL: readonly { path: Path; label: string }[] = [...TRADER, ...ADMIN];
const ACTIVE_KEY = "tradeguard.active-session";

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

function storedActive(): string | null {
  try {
    return window.sessionStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

function remember(id: string | null) {
  try {
    if (id) window.sessionStorage.setItem(ACTIVE_KEY, id);
    else window.sessionStorage.removeItem(ACTIVE_KEY);
  } catch {
    // Storage blocked: the active trader just isn't remembered across reloads.
  }
}

/** Every open trader session, refreshed in the background. */
function useSessions(pollMs: number) {
  const api = useApi();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const refresh = useCallback(async () => {
    try {
      setSessions(await api.sessions());
    } catch {
      // API down: keep the last list; pages show their own errors.
    }
  }, [api]);
  useEffect(() => {
    let live = true;
    const load = () => api.sessions().then((list) => live && setSessions(list), () => undefined);
    void load();
    const timer = window.setInterval(() => void load(), pollMs);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [api, pollMs]);
  return { sessions, setSessions, refresh };
}

function NavLink({ path, label, active, badge }: { path: Path; label: string; active: boolean;
                                                  badge?: number }) {
  return (
    <a href={`#/${path}`} className={`nav-link ${active ? "nav-active" : ""}`}
       aria-current={active ? "page" : undefined}>
      {label}
      {badge ? <span className="count" aria-label={`${badge} waiting`}>{badge}</span> : null}
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
            Start a session on the left, then tell the agent what to trade. Every order is
            checked against your limits and your request before it reaches the broker.
          </p>
          <div className="hero-steps">
            <span><strong>1</strong> Start a session</span>
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
  const { sessions, setSessions, refresh } = useSessions(3000);
  const [activeId, setActiveId] = useState<string | null>(storedActive);
  const [howOpen, setHowOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [admin, setAdmin] = useState(() => api.isAdmin());

  const active = sessions.find((session) => session.session_id === activeId) ?? null;
  const waiting = sessions.filter((session) => session.waiting_for_approval).length;
  const adminPage = ADMIN.some((page) => page.path === path);

  function select(id: string | null) {
    setActiveId(id);
    remember(id);
  }

  function logout() {
    api.adminLogout();
    setAdmin(false);
    if (adminPage) window.location.hash = "#/chat";
  }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/chat">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
            <path d="M16 2 4 7v8c0 7.5 5.1 13.4 12 15 6.9-1.6 12-7.5 12-15V7z" fill="currentColor" />
          </svg>
          TradeGuard
        </a>
        <nav aria-label="Pages">
          {TRADER.map((page) => (
            <NavLink key={page.path} path={page.path} label={page.label}
                     active={page.path === path} />
          ))}
          {admin ? ADMIN.map((page) => (
            <NavLink key={page.path} path={page.path} label={page.label}
                     active={page.path === path}
                     badge={page.path === "approvals" ? waiting : undefined} />
          )) : null}
        </nav>
        <div className="top-actions">
          <button type="button" className="button button-quiet how" onClick={() => setHowOpen(true)}>
            How it works
          </button>
          {admin ? (
            <button type="button" className="link-button" onClick={logout}>Log out</button>
          ) : (
            <button type="button" className="link-button admin-link"
                    onClick={() => setLoginOpen(true)}>
              Admin
            </button>
          )}
        </div>
      </header>
      <HowItWorks open={howOpen} onClose={() => setHowOpen(false)} />
      <AdminLogin open={loginOpen} onClose={() => setLoginOpen(false)}
                  onLoggedIn={() => {
                    setAdmin(true);
                    setLoginOpen(false);
                  }} />
      {adminPage && admin ? (
        <main className="content">
          {path === "approvals" ? <ApprovalsPage /> : null}
          {path === "audit" ? <AuditPage /> : null}
          {path === "activity" ? <PoliciesPage /> : null}
          {path === "results" ? <EvalsPage /> : null}
        </main>
      ) : (
        <div className="desk">
          <TraderBar
            sessions={sessions}
            activeId={active?.session_id ?? null}
            onSelect={select}
            onStarted={(session) => {
              setSessions((list) => [...list, session]);
              select(session.session_id);
              void refresh();
            }}
            onEnded={(id) => {
              setSessions((list) => list.filter((session) => session.session_id !== id));
              if (id === activeId) select(null);
              void refresh();
            }}
          />
          <main className="desk-main">
            {active ? (
              <>
                <div className="desk-head">
                  <h1>{active.user}</h1>
                  <span className="muted small">
                    {active.policy === "strict" ? "Strict" : "Standard"} profile · guarded by
                    TradeGuard
                  </span>
                </div>
                {path === "positions" ? (
                  <PositionsPage key={active.session_id} session={active} />
                ) : path === "limits" ? (
                  <LimitsPage key={active.session_id} session={active} />
                ) : (
                  <div className="chat-layout">
                    <ChatPage key={active.session_id} session={active} />
                    <MarketWatch />
                  </div>
                )}
              </>
            ) : (
              <Landing onHow={() => setHowOpen(true)} />
            )}
          </main>
        </div>
      )}
    </div>
  );
}
