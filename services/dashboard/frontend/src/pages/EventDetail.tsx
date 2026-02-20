import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { apiFetch } from "../api/client";
import { TierBadge } from "../components/VerdictBadge";
import AnalystReviewForm from "../components/AnalystReviewForm";
import UserProfileCard from "../components/UserProfileCard";
import { uvu, card } from "../theme";

interface ChildEvent {
  event_id: string;
  file_name: string | null;
  relative_path: string | null;
  status: string | null;
  failure_reason: string | null;
  child_index: number | null;
  file_size_bytes: number | null;
  mime_type: string | null;
  web_url: string | null;
  escalation_tier: string | null;
  category_assessments: unknown;
  summary: string | null;
  analysis_mode: string | null;
}

interface LifecycleRecord {
  link_created_at: string | null;
  ms_expiration_at: string | null;
  enforced_expiration_at: string | null;
  status: string | null;
  file_name: string | null;
  sharing_scope: string | null;
  sharing_type: string | null;
  link_url: string | null;
  permission_id: string | null;
}

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
  child_events?: ChildEvent[];
  lifecycle?: LifecycleRecord[];
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

function parseJsonArray<T>(val: unknown): T[] {
  if (Array.isArray(val)) return val as T[];
  if (typeof val === "string") { try { return JSON.parse(val); } catch { return []; } }
  return [];
}

function daysUntil(dateStr: string): number {
  const target = new Date(dateStr);
  const now = new Date();
  return Math.ceil((target.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));
}

function ExpirationDate({ label, dateStr }: { label: string; dateStr: string | null }) {
  if (!dateStr) return null;
  const days = daysUntil(dateStr);
  const isPast = days <= 0;
  const isUrgent = days > 0 && days <= 14;
  const color = isPast ? "#6b778c" : isUrgent ? "#de350b" : uvu.text;
  return (
    <div style={{ marginBottom: 8 }}>
      <span style={labelCss}>{label}</span>
      <div style={{ fontSize: "0.9rem", color, marginTop: 1, display: "flex", alignItems: "center", gap: 8 }}>
        <span>{new Date(dateStr).toLocaleDateString()}</span>
        <span style={{
          fontSize: "0.75rem",
          fontWeight: 600,
          padding: "2px 8px",
          borderRadius: 4,
          background: isPast ? "#dfe1e6" : isUrgent ? "#fce8e4" : uvu.seaHaze,
          color: isPast ? "#6b778c" : isUrgent ? "#de350b" : uvu.greenD2,
        }}>
          {isPast ? "Expired" : `${days}d remaining`}
        </span>
      </div>
    </div>
  );
}

