import { useEffect, useRef, useState } from "react";
import { apiFetch, apiPatch } from "../api/client";
import { uvu } from "../theme";

interface Props {
  eventId: string;
  currentDisposition?: string | null;
  currentNotes?: string | null;
  onSaved: () => void;
}

interface RemediationStatus {
  id: number;
  status: string;
  permissions_removed: number;
  permissions_failed: number;
  report_sent: boolean;
  error_message: string | null;
}

const DISPOSITIONS = [
  { value: "true_positive", label: "True Positive", color: "#8b1a1a", desc: "Confirmed sensitive" },
  { value: "moderate_risk", label: "Moderate Risk", color: "#d97706", desc: "Some risk, monitor closely" },
  { value: "acceptable_risk", label: "Acceptable Risk", color: "#2563eb", desc: "Reviewed, no action needed" },
  { value: "needs_investigation", label: "Needs Investigation", color: "#ca8a04", desc: "Requires follow-up" },
  { value: "false_positive", label: "False Positive", color: "#16a34a", desc: "Not actually sensitive" },
] as const;

const BADGE_STYLES: Record<string, { bg: string; fg: string; label: string }> = {
  pending: { bg: "#fffae6", fg: "#ca8a04", label: "Queued" },
  in_progress: { bg: "#deebff", fg: "#0052cc", label: "Processing..." },
  completed: { bg: "#e3fcef", fg: "#00875a", label: "Sharing link removed and report sent" },
  failed: { bg: "#ffebe6", fg: "#de350b", label: "Remediation failed" },
  skipped: { bg: "#f4f5f7", fg: "#6b778c", label: "Skipped" },
};

export default function AnalystReviewForm({
  eventId,
  currentDisposition,
  currentNotes,
  onSaved,
}: Props) {
  const [disposition, setDisposition] = useState(currentDisposition ?? "");
  const [notes, setNotes] = useState(currentNotes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remediation, setRemediation] = useState<RemediationStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Load existing remediation status on mount (for already-reviewed true positives)
  useEffect(() => {
    if (currentDisposition === "true_positive") {
      fetchRemediation();
    }
    return () => stopPolling();
  }, [eventId, currentDisposition]);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  async function fetchRemediation() {
    try {
      const res = await apiFetch<{ remediation: RemediationStatus | null }>(
        `/remediations/${eventId}`
      );
      if (res.remediation) {
        setRemediation(res.remediation);
        if (["completed", "failed", "skipped"].includes(res.remediation.status)) {
          stopPolling();
        }
      }
    } catch {
      // Silently ignore — endpoint may not exist yet during migration
    }
  }

  function startPolling() {
    stopPolling();
    pollRef.current = setInterval(fetchRemediation, 5000);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();

    if (
      disposition === "true_positive" &&
      !window.confirm(
        "Marking as True Positive will remove the sharing link and send a report " +
        "to the file owner and security team. Continue?"
      )
    ) {
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await apiPatch<{
        status: string;
        event_id: string;
        remediation_id: number | null;
        remediation_status: string | null;
      }>(`/verdicts/${eventId}`, { disposition, notes });

      if (res.remediation_id) {
        setRemediation({
          id: res.remediation_id,
          status: res.remediation_status ?? "pending",
          permissions_removed: 0,
          permissions_failed: 0,
          report_sent: false,
          error_message: null,
        });
        startPolling();
      }
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save review");
    } finally {
      setSaving(false);
    }
  }

  const badge = remediation ? BADGE_STYLES[remediation.status] ?? BADGE_STYLES.pending : null;

  return (
    <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <div style={{ fontSize: "0.8rem", color: uvu.textMuted, marginBottom: 8, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.04em" }}>
          Disposition
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          {DISPOSITIONS.map((d) => {
            const selected = disposition === d.value;
            return (
              <button
                key={d.value}
                type="button"
                onClick={() => setDisposition(d.value)}
                style={{
                  flex: 1,
                  padding: "12px 14px",
                  border: `2px solid ${d.color}`,
                  borderRadius: 8,
                  background: selected ? d.color : `${d.color}18`,
                  color: selected ? "#fff" : d.color,
                  fontWeight: 600,
                  fontSize: "0.85rem",
                  cursor: "pointer",
                  textAlign: "center",
                  boxShadow: selected ? `0 2px 8px ${d.color}40` : "none",
                }}
              >
                {d.label}
                <div style={{
                  fontSize: "0.72rem",
                  fontWeight: 400,
                  marginTop: 2,
                  opacity: selected ? 0.9 : 0.75,
                }}>
                  {d.desc}
                </div>
              </button>
            );
          })}
        </div>
      </div>
      <label>
        <div style={{ fontSize: "0.8rem", color: uvu.textMuted, marginBottom: 4, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.04em" }}>
          Notes
        </div>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          placeholder="Add context or observations..."
          style={{
            padding: 10,
            width: "100%",
            resize: "vertical",
            border: `1px solid ${uvu.border}`,
            borderRadius: 8,
            fontSize: "0.85rem",
            color: uvu.text,
          }}
        />
      </label>
      <button
        type="submit"
        disabled={saving || !disposition}
        style={{
          padding: "10px 24px",
          background: !disposition ? uvu.border : uvu.green,
          color: !disposition ? uvu.textMuted : "#fff",
          border: "none",
          borderRadius: 8,
          cursor: !disposition ? "default" : "pointer",
          fontWeight: 600,
          fontSize: "0.9rem",
          alignSelf: "flex-start",
        }}
      >
        {saving ? "Saving..." : "Save Review"}
      </button>

      {error && (
        <div
          style={{
            padding: "10px 14px",
            background: "#ffebe6",
            color: "#de350b",
            borderRadius: 8,
            fontSize: "0.85rem",
            fontWeight: 600,
            border: "1px solid #de350b30",
          }}
        >
          {error}
        </div>
      )}

      {badge && remediation && (
        <div
          style={{
            padding: "10px 14px",
            background: badge.bg,
            color: badge.fg,
            borderRadius: 8,
            fontSize: "0.85rem",
            fontWeight: 600,
            border: `1px solid ${badge.fg}30`,
          }}
        >
          {badge.label}
          {remediation.status === "completed" && remediation.permissions_removed > 0 && (
            <span style={{ fontWeight: 400, marginLeft: 8 }}>
              ({remediation.permissions_removed} permission{remediation.permissions_removed !== 1 ? "s" : ""} removed)
            </span>
          )}
          {remediation.status === "failed" && remediation.error_message && (
            <div style={{ fontWeight: 400, marginTop: 4, fontSize: "0.8rem" }}>
              {remediation.error_message}
            </div>
          )}
        </div>
      )}
    </form>
  );
}
