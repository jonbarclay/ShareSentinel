import "./BulkActionBar.css";

const DISPOSITIONS = [
  { value: "true_positive", label: "True Positive", color: "#8b1a1a" },
  { value: "moderate_risk", label: "Moderate Risk", color: "#d97706" },
  { value: "acceptable_risk", label: "Acceptable Risk", color: "#2563eb" },
  { value: "needs_investigation", label: "Needs Investigation", color: "#ca8a04" },
  { value: "false_positive", label: "False Positive", color: "#16a34a" },
];

interface BulkActionBarProps {
  selectedCount: number;
  onDispositionClick: (disposition: string) => void;
  onClearSelection: () => void;
}

export default function BulkActionBar({ selectedCount, onDispositionClick, onClearSelection }: BulkActionBarProps) {
  return (
    <div className="bulk-action-bar">
      <span className="bulk-selected-count">{selectedCount} selected</span>
      <div className="bulk-action-buttons">
        {DISPOSITIONS.map((d) => (
          <button
            key={d.value}
            className="bulk-disposition-btn"
            style={{ backgroundColor: d.color }}
            onClick={() => onDispositionClick(d.value)}
          >
            {d.label}
          </button>
        ))}
      </div>
      <button className="bulk-clear-btn" onClick={onClearSelection}>
        Clear
      </button>
    </div>
  );
}