function LifecycleCard({ records }: { records: LifecycleRecord[] }) {
  if (records.length === 0) return null;
  const statusColors: Record<string, { bg: string; fg: string }> = {
    active: { bg: "#e3fcef", fg: "#00875a" },
    ms_managed: { bg: "#deebff", fg: "#0052cc" },
    expired_removed: { bg: "#dfe1e6", fg: "#6b778c" },
    manually_removed: { bg: "#fff3cd", fg: "#856404" },
    error: { bg: "#fce8e4", fg: "#de350b" },
  };
  return (
    <div style={{ ...card, gridColumn: "1 / -1" }}>
      <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Sharing Link Lifecycle</h3>
      {records.map((lc, i) => {
        const sc = statusColors[lc.status ?? ""] ?? { bg: "#dfe1e6", fg: "#6b778c" };
        return (
          <div key={lc.permission_id ?? i} style={{
            padding: "12px 14px",
            background: uvu.seaHaze,
            borderRadius: 6,
            marginBottom: i < records.length - 1 ? 10 : 0,
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
              <span style={{
                display: "inline-block",
                padding: "3px 10px",
                borderRadius: 5,
                fontWeight: 600,
                fontSize: "0.78rem",
                background: sc.bg,
                color: sc.fg,
              }}>
                {(lc.status ?? "unknown").replace("_", " ")}
              </span>
              {lc.sharing_scope && (
                <span style={{ fontSize: "0.78rem", color: uvu.textMuted }}>{lc.sharing_scope} / {lc.sharing_type}</span>
              )}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 2rem" }}>
              <Field label="Link Created" value={lc.link_created_at ? new Date(lc.link_created_at).toLocaleDateString() : null} />
              {lc.status !== "ms_managed" && lc.enforced_expiration_at && (
                <ExpirationDate label="180-Day Enforced Expiration" dateStr={lc.enforced_expiration_at} />
              )}
              {lc.ms_expiration_at && (
                <ExpirationDate label="Microsoft-Managed Expiration" dateStr={lc.ms_expiration_at} />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SecondLookCard({ v }: { v: Record<string, unknown> }) {
  const agreed = Boolean(v.second_look_agreed);
  const slCats = parseJsonArray<string>(v.second_look_categories);
  const primaryCats = parseJsonArray<{ id: string; confidence: string; evidence?: string }>(v.category_assessments);

  return (
    <div style={{ ...card, gridColumn: "1 / -1" }}>
      <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>
        {"Second Look "}
        <span style={{
          marginLeft: 10, display: "inline-block", padding: "3px 10px",
          borderRadius: 5, fontWeight: 600, fontSize: "0.78rem",
          backgroundColor: agreed ? "#e3fcef" : "#fff3cd",
          color: agreed ? "#00875a" : "#856404",
        }}>
          {agreed ? "Agreed" : "Disagreed \u2014 Downgraded"}
        </span>
      </h3>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
        <div>
          <div style={labelCss}>Primary Model ({String(v.ai_provider)}/{String(v.ai_model)})</div>
          <div style={{ marginTop: 6, padding: "10px 12px", background: uvu.seaHaze, borderRadius: 6, fontSize: "0.85rem" }}>
            {primaryCats.map((cat, i) => (
              <div key={i} style={{ marginBottom: 4 }}>
                <span style={{ fontWeight: 600, color: uvu.greenD2 }}>{cat.id}</span>
                <span style={{ color: uvu.textMuted, marginLeft: 6, fontSize: "0.78rem" }}>({cat.confidence})</span>
              </div>
            ))}
            <div style={{ marginTop: 8, color: uvu.textSecondary, lineHeight: 1.5 }}>{String(v.summary ?? "")}</div>
            {typeof v.reasoning === "string" && v.reasoning && (
              <details style={{ marginTop: 8 }}>
                <summary style={{ ...labelCss, cursor: "pointer", userSelect: "none", fontSize: "0.72rem" }}>Reasoning</summary>
                <p style={{ fontSize: "0.82rem", lineHeight: 1.5, color: uvu.textSecondary, marginTop: 4, whiteSpace: "pre-wrap" }}>{v.reasoning}</p>
              </details>
            )}
          </div>
        </div>
        <div>
          <div style={labelCss}>Second Look ({String(v.second_look_provider)}/{String(v.second_look_model)})</div>
          <div style={{ marginTop: 6, padding: "10px 12px", background: agreed ? uvu.seaHaze : "#fff8e1", borderRadius: 6, fontSize: "0.85rem" }}>
            {slCats.map((catId, i) => (
              <div key={i} style={{ marginBottom: 4 }}>
                <span style={{ fontWeight: 600, color: agreed ? uvu.greenD2 : "#856404" }}>{catId}</span>
              </div>
            ))}
            <div style={{ color: uvu.textSecondary, marginTop: 8, lineHeight: 1.5 }}>
              <TierBadge tier={v.second_look_tier as string | null} />
            </div>
            {typeof v.second_look_summary === "string" && v.second_look_summary && (
              <div style={{ marginTop: 8, color: uvu.textSecondary, lineHeight: 1.5 }}>{v.second_look_summary}</div>
            )}
            {typeof v.second_look_reasoning === "string" && v.second_look_reasoning && (
              <details style={{ marginTop: 8 }}>
                <summary style={{ ...labelCss, cursor: "pointer", userSelect: "none", fontSize: "0.72rem" }}>Reasoning</summary>
                <p style={{ fontSize: "0.82rem", lineHeight: 1.5, color: uvu.textSecondary, marginTop: 4, whiteSpace: "pre-wrap" }}>{v.second_look_reasoning}</p>
              </details>
            )}
            {v.second_look_cost_usd != null && (
              <div style={{ marginTop: 8, fontSize: "0.78rem", color: uvu.textMuted }}>
                Cost: ${Number(v.second_look_cost_usd).toFixed(4)}
              </div>
            )}
          </div>
        </div>
      </div>
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
          <div style={{ marginBottom: 8 }}>
            <span style={labelCss}>Object ID</span>
            <div style={{ fontSize: "0.9rem", color: uvu.text, marginTop: 1, wordBreak: "break-all" }}>
              {e.object_id && String(e.object_id).startsWith("http") ? (
                <a href={String(e.object_id)} target="_blank" rel="noopener noreferrer"
                  style={{ color: uvu.greenL1 }}>{String(e.object_id)}</a>
              ) : String(e.object_id ?? "—")}
            </div>
          </div>
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
            if (links.length > 0) {
              return (
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
              );
            }
            // Fallback: show sharing_link_url or object_id as the sharing link
            const fallbackUrl = e.sharing_link_url || e.object_id;
            if (fallbackUrl && String(fallbackUrl).startsWith("http")) {
              const scope = e.sharing_type || e.sharing_scope || "Unknown";
              return (
                <div style={{ marginTop: 10 }}>
                  <span style={labelCss}>Sharing Link</span>
                  <div style={{ marginTop: 6, display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      display: "inline-block",
                      background: uvu.seaHaze,
                      color: uvu.greenD2,
                      padding: "2px 8px",
                      borderRadius: 4,
                      fontSize: "0.72rem",
                      fontWeight: 600,
                    }}>{String(scope)}</span>
                    <a href={String(fallbackUrl)} target="_blank" rel="noopener noreferrer"
                      style={{ color: uvu.greenL1, fontSize: "0.85rem", wordBreak: "break-all" }}>{String(fallbackUrl)}</a>
                  </div>
                </div>
              );
            }
            return null;
          })()}
        </div>

        {v && (
          <div style={card}>
            <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>AI Verdict</h3>
            <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 10 }}>
              <TierBadge tier={v.escalation_tier as string | null} />
              {v.risk_score != null && Number(v.risk_score) > 0 && (
                <span style={{
                  display: "inline-block",
                  padding: "3px 10px",
                  borderRadius: 5,
                  fontWeight: 600,
                  fontSize: "0.82rem",
                  backgroundColor: Number(v.risk_score) >= 7 ? "#de350b" : Number(v.risk_score) >= 4 ? "#ff991f" : "#dfe1e6",
                  color: Number(v.risk_score) >= 7 ? "#fff" : Number(v.risk_score) >= 4 ? "#172b4d" : "#6b778c",
                }}>
                  Risk: {String(v.risk_score)}/10
                </span>
              )}
            </div>
            <Field label="Context" value={v.overall_context} />
            {v.data_recency && String(v.data_recency) !== "unknown" ? (
              <Field label="Data Recency" value={v.data_recency} />
            ) : null}
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
            {typeof v.reasoning === "string" && v.reasoning && (
              <details style={{ marginTop: 10 }}>
                <summary style={{ ...labelCss, cursor: "pointer", userSelect: "none" }}>AI Reasoning (click to expand)</summary>
                <p style={{ fontSize: "0.85rem", lineHeight: 1.5, color: uvu.textSecondary, marginTop: 6, whiteSpace: "pre-wrap" }}>{v.reasoning}</p>
              </details>
            )}
          </div>
        )}

        {v && v.second_look_performed === true && <SecondLookCard v={v} />}

        {data.lifecycle && data.lifecycle.length > 0 && <LifecycleCard records={data.lifecycle} />}
      </div>

      {data.child_events && data.child_events.length > 0 && (
        <div style={{ ...card, marginTop: "1rem" }}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>
            Folder Contents ({data.child_events.length} files)
          </h3>
          {(() => {
            const flagged = data.child_events!.filter(c => c.escalation_tier === "tier_1" || c.escalation_tier === "tier_2");
            const failed = data.child_events!.filter(c => c.status === "failed");
            const clean = data.child_events!.length - flagged.length - failed.length;
            return (
              <div style={{ display: "flex", gap: "1.5rem", marginBottom: "1rem", fontSize: "0.85rem" }}>
                <div><span style={{ fontWeight: 700, color: "#de350b" }}>{flagged.length}</span> flagged</div>
                <div><span style={{ fontWeight: 700, color: "#00875a" }}>{clean}</span> clean</div>
                {failed.length > 0 && <div><span style={{ fontWeight: 700, color: "#ff991f" }}>{failed.length}</span> failed</div>}
              </div>
            );
          })()}
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr>
                {["#", "File Name", "Tier", "Categories", "Status", "Summary"].map((h) => (
                  <th key={h} style={{ textAlign: "left", padding: "6px 10px", borderBottom: `2px solid ${uvu.border}`, fontSize: "0.75rem", color: uvu.textMuted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.child_events!.map((child) => {
                const cats = parseJsonArray<{ id: string }>(child.category_assessments);
                return (
                  <tr key={child.event_id} style={{ cursor: "pointer" }} onClick={() => nav(`/events/${encodeURIComponent(child.event_id)}`)}>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.textMuted }}>{child.child_index != null ? child.child_index + 1 : "—"}</td>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.text, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{child.file_name ?? "—"}</td>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}` }}>
                      <TierBadge tier={child.escalation_tier} />
                    </td>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.textSecondary, fontSize: "0.78rem" }}>
                      {cats.map(c => c.id).join(", ") || "—"}
                    </td>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}` }}>
                      {child.status === "failed" ? (
                        <span style={{ color: "#de350b", fontWeight: 600, fontSize: "0.78rem" }}>Failed</span>
                      ) : child.status === "completed" ? (
                        <span style={{ color: "#00875a", fontSize: "0.78rem" }}>Completed</span>
                      ) : (
                        <span style={{ color: uvu.textMuted, fontSize: "0.78rem" }}>{child.status ?? "—"}</span>
                      )}
                    </td>
                    <td style={{ padding: "6px 10px", borderBottom: `1px solid ${uvu.divider}`, color: uvu.textSecondary, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: "0.82rem" }}>
                      {child.summary ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

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
