import { useCallback, useEffect, useState } from "react";
import { useApi } from "./api";
import { HowItWorks } from "./components/HowItWorks";
import { TraderBar } from "./components/TraderBar";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditPage } from "./pages/AuditPage";
import { ChatPage } from "./pages/ChatPage";
import { EvalsPage } from "./pages/EvalsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { PositionsPage } from "./pages/PositionsPage";
import type { SessionInfo } from "./types";

const TRADE = [
  { path: "chat", label: "Chat" },
  { path: "positions", label: "Positions" },
] as const;

const OPERATOR = [
  { path: "approvals", label: "Approvals" },
  { path: "audit", label: "Audit log" },
  { path: "policies", label: "Policies" },
  { path: "evals", label: "Evals" },
] as const;

type Path = (typeof TRADE)[number]["path"] | (typeof OPERATOR)[number]["path"];
const ALL: readonly { path: Path; label: string }[] = [...TRADE, ...OPERATOR];
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

export function App() {
  const path = useRoute();
  const { sessions, setSessions, refresh } = useSessions(3000);
  const [activeId, setActiveId] = useState<string | null>(storedActive);
  const [howOpen, setHowOpen] = useState(false);

  const active = sessions.find((session) => session.session_id === activeId) ?? null;
  const waiting = sessions.filter((session) => session.waiting_for_approval).length;
  const trading = path === "chat" || path === "positions";

  function select(id: string | null) {
    setActiveId(id);
    remember(id);
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
        <span className="mode-pill" title="No live trading is possible in this build">
          demo market · paper money
        </span>
        <nav aria-label="Pages">
          <span className="nav-group" aria-hidden="true">Trade</span>
          {TRADE.map((page) => (
            <NavLink key={page.path} path={page.path} label={page.label}
                     active={page.path === path} />
          ))}
          <span className="nav-sep" aria-hidden="true" />
          <span className="nav-group" aria-hidden="true">Operator</span>
          {OPERATOR.map((page) => (
            <NavLink key={page.path} path={page.path} label={page.label}
                     active={page.path === path}
                     badge={page.path === "approvals" ? waiting : undefined} />
          ))}
        </nav>
        <button type="button" className="button button-quiet how" onClick={() => setHowOpen(true)}>
          How it works
        </button>
      </header>
      <HowItWorks open={howOpen} onClose={() => setHowOpen(false)} />
      {trading ? (
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
                    {active.policy === "strict" ? "Strict" : "Standard"} risk profile · guarded
                    by TradeGuard
                  </span>
                </div>
                {path === "chat" ? <ChatPage key={active.session_id} session={active} />
                                 : <PositionsPage key={active.session_id} session={active} />}
              </>
            ) : (
              <div className="empty-desk">
                <h1>Start trading with a guarded AI agent</h1>
                <p className="lede">
                  Enter a trader name on the left and press Start session. Then ask the agent
                  to trade in plain words, and watch your positions update.
                </p>
                <button type="button" className="button button-quiet"
                        onClick={() => setHowOpen(true)}>
                  See how it works
                </button>
              </div>
            )}
          </main>
        </div>
      ) : (
        <main className="content">
          {path === "approvals" ? <ApprovalsPage /> : null}
          {path === "audit" ? <AuditPage /> : null}
          {path === "policies" ? <PoliciesPage /> : null}
          {path === "evals" ? <EvalsPage /> : null}
        </main>
      )}
    </div>
  );
}
