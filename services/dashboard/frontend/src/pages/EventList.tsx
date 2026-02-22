import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiFetch } from "../api/client";
import EventTable from "../components/EventTable";
import "./EventList.css";

export default function EventList() {
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<{ total: number; events: Array<Record<string, unknown>> }>({ total: 0, events: [] });
  const page = Number(params.get("page") ?? 1);
  const statusFilter = params.get("status") ?? "";
  const userFilter = params.get("user") ?? "";
  const itemTypeFilter = params.get("item_type") ?? "";
  
  const tierFilter = params.get("tier") ?? "";
  const categoryFilter = params.get("category") ?? "";

  const hideReviewed = params.get("hide_reviewed") === "1";
  const onlyReviewed = params.get("reviewed") === "true";

  useEffect(() => {
    function fetchEvents() {
      const qs = new URLSearchParams();
      qs.set("page", String(page));
      qs.set("per_page", "50");
      if (statusFilter) qs.set("status", statusFilter);
      if (userFilter) qs.set("user", userFilter);
      if (itemTypeFilter) qs.set("item_type", itemTypeFilter);
      if (tierFilter) qs.set("tier", tierFilter);
      if (categoryFilter) qs.set("category", categoryFilter);

      if (onlyReviewed) {
        qs.set("reviewed", "true");
      } else if (hideReviewed) {
        qs.set("reviewed", "false");
      }

      apiFetch<typeof data>(`/events?${qs}`).then(setData);
    }
    fetchEvents();
    const id = setInterval(fetchEvents, 30_000);
    return () => clearInterval(id);
  }, [page, statusFilter, userFilter, itemTypeFilter, tierFilter, categoryFilter, hideReviewed, onlyReviewed]);

  const totalPages = Math.ceil(data.total / 50);

  function setFilter(key: string, value: string) {
    params.set(key, value);
    params.set("page", "1");
    setParams(params);
  }

  return (
    <div className="event-list-container">
      <h2 className="page-title">Events ({data.total})</h2>

      <div className="filters-bar">
        <select
          className="filter-select"
          value={statusFilter}
          onChange={(e) => setFilter("status", e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="completed">Completed</option>
          <option value="processing">Processing</option>
          <option value="failed">Failed</option>
          <option value="remediated">Remediated</option>
        </select>
        <select
          className="filter-select"
          value={tierFilter}
          onChange={(e) => setFilter("tier", e.target.value)}
        >
          <option value="">All tiers</option>
          <option value="escalated">Escalated (Tier 1 &amp; 2)</option>
          <option value="tier_1">Tier 1 (Urgent)</option>
          <option value="tier_2">Tier 2 (Normal)</option>
          <option value="none">No Escalation</option>
        </select>
        <select
          className="filter-select"
          value={categoryFilter}
          onChange={(e) => setFilter("category", e.target.value)}
        >
          <option value="">All categories</option>
          <optgroup label="Tier 1 (Urgent)">
            <option value="pii_government_id">Government PII</option>
            <option value="pii_financial">Financial Data</option>
            <option value="ferpa">FERPA Records</option>
            <option value="hipaa">HIPAA Health Info</option>
            <option value="security_credentials">Security Credentials</option>
          </optgroup>
          <optgroup label="Tier 2 (Normal)">
            <option value="hr_personnel">HR/Personnel</option>
            <option value="legal_confidential">Legal/Confidential</option>
            <option value="pii_contact">Contact PII</option>
          </optgroup>
        </select>
        <input
          className="filter-input"
          placeholder="Filter by user..."
          value={userFilter}
          onChange={(e) => setFilter("user", e.target.value)}
        />
        <button
          className={`filter-toggle ${hideReviewed ? "active" : ""}`}
          type="button"
          onClick={() => setFilter("hide_reviewed", hideReviewed ? "0" : "1")}
        >
          {hideReviewed ? "Showing unreviewed only" : "Hide reviewed"}
        </button>
      </div>

      <div className="table-wrapper card">
        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        <EventTable events={data.events as any} />
      </div>

      {totalPages > 1 && (
        <div className="pagination-bar">
          <button
            className="pagination-btn"
            disabled={page <= 1}
            onClick={() => { params.set("page", String(page - 1)); setParams(params); }}
          >Prev</button>
          <span className="pagination-info">Page {page} / {totalPages}</span>
          <button
            className="pagination-btn"
            disabled={page >= totalPages}
            onClick={() => { params.set("page", String(page + 1)); setParams(params); }}
          >Next</button>
        </div>
      )}
    </div>
  );
}
