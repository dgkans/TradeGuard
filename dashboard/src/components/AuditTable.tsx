import { compactJson, ms, time } from "../format";
import type { AuditEvent } from "../types";
import { DecisionBadge, Empty } from "./common";

interface Props {
  events: AuditEvent[];
  /** Called with a session id when the user clicks it (to filter by that session). */
  onSession?: (sessionId: string) => void;
}

/** Every gateway decision: tool, arguments, decision, rule, reason, overhead, trace link. */
export function AuditTable({ events, onSession }: Props) {
  if (events.length === 0) {
    return <Empty>No audit events match. Chat with the agent to create some.</Empty>;
  }
  return (
    <div className="table-wrap">
      <table className="audit-table">
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Session</th>
            <th scope="col">Tool</th>
            <th scope="col">Decision</th>
            <th scope="col">Rule and reason</th>
            <th scope="col" className="num">Overhead</th>
            <th scope="col">Trace</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id} className={`row-${event.decision.toLowerCase()}`}>
              <td className="nowrap muted">{time(event.created_at)}</td>
              <td>
                {onSession ? (
                  <button type="button" className="link-button" onClick={() => onSession(event.session_id)}>
                    {event.session_id}
                  </button>
                ) : (
                  <span>{event.session_id}</span>
                )}
              </td>
              <td>
                <code>{event.tool}</code>
                <div className="args">{compactJson(event.args)}</div>
              </td>
              <td>
                <DecisionBadge decision={event.decision} />
              </td>
              <td>
                <code>{event.rule_id}</code>
                <div className="reason">{event.reason}</div>
              </td>
              <td className="num nowrap">{ms(event.overhead_ms)}</td>
              <td>
                {event.trace_url ? (
                  <a href={event.trace_url} target="_blank" rel="noreferrer">
                    Trace
                  </a>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
