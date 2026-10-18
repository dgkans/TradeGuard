import { useEffect, useState } from "react";
import { useApi } from "./api";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditPage } from "./pages/AuditPage";
import { ChatPage } from "./pages/ChatPage";
import { EvalsPage } from "./pages/EvalsPage";
import { PoliciesPage } from "./pages/PoliciesPage";

const PAGES = [
  { path: "chat", label: "Chat" },
  { path: "approvals", label: "Approvals" },
  { path: "audit", label: "Audit log" },
  { path: "policies", label: "Policies" },
  { path: "evals", label: "Evals" },
] as const;

type Path = (typeof PAGES)[number]["path"];

function current(): Path {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const found = PAGES.find((page) => page.path === hash);
  return found ? found.path : "chat";
}

/** Hash routing (#/chat, #/approvals, ...): works on any static host without rewrites. */
function useRoute(): Path {
  const [path, setPath] = useState<Path>(current);
  useEffect(() => {
    const onChange = () => setPath(current());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

function usePendingCount(pollMs: number): number | null {
  const api = useApi();
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    const load = () =>
      api.listApprovals("pending").then(
        (items) => live && setCount(items.length),
        () => live && setCount(null),
      );
    void load();
    const timer = window.setInterval(() => void load(), pollMs);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [api, pollMs]);
  return count;
}

export function App() {
  const path = useRoute();
  const pending = usePendingCount(4000);
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
          paper / replay only
        </span>
        <nav aria-label="Pages">
          {PAGES.map((page) => (
            <a key={page.path} href={`#/${page.path}`}
               className={`nav-link ${page.path === path ? "nav-active" : ""}`}
               aria-current={page.path === path ? "page" : undefined}>
              {page.label}
              {page.path === "approvals" && pending ? (
                <span className="count" aria-label={`${pending} pending`}>{pending}</span>
              ) : null}
            </a>
          ))}
        </nav>
      </header>
      <main className="content">
        {path === "chat" ? <ChatPage /> : null}
        {path === "approvals" ? <ApprovalsPage /> : null}
        {path === "audit" ? <AuditPage /> : null}
        {path === "policies" ? <PoliciesPage /> : null}
        {path === "evals" ? <EvalsPage /> : null}
      </main>
    </div>
  );
}
