import { useNavigate } from "react-router-dom";
import "./VerdictBadge.css";

const TIER_COLORS: Record<string, { bg: string; text: string }> = {
  tier_1: { bg: "#de350b", text: "#fff" },
  tier_2: { bg: "#ff991f", text: "#172b4d" },
  none: { bg: "#dfe1e6", text: "#6b778c" },
};

const CATEGORY_LABELS: Record<string, string> = {
  pii_government_id: "Gov PII",
  pii_financial: "Financial",
  ferpa: "FERPA",
  hipaa: "HIPAA",
  security_credentials: "Credentials",
  hr_personnel: "HR/Personnel",
  legal_confidential: "Legal",
  pii_contact: "Contact PII",
  coursework: "Coursework",
  casual_personal: "Personal",
  none: "None",
};

interface CategoryAssessment {
  id: string;
  confidence: string;
  evidence?: string;
}

export default function VerdictBadge({
  tier,
  categories,
}: {
  tier: string | null;
  categories?: CategoryAssessment[] | null;
}) {
  const nav = useNavigate();

  if (!tier && !categories?.length)
    return <span className="verdict-na">N/A</span>;

  const effectiveTier = tier || "none";
  const colors = TIER_COLORS[effectiveTier] || TIER_COLORS.none;
  const cats = categories || [];

  const handleCategoryClick = (e: React.MouseEvent, categoryId: string) => {
    e.stopPropagation();
    nav(`/events?category=${categoryId}`);
  };

  return (
    <span className="verdict-categories">
      {cats
        .filter((c) => c.id !== "none")
        .slice(0, 3)
        .map((c, i) => (
          <span
            key={i}
            className="category-chip clickable"
            style={{ backgroundColor: colors.bg, color: colors.text }}
            title={c.evidence || c.id}
            onClick={(e) => handleCategoryClick(e, c.id)}
          >
            {CATEGORY_LABELS[c.id] || c.id}
          </span>
        ))}
      {cats.length === 0 || (cats.length === 1 && cats[0].id === "none") ? (
        <span
          className="category-chip"
          style={{ backgroundColor: "#dfe1e6", color: "#6b778c" }}
        >
          None
        </span>
      ) : null}
    </span>
  );
}

export function TierBadge({ tier }: { tier: string | null }) {
  if (!tier) return <span className="verdict-na">—</span>;
  const colors = TIER_COLORS[tier] || TIER_COLORS.none;
  const label = tier === "tier_1" ? "Tier 1" : tier === "tier_2" ? "Tier 2" : "No Escalation";
  return (
    <span
      className="verdict-badge"
      style={{ backgroundColor: colors.bg, color: colors.text }}
    >
      {label}
    </span>
  );
}
