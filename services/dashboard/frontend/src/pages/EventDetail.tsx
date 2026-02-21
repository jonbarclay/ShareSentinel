import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { apiFetch } from "../api/client";
import { TierBadge } from "../components/VerdictBadge";
import AnalystReviewForm from "../components/AnalystReviewForm";
import UserProfileCard from "../components/UserProfileCard";
import { uvu, card } from "../theme";

interface Detail {
  event: Record<string, unknown> | null;
  verdict: Record<string, unknown> | null;
  audit_log: Array<Record<string, unknown>>;
  user_profile: {
    user_id: string;
    display_name: string | null;
    job_title: string | null;
    department: string | null;
    mail: string | null;
    manager_name: string | null;
    photo_base64: string | null;
  } | null;
}

const labelCss: React.CSSProperties = { color: uvu.textMuted, fontSize: "0.75rem", fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.04em" };

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <span style={labelCss}>{label}</span>
      <div style={{ fontSize: "0.9rem", color: uvu.text, marginTop: 1 }}>{String(value ?? "—")}</div>
    </div>
  );
}

export default function EventDetail() {
  const { eventId } = useParams<{ eventId: string }>();
  const nav = useNavigate();
  const [data, setData] = useState<Detail | null>(null);

  function load() {
    apiFetch<Detail>(`/events/${eventId}`).then(setData);
  }
  useEffect(load, [eventId]);

  if (!data) return <div>Loading...</div>;
  if (!data.event) return <div>Event not found. <button onClick={() => nav("/events")}>Back</button></div>;

  const e = data.event;
  const v = data.verdict;

  return (
    <div>
      <button
        onClick={() => nav("/events")}
        style={{
          marginBottom: "1rem",
          cursor: "pointer",
          background: "none",
          border: `1px solid ${uvu.border}`,
          borderRadius: 6,
          padding: "6px 14px",
          fontSize: "0.85rem",
          color: uvu.textSecondary,
        }}
      >
        &larr; Back to Events
      </button>
      <h2 style={{ marginBottom: "1rem", fontWeight: 700 }}>Event: {String(e.file_name || e.event_id)}</h2>

      {v && Boolean(v.analyst_disposition) && (
        <div style={{
          padding: "12px 16px",
          marginBottom: "1rem",
          borderRadius: 8,
          background: v.analyst_disposition === "true_positive" ? "#fce8e4" :
                       v.analyst_disposition === "false_positive" ? "#e3fcef" : "#fff3cd",
          color: v.analyst_disposition === "true_positive" ? "#c92a2a" :
                 v.analyst_disposition === "false_positive" ? "#00875a" : "#856404",
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.03em",
          border: `1px solid ${v.analyst_disposition === "true_positive" ? "#f5c6cb" :
                   v.analyst_disposition === "false_positive" ? "#c3e6cb" : "#ffeeba"}`
        }}>
          Analyst Review: {String(v.analyst_disposition).replace("_", " ")}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
        <div style={card}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Event Details</h3>
          <Field label="Event ID" value={e.event_id} />
          <Field label="File Name" value={e.file_name} />
          <UserProfileCard profile={data.user_profile} userId={String(e.user_id)} />
          <Field label="Object ID" value={e.object_id} />
          <Field label="Item Type" value={e.item_type} />
          <div style={{ marginBottom: 8 }}>
            <span style={labelCss}>Status</span>
            <div style={{ fontSize: "0.9rem", marginTop: 1 }}>
              {e.status === "remediated" ? (
                <span style={{
                  display: "inline-block",
                  background: "#e3fcef",
                  color: "#00875a",
                  padding: "3px 10px",
                  borderRadius: 5,
                  fontWeight: 600,
                  fontSize: "0.82rem",
                }}>Remediated</span>
              ) : (
                <span style={{ color: uvu.text }}>{String(e.status ?? "—")}</span>
              )}
            </div>
          </div>
          <Field label="Sharing Type" value={e.sharing_type} />
          <Field label="Sharing Scope" value={e.sharing_scope} />
          <Field label="Sharing Permission" value={e.sharing_permission} />
          <Field label="Received" value={e.received_at ? new Date(String(e.received_at)).toLocaleString() : null} />
          {(() => {
            const raw = e.sharing_links;
            const links: Array<{url: string; label: string}> = Array.isArray(raw) ? raw : typeof raw === "string" ? (() => { try { return JSON.parse(raw); } catch { return []; } })() : [];
            return links.length > 0 ? (
            <div style={{ marginTop: 10 }}>
              <span style={labelCss}>Sharing Links</span>
              <div style={{ marginTop: 6 }}>
                {links.map((link, i) => (
                  <div key={i} style={{ marginBottom: 6, display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      display: "inline-block",
                      background: link.label.startsWith("Anonymous") ? "#fce8e4" : uvu.seaHaze,
                      color: link.label.startsWith("Anonymous") ? uvu.brick : uvu.greenD2,
                      padding: "2px 8px",
                      borderRadius: 4,
                      fontSize: "0.72rem",
                      fontWeight: 600,
                    }}>{link.label}</span>
                    <a href={link.url} target="_blank" rel="noopener noreferrer"
                      style={{ color: uvu.greenL1, fontSize: "0.85rem", wordBreak: "break-all" }}>{link.url}</a>
                  </div>
                ))}
              </div>
            </div>
            ) : null;
          })()}
        </div>

        {v && (
          <div style={card}>
            <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>AI Verdict</h3>
            <div style={{ marginBottom: 10 }}>
              <TierBadge tier={v.escalation_tier as string | null} />
            </div>
            <Field label="Context" value={v.overall_context} />
            <Field label="Analysis Mode" value={v.analysis_mode} />
            <Field label="Provider" value={`${v.ai_provider} / ${v.ai_model}`} />
            <Field label="Tokens" value={`${v.input_tokens} in / ${v.output_tokens} out`} />
            <Field label="Cost" value={`$${Number(v.estimated_cost_usd ?? 0).toFixed(4)}`} />
            <Field label="Latency" value={`${Number(v.processing_time_seconds ?? 0).toFixed(2)}s`} />
            {(() => {
              const cats: Array<{id: string; confidence: string; evidence?: string}> =
                Array.isArray(v.category_assessments) ? v.category_assessments :
                typeof v.category_assessments === "string" ? (() => { try { return JSON.parse(v.category_assessments as string); } catch { return []; } })() : [];
              return cats.length > 0 ? (
                <div style={{ marginTop: 10 }}>
                  <div style={labelCss}>Detected Categories</div>
                  {cats.map((cat, i) => (
                    <div key={i} style={{ marginTop: 6, padding: "8px 10px", background: uvu.seaHaze, borderRadius: 6, fontSize: "0.85rem" }}>
                      <span style={{ fontWeight: 600, color: uvu.greenD2 }}>{cat.id}</span>
                      <span style={{ color: uvu.textMuted, marginLeft: 8, fontSize: "0.78rem" }}>({cat.confidence})</span>
                      {cat.evidence && <div style={{ color: uvu.textSecondary, marginTop: 3, fontSize: "0.82rem" }}>{cat.evidence}</div>}
                    </div>
                  ))}
                </div>
              ) : null;
            })()}
            <div style={{ marginTop: 10 }}>
              <div style={labelCss}>Summary</div>
              <p style={{ fontSize: "0.9rem", lineHeight: 1.5, color: uvu.text, marginTop: 3 }}>{String(v.summary ?? "—")}</p>
            </div>
            <div style={{ marginTop: 10 }}>
              <div style={labelCss}>Recommendation</div>
              <p style={{ fontSize: "0.9rem", lineHeight: 1.5, color: uvu.text, marginTop: 3 }}>{String(v.recommendation ?? "—")}</p>
            </div>
          </div>
        )}
      </div>

      {v && (
        <div style={{ ...card, marginTop: "1rem" }}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Analyst Review</h3>
          <AnalystReviewForm
            eventId={String(e.event_id)}
            currentDisposition={v.analyst_disposition as string | null}
            currentNotes={v.analyst_notes as string | null}
            onSaved={load}
          />
        </div>
      )}

      <div style={{ ...card, marginTop: "1rem" }}>
        <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Audit Trail ({data.audit_log.length})</h3>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
          <thead>
            <tr>
              {["Time", "Action", "Status", "Details"].map((h) => (
                <th key={h} style={{ textAlign: "left", padding: "6px 10px", borderBottom: `2px solid ${uvu.border}`, fontSize: "0.75rem", color: uvu.textMuted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.audit_log.map((log, i) => (
              <tr key={i}>
                <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.textSecondary }}>
                  {log.created_at ? new Date(String(log.created_at)).toLocaleTimeString() : "—"}
                </td>
                <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.text }}>{String(log.action)}</td>
                <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.textSecondary }}>{String(log.status ?? "")}</td>
                <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: uvu.textSecondary }}>
                  {log.details ? JSON.stringify(log.details) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
