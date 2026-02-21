import { uvu } from "../theme";

const COLORS: Record<number, { bg: string; fg: string }> = {
  1: { bg: "#f0f0f0", fg: uvu.textMuted },
  2: { bg: uvu.seaHaze, fg: uvu.greenD2 },
  3: { bg: "#f5e6b8", fg: "#7a6520" },
  4: { bg: uvu.brick, fg: "#fff" },
  5: { bg: "#8a3520", fg: "#fff" },
};

export default function VerdictBadge({ rating }: { rating: number | null }) {
  if (rating == null || rating === 0)
    return <span style={{ color: uvu.textMuted, fontSize: "0.85rem" }}>N/A</span>;
  const c = COLORS[rating] ?? COLORS[3];
  return (
    <span
      style={{
        background: c.bg,
        color: c.fg,
        padding: "3px 10px",
        borderRadius: 5,
        fontWeight: 600,
        fontSize: "0.82rem",
        letterSpacing: "0.01em",
      }}
    >
      {rating}/5
    </span>
  );
}
