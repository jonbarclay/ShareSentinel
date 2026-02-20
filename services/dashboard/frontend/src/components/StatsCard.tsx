import "./StatsCard.css";

interface Props {
  label: string;
  value: string | number;
  sub?: string;
  onClick?: () => void;
}

export default function StatsCard({ label, value, sub, onClick }: Props) {
  return (
    <div className={`card stats-card ${onClick ? "clickable" : ""}`} onClick={onClick}>
      <div className="stats-label">{label}</div>
      <div className="stats-value">{value}</div>
      {sub && <div className="stats-sub" style={{ fontSize: "0.75rem", color: "var(--uvu-text-muted)", marginTop: "4px" }}>{sub}</div>}
    </div>
  );
}
