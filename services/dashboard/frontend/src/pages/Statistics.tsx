import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import StatsCard from "../components/StatsCard";
import { uvu, card } from "../theme";

interface ProviderRow {
  ai_provider: string;
  count: number;
  avg_rating: number | null;
  total_cost: number | null;
  avg_latency: number | null;
}

interface RatingRow {
  sensitivity_rating: number;
  count: number;
}

interface Stats {
  events: { total: number; completed: number; processing: number; failed: number };
  verdicts: { total_verdicts: number; avg_rating: number | null; high_risk: number; reviewed: number; total_cost: number | null };
  by_provider: ProviderRow[];
  by_rating: RatingRow[];
}

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 14px", borderBottom: `2px solid ${uvu.border}`, fontSize: "0.75rem", color: uvu.textMuted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em",
};
const tdStyle: React.CSSProperties = { padding: "8px 14px", borderBottom: `1px solid ${uvu.divider}`, fontSize: "0.85rem", color: uvu.text };

function ratingBarColor(rating: number): string {
  if (rating >= 4) return uvu.brick;
  if (rating === 3) return uvu.gold;
  return uvu.lakeCalm;
}

export default function Statistics() {
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => { apiFetch<Stats>("/stats").then(setStats); }, []);

  if (!stats) return <div>Loading...</div>;

  const v = stats.verdicts;
  const maxCount = Math.max(...stats.by_rating.map((r) => r.count), 1);

  return (
    <div>
      <h2 style={{ marginBottom: "1rem", fontWeight: 700 }}>Statistics</h2>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1.5rem" }}>
        <StatsCard label="Total Events" value={stats.events.total} />
        <StatsCard label="Total Verdicts" value={v.total_verdicts ?? 0} />
        <StatsCard label="Avg Rating" value={v.avg_rating?.toFixed(1) ?? "—"} />
        <StatsCard label="High Risk" value={v.high_risk ?? 0} />
        <StatsCard label="Reviewed" value={v.reviewed ?? 0} sub={`of ${v.total_verdicts ?? 0}`} />
        <StatsCard label="Total AI Cost" value={`$${(v.total_cost ?? 0).toFixed(2)}`} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem", marginBottom: "1.5rem" }}>
        <div style={card}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Rating Distribution</h3>
          {stats.by_rating.map((r) => (
            <div key={r.sensitivity_rating} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
              <span style={{ width: 24, textAlign: "right", fontWeight: 600, fontSize: "0.9rem", color: uvu.text }}>{r.sensitivity_rating}</span>
              <div style={{ flex: 1, background: uvu.divider, borderRadius: 5, height: 22 }}>
                <div
                  style={{
                    width: `${(r.count / maxCount) * 100}%`,
                    background: ratingBarColor(r.sensitivity_rating),
                    height: "100%",
                    borderRadius: 5,
                    minWidth: r.count > 0 ? 4 : 0,
                    transition: "width 0.3s ease",
                  }}
                />
              </div>
              <span style={{ width: 40, fontSize: "0.85rem", color: uvu.textSecondary }}>{r.count}</span>
            </div>
          ))}
        </div>

        <div style={card}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>By Provider</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={thStyle}>Provider</th>
                <th style={thStyle}>Count</th>
                <th style={thStyle}>Avg Rating</th>
                <th style={thStyle}>Total Cost</th>
                <th style={thStyle}>Avg Latency</th>
              </tr>
            </thead>
            <tbody>
              {stats.by_provider.map((p) => (
                <tr key={p.ai_provider}>
                  <td style={{ ...tdStyle, fontWeight: 500 }}>{p.ai_provider}</td>
                  <td style={tdStyle}>{p.count}</td>
                  <td style={tdStyle}>{p.avg_rating?.toFixed(1) ?? "—"}</td>
                  <td style={tdStyle}>${(p.total_cost ?? 0).toFixed(4)}</td>
                  <td style={tdStyle}>{p.avg_latency?.toFixed(2) ?? "—"}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
