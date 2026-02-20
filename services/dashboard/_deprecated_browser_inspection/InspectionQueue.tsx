import { useEffect, useState, useCallback } from "react";
import { apiFetch, apiPost } from "../api/client";
import BrowserAuthModal from "../components/BrowserAuthModal";
import "../components/BrowserAuthModal.css";
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
  has_browser_auth: boolean;
}

interface ProcessResult {
  event_id: string;
  file_name: string | null;
  status: string;
  reason?: string;
}

interface BatchStartResponse {
  batch_id: string | null;
  total: number;
  message: string;
}

interface BatchStatusResponse {
  status: "processing" | "done" | "error";
  total: number;
  current: number;
  completed: number;
  failed: number;
  results: ProcessResult[];
  error?: string;
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
  const [batchProgress, setBatchProgress] = useState<string | null>(null);
  const [results, setResults] = useState<ProcessResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [browserAuth, setBrowserAuth] = useState<{ authenticated: boolean; expires_in_seconds: number } | null>(null);

  const fetchBrowserAuthStatus = useCallback(() => {
    apiFetch<{ authenticated: boolean; expires_in_seconds: number }>("/inspect/browser-session/status")
      .then(setBrowserAuth)
      .catch(() => setBrowserAuth(null));
  }, []);

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

  useEffect(() => {
    fetchBrowserAuthStatus();
  }, [fetchBrowserAuthStatus]);

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

  async function handleProcess(eventIds?: string[], processAll = false) {
    setProcessing(true);
    setResults(null);
    setError(null);
    setBatchProgress(null);
    try {
      const body = eventIds
        ? { event_ids: eventIds }
        : processAll
          ? { process_all: true }
          : {};
      const startRes = await apiPost<BatchStartResponse>("/inspect/process", body);
      if (!startRes.batch_id) {
        setProcessing(false);
        setBatchProgress(null);
        return;
      }
      setBatchProgress(`Processing 0 of ${startRes.total}...`);

      // Poll for progress
      const batchId = startRes.batch_id;
      let done = false;
      while (!done) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const status = await apiFetch<BatchStatusResponse>(`/inspect/process/status/${batchId}`);
          setBatchProgress(`Processing ${status.current} of ${status.total}... (${status.completed} completed, ${status.failed} failed)`);
          if (status.status === "done" || status.status === "error") {
            done = true;
            setResults(status.results);
            if (status.error) setError(status.error);
            setSelected(new Set());
            fetchPending();
          }
        } catch {
          done = true;
          setError("Lost connection to batch status");
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Processing failed");
    } finally {
      setProcessing(false);
      setBatchProgress(null);
    }
  }

  function formatExpiry(seconds: number): string {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
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
  const hasBrowserAuth = data?.has_browser_auth ?? false;

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

      {/* Browser auth status */}
      <div className={`browser-auth-indicator ${browserAuth?.authenticated ? "auth-active" : "auth-inactive"}`}>
        <span className="browser-auth-indicator-text">
          {browserAuth?.authenticated
            ? `Browser authenticated — expires in ${formatExpiry(browserAuth.expires_in_seconds)}`
            : "Browser not authenticated — org-wide links will show sign-in page"}
        </span>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => setAuthModalOpen(true)}
        >
          {browserAuth?.authenticated ? "Re-authenticate Browser" : "Authenticate Browser"}
        </button>
      </div>

      {/* Auth warning */}
      {!hasBrowserAuth && (
        <div className="inspection-warning">
          Browser not authenticated. Click "Authenticate Browser" above to enable processing.
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
            onClick={() => handleProcess(undefined, true)}
            disabled={processing || !hasBrowserAuth}
          >
            {processing ? "Processing..." : `Process All (${total})`}
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleProcess()}
            disabled={processing || !hasBrowserAuth}
          >
            Process Next 10
          </button>
          {selected.size > 0 && (
            <button
              className="btn btn-secondary"
              onClick={() => handleProcess(Array.from(selected))}
              disabled={processing || !hasBrowserAuth}
            >
              Process Selected ({selected.size})
            </button>
          )}
        </div>
      )}

      {/* Batch progress */}
      {batchProgress && (
        <div className="batch-progress">
          {batchProgress}
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
              {r.reason && (
                <span style={{ color: "#dc3545", fontSize: "0.8rem" }}>
                  {r.reason}
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

      <BrowserAuthModal
        open={authModalOpen}
        onClose={() => {
          setAuthModalOpen(false);
          fetchBrowserAuthStatus();
        }}
        onAuthComplete={fetchBrowserAuthStatus}
      />
    </div>
  );
}
