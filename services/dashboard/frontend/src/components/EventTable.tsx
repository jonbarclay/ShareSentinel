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

interface EventTableProps {
  events: Event[];
  selectable?: boolean;
  selectedIds?: Set<string>;
  onSelectionChange?: (ids: Set<string>) => void;
}

export default function EventTable({ events, selectable, selectedIds, onSelectionChange }: EventTableProps) {
  const nav = useNavigate();

  const selectableIds = selectable
    ? new Set(events.filter((e) => e.status === "completed" && e.escalation_tier != null).map((e) => e.event_id))
    : new Set<string>();
  const allSelected = selectableIds.size > 0 && [...selectableIds].every((id) => selectedIds?.has(id));
  const someSelected = [...(selectedIds ?? [])].some((id) => selectableIds.has(id));

  function toggleAll() {
    if (!onSelectionChange || !selectedIds) return;
    const next = new Set(selectedIds);
    if (allSelected) {
      selectableIds.forEach((id) => next.delete(id));
    } else {
      selectableIds.forEach((id) => next.add(id));
    }
    onSelectionChange(next);
  }

  function toggleOne(id: string) {
    if (!onSelectionChange || !selectedIds) return;
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectionChange(next);
  }

  return (
    <div className="event-table-container">
      <table className="event-table">
        <thead>
          <tr>
            {selectable && (
              <th className="cell-checkbox">
                <input
                  type="checkbox"
                  ref={(el) => { if (el) el.indeterminate = someSelected && !allSelected; }}
                  checked={allSelected}
                  onChange={toggleAll}
                />
              </th>
            )}
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
              className={`event-row${selectedIds?.has(e.event_id) ? " selected" : ""}`}
            >
              {selectable && (
                <td className="cell-checkbox">
                  {selectableIds.has(e.event_id) ? (
                    <input
                      type="checkbox"
                      checked={selectedIds?.has(e.event_id) ?? false}
                      onChange={() => toggleOne(e.event_id)}
                      onClick={(ev) => ev.stopPropagation()}
                    />
                  ) : null}
                </td>
              )}
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
