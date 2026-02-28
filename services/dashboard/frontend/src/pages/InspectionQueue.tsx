import { useEffect, useState, useCallback } from "react";
import { apiFetch, apiPost } from "../api/client";
import "./InspectionQueue.css";

interface PendingItem {
  event_id: string;
  file_name: string | null;
  content_type: string | null;
  user_id: string | null;
  site_url: string | null;
  drive_id: string | null;
  item_id: string | null;
  received_at: string | null;
  sharing_type: string | null;
  user_display_name: string | null;
}

interface PendingResponse {
  counts: Record<string, number>;
  total: number;
  items: PendingItem[];
  has_graph_token: boolean;
}

interface ProcessResult {
  event_id: string;
  file_name: string | null;
  status: string;
  error?: string;
}

interface ProcessResponse {
  processed: number;
  completed: number;
  failed: number;
  results: ProcessResult[];
}

const TYPE_LABELS: Record<string, string> = {
  loop: "Loop",
  onenote: "OneNote",
  whiteboard: "Whiteboard",
};

export default function InspectionQueue() {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<PendingResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [processing, setProcessing] = useState(false);
  const [results, setResults] = useState<ProcessResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchPending = useCallback(() => {
    apiFetch<PendingResponse>("/inspect/pending")
      .then((res) => {
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load pending items");
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    fetchPending();
  }, [fetchPending]);

  function toggleSelect(eventId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  }

  function toggleSelectAll() {
    if (!data) return;
    if (selected.size === data.items.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(data.items.map((i) => i.event_id)));
    }
  }

  async function handleProcess(eventIds?: string[]) {
    setProcessing(true);
    setResults(null);
    setError(null);
    try {
      const body = eventIds ? { event_ids: eventIds } : {};
      const res = await apiPost<ProcessResponse>("/inspect/process", body);
      setResults(res.results);
      setSelected(new Set());
      fetchPending();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Processing failed");
    } finally {
      setProcessing(false);
    }
  }

  function formatDate(iso: string | null) {
    if (!iso) return "-";
    return new Date(iso).toLocaleString();
  }

  function typeLabel(contentType: string | null): string {
    if (!contentType) return "Unknown";
    return TYPE_LABELS[contentType] || contentType;
  }

  function typeClass(contentType: string | null): string {
    if (!contentType) return "";
    return `type-${contentType}`;
  }

  if (loading) {
    return (
      <div className="inspection-queue">
        <div className="inspection-loading">Loading inspection queue...</div>
      </div>
    );
  }

  const items = data?.items ?? [];
  const counts = data?.counts ?? {};
  const total = data?.total ?? 0;
  const hasToken = data?.has_graph_token ?? false;

  return (
    <div className="inspection-queue">
      {/* Header */}
      <div className="inspection-header">
        <div>
          <h2 className="page-title">Inspection Queue</h2>
          <p className="inspection-subtitle">
            Items requiring manual inspection with delegated Graph API access
          </p>
        </div>
        <div className="inspection-counts">
          <span className="count-badge total">
            Total <span className="count-number">{total}</span>
          </span>
          {Object.entries(counts).map(([type, count]) => (
            <span key={type} className="count-badge">
              {typeLabel(type)} <span className="count-number">{count}</span>
            </span>
          ))}
        </div>
      </div>

      {/* Auth warning */}
      {!hasToken && (
        <div className="inspection-warning">
          Please log out and log back in to grant Graph API access. Processing requires delegated permissions.
        </div>
      )}

      {/* Error display */}
      {error && (
        <div className="card" style={{ background: "#ffebe6", color: "#de350b", padding: "10px 14px", fontWeight: 600, marginBottom: 0 }}>
          {error}
        </div>
      )}

      {/* Action buttons */}
      {items.length > 0 && (
        <div className="inspection-actions">
          <button
            className="btn btn-primary"
            onClick={() => handleProcess()}
            disabled={processing || !hasToken}
          >
            {processing ? "Processing..." : "Process All"}
          </button>
          {selected.size > 0 && (
            <button
              className="btn btn-secondary"
              onClick={() => handleProcess(Array.from(selected))}
              disabled={processing || !hasToken}
            >
              Process Selected ({selected.size})
            </button>
          )}
        </div>
      )}

      {/* Processing results */}
      {results && results.length > 0 && (
        <div className="inspection-results">
          <h3>Processing Results</h3>
          {results.map((r) => (
            <div key={r.event_id} className="result-item">
              <span className={`result-status status-${r.status}`}>
                {r.status}
              </span>
              <span>{r.file_name || r.event_id}</span>
              {r.error && (
                <span style={{ color: "#dc3545", fontSize: "0.8rem" }}>
                  {r.error}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Items table */}
      {items.length === 0 ? (
        <div className="card">
          <div className="inspection-empty">
            No items pending inspection.
          </div>
        </div>
      ) : (
        <div className="table-wrapper card">
          <table className="inspection-table">
            <thead>
              <tr>
                <th style={{ width: 40 }}>
                  <input
                    type="checkbox"
                    checked={selected.size === items.length && items.length > 0}
                    onChange={toggleSelectAll}
                  />
                </th>
                <th>Type</th>
                <th>File Name</th>
                <th>User</th>
                <th>Event Time</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.event_id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(item.event_id)}
                      onChange={() => toggleSelect(item.event_id)}
                    />
                  </td>
                  <td>
                    <span className={`type-badge ${typeClass(item.content_type)}`}>
                      {typeLabel(item.content_type)}
                    </span>
                  </td>
                  <td>{item.file_name || "-"}</td>
                  <td>{item.user_display_name || item.user_id || "-"}</td>
                  <td>{formatDate(item.received_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
