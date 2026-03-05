import { useState } from "react";
import { apiPost } from "../api/client";
import "./BulkReviewDialog.css";

const DISPOSITION_LABELS: Record<string, string> = {
  true_positive: "True Positive",
  moderate_risk: "Moderate Risk",
  acceptable_risk: "Acceptable Risk",
  needs_investigation: "Needs Investigation",
  false_positive: "False Positive",
};

const DISPOSITION_COLORS: Record<string, string> = {
  true_positive: "#8b1a1a",
  moderate_risk: "#d97706",
  acceptable_risk: "#2563eb",
  needs_investigation: "#ca8a04",
  false_positive: "#16a34a",
};

interface BulkReviewResult {
  event_id: string;
  status: string;
  reason?: string;
  remediation_id?: number;
}

interface BulkReviewResponse {
  total: number;
  succeeded: number;
  skipped: number;
  failed: number;
  remediation_count: number;
  results: BulkReviewResult[];
}

interface BulkReviewDialogProps {
  disposition: string;
  eventIds: string[];
  fileNames: Record<string, string>;
  onClose: () => void;
  onComplete: () => void;
}

export default function BulkReviewDialog({
  disposition,
  eventIds,
  fileNames,
  onClose,
  onComplete,
}: BulkReviewDialogProps) {
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BulkReviewResponse | null>(null);

  async function handleSubmit() {
    setLoading(true);
    try {
      const res = await apiPost<BulkReviewResponse>("/verdicts/bulk-review", {
        event_ids: eventIds,
        disposition,
        notes,
      });
      setResult(res);
    } catch {
      setResult({
        total: eventIds.length,
        succeeded: 0,
        skipped: 0,
        failed: eventIds.length,
        remediation_count: 0,
        results: [],
      });
    } finally {
      setLoading(false);
    }
  }

  const label = DISPOSITION_LABELS[disposition] ?? disposition;
  const color = DISPOSITION_COLORS[disposition] ?? "#333";

  return (
    <div className="bulk-dialog-overlay" onClick={onClose}>
      <div className="bulk-dialog" onClick={(e) => e.stopPropagation()}>
        {!result ? (
          <>
            <h3 className="bulk-dialog-title">
              Bulk Review: <span style={{ color }}>{label}</span>
            </h3>
            <p className="bulk-dialog-count">
              {eventIds.length} event{eventIds.length !== 1 ? "s" : ""} selected
            </p>
            {disposition === "true_positive" && (
              <div className="bulk-dialog-warning">
                This will queue sharing link removal for these events.
              </div>
            )}
            <div className="bulk-dialog-files">
              {eventIds.slice(0, 10).map((id) => (
                <div key={id} className="bulk-dialog-file">
                  {fileNames[id] || id}
                </div>
              ))}
              {eventIds.length > 10 && (
                <div className="bulk-dialog-file muted">
                  ...and {eventIds.length - 10} more
                </div>
              )}
            </div>
            <textarea
              className="bulk-dialog-notes"
              placeholder="Notes (optional)"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
            />
            <div className="bulk-dialog-actions">
              <button className="bulk-dialog-cancel" onClick={onClose} disabled={loading}>
                Cancel
              </button>
              <button
                className="bulk-dialog-confirm"
                style={{ backgroundColor: color }}
                onClick={handleSubmit}
                disabled={loading}
              >
                {loading ? "Processing..." : `Apply ${label}`}
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="bulk-dialog-title">Bulk Review Complete</h3>
            <div className="bulk-dialog-results">
              <div className="bulk-result-row">
                <span>Succeeded</span>
                <strong>{result.succeeded}</strong>
              </div>
              {result.skipped > 0 && (
                <div className="bulk-result-row">
                  <span>Skipped (already same disposition)</span>
                  <strong>{result.skipped}</strong>
                </div>
              )}
              {result.failed > 0 && (
                <div className="bulk-result-row failed">
                  <span>Failed</span>
                  <strong>{result.failed}</strong>
                </div>
              )}
              {result.remediation_count > 0 && (
                <div className="bulk-result-row">
                  <span>Remediations queued</span>
                  <strong>{result.remediation_count}</strong>
                </div>
              )}
            </div>
            <div className="bulk-dialog-actions">
              <button className="bulk-dialog-confirm" style={{ backgroundColor: color }} onClick={onComplete}>
                Done
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
