import { useEffect, useState } from "react";
import { apiFetch, apiPatch } from "../api/client";
import { useIsAdmin } from "../context/AuthContext";
import "./AdminSettings.css";

interface Setting {
  key: string;
  value: string;
  description: string;
  category: string;
  data_type: string;
  display_name: string;
  updated_at: string | null;
  updated_by: string | null;
}

interface SettingsResponse {
  categories: Record<string, Setting[]>;
}

const CATEGORY_ORDER = ["email", "ai", "notifications", "processing", "lifecycle", "audit", "general"];
const CATEGORY_LABELS: Record<string, string> = {
  email: "Email",
  ai: "AI Provider",
  notifications: "Notifications",
  processing: "Processing",
  lifecycle: "Lifecycle",
  audit: "Audit Polling",
  general: "General",
};

export default function AdminSettings() {
  const isAdmin = useIsAdmin();
  const [categories, setCategories] = useState<Record<string, Setting[]>>({});
  const [changes, setChanges] = useState<Record<string, string>>({});
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiFetch<SettingsResponse>("/admin/settings")
      .then((data) => setCategories(data.categories))
      .catch(() => setFeedback({ type: "error", message: "Failed to load settings" }))
      .finally(() => setLoading(false));
  }, []);

  if (!isAdmin) {
    return <div className="admin-forbidden"><h2>Access Denied</h2><p>You need admin privileges to access this page.</p></div>;
  }

  const handleChange = (key: string, value: string) => {
    setChanges((prev) => ({ ...prev, [key]: value }));
  };

  const hasChanges = Object.keys(changes).length > 0;

  const handleSave = async () => {
    setSaving(true);
    setFeedback(null);
    try {
      const settings = Object.entries(changes).map(([key, value]) => ({ key, value }));
      const result = await apiPatch<{ updated: string[]; errors: { key: string; error: string }[] }>(
        "/admin/settings",
        { settings }
      );
      if (result.errors?.length) {
        setFeedback({ type: "error", message: `Errors: ${result.errors.map((e) => `${e.key}: ${e.error}`).join(", ")}` });
      } else {
        setFeedback({ type: "success", message: `Updated ${result.updated.length} setting(s). Restart worker/lifecycle-cron to apply.` });
        setChanges({});
        // Reload settings
        const data = await apiFetch<SettingsResponse>("/admin/settings");
        setCategories(data.categories);
      }
    } catch (err: any) {
      setFeedback({ type: "error", message: err.message || "Failed to save settings" });
    } finally {
      setSaving(false);
    }
  };

  const toggleCategory = (cat: string) => {
    setCollapsed((prev) => ({ ...prev, [cat]: !prev[cat] }));
  };

  const currentValue = (setting: Setting) => {
    return changes[setting.key] !== undefined ? changes[setting.key] : setting.value;
  };

  const renderInput = (setting: Setting) => {
    const val = currentValue(setting);
    const isModified = changes[setting.key] !== undefined;

    if (setting.data_type === "boolean") {
      return (
        <label className={`toggle-label ${isModified ? "modified" : ""}`}>
          <input
            type="checkbox"
            checked={val.toLowerCase() === "true"}
            onChange={(e) => handleChange(setting.key, e.target.checked ? "true" : "false")}
          />
          <span>{val.toLowerCase() === "true" ? "Enabled" : "Disabled"}</span>
        </label>
      );
    }

    if (setting.data_type === "select" && setting.key === "ai_provider") {
      return (
        <select
          className={`setting-input ${isModified ? "modified" : ""}`}
          value={val}
          onChange={(e) => handleChange(setting.key, e.target.value)}
        >
          <option value="">Use environment default</option>
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
          <option value="gemini">Gemini</option>
        </select>
      );
    }

    if (setting.data_type === "select" && setting.key === "second_look_provider") {
      return (
        <select
          className={`setting-input ${isModified ? "modified" : ""}`}
          value={val}
          onChange={(e) => handleChange(setting.key, e.target.value)}
        >
          <option value="">Use environment default</option>
          <option value="gemini">Gemini</option>
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
        </select>
      );
    }

    if (setting.data_type === "select" && setting.key === "log_level") {
      return (
        <select
          className={`setting-input ${isModified ? "modified" : ""}`}
          value={val}
          onChange={(e) => handleChange(setting.key, e.target.value)}
        >
          <option value="">Use environment default</option>
          <option value="DEBUG">DEBUG</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>
      );
    }

    return (
      <input
        type={setting.data_type === "int" || setting.data_type === "float" ? "number" : "text"}
        className={`setting-input ${isModified ? "modified" : ""}`}
        value={val}
        placeholder="Using environment default"
        step={setting.data_type === "float" ? "0.1" : undefined}
        onChange={(e) => handleChange(setting.key, e.target.value)}
      />
    );
  };

  if (loading) return <div className="admin-loading">Loading settings...</div>;

  const sortedCategories = CATEGORY_ORDER.filter((c) => categories[c]);

  return (
    <div className="admin-settings-container">
      <div className="admin-settings-header">
        <h2>Admin Settings</h2>
        <div className="admin-settings-actions">
          {feedback && (
            <span className={`feedback-msg ${feedback.type}`}>{feedback.message}</span>
          )}
          <button className="save-btn" onClick={handleSave} disabled={!hasChanges || saving}>
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>

      {sortedCategories.map((cat) => (
        <div key={cat} className="settings-category">
          <div className="category-header" onClick={() => toggleCategory(cat)}>
            <h3>{CATEGORY_LABELS[cat] || cat}</h3>
            <span className="category-toggle">{collapsed[cat] ? "+" : "-"}</span>
          </div>
          {!collapsed[cat] && (
            <div className="category-body">
              {categories[cat].map((setting) => (
                <div key={setting.key} className="setting-row">
                  <div className="setting-label">
                    <span className="setting-name">{setting.display_name || setting.key}</span>
                    {setting.description && <span className="setting-desc">{setting.description}</span>}
                  </div>
                  <div className="setting-control">
                    {renderInput(setting)}
                    {setting.updated_by && (
                      <span className="setting-meta">
                        Last changed by {setting.updated_by}
                        {setting.updated_at && ` on ${new Date(setting.updated_at).toLocaleDateString()}`}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
