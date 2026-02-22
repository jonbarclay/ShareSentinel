import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiFetch } from "../api/client";
import StatsCard from "../components/StatsCard";
import { uvu, card } from "../theme";

interface ProviderRow {
  ai_provider: string;
  count: number;
  total_cost: number | null;
  avg_latency: number | null;
}

interface CategoryRow {
  category_id: string;
  count: number;
}

interface TierRow {
  escalation_tier: string;
  count: number;
}

interface Stats {
  events: { total: number; completed: number; processing: number; failed: number };
  verdicts: { total_verdicts: number; escalated: number; tier_1_count: number; tier_2_count: number; reviewed: number; total_cost: number | null };
  by_provider: ProviderRow[];
  by_category: CategoryRow[];
  by_tier: TierRow[];
}

const CATEGORY_LABELS: Record<string, string> = {
  pii_government_id: "Government PII",
  pii_financial: "Financial Data",
  ferpa: "FERPA Records",
  hipaa: "HIPAA Health Info",
  security_credentials: "Security Credentials",
  hr_personnel: "HR/Personnel",
  legal_confidential: "Legal/Confidential",
  pii_contact: "Contact PII",
  coursework: "Coursework",
  casual_personal: "Personal Content",
  none: "No Sensitive Content",
};

const TIER_COLORS: Record<string, string> = {
  tier_1: uvu.brick,
  tier_2: uvu.gold,
  none: uvu.lakeCalm,
};

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 14px", borderBottom: `2px solid ${uvu.border}`, fontSize: "0.75rem", color: uvu.textMuted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em",
};
const tdStyle: React.CSSProperties = { padding: "8px 14px", borderBottom: `1px solid ${uvu.divider}`, fontSize: "0.85rem", color: uvu.text };

function categoryBarColor(catId: string): string {
  const tier1 = new Set(["pii_government_id", "pii_financial", "ferpa", "hipaa", "security_credentials"]);
  const tier2 = new Set(["hr_personnel", "legal_confidential", "pii_contact"]);
  if (tier1.has(catId)) return uvu.brick;
  if (tier2.has(catId)) return uvu.gold;
  return uvu.lakeCalm;
}

export default function Statistics() {
  const nav = useNavigate();
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    apiFetch<Stats>("/stats").then(setStats);
    const id = setInterval(() => { apiFetch<Stats>("/stats").then(setStats); }, 30_000);
    return () => clearInterval(id);
  }, []);

  if (!stats) return <div>Loading...</div>;

  const v = stats.verdicts;
  const maxCatCount = Math.max(...stats.by_category.map((r) => r.count), 1);

  return (
    <div>
      <h2 style={{ marginBottom: "1rem", fontWeight: 700 }}>Statistics</h2>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1.5rem" }}>
        <StatsCard label="Total Events" value={stats.events.total} />
        <StatsCard label="Total Verdicts" value={v.total_verdicts ?? 0} />
        <StatsCard label="Escalated" value={v.escalated ?? 0} />
        <StatsCard label="Tier 1 (Urgent)" value={v.tier_1_count ?? 0} />
        <StatsCard label="Tier 2 (Normal)" value={v.tier_2_count ?? 0} />
        <StatsCard label="Reviewed" value={v.reviewed ?? 0} sub={`of ${v.total_verdicts ?? 0}`} />
        <StatsCard label="Total AI Cost" value={`$${(v.total_cost ?? 0).toFixed(2)}`} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem", marginBottom: "1.5rem" }}>
        <div style={card}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>Category Distribution</h3>
          {stats.by_category.map((r) => (
            <div
              key={r.category_id}
              style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, cursor: "pointer" }}
              onClick={() => nav(`/events?category=${r.category_id}&hide_reviewed=1`)}
              title={`Filter events by ${CATEGORY_LABELS[r.category_id] || r.category_id}`}
            >
              <span style={{ width: 130, textAlign: "right", fontWeight: 500, fontSize: "0.8rem", color: uvu.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {CATEGORY_LABELS[r.category_id] || r.category_id}
              </span>
              <div style={{ flex: 1, background: uvu.divider, borderRadius: 5, height: 22 }}>
                <div
                  style={{
                    width: `${(r.count / maxCatCount) * 100}%`,
                    background: categoryBarColor(r.category_id),
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
          {stats.by_category.length === 0 && (
            <div style={{ color: uvu.textMuted, fontSize: "0.85rem" }}>No category data yet.</div>
          )}
        </div>

        <div style={card}>
          <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>By Provider</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={thStyle}>Provider</th>
                <th style={thStyle}>Count</th>
                <th style={thStyle}>Total Cost</th>
                <th style={thStyle}>Avg Latency</th>
              </tr>
            </thead>
            <tbody>
              {stats.by_provider.map((p) => (
                <tr key={p.ai_provider}>
                  <td style={{ ...tdStyle, fontWeight: 500 }}>{p.ai_provider}</td>
                  <td style={tdStyle}>{p.count}</td>
                  <td style={tdStyle}>${(p.total_cost ?? 0).toFixed(4)}</td>
                  <td style={tdStyle}>{p.avg_latency?.toFixed(2) ?? "—"}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div style={card}>
        <h3 style={{ marginBottom: "1rem", fontSize: "0.95rem", fontWeight: 600 }}>By Escalation Tier</h3>
        <div style={{ display: "flex", gap: "2rem" }}>
          {stats.by_tier.map((t) => (
            <div key={t.escalation_tier} style={{ textAlign: "center" }}>
              <div style={{
                display: "inline-block",
                width: 60, height: 60,
                borderRadius: "50%",
                background: TIER_COLORS[t.escalation_tier] || uvu.lakeCalm,
                color: "#fff",
                lineHeight: "60px",
                fontSize: "1.2rem",
                fontWeight: 700,
              }}>{t.count}</div>
              <div style={{ marginTop: 6, fontSize: "0.8rem", color: uvu.textSecondary, fontWeight: 500 }}>
                {t.escalation_tier === "tier_1" ? "Tier 1" : t.escalation_tier === "tier_2" ? "Tier 2" : "No Escalation"}
              </div>
            </div>
          ))}
          {stats.by_tier.length === 0 && (
            <div style={{ color: uvu.textMuted, fontSize: "0.85rem" }}>No tier data yet.</div>
          )}
        </div>
      </div>
    </div>
  );
}
