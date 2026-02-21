import { uvu, card } from "../theme";

interface Props {
  label: string;
  value: string | number;
  sub?: string;
}

export default function StatsCard({ label, value, sub }: Props) {
  return (
    <div
      style={{
        ...card,
        minWidth: 160,
        borderTop: `3px solid ${uvu.green}`,
      }}
    >
      <div style={{ fontSize: "0.75rem", color: uvu.textMuted, marginBottom: 4, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      <div style={{ fontSize: "1.6rem", fontWeight: 700, color: uvu.text }}>{value}</div>
      {sub && <div style={{ fontSize: "0.75rem", color: uvu.textMuted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}
