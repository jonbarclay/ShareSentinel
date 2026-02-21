import { useNavigate } from "react-router-dom";
import VerdictBadge from "./VerdictBadge";
import { uvu } from "../theme";

interface Event {
  event_id: string;
  file_name: string | null;
  user_id: string;
  user_display_name: string | null;
  item_type: string;
  status: string;
  sensitivity_rating: number | null;
  received_at: string;
  analyst_reviewed: boolean | null;
  [key: string]: unknown;
}

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "10px 14px",
  borderBottom: `2px solid ${uvu.border}`,
  fontSize: "0.75rem",
  color: uvu.textMuted,
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};
const td: React.CSSProperties = {
  padding: "10px 14px",
  borderBottom: `1px solid ${uvu.divider}`,
  fontSize: "0.85rem",
  color: uvu.text,
};

export default function EventTable({ events }: { events: Event[] }) {
  const nav = useNavigate();
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", background: uvu.surface, borderRadius: 10, overflow: "hidden", border: `1px solid ${uvu.border}` }}>
      <thead>
        <tr style={{ background: uvu.hover }}>
          <th style={th}>File</th>
          <th style={th}>User</th>
          <th style={th}>Type</th>
          <th style={th}>Status</th>
          <th style={th}>Rating</th>
          <th style={th}>Reviewed</th>
          <th style={th}>Received</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => (
          <tr
            key={e.event_id}
            onClick={() => nav(`/events/${e.event_id}`)}
            style={{ cursor: "pointer" }}
            onMouseEnter={(ev) => ((ev.currentTarget as HTMLElement).style.background = uvu.hover)}
            onMouseLeave={(ev) => ((ev.currentTarget as HTMLElement).style.background = "")}
          >
            <td style={{ ...td, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: 500 }}>
              {e.file_name || "—"}
            </td>
            <td style={td} title={e.user_id}>{e.user_display_name || e.user_id}</td>
            <td style={{ ...td, color: uvu.textSecondary }}>{e.item_type}</td>
            <td style={td}>
              {e.status === "remediated" ? (
                <span style={{
                  background: "#e3fcef",
                  color: "#00875a",
                  padding: "3px 10px",
                  borderRadius: 5,
                  fontWeight: 600,
                  fontSize: "0.82rem",
                }}>Remediated</span>
              ) : (
                <span style={{ color: uvu.textSecondary }}>{e.status}</span>
              )}
            </td>
            <td style={td}><VerdictBadge rating={e.sensitivity_rating} /></td>
            <td style={{ ...td, color: e.analyst_reviewed ? uvu.green : uvu.textMuted }}>{e.analyst_reviewed ? "Yes" : "—"}</td>
            <td style={{ ...td, color: uvu.textSecondary }}>{e.received_at ? new Date(e.received_at).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
