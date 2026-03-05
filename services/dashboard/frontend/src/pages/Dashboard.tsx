import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiFetch } from "../api/client";
import StatsCard from "../components/StatsCard";
import EventTable from "../components/EventTable";
import ScanActivityChart from "../components/ScanActivityChart";
import "./Dashboard.css";

interface Stats {
  events: { total: number; completed: number; processing: number; failed: number };
  verdicts: { total_verdicts: number; escalated: number; tier_1_count: number; tier_2_count: number; reviewed: number; unreviewed_escalated: number; unreviewed_tier_1: number; total_cost: number | null };
  total_files_scanned: number;
  queue_depth: number | null;
  needs_review: Array<Record<string, unknown>>;
}

export default function Dashboard() {
  const nav = useNavigate();
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    apiFetch<Stats>("/stats").then(setStats);
    const id = setInterval(() => {
      apiFetch<Stats>("/stats").then(setStats);
    }, 10_000);
    return () => clearInterval(id);
  }, []);

  if (!stats) return <div className="loading-state">Loading dashboard data...</div>;

  const v = stats.verdicts;
  return (
    <div className="dashboard-container">
      <h2 className="page-title">Dashboard Overview</h2>

      <div className="stats-grid">
        <StatsCard label="Needs Review" value={v.unreviewed_escalated ?? 0} onClick={() => nav("/events?tier=escalated&hide_reviewed=1")} />
        <StatsCard label="Urgent Unreviewed" value={v.unreviewed_tier_1 ?? 0} onClick={() => nav("/events?tier=tier_1&hide_reviewed=1")} />
        <StatsCard label="Files Scanned" value={stats.total_files_scanned} />
        <StatsCard label="Total Events" value={stats.events.total} onClick={() => nav("/events")} />
        <StatsCard label="Completed" value={stats.events.completed} onClick={() => nav("/events?status=completed")} />
        <StatsCard label="Processing" value={stats.events.processing} onClick={() => nav("/events?status=processing")} />
        <StatsCard label="Queued" value={stats.queue_depth ?? 0} />
        <StatsCard label="Failed" value={stats.events.failed} onClick={() => nav("/events?status=failed")} />
        <StatsCard label="Reviewed" value={v.reviewed ?? 0} sub={`of ${v.total_verdicts ?? 0}`} onClick={() => nav("/events?reviewed=true")} />
        <StatsCard label="Total AI Cost" value={`$${(v.total_cost ?? 0).toFixed(2)}`} />
      </div>

      <ScanActivityChart />

      <div className="recent-events-section card">
        <h3 className="section-title">Needs Review</h3>
        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        <EventTable events={stats.needs_review as any} />
      </div>
    </div>
  );
}
