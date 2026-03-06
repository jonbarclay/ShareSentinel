import { useEffect, useState, useCallback, useRef } from "react";
import { apiFetch, apiPost, apiDelete } from "../api/client";
import "./AllowList.css";

// --- Types ---

interface AllowListSite {
  id: number;
  site_id: string;
  site_url: string;
  site_display_name: string;
  added_by: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

interface SearchResult {
  site_id: string;
  display_name: string;
  web_url: string;
  already_allowed: boolean;
}

interface VisibilitySite {
  id: number;
  group_id: string;
  site_url: string;
  group_display_name: string;
  added_by: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

interface GroupSearchResult {
  group_id: string;
  display_name: string;
  visibility: string;
  site_url: string;
  already_allowed: boolean;
}

interface SiteDetails {
  group_id: string;
  site_url: string;
  visibility: string;
  description: string;
  created_datetime: string;
  sharing_capability: string;
  mail?: string;
  owners: { displayName: string; mail: string }[];
  members: { displayName: string; mail: string }[];
  member_count: number;
}

interface PolicyEvent {
  id: number;
  scan_id: number;
  policy_type: string;
  site_url: string;
  site_display_name: string;
  group_id: string;
  previous_value: string;
  new_value: string;
  action: string;
  error_message: string | null;
  created_at: string;
}

interface PolicyScan {
  id: number;
  trigger_type: string;
  triggered_by: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  total_sites_scanned: number;
  visibility_violations_found: number;
  visibility_remediated: number;
  sharing_violations_found: number;
  sharing_remediated: number;
  errors: number;
  error_message: string | null;
  created_at: string;
}

interface EventSummary {
  last_scan: PolicyScan | null;
  last_30_days: {
    visibility_remediated_30d: number;
    sharing_remediated_30d: number;
    errors_30d: number;
  };
}

type TabId = "sharing" | "visibility" | "events";

function formatDate(iso: string | null) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

// --- Shared: Expandable Site Detail Panel ---

function SiteDetailPanel({
  siteUrl,
  groupId,
}: {
  siteUrl: string;
  groupId?: string;
}) {
  const [details, setDetails] = useState<SiteDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams();
    if (siteUrl) params.set("site_url", siteUrl);
    if (groupId) params.set("group_id", groupId);

    apiFetch<{ details: SiteDetails | null; error?: string }>(
      `/allowlist/site-details?${params}`
    )
      .then((data) => {
        if (data.error) {
          setError(data.error);
        } else {
          setDetails(data.details);
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"))
      .finally(() => setLoading(false));
  }, [siteUrl, groupId]);

  if (loading) {
    return <div className="detail-panel"><div className="detail-loading">Loading site details...</div></div>;
  }

  if (error) {
    return <div className="detail-panel"><div className="detail-error">{error}</div></div>;
  }

  if (!details) {
    return <div className="detail-panel"><div className="detail-error">No details available</div></div>;
  }

  const sharingLabel = details.sharing_capability || "Unknown";
  const sharingIsAnonymous =
    sharingLabel === "ExternalUserAndGuestSharing" || sharingLabel === "2";

  return (
    <div className="detail-panel">
      <div className="detail-grid">
        {/* Status badges row */}
        <div className="detail-status-row">
          <div className="detail-status-item">
            <span className="detail-label">Visibility</span>
            <span
              className={`detail-badge ${
                details.visibility?.toLowerCase() === "public"
                  ? "badge-public"
                  : "badge-private"
              }`}
            >
              {details.visibility || "Unknown"}
            </span>
          </div>
          <div className="detail-status-item">
            <span className="detail-label">Anonymous Sharing</span>
            <span
              className={`detail-badge ${
                sharingIsAnonymous ? "badge-enabled" : "badge-disabled"
              }`}
            >
              {sharingIsAnonymous ? "Enabled" : "Disabled"}
            </span>
          </div>
          <div className="detail-status-item">
            <span className="detail-label">Sharing Capability</span>
            <span className="detail-value-mono">{sharingLabel}</span>
          </div>
          {details.member_count > 0 && (
            <div className="detail-status-item">
              <span className="detail-label">Members</span>
              <span className="detail-value-mono">{details.member_count}</span>
            </div>
          )}
        </div>

        {/* Description */}
        {details.description && (
          <div className="detail-section">
            <span className="detail-label">Description</span>
            <span className="detail-value">{details.description}</span>
          </div>
        )}

        {/* Metadata row */}
        <div className="detail-meta-row">
          {details.created_datetime && (
            <div className="detail-meta-item">
              <span className="detail-label">Created</span>
              <span className="detail-value">{formatDate(details.created_datetime)}</span>
            </div>
          )}
          {details.mail && (
            <div className="detail-meta-item">
              <span className="detail-label">Group Email</span>
              <span className="detail-value">{details.mail}</span>
            </div>
          )}
          {details.group_id && (
            <div className="detail-meta-item">
              <span className="detail-label">Group ID</span>
              <span className="detail-value-mono detail-value-small">{details.group_id}</span>
            </div>
          )}
        </div>

        {/* Owners */}
        {details.owners.length > 0 && (
          <div className="detail-section">
            <span className="detail-label">
              Owners ({details.owners.length})
            </span>
            <div className="detail-people-list">
              {details.owners.map((o, i) => (
                <span key={i} className="detail-person">
                  <span className="detail-person-name">{o.displayName}</span>
                  {o.mail && (
                    <span className="detail-person-email">{o.mail}</span>
                  )}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Members (first 20) */}
        {details.members.length > 0 && (
          <div className="detail-section">
            <span className="detail-label">
              Members{" "}
              {details.member_count > details.members.length
                ? `(showing ${details.members.length} of ${details.member_count})`
                : `(${details.members.length})`}
            </span>
            <div className="detail-people-list">
              {details.members.map((m, i) => (
                <span key={i} className="detail-person">
                  <span className="detail-person-name">{m.displayName}</span>
                  {m.mail && (
                    <span className="detail-person-email">{m.mail}</span>
                  )}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Tab 1: Anonymous Sharing Allow List ---

function SharingTab() {
  const [sites, setSites] = useState<AllowListSite[]>([]);
  const [sitesTotal, setSitesTotal] = useState(0);
  const [sitesPage, setSitesPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedSite, setSelectedSite] = useState<SearchResult | null>(null);
  const [notes, setNotes] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>();

  const fetchSites = useCallback(() => {
    apiFetch<{ total: number; sites: AllowListSite[] }>(
      `/allowlist/sites?page=${sitesPage}&per_page=50`
    ).then((data) => {
      setSites(data.sites);
      setSitesTotal(data.total);
    });
  }, [sitesPage]);

  useEffect(() => {
    fetchSites();
  }, [fetchSites]);

  useEffect(() => {
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    if (searchQuery.length < 2) {
      setSearchResults([]);
      setShowDropdown(false);
      return;
    }
    searchTimeout.current = setTimeout(() => {
      apiFetch<{ sites: SearchResult[] }>(
        `/allowlist/sites/search?q=${encodeURIComponent(searchQuery)}`
      ).then((data) => {
        setSearchResults(data.sites);
        setShowDropdown(true);
      });
    }, 300);
  }, [searchQuery]);

  function handleSelectSite(site: SearchResult) {
    if (site.already_allowed) return;
    setSelectedSite(site);
    setShowDropdown(false);
    setSearchQuery("");
    setNotes("");
  }

  async function handleAddSite() {
    if (!selectedSite) return;
    setActionError(null);
    try {
      await apiPost("/allowlist/sites", {
        site_id: selectedSite.site_id,
        site_url: selectedSite.web_url,
        site_display_name: selectedSite.display_name,
        notes,
      });
      setSelectedSite(null);
      setNotes("");
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to add site");
    }
  }

  async function handleRemoveSite(id: number) {
    if (!confirm("Remove this site from the allow list?")) return;
    setActionError(null);
    try {
      await apiDelete(`/allowlist/sites/${id}`);
      if (expandedId === id) setExpandedId(null);
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to remove site");
    }
  }

  const totalPages = Math.ceil(sitesTotal / 50);

  return (
    <>
      {actionError && (
        <div className="card" style={{ background: "#ffebe6", color: "#de350b", padding: "10px 14px", fontWeight: 600, marginBottom: 12 }}>
          {actionError}
        </div>
      )}

      <div className="add-site-section">
        <h3>Add Site</h3>
        {!selectedSite ? (
          <div className="search-wrapper">
            <input
              className="filter-input search-input"
              placeholder="Search SharePoint sites..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
              onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
            />
            {showDropdown && searchResults.length > 0 && (
              <div className="search-dropdown">
                {searchResults.map((site) => (
                  <div
                    key={site.site_id}
                    className="search-item"
                    onMouseDown={() => handleSelectSite(site)}
                  >
                    <div className="search-item-name">
                      {site.display_name}
                      {site.already_allowed && (
                        <span className="badge-allowed">Already allowed</span>
                      )}
                    </div>
                    <div className="search-item-url">{site.web_url}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="add-site-form">
            <span className="selected-name">
              {selectedSite.display_name} ({selectedSite.web_url})
            </span>
            <input
              className="filter-input notes-input"
              placeholder="Notes (optional)"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
            <button className="add-btn" onClick={handleAddSite}>
              Add to Allow List
            </button>
            <button className="cancel-btn" onClick={() => setSelectedSite(null)}>
              Cancel
            </button>
          </div>
        )}
      </div>

      <div className="table-wrapper card">
        {sites.length === 0 ? (
          <div className="empty-message">No sites in the allow list yet.</div>
        ) : (
          <table className="allowlist-table">
            <thead>
              <tr>
                <th style={{ width: 30 }}></th>
                <th>Site Name</th>
                <th>Site URL</th>
                <th>Added By</th>
                <th>Added At</th>
                <th>Notes</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {sites.map((site) => (
                <>
                  <tr
                    key={site.id}
                    className="expandable-row"
                    onClick={() =>
                      setExpandedId(expandedId === site.id ? null : site.id)
                    }
                  >
                    <td className="expand-toggle">
                      <span
                        className={`expand-arrow ${
                          expandedId === site.id ? "expanded" : ""
                        }`}
                      >
                        &#9654;
                      </span>
                    </td>
                    <td>{site.site_display_name || site.site_id}</td>
                    <td>
                      <a
                        href={site.site_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {site.site_url}
                      </a>
                    </td>
                    <td>{site.added_by || "-"}</td>
                    <td>{formatDate(site.created_at)}</td>
                    <td>{site.notes || "-"}</td>
                    <td>
                      <button
                        className="remove-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRemoveSite(site.id);
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                  {expandedId === site.id && (
                    <tr key={`detail-${site.id}`} className="detail-row">
                      <td colSpan={7}>
                        <SiteDetailPanel siteUrl={site.site_url} />
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {totalPages > 1 && (
        <div className="pagination-bar">
          <button
            className="pagination-btn"
            disabled={sitesPage <= 1}
            onClick={() => setSitesPage((p) => p - 1)}
          >
            Prev
          </button>
          <span className="pagination-info">
            Page {sitesPage} / {totalPages}
          </span>
          <button
            className="pagination-btn"
            disabled={sitesPage >= totalPages}
            onClick={() => setSitesPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </>
  );
}

// --- Tab 2: Public Visibility Allow List ---

function VisibilityTab() {
  const [sites, setSites] = useState<VisibilitySite[]>([]);
  const [sitesTotal, setSitesTotal] = useState(0);
  const [sitesPage, setSitesPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<GroupSearchResult[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<GroupSearchResult | null>(null);
  const [notes, setNotes] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>();

  const fetchSites = useCallback(() => {
    apiFetch<{ total: number; sites: VisibilitySite[] }>(
      `/visibility-allowlist/sites?page=${sitesPage}&per_page=50`
    ).then((data) => {
      setSites(data.sites);
      setSitesTotal(data.total);
    });
  }, [sitesPage]);

  useEffect(() => {
    fetchSites();
  }, [fetchSites]);

  useEffect(() => {
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    if (searchQuery.length < 2) {
      setSearchResults([]);
      setShowDropdown(false);
      return;
    }
    searchTimeout.current = setTimeout(() => {
      apiFetch<{ groups: GroupSearchResult[] }>(
        `/visibility-allowlist/sites/search?q=${encodeURIComponent(searchQuery)}`
      ).then((data) => {
        setSearchResults(data.groups);
        setShowDropdown(true);
      });
    }, 300);
  }, [searchQuery]);

  function handleSelectGroup(group: GroupSearchResult) {
    if (group.already_allowed) return;
    setSelectedGroup(group);
    setShowDropdown(false);
    setSearchQuery("");
    setNotes("");
  }

  async function handleAddGroup() {
    if (!selectedGroup) return;
    setActionError(null);
    try {
      await apiPost("/visibility-allowlist/sites", {
        group_id: selectedGroup.group_id,
        site_url: selectedGroup.site_url,
        group_display_name: selectedGroup.display_name,
        notes,
      });
      setSelectedGroup(null);
      setNotes("");
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to add group");
    }
  }

  async function handleRemoveSite(id: number) {
    if (!confirm("Remove this group from the visibility allow list?")) return;
    setActionError(null);
    try {
      await apiDelete(`/visibility-allowlist/sites/${id}`);
      if (expandedId === id) setExpandedId(null);
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to remove group");
    }
  }

  const totalPages = Math.ceil(sitesTotal / 50);

  return (
    <>
      {actionError && (
        <div className="card" style={{ background: "#ffebe6", color: "#de350b", padding: "10px 14px", fontWeight: 600, marginBottom: 12 }}>
          {actionError}
        </div>
      )}

      <div className="add-site-section">
        <h3>Add Group</h3>
        {!selectedGroup ? (
          <div className="search-wrapper">
            <input
              className="filter-input search-input"
              placeholder="Search M365 groups..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
              onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
            />
            {showDropdown && searchResults.length > 0 && (
              <div className="search-dropdown">
                {searchResults.map((group) => (
                  <div
                    key={group.group_id}
                    className="search-item"
                    onMouseDown={() => handleSelectGroup(group)}
                  >
                    <div className="search-item-name">
                      {group.display_name}
                      {group.already_allowed && (
                        <span className="badge-allowed">Already allowed</span>
                      )}
                      {group.visibility && (
                        <span className={`badge-visibility ${group.visibility.toLowerCase()}`}>
                          {group.visibility}
                        </span>
                      )}
                    </div>
                    <div className="search-item-url">{group.site_url || "No SharePoint site"}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="add-site-form">
            <span className="selected-name">
              {selectedGroup.display_name}
              {selectedGroup.site_url && ` (${selectedGroup.site_url})`}
            </span>
            <input
              className="filter-input notes-input"
              placeholder="Notes (optional)"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
            <button className="add-btn" onClick={handleAddGroup}>
              Add to Allow List
            </button>
            <button className="cancel-btn" onClick={() => setSelectedGroup(null)}>
              Cancel
            </button>
          </div>
        )}
      </div>

      <div className="table-wrapper card">
        {sites.length === 0 ? (
          <div className="empty-message">No groups in the visibility allow list yet.</div>
        ) : (
          <table className="allowlist-table">
            <thead>
              <tr>
                <th style={{ width: 30 }}></th>
                <th>Group Name</th>
                <th>Site URL</th>
                <th>Added By</th>
                <th>Added At</th>
                <th>Notes</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {sites.map((site) => (
                <>
                  <tr
                    key={site.id}
                    className="expandable-row"
                    onClick={() =>
                      setExpandedId(expandedId === site.id ? null : site.id)
                    }
                  >
                    <td className="expand-toggle">
                      <span
                        className={`expand-arrow ${
                          expandedId === site.id ? "expanded" : ""
                        }`}
                      >
                        &#9654;
                      </span>
                    </td>
                    <td>{site.group_display_name || site.group_id}</td>
                    <td>
                      {site.site_url ? (
                        <a
                          href={site.site_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {site.site_url}
                        </a>
                      ) : (
                        "-"
                      )}
                    </td>
                    <td>{site.added_by || "-"}</td>
                    <td>{formatDate(site.created_at)}</td>
                    <td>{site.notes || "-"}</td>
                    <td>
                      <button
                        className="remove-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRemoveSite(site.id);
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                  {expandedId === site.id && (
                    <tr key={`detail-${site.id}`} className="detail-row">
                      <td colSpan={7}>
                        <SiteDetailPanel
                          siteUrl={site.site_url}
                          groupId={site.group_id}
                        />
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {totalPages > 1 && (
        <div className="pagination-bar">
          <button
            className="pagination-btn"
            disabled={sitesPage <= 1}
            onClick={() => setSitesPage((p) => p - 1)}
          >
            Prev
          </button>
          <span className="pagination-info">
            Page {sitesPage} / {totalPages}
          </span>
          <button
            className="pagination-btn"
            disabled={sitesPage >= totalPages}
            onClick={() => setSitesPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </>
  );
}

// --- Tab 3: Enforcement Events ---

function EventsTab() {
  const [events, setEvents] = useState<PolicyEvent[]>([]);
  const [eventsTotal, setEventsTotal] = useState(0);
  const [eventsPage, setEventsPage] = useState(1);
  const [policyFilter, setPolicyFilter] = useState<string>("");
  const [summary, setSummary] = useState<EventSummary | null>(null);

  const [scanning, setScanning] = useState(false);
  const [activeScanId, setActiveScanId] = useState<number | null>(null);
  const [scanResult, setScanResult] = useState("");
  const [scans, setScans] = useState<PolicyScan[]>([]);
  const [scansTotal, setScansTotal] = useState(0);
  const [showHistory, setShowHistory] = useState(false);

  const fetchEvents = useCallback(() => {
    const params = new URLSearchParams({
      page: String(eventsPage),
      per_page: "50",
    });
    if (policyFilter) params.set("policy_type", policyFilter);
    apiFetch<{ total: number; events: PolicyEvent[] }>(
      `/site-policy/events?${params}`
    ).then((data) => {
      setEvents(data.events);
      setEventsTotal(data.total);
    });
  }, [eventsPage, policyFilter]);

  const fetchSummary = useCallback(() => {
    apiFetch<EventSummary>("/site-policy/events/summary").then(setSummary);
  }, []);

  const fetchScans = useCallback(() => {
    apiFetch<{ total: number; scans: PolicyScan[] }>(
      "/site-policy/scans?page=1&per_page=20"
    ).then((data) => {
      setScans(data.scans);
      setScansTotal(data.total);
    });
  }, []);

  useEffect(() => {
    fetchEvents();
    fetchSummary();
    fetchScans();
    const id = setInterval(() => {
      fetchEvents();
      fetchSummary();
      fetchScans();
    }, 30_000);
    return () => clearInterval(id);
  }, [fetchEvents, fetchSummary, fetchScans]);

  // Poll active scan
  useEffect(() => {
    if (activeScanId === null) return;
    const id = setInterval(() => {
      apiFetch<{ scan: PolicyScan | null }>(
        `/site-policy/scans/${activeScanId}`
      ).then((data) => {
        if (!data.scan) return;
        if (data.scan.status === "completed") {
          setScanning(false);
          setScanResult(
            `Visibility: ${data.scan.visibility_remediated} remediated, ` +
            `Sharing: ${data.scan.sharing_remediated} remediated, ` +
            `Errors: ${data.scan.errors}`
          );
          setActiveScanId(null);
          fetchEvents();
          fetchSummary();
          fetchScans();
        } else if (data.scan.status === "failed") {
          setScanning(false);
          setScanResult(`Scan failed: ${data.scan.error_message || "unknown error"}`);
          setActiveScanId(null);
          fetchScans();
        }
      });
    }, 3_000);
    return () => clearInterval(id);
  }, [activeScanId, fetchEvents, fetchSummary, fetchScans]);

  async function handleScanNow() {
    setScanning(true);
    setScanResult("");
    try {
      const data = await apiPost<{ scan_id: number }>("/site-policy/scan", {});
      setActiveScanId(data.scan_id);
    } catch (err) {
      setScanning(false);
      setScanResult(err instanceof Error ? err.message : "Failed to trigger scan");
    }
  }

  const totalPages = Math.ceil(eventsTotal / 50);

  return (
    <>
      {/* Summary cards */}
      <div className="summary-cards">
        <div className="summary-card">
          <div className="summary-label">Last Scan</div>
          <div className="summary-value">
            {summary?.last_scan ? formatDate(summary.last_scan.completed_at) : "Never"}
          </div>
        </div>
        <div className="summary-card">
          <div className="summary-label">Visibility (30d)</div>
          <div className="summary-value">
            {summary?.last_30_days.visibility_remediated_30d ?? 0} remediated
          </div>
        </div>
        <div className="summary-card">
          <div className="summary-label">Sharing (30d)</div>
          <div className="summary-value">
            {summary?.last_30_days.sharing_remediated_30d ?? 0} remediated
          </div>
        </div>
        <div className="summary-card">
          <div className="summary-label">Errors (30d)</div>
          <div className="summary-value">
            {summary?.last_30_days.errors_30d ?? 0}
          </div>
        </div>
      </div>

      {/* Scan controls */}
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1rem" }}>
        <button className="sync-btn" onClick={handleScanNow} disabled={scanning}>
          {scanning ? "Scanning..." : "Scan Now"}
        </button>
        {scanResult && <span className="sync-result">{scanResult}</span>}
      </div>

      {/* Filter + events table */}
      <div className="table-wrapper card">
        <div style={{ padding: "0.75rem 1rem", borderBottom: "1px solid var(--uvu-border)" }}>
          <select
            className="filter-input"
            value={policyFilter}
            onChange={(e) => {
              setPolicyFilter(e.target.value);
              setEventsPage(1);
            }}
            style={{ width: "auto", minWidth: 180 }}
          >
            <option value="">All Policy Types</option>
            <option value="visibility">Visibility</option>
            <option value="sharing">Sharing</option>
          </select>
        </div>
        {events.length === 0 ? (
          <div className="empty-message">No enforcement events recorded yet.</div>
        ) : (
          <table className="allowlist-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Policy</th>
                <th>Site Name</th>
                <th>Site URL</th>
                <th>Previous</th>
                <th>New</th>
                <th>Action</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {events.map((evt) => (
                <tr key={evt.id}>
                  <td>{formatDate(evt.created_at)}</td>
                  <td>
                    <span className={`policy-badge ${evt.policy_type}`}>
                      {evt.policy_type}
                    </span>
                  </td>
                  <td>{evt.site_display_name || "-"}</td>
                  <td>
                    {evt.site_url ? (
                      <a href={evt.site_url} target="_blank" rel="noopener noreferrer">
                        {evt.site_url}
                      </a>
                    ) : (
                      "-"
                    )}
                  </td>
                  <td>{evt.previous_value || "-"}</td>
                  <td>{evt.new_value || "-"}</td>
                  <td>
                    <span className={`action-badge ${evt.action}`}>
                      {evt.action}
                    </span>
                  </td>
                  <td>{evt.error_message || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="pagination-bar">
          <button
            className="pagination-btn"
            disabled={eventsPage <= 1}
            onClick={() => setEventsPage((p) => p - 1)}
          >
            Prev
          </button>
          <span className="pagination-info">
            Page {eventsPage} / {totalPages}
          </span>
          <button
            className="pagination-btn"
            disabled={eventsPage >= totalPages}
            onClick={() => setEventsPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}

      {/* Scan History */}
      <div className="sync-history-section">
        <div
          className="sync-history-header"
          onClick={() => setShowHistory((v) => !v)}
        >
          <h3>Scan History ({scansTotal})</h3>
          <span className="sync-history-toggle">
            {showHistory ? "Collapse" : "Expand"}
          </span>
        </div>
        {showHistory && (
          <table className="sync-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Type</th>
                <th>Status</th>
                <th>Sites</th>
                <th>Vis. Found</th>
                <th>Vis. Fixed</th>
                <th>Shr. Found</th>
                <th>Shr. Fixed</th>
                <th>Errors</th>
              </tr>
            </thead>
            <tbody>
              {scans.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty-message">
                    No scans yet.
                  </td>
                </tr>
              )}
              {scans.map((scan) => (
                <tr key={scan.id}>
                  <td>{formatDate(scan.created_at)}</td>
                  <td>{scan.trigger_type}</td>
                  <td>
                    <span className={`status-badge ${scan.status}`}>
                      {scan.status}
                    </span>
                  </td>
                  <td>{scan.total_sites_scanned}</td>
                  <td>{scan.visibility_violations_found}</td>
                  <td>{scan.visibility_remediated}</td>
                  <td>{scan.sharing_violations_found}</td>
                  <td>{scan.sharing_remediated}</td>
                  <td>{scan.errors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// --- Main Component ---

export default function AllowList() {
  const [activeTab, setActiveTab] = useState<TabId>("sharing");

  return (
    <div className="allowlist-container">
      <div className="allowlist-header">
        <h2 className="page-title">Site Policy Management</h2>
      </div>

      <div className="tab-nav">
        <button
          className={`tab-btn ${activeTab === "sharing" ? "active" : ""}`}
          onClick={() => setActiveTab("sharing")}
        >
          Anonymous Sharing
        </button>
        <button
          className={`tab-btn ${activeTab === "visibility" ? "active" : ""}`}
          onClick={() => setActiveTab("visibility")}
        >
          Public Visibility
        </button>
        <button
          className={`tab-btn ${activeTab === "events" ? "active" : ""}`}
          onClick={() => setActiveTab("events")}
        >
          Enforcement Events
        </button>
      </div>

      {activeTab === "sharing" && <SharingTab />}
      {activeTab === "visibility" && <VisibilityTab />}
      {activeTab === "events" && <EventsTab />}
    </div>
  );
}
