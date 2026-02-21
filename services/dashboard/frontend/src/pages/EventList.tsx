import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiFetch } from "../api/client";
import EventTable from "../components/EventTable";
import { uvu } from "../theme";

const inputStyle: React.CSSProperties = {
  padding: "8px 12px",
  border: `1px solid ${uvu.border}`,
  borderRadius: 6,
  fontSize: "0.85rem",
  color: uvu.text,
  background: uvu.surface,
};

const paginationBtn = (disabled: boolean): React.CSSProperties => ({
  padding: "7px 18px",
  border: `1px solid ${uvu.border}`,
  borderRadius: 6,
  background: disabled ? uvu.bg : uvu.surface,
  cursor: disabled ? "default" : "pointer",
  fontSize: "0.85rem",
  color: disabled ? uvu.textMuted : uvu.text,
  fontWeight: 500,
});

const toggleStyle = (active: boolean): React.CSSProperties => ({
  padding: "8px 14px",
  border: `1px solid ${active ? uvu.green : uvu.border}`,
  borderRadius: 6,
  fontSize: "0.85rem",
  fontWeight: 500,
  cursor: "pointer",
  background: active ? `${uvu.green}18` : uvu.surface,
  color: active ? uvu.green : uvu.text,
});

export default function EventList() {
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<{ total: number; events: Array<Record<string, unknown>> }>({ total: 0, events: [] });
  const page = Number(params.get("page") ?? 1);
  const statusFilter = params.get("status") ?? "";
  const userFilter = params.get("user") ?? "";
  const ratingMin = params.get("rating_min") ?? "";
  const hideReviewed = params.get("hide_reviewed") === "1";

  useEffect(() => {
    const qs = new URLSearchParams();
    qs.set("page", String(page));
    qs.set("per_page", "50");
    if (statusFilter) qs.set("status", statusFilter);
    if (userFilter) qs.set("user", userFilter);
    if (ratingMin) qs.set("rating_min", ratingMin);
    if (hideReviewed) qs.set("reviewed", "false");
    apiFetch<typeof data>(`/events?${qs}`).then(setData);
  }, [page, statusFilter, userFilter, ratingMin, hideReviewed]);

  const totalPages = Math.ceil(data.total / 50);

  function setFilter(key: string, value: string) {
    params.set(key, value);
    params.set("page", "1");
    setParams(params);
  }

  return (
    <div>
      <h2 style={{ marginBottom: "1rem", fontWeight: 700 }}>Events ({data.total})</h2>

      <div style={{ display: "flex", gap: "0.75rem", marginBottom: "1rem", flexWrap: "wrap", alignItems: "center" }}>
        <select
          value={statusFilter}
          onChange={(e) => setFilter("status", e.target.value)}
          style={inputStyle}
        >
          <option value="">All statuses</option>
          <option value="completed">Completed</option>
          <option value="processing">Processing</option>
          <option value="failed">Failed</option>
          <option value="remediated">Remediated</option>
        </select>
        <select
          value={ratingMin}
          onChange={(e) => setFilter("rating_min", e.target.value)}
          style={inputStyle}
        >
          <option value="">Any rating</option>
          <option value="4">4+ (High &amp; Critical)</option>
          <option value="5">5 only (Critical)</option>
          <option value="3">3+ (Medium and above)</option>
        </select>
        <input
          placeholder="Filter by user..."
          value={userFilter}
          onChange={(e) => setFilter("user", e.target.value)}
          style={{ ...inputStyle, width: 220 }}
        />
        <button
          type="button"
          onClick={() => setFilter("hide_reviewed", hideReviewed ? "0" : "1")}
          style={toggleStyle(hideReviewed)}
        >
          {hideReviewed ? "Showing unreviewed only" : "Hide reviewed"}
        </button>
      </div>

      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
      <EventTable events={data.events as any} />

      {totalPages > 1 && (
        <div style={{ marginTop: "1.25rem", display: "flex", gap: "0.5rem", justifyContent: "center", alignItems: "center" }}>
          <button
            disabled={page <= 1}
            onClick={() => { params.set("page", String(page - 1)); setParams(params); }}
            style={paginationBtn(page <= 1)}
          >Prev</button>
          <span style={{ fontSize: "0.85rem", color: uvu.textMuted, padding: "0 8px" }}>Page {page} / {totalPages}</span>
          <button
            disabled={page >= totalPages}
            onClick={() => { params.set("page", String(page + 1)); setParams(params); }}
            style={paginationBtn(page >= totalPages)}
          >Next</button>
        </div>
      )}
    </div>
  );
}
