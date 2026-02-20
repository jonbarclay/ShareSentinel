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
  risk_score: number | null;
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
            <th>Risk</th>
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
              <td className="cell-filename" title={e.file_name || String(e.object_id ?? "") || "—"}>
                {e.file_name || (e.object_id ? decodeURIComponent(String(e.object_id).split("/").pop() || "") : "") || "—"}
                {!e.file_name && e.item_type === "Folder" && (
                  <span style={{ marginLeft: 6, fontSize: "0.7rem", fontWeight: 600, color: "#6b778c", background: "#f4f5f7", padding: "1px 6px", borderRadius: 3 }}>Folder</span>
                )}
              </td>
              <td title={e.user_id}>
                <span
                  className="cell-clickable in-table"
                  onClick={(ev) => {
                    ev.stopPropagation();
                    nav(`/events?user=${e.user_id}`);
                  }}
                >
                  {e.user_display_name || e.user_id}
                </span>
              </td>
              <td className="cell-muted">
                <span
                  className="cell-clickable in-table"
                  onClick={(ev) => {
                    ev.stopPropagation();
                    nav(`/events?item_type=${e.item_type}`);
                  }}
                >
                  {e.item_type}
                </span>
              </td>
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
                {e.risk_score != null && e.risk_score > 0 ? (
                  <span
                    className="category-chip"
                    style={{
                      backgroundColor: e.risk_score >= 7 ? "#de350b" : e.risk_score >= 4 ? "#ff991f" : "#dfe1e6",
                      color: e.risk_score >= 7 ? "#fff" : e.risk_score >= 4 ? "#172b4d" : "#6b778c",
                      minWidth: 24,
                      textAlign: "center",
                    }}
                  >
                    {e.risk_score}
                  </span>
                ) : (
                  <span className="cell-muted">—</span>
                )}
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
