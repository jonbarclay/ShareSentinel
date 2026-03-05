import { useEffect, useState, useCallback } from "react";
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import { apiFetch } from "../api/client";
import "./ScanActivityChart.css";

interface DataPoint {
  date: string;
  scanned: number;
  flagged: number;
}

const RANGES = [
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "90d", label: "90d" },
  { key: "180d", label: "180d" },
  { key: "ytd", label: "YTD" },
  { key: "all", label: "All" },
] as const;

type RangeKey = (typeof RANGES)[number]["key"];

function formatLabel(isoDate: string, range: RangeKey): string {
  const d = new Date(isoDate);
  if (range === "24h") {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (range === "7d" || range === "30d") {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "2-digit",
  });
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function CustomTooltip({ active, payload, label, range }: any) {
  if (!active || !payload?.length) return null;
  const d = new Date(label);
  const heading =
    range === "24h"
      ? d.toLocaleString()
      : d.toLocaleDateString([], {
          weekday: "short",
          month: "short",
          day: "numeric",
          year: "numeric",
        });

  return (
    <div className="chart-tooltip">
      <p className="chart-tooltip-date">{heading}</p>
      {payload.map((entry: any) => (
        <p key={entry.dataKey} className="chart-tooltip-row">
          <span
            className="chart-tooltip-swatch"
            style={{ background: entry.color }}
          />
          {entry.name}:{" "}
          <strong>{entry.value}</strong>
        </p>
      ))}
    </div>
  );
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export default function ScanActivityChart() {
  const [range, setRange] = useState<RangeKey>("30d");
  const [data, setData] = useState<DataPoint[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async (r: RangeKey) => {
    setLoading(true);
    try {
      const rows = await apiFetch<DataPoint[]>(
        `/stats/scan-activity?range=${r}`,
      );
      setData(rows);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData(range);
  }, [range, fetchData]);

  return (
    <div className="scan-activity-chart card">
      <div className="chart-header">
        <h3
          className="section-title"
          style={{ border: "none", margin: 0, padding: 0 }}
        >
          Scan Activity
        </h3>
        <div className="range-selector">
          {RANGES.map((r) => (
            <button
              key={r.key}
              className={`range-btn${range === r.key ? " active" : ""}`}
              onClick={() => setRange(r.key)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="chart-body">
        {loading ? (
          <div className="chart-loading">Loading...</div>
        ) : data.length === 0 ? (
          <div className="chart-empty">No data for this range.</div>
        ) : (
          <ResponsiveContainer width="100%" height={340}>
            <ComposedChart
              data={data}
              margin={{ top: 8, right: 12, left: 4, bottom: 0 }}
            >
              <defs>
                <linearGradient id="gradScanned" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3f9f5f" stopOpacity={0.2} />
                  <stop offset="95%" stopColor="#3f9f5f" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#e8e8e8"
                vertical={false}
              />
              <XAxis
                dataKey="date"
                tickFormatter={(v) => formatLabel(v, range)}
                tick={{ fontSize: 12 }}
                stroke="#aaa"
                tickLine={false}
              />

              {/* Left axis — total files scanned (green) */}
              <YAxis
                yAxisId="left"
                allowDecimals={false}
                tick={{ fontSize: 12, fill: "#3f9f5f" }}
                stroke="#3f9f5f"
                tickLine={false}
                axisLine={false}
                label={{
                  value: "Files Scanned",
                  angle: -90,
                  position: "insideLeft",
                  offset: 10,
                  style: { fontSize: 11, fill: "#3f9f5f", fontWeight: 600 },
                }}
              />

              {/* Right axis — flagged files (red), independent scale */}
              <YAxis
                yAxisId="right"
                orientation="right"
                allowDecimals={false}
                tick={{ fontSize: 12, fill: "#d94040" }}
                stroke="#d94040"
                tickLine={false}
                axisLine={false}
                label={{
                  value: "Flagged (Tier 1/2)",
                  angle: 90,
                  position: "insideRight",
                  offset: 10,
                  style: { fontSize: 11, fill: "#d94040", fontWeight: 600 },
                }}
              />

              <Tooltip
                content={<CustomTooltip range={range} />}
              />
              <Legend
                iconType="plainline"
                formatter={(value: string) => (
                  <span style={{ fontSize: 12.5, color: "#444" }}>{value}</span>
                )}
              />

              {/* Bars first so the area line draws on top */}
              <Bar
                yAxisId="right"
                dataKey="flagged"
                name="Flagged (Tier 1/2)"
                fill="#d94040"
                opacity={0.75}
                radius={[3, 3, 0, 0]}
                barSize={data.length > 60 ? 4 : data.length > 30 ? 8 : 14}
                animationDuration={800}
                animationEasing="ease-out"
              />
              <Area
                yAxisId="left"
                type="monotone"
                dataKey="scanned"
                name="Files Scanned"
                stroke="#3f9f5f"
                strokeWidth={2.5}
                fill="url(#gradScanned)"
                dot={false}
                activeDot={{ r: 4, strokeWidth: 0, fill: "#3f9f5f" }}
                animationDuration={800}
                animationEasing="ease-out"
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
