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

type TabId =
  | "sharing"
  | "visibility"
  | "company-wide-sharing"
  | "cws-verification"
  | "events";

interface CwsAllowlistSite {
  id: number;
  site_id: string;
  site_url: string;
  site_display_name: string;
  added_by: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

interface CwsExceptionMember {
  id: string;
  displayName: string;
  mail: string;
  userPrincipalName: string;
}

interface CwsScan {
  id: number;
  trigger_type: string;
  triggered_by: string;
  status: string;
  dry_run: boolean;
  started_at: string | null;
  completed_at: string | null;
  total_sites_scanned: number;
  violations_found: number;
  remediated: number;
  would_remediate: number;
  skipped_allowlist: number;
  skipped_exception_group: number;
  already_compliant: number;
  errors: number;
  exception_group_member_count: number;
  error_message: string | null;
  created_at: string;
}

interface CwsFailure {
  event_id: number;
  scan_id: number;
  site_url: string;
  site_display_name: string;
  site_type: string;
  user_upn: string;
  previous_value: string;
  new_value: string;
  error_message: string | null;
  failed_at: string;
  failure_category: string | null;
  diagnostic_summary: string | null;
  last_diagnosed_at: string | null;
  diagnostic_exit_code: number | null;
  diagnostic_event_id: number | null;
}

interface CwsFailureCategoryCount {
  failure_category: string;
  count: number;
}

interface CwsVerificationScan {
  id: number;
  trigger_type: string;
  triggered_by: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  total_candidates: number;
  checked: number;
  drifts_found: number;
  unprovisioned_count: number;
  ok_count: number;
  errors: number;
  exception_group_member_count: number;
  last_checkpoint_cursor: string;
  error_message: string | null;
  created_at: string;
}

interface CwsVerificationEvent {
  id: number;
  scan_id: number | null;
  site_url: string;
  site_display_name: string;
  site_type: string;
  user_upn: string;
  expected_value: string;
  actual_value: string;
  drift_type: string;
  exempt_reason: string;
  trigger_source: string;
  error_message: string | null;
  created_at: string;
}

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

// --- Tab 3: Company-Wide Sharing Links ---

function CompanyWideSharingTab() {
  const [sites, setSites] = useState<CwsAllowlistSite[]>([]);
  const [sitesTotal, setSitesTotal] = useState(0);
  const [sitesPage, setSitesPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedSite, setSelectedSite] = useState<SearchResult | null>(null);
  const [notes, setNotes] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>();

  const [members, setMembers] = useState<CwsExceptionMember[]>([]);
  const [memberGroupId, setMemberGroupId] = useState("");
  const [membersError, setMembersError] = useState("");
  const [membersLoading, setMembersLoading] = useState(true);
  const [showMembers, setShowMembers] = useState(false);

  const [scanning, setScanning] = useState(false);
  const [scanMessage, setScanMessage] = useState("");
  const [recentScans, setRecentScans] = useState<CwsScan[]>([]);

  const [failures, setFailures] = useState<CwsFailure[]>([]);
  const [failuresTotal, setFailuresTotal] = useState(0);
  const [failuresPage, setFailuresPage] = useState(1);
  const [categoryCounts, setCategoryCounts] = useState<CwsFailureCategoryCount[]>([]);
  const [categoryFilter, setCategoryFilter] = useState("");
  const [diagnosingSite, setDiagnosingSite] = useState<string | null>(null);
  const [remediatingSite, setRemediatingSite] = useState<string | null>(null);
  const [failuresError, setFailuresError] = useState<string | null>(null);
  const failuresPerPage = 25;

  const fetchFailures = useCallback(() => {
    const params = new URLSearchParams({
      page: String(failuresPage),
      per_page: String(failuresPerPage),
    });
    if (categoryFilter) params.set("category", categoryFilter);
    apiFetch<{
      total: number;
      failures: CwsFailure[];
      category_counts: CwsFailureCategoryCount[];
    }>(`/company-wide-sharing/failures?${params.toString()}`)
      .then((data) => {
        setFailures(data.failures);
        setFailuresTotal(data.total);
        setCategoryCounts(data.category_counts);
        setFailuresError(null);
      })
      .catch((err) => {
        setFailuresError(err instanceof Error ? err.message : "Failed to load");
      });
  }, [failuresPage, categoryFilter]);

  const fetchSites = useCallback(() => {
    apiFetch<{ total: number; sites: CwsAllowlistSite[] }>(
      `/company-wide-sharing/allowlist?page=${sitesPage}&per_page=50`
    ).then((data) => {
      setSites(data.sites);
      setSitesTotal(data.total);
    });
  }, [sitesPage]);

  const fetchMembers = useCallback(() => {
    setMembersLoading(true);
    apiFetch<{ group_id: string; members: CwsExceptionMember[]; error?: string }>(
      "/company-wide-sharing/exception-group/members"
    )
      .then((data) => {
        setMemberGroupId(data.group_id);
        setMembers(data.members);
        setMembersError(data.error || "");
      })
      .catch((err) =>
        setMembersError(err instanceof Error ? err.message : "Failed"),
      )
      .finally(() => setMembersLoading(false));
  }, []);

  const fetchRecentScans = useCallback(() => {
    apiFetch<{ scans: CwsScan[] }>(
      "/company-wide-sharing/scans?page=1&per_page=5",
    ).then((data) => setRecentScans(data.scans));
  }, []);

  useEffect(() => {
    fetchSites();
  }, [fetchSites]);

  useEffect(() => {
    fetchMembers();
    fetchRecentScans();
  }, [fetchMembers, fetchRecentScans]);

  useEffect(() => {
    fetchFailures();
  }, [fetchFailures]);

  async function handleDiagnoseSite(siteUrl: string) {
    setDiagnosingSite(siteUrl);
    try {
      await apiFetch<{ queued: boolean; reason?: string }>(
        "/company-wide-sharing/failures/diagnose",
        { method: "POST", body: JSON.stringify({ site_url: siteUrl }) },
      );
    } catch (err) {
      setFailuresError(err instanceof Error ? err.message : "Failed to queue");
    } finally {
      setDiagnosingSite(null);
      // Diagnostic runs async in lifecycle-cron — give it a moment, then refresh.
      setTimeout(fetchFailures, 4000);
    }
  }

  async function handleRemediateSite(siteUrl: string, siteDisplayName: string) {
    const label = siteDisplayName || siteUrl;
    if (
      !window.confirm(
        `Re-run the disable-CWS action against:\n\n${label}\n\nThis runs Invoke-CwsAllowlistAction.ps1 and sets DisableCompanyWideSharingLinks=Disabled on the site.`,
      )
    ) {
      return;
    }
    setRemediatingSite(siteUrl);
    try {
      await apiFetch<{ queued: boolean; reason?: string }>(
        "/company-wide-sharing/failures/remediate",
        { method: "POST", body: JSON.stringify({ site_url: siteUrl }) },
      );
    } catch (err) {
      setFailuresError(err instanceof Error ? err.message : "Failed to queue");
    } finally {
      setRemediatingSite(null);
      // Remediation runs async via the CWS allowlist action loop — refresh
      // after a delay so the row clears once the success event lands.
      setTimeout(fetchFailures, 8000);
    }
  }

  useEffect(() => {
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    if (searchQuery.length < 2) {
      setSearchResults([]);
      setShowDropdown(false);
      return;
    }
    searchTimeout.current = setTimeout(() => {
      apiFetch<{ sites: SearchResult[] }>(
        `/company-wide-sharing/allowlist/search?q=${encodeURIComponent(searchQuery)}`,
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
      await apiPost("/company-wide-sharing/allowlist", {
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
    if (!confirm("Remove this site from the Company-Wide Sharing allow list?")) return;
    setActionError(null);
    try {
      await apiDelete(`/company-wide-sharing/allowlist/${id}`);
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to remove site");
    }
  }

  async function handleTriggerScan() {
    setScanning(true);
    setScanMessage("");
    try {
      const res = await apiPost<{ scan_id: number; dry_run: boolean }>(
        "/company-wide-sharing/scan",
        {},
      );
      setScanMessage(
        `Queued scan #${res.scan_id} (${res.dry_run ? "dry-run" : "live enforcement"}). Check Recent Scans below.`,
      );
      setTimeout(fetchRecentScans, 2000);
    } catch (err) {
      setScanMessage(err instanceof Error ? err.message : "Scan failed to queue");
    } finally {
      setScanning(false);
    }
  }

  const totalPages = Math.ceil(sitesTotal / 50);
  const entraGroupUrl = memberGroupId
    ? `https://entra.microsoft.com/#view/Microsoft_AAD_IAM/GroupDetailsMenuBlade/~/Members/groupId/${memberGroupId}`
    : "";

  return (
    <>
      <div className="card" style={{ padding: "10px 14px", marginBottom: 12, background: "#f4f6fb" }}>
        <strong>Company-Wide Sharing Links policy.</strong> When enabled, ShareSentinel
        sets <code>DisableCompanyWideSharingLinks = Disabled</code> on every SharePoint site
        and OneDrive personal site, except sites on the allow list below and OneDrive users
        in the Entra ID exception group. Until <code>COMPANY_WIDE_SHARING_DRY_RUN</code> is
        flipped to <code>false</code>, scans only log what <em>would</em> change.
      </div>

      {actionError && (
        <div className="card" style={{ background: "#ffebe6", color: "#de350b", padding: "10px 14px", fontWeight: 600, marginBottom: 12 }}>
          {actionError}
        </div>
      )}

      <div className="add-site-section">
        <h3>Add Site (allow company-wide links)</h3>
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
          <div className="empty-message">No sites on the company-wide sharing allow list yet.</div>
        ) : (
          <table className="allowlist-table">
            <thead>
              <tr>
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
                <tr key={site.id}>
                  <td>{site.site_display_name || site.site_id}</td>
                  <td>
                    <a
                      href={site.site_url}
                      target="_blank"
                      rel="noopener noreferrer"
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
                      onClick={() => handleRemoveSite(site.id)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
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

      <div className="card" style={{ padding: 14, marginTop: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Exempt users (OrgWideSharingLinkException)</h3>
          <button
            className="pagination-btn"
            onClick={() => setShowMembers((v) => !v)}
          >
            {showMembers ? "Hide" : "Show"}
          </button>
        </div>
        <div style={{ marginTop: 8, fontSize: 13, color: "#5e6c84" }}>
          Membership is managed in Entra ID. Edit the group there to change exemptions.{" "}
          {entraGroupUrl && (
            <a href={entraGroupUrl} target="_blank" rel="noopener noreferrer">
              Open group in Entra
            </a>
          )}
        </div>
        {showMembers && (
          <div style={{ marginTop: 12 }}>
            {membersLoading && <div>Loading members...</div>}
            {membersError && (
              <div style={{ color: "#de350b" }}>{membersError}</div>
            )}
            {!membersLoading && !membersError && members.length === 0 && (
              <div className="empty-message">No members in the exception group.</div>
            )}
            {!membersLoading && !membersError && members.length > 0 && (
              <table className="allowlist-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>UPN</th>
                    <th>Mail</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.id}>
                      <td>{m.displayName || "-"}</td>
                      <td>{m.userPrincipalName || "-"}</td>
                      <td>{m.mail || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      <div className="card" style={{ padding: 14, marginTop: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>
            Failed to disable{" "}
            <span style={{ fontSize: 13, color: "#5e6c84", fontWeight: 400 }}>
              ({failuresTotal} site{failuresTotal === 1 ? "" : "s"} currently
              unremediated)
            </span>
          </h3>
          <button className="pagination-btn" onClick={fetchFailures}>
            Refresh
          </button>
        </div>
        <div style={{ marginTop: 6, fontSize: 13, color: "#5e6c84" }}>
          Sites and OneDrives whose last CWS remediation attempt did not succeed.
          The diagnostic loop runs Diagnose-OneDriveSite.ps1 against each one and
          parses the reason (locked OneDrive, A1 quota cap, deleted user, etc.).
          Click "Re-diagnose" to refresh on demand.
        </div>
        {categoryCounts.length > 0 && (
          <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
            <button
              className="pagination-btn"
              style={categoryFilter === "" ? { background: "#0052cc", color: "white" } : undefined}
              onClick={() => { setCategoryFilter(""); setFailuresPage(1); }}
            >
              All
            </button>
            {categoryCounts.map((c) => (
              <button
                key={c.failure_category}
                className="pagination-btn"
                style={
                  categoryFilter === c.failure_category
                    ? { background: "#0052cc", color: "white" }
                    : undefined
                }
                onClick={() => {
                  setCategoryFilter(
                    c.failure_category === "undiagnosed" ? "" : c.failure_category,
                  );
                  setFailuresPage(1);
                }}
              >
                {c.failure_category} ({c.count})
              </button>
            ))}
          </div>
        )}
        {failuresError && (
          <div style={{ marginTop: 10, color: "#de350b" }}>{failuresError}</div>
        )}
        {failures.length === 0 ? (
          <div className="empty-message" style={{ marginTop: 12 }}>
            No currently-failing CWS remediations. Either everything succeeded or
            no remediation has run since the failures were last cleared.
          </div>
        ) : (
          <>
            <table className="allowlist-table" style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>Site / OneDrive</th>
                  <th>User</th>
                  <th>Reason</th>
                  <th>Diagnostic summary</th>
                  <th>Original error</th>
                  <th>Last diagnosed</th>
                  <th>Failed at</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {failures.map((f) => {
                  const cat = f.failure_category || "undiagnosed";
                  const catBg = cat === "undiagnosed"
                    ? "#dfe1e6"
                    : cat === "user_purged"
                      ? "#c1c7d0"
                      : cat === "no_onedrive_provisioned" || cat === "user_deleted" || cat === "user_recently_deleted"
                        ? "#fff0b3"
                        : cat === "access_denied" || cat === "lockstate_locked"
                          ? "#ffbdad"
                          : cat === "quota_exceeded" || cat === "a1_license_cap"
                            ? "#ffe2bd"
                            : "#deebff";
                  return (
                    <tr key={f.event_id}>
                      <td style={{ maxWidth: 300, wordBreak: "break-all" }}>
                        <a href={f.site_url} target="_blank" rel="noopener noreferrer">
                          {f.site_display_name || f.site_url}
                        </a>
                        <div style={{ fontSize: 11, color: "#5e6c84" }}>
                          {f.site_type}
                        </div>
                      </td>
                      <td>{f.user_upn || "-"}</td>
                      <td>
                        <span
                          style={{
                            background: catBg,
                            color: "#172b4d",
                            padding: "2px 8px",
                            borderRadius: 4,
                            fontSize: 12,
                            fontWeight: 600,
                            whiteSpace: "nowrap",
                          }}
                        >
                          {cat}
                        </span>
                      </td>
                      <td
                        style={{ maxWidth: 360, fontSize: 12 }}
                        title={f.diagnostic_summary || ""}
                      >
                        {f.diagnostic_summary || (
                          <span style={{ color: "#5e6c84" }}>not yet diagnosed</span>
                        )}
                      </td>
                      <td
                        style={{ maxWidth: 260, fontSize: 12 }}
                        title={f.error_message || ""}
                      >
                        {f.error_message
                          ? f.error_message.length > 80
                            ? f.error_message.slice(0, 80) + "…"
                            : f.error_message
                          : "-"}
                      </td>
                      <td style={{ fontSize: 12 }}>{formatDate(f.last_diagnosed_at)}</td>
                      <td style={{ fontSize: 12 }}>{formatDate(f.failed_at)}</td>
                      <td>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          <button
                            className="pagination-btn"
                            disabled={diagnosingSite === f.site_url}
                            onClick={() => handleDiagnoseSite(f.site_url)}
                          >
                            {diagnosingSite === f.site_url ? "Queuing…" : "Re-diagnose"}
                          </button>
                          <button
                            className="pagination-btn"
                            disabled={remediatingSite === f.site_url}
                            title="Re-run Invoke-CwsAllowlistAction.ps1 to set DisableCompanyWideSharingLinks=Disabled on this site"
                            onClick={() =>
                              handleRemediateSite(
                                f.site_url,
                                f.site_display_name || "",
                              )
                            }
                          >
                            {remediatingSite === f.site_url ? "Queuing…" : "Remediate"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {failuresTotal > failuresPerPage && (
              <div className="pagination-bar">
                <button
                  className="pagination-btn"
                  disabled={failuresPage <= 1}
                  onClick={() => setFailuresPage((p) => p - 1)}
                >
                  Prev
                </button>
                <span className="pagination-info">
                  Page {failuresPage} /{" "}
                  {Math.max(1, Math.ceil(failuresTotal / failuresPerPage))}
                </span>
                <button
                  className="pagination-btn"
                  disabled={
                    failuresPage >= Math.ceil(failuresTotal / failuresPerPage)
                  }
                  onClick={() => setFailuresPage((p) => p + 1)}
                >
                  Next
                </button>
              </div>
            )}
          </>
        )}
      </div>

      <div className="card" style={{ padding: 14, marginTop: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Scan now</h3>
          <button
            className="add-btn"
            disabled={scanning}
            onClick={handleTriggerScan}
          >
            {scanning ? "Queuing..." : "Run Company-Wide Sharing Scan"}
          </button>
        </div>
        {scanMessage && (
          <div style={{ marginTop: 8, fontSize: 13 }}>{scanMessage}</div>
        )}
        {recentScans.length > 0 && (
          <table className="allowlist-table" style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>#</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Dry-run</th>
                <th>Sites</th>
                <th>Violations</th>
                <th>Remediated</th>
                <th>Would Remediate</th>
                <th>Compliant</th>
                <th>Errors</th>
                <th>Completed</th>
              </tr>
            </thead>
            <tbody>
              {recentScans.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td>{s.trigger_type}</td>
                  <td>{s.status}</td>
                  <td>{s.dry_run ? "yes" : "no"}</td>
                  <td>{s.total_sites_scanned}</td>
                  <td>{s.violations_found}</td>
                  <td>{s.remediated}</td>
                  <td>{s.would_remediate}</td>
                  <td>{s.already_compliant}</td>
                  <td>{s.errors}</td>
                  <td>{formatDate(s.completed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// --- Tab 4: CWS Verification Sweep ---

function CwsVerificationTab() {
  const [scans, setScans] = useState<CwsVerificationScan[]>([]);
  const [triggering, setTriggering] = useState(false);
  const [message, setMessage] = useState("");
  const [selectedScanId, setSelectedScanId] = useState<number | null>(null);
  const [events, setEvents] = useState<CwsVerificationEvent[]>([]);
  const [driftTypeFilter, setDriftTypeFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");

  const fetchScans = useCallback(() => {
    apiFetch<{ scans: CwsVerificationScan[] }>(
      "/cws-verification/scans?page=1&per_page=10",
    )
      .then((data) => setScans(data.scans))
      .catch(() => setScans([]));
  }, []);

  const fetchEvents = useCallback(() => {
    const params = new URLSearchParams({ page: "1", per_page: "100" });
    if (selectedScanId != null) params.set("scan_id", String(selectedScanId));
    if (driftTypeFilter) params.set("drift_type", driftTypeFilter);
    if (sourceFilter) params.set("trigger_source", sourceFilter);
    apiFetch<{ events: CwsVerificationEvent[] }>(
      `/cws-verification/events?${params.toString()}`,
    )
      .then((data) => setEvents(data.events))
      .catch(() => setEvents([]));
  }, [selectedScanId, driftTypeFilter, sourceFilter]);

  useEffect(() => {
    fetchScans();
  }, [fetchScans]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  async function handleTrigger() {
    setTriggering(true);
    setMessage("");
    try {
      const res = await apiPost<{ scan_id: number }>(
        "/cws-verification/scan",
        {},
      );
      setMessage(
        `Queued verification sweep #${res.scan_id}. This runs against every enabled user + every Graph-discovered site and typically takes hours — refresh to watch progress.`,
      );
      setTimeout(fetchScans, 2000);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to queue sweep");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <>
      <div
        className="card"
        style={{ padding: "10px 14px", marginBottom: 12, background: "#f4f6fb" }}
      >
        <strong>CWS Verification Sweep (detect-only).</strong> Belt-and-suspenders
        cross-check that complements the daily CWS scan. Enumerates every enabled
        user's OneDrive URL plus every Graph-discovered SharePoint site, checks
        the <code>DisableCompanyWideSharingLinks</code> setting on each, and
        records drift to this table. No remediation is performed — the daily CWS
        scan owns remediation for the subset it can see, and event-driven drift
        from <code>CompanyLinkCreated</code> is also recorded here with{" "}
        <code>trigger_source = audit_event</code>.
      </div>

      <div className="card" style={{ padding: 14, marginBottom: 16 }}>
        <div
          style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
        >
          <h3 style={{ margin: 0 }}>Run verification sweep</h3>
          <button
            className="add-btn"
            disabled={triggering}
            onClick={handleTrigger}
          >
            {triggering ? "Queuing..." : "Run Verification Sweep"}
          </button>
        </div>
        {message && (
          <div style={{ marginTop: 8, fontSize: 13 }}>{message}</div>
        )}

        {scans.length > 0 && (
          <table className="allowlist-table" style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>#</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Candidates</th>
                <th>Checked</th>
                <th>Drifts</th>
                <th>Unprov.</th>
                <th>OK</th>
                <th>Errors</th>
                <th>Started</th>
                <th>Completed</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {scans.map((s) => (
                <tr
                  key={s.id}
                  style={{
                    background: selectedScanId === s.id ? "#eef4ff" : undefined,
                  }}
                >
                  <td>{s.id}</td>
                  <td>{s.trigger_type}</td>
                  <td>{s.status}</td>
                  <td>{s.total_candidates}</td>
                  <td>{s.checked}</td>
                  <td>{s.drifts_found}</td>
                  <td>{s.unprovisioned_count}</td>
                  <td>{s.ok_count}</td>
                  <td>{s.errors}</td>
                  <td>{formatDate(s.started_at)}</td>
                  <td>{formatDate(s.completed_at)}</td>
                  <td>
                    <button
                      className="pagination-btn"
                      onClick={() =>
                        setSelectedScanId(selectedScanId === s.id ? null : s.id)
                      }
                    >
                      {selectedScanId === s.id ? "Clear" : "View events"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ padding: 14 }}>
        <div
          style={{
            display: "flex",
            gap: 12,
            alignItems: "center",
            marginBottom: 8,
          }}
        >
          <h3 style={{ margin: 0 }}>
            Drift events
            {selectedScanId != null ? ` (scan #${selectedScanId})` : ""}
          </h3>
          <select
            className="filter-input"
            value={driftTypeFilter}
            onChange={(e) => setDriftTypeFilter(e.target.value)}
          >
            <option value="">All drift types</option>
            <option value="drift">drift</option>
            <option value="error">error</option>
          </select>
          <select
            className="filter-input"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
          >
            <option value="">All sources</option>
            <option value="scheduled">scheduled</option>
            <option value="audit_event">audit_event</option>
          </select>
        </div>
        {events.length === 0 ? (
          <div className="empty-message">No drift events recorded.</div>
        ) : (
          <table className="allowlist-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Scan</th>
                <th>Source</th>
                <th>Type</th>
                <th>Site</th>
                <th>User UPN</th>
                <th>Expected</th>
                <th>Actual</th>
                <th>Exempt?</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>{formatDate(e.created_at)}</td>
                  <td>{e.scan_id ?? "—"}</td>
                  <td>{e.trigger_source}</td>
                  <td>{e.drift_type}</td>
                  <td>
                    <a href={e.site_url} target="_blank" rel="noopener noreferrer">
                      {e.site_url}
                    </a>
                  </td>
                  <td>{e.user_upn || "—"}</td>
                  <td>{e.expected_value || "—"}</td>
                  <td>{e.actual_value || "—"}</td>
                  <td>{e.exempt_reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// --- Tab 5: Enforcement Events ---

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
          className={`tab-btn ${activeTab === "company-wide-sharing" ? "active" : ""}`}
          onClick={() => setActiveTab("company-wide-sharing")}
        >
          Company-Wide Sharing
        </button>
        <button
          className={`tab-btn ${activeTab === "cws-verification" ? "active" : ""}`}
          onClick={() => setActiveTab("cws-verification")}
        >
          Verification Sweep
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
      {activeTab === "company-wide-sharing" && <CompanyWideSharingTab />}
      {activeTab === "cws-verification" && <CwsVerificationTab />}
      {activeTab === "events" && <EventsTab />}
    </div>
  );
}
