import { useNavigate } from "react-router-dom";
import VerdictBadge from "./VerdictBadge";
import "./EventTable.css";

interface CategoryAssessment {
  id: string;
  confidence: string;
  evidence?: string;
}

interface Event {
  event_id: string;
  file_name: string | null;
  user_id: string;
  user_display_name: string | null;
  item_type: string;
  status: string;
  failure_reason: string | null;
  escalation_tier: string | null;
  category_assessments: CategoryAssessment[] | null;
  received_at: string;
  analyst_reviewed: boolean | null;
  [key: string]: unknown;
}

export default function EventTable({ events }: { events: Event[] }) {
  const nav = useNavigate();
  return (
    <div className="event-table-container">
      <table className="event-table">
        <thead>
          <tr>
            <th>File</th>
            <th>User</th>
            <th>Type</th>
            <th>Status</th>
            <th>Categories</th>
            <th>Reviewed</th>
            <th>Received</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr
              key={e.event_id}
              onClick={() => nav(`/events/${e.event_id}`)}
              className="event-row"
            >
              <td className="cell-filename" title={e.file_name ?? "—"}>
                {e.file_name || "—"}
              </td>
              <td title={e.user_id}>{e.user_display_name || e.user_id}</td>
              <td className="cell-muted">{e.item_type}</td>
              <td>
                {e.status === "failed" ? (
                  <span className="status-badge failed" title={e.failure_reason ?? undefined}>Failed</span>
                ) : e.status === "remediated" ? (
                  <span className="status-badge remediated">Remediated</span>
                ) : (
                  <span className="status-text">{e.status}</span>
                )}
              </td>
              <td>
                <VerdictBadge
                  tier={e.escalation_tier}
                  categories={e.category_assessments}
                />
              </td>
              <td>
                {e.analyst_disposition ? (
                  <span className={`analyst-flag ${e.analyst_disposition}`}>
                    {(e.analyst_disposition as string).replace("_", " ")}
                  </span>
                ) : e.analyst_reviewed ? (
                  <span className="cell-success">Yes</span>
                ) : (
                  <span className="cell-muted">—</span>
                )}
              </td>
              <td className="cell-muted">
                {e.received_at ? new Date(e.received_at).toLocaleString() : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
