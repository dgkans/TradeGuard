import { useEffect, useId, useState } from "react";
import { useApi } from "../api";
import { AuditTable } from "../components/AuditTable";
import { ErrorNote, PageHeader } from "../components/common";
import type { AuditEvent, AuditFilters, Decision } from "../types";

const PAGE = 50;

/** Every tool call the gateway saw, newest first, with filters and trace links. */
export function AuditPage({ pollMs = 5000 }: { pollMs?: number }) {
  const api = useApi();
  const [filters, setFilters] = useState<AuditFilters>({});
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [older, setOlder] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [live, setLive] = useState(true);
  const ids = { session: useId(), decision: useId(), tool: useId() };

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.audit({ ...filters, limit: PAGE }).then(
        (page) => {
          if (!alive) return;
          setEvents(page.events);
          setOlder(page.next_before_id);
          setError(null);
        },
        (failure: unknown) => alive && setError(failure),
      );
    void load();
    const timer = live && pollMs > 0 ? window.setInterval(() => void load(), pollMs) : null;
    return () => {
      alive = false;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [api, filters, live, pollMs]);

  async function loadOlder() {
    if (older === null) return;
    setLive(false); // keep the extra rows on screen
    try {
      const page = await api.audit({ ...filters, limit: PAGE, before_id: older });
      setEvents((current) => [...current, ...page.events]);
      setOlder(page.next_before_id);
    } catch (failure) {
      setError(failure);
    }
  }

  function update(next: AuditFilters) {
    setFilters(next);
    setLive(true);
  }

  return (
    <section>
      <PageHeader title="Audit log">
        Each call is recorded before it is forwarded; if that write fails, the call is denied.
      </PageHeader>
      <div className="toolbar filters">
        <div>
          <label htmlFor={ids.session} className="field-label">Session</label>
          <input id={ids.session} className="input" value={filters.session_id ?? ""}
                 placeholder="all sessions"
                 onChange={(event) => update({ ...filters, session_id: event.target.value || undefined })} />
        </div>
        <div>
          <label htmlFor={ids.decision} className="field-label">Decision</label>
          <select id={ids.decision} className="input" value={filters.decision ?? ""}
                  onChange={(event) => update({ ...filters,
                    decision: (event.target.value || undefined) as Decision | undefined })}>
            <option value="">all</option>
            <option value="ALLOW">ALLOW</option>
            <option value="DENY">DENY</option>
            <option value="ESCALATE">ESCALATE</option>
          </select>
        </div>
        <div>
          <label htmlFor={ids.tool} className="field-label">Tool</label>
          <input id={ids.tool} className="input" value={filters.tool ?? ""} placeholder="any tool"
                 onChange={(event) => update({ ...filters, tool: event.target.value || undefined })} />
        </div>
        <label className="check">
          <input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} />{" "}
          Live
        </label>
      </div>
      <ErrorNote error={error} />
      <AuditTable events={events} onSession={(session) => update({ ...filters, session_id: session })} />
      {older !== null ? (
        <div className="actions">
          <button type="button" className="button button-quiet" onClick={() => void loadOlder()}>
            Load older
          </button>
        </div>
      ) : null}
    </section>
  );
}
