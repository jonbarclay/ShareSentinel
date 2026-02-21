import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import StatsCard from "../components/StatsCard";
import EventTable from "../components/EventTable";

interface Stats {
  events: { total: number; completed: number; processing: number; failed: number };
  verdicts: { total_verdicts: number; avg_rating: number | null; high_risk: number; reviewed: number; total_cost: number | null };
  recent_high_risk: Array<Record<string, unknown>>;
}

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [recent, setRecent] = useState<Array<Record<string, unknown>>>([]);

  useEffect(() => {
    apiFetch<Stats>("/stats").then(setStats);
    apiFetch<{ events: Array<Record<string, unknown>> }>("/events?per_page=10").then(
      (d) => setRecent(d.events)
    );
    const id = setInterval(() => {
      apiFetch<Stats>("/stats").then(setStats);
      apiFetch<{ events: Array<Record<string, unknown>> }>("/events?per_page=10").then(
        (d) => setRecent(d.events)
      );
    }, 10_000);
    return () => clearInterval(id);
  }, []);

  if (!stats) return <div>Loading...</div>;

  const v = stats.verdicts;
  return (
    <div>
      <h2 style={{ marginBottom: "1rem" }}>Dashboard</h2>
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1.5rem" }}>
        <StatsCard label="Total Events" value={stats.events.total} />
        <StatsCard label="Completed" value={stats.events.completed} />
        <StatsCard label="Processing" value={stats.events.processing} />
        <StatsCard label="Failed" value={stats.events.failed} />
        <StatsCard label="High Risk (4-5)" value={v.high_risk ?? 0} />
        <StatsCard label="Avg Rating" value={v.avg_rating?.toFixed(1) ?? "—"} />
        <StatsCard label="Reviewed" value={v.reviewed ?? 0} />
        <StatsCard label="Total AI Cost" value={`$${(v.total_cost ?? 0).toFixed(2)}`} />
      </div>

      <h3 style={{ marginBottom: "0.5rem" }}>Recent Events</h3>
      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
      <EventTable events={recent as any} />
    </div>
  );
}
