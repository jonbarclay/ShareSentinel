// Project brand palette

export const uvu = {
  // Brand
  green: "#275d38",
  greenD2: "#1e482c",
  greenL1: "#3f6f4e",
  greenL3: "#6f937a",
  gold: "#d2ac5f",
  brick: "#b45336",
  lakeCalm: "#87c7ba",
  seaHaze: "#c4d6c1",

  // Neutrals
  text: "#1a1a1a",
  textSecondary: "#555",
  textMuted: "#888",
  border: "#e2e2e2",
  divider: "#f0f0f0",
  surface: "#fff",
  bg: "#f7f7f8",
  hover: "#fafafa",
} as const;

// Shared card style
export const card: React.CSSProperties = {
  background: uvu.surface,
  borderRadius: 10,
  padding: "1.25rem",
  border: `1px solid ${uvu.border}`,
};
