import { Fragment, useEffect, useState, useCallback, useRef } from "react";
import { apiFetch, apiPost, apiDelete } from "../api/client";
import "./AllowList.css";

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

interface SyncRecord {
  id: number;
  trigger_type: string;
  triggered_by: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  total_sites_checked: number;
  sites_disabled: number;
  sites_enabled: number;
  sites_already_correct: number;
  sites_failed: number;
  error_message: string | null;
  created_at: string;
}

interface SyncDetail {
  id: number;
  site_id: string;
  site_url: string;
  site_display_name: string;
  previous_capability: string | null;
  desired_capability: string | null;
  action_taken: string;
  error_message: string | null;
  created_at: string;
}

export default function AllowList() {
  // Allow list state
  const [sites, setSites] = useState<AllowListSite[]>([]);
  const [sitesTotal, setSitesTotal] = useState(0);
  const [sitesPage, setSitesPage] = useState(1);

  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedSite, setSelectedSite] = useState<SearchResult | null>(null);
  const [notes, setNotes] = useState("");
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>();

  // Error state
  const [actionError, setActionError] = useState<string | null>(null);

  // Sync state
  const [syncing, setSyncing] = useState(false);
  const [activeSyncId, setActiveSyncId] = useState<number | null>(null);
  const [syncResult, setSyncResult] = useState("");
  const [syncs, setSyncs] = useState<SyncRecord[]>([]);
  const [syncsTotal, setSyncsTotal] = useState(0);
  const [showHistory, setShowHistory] = useState(false);
  const [expandedSyncId, setExpandedSyncId] = useState<number | null>(null);
  const [syncDetails, setSyncDetails] = useState<SyncDetail[]>([]);

  // Fetch allow list
  const fetchSites = useCallback(() => {
    apiFetch<{ total: number; sites: AllowListSite[] }>(
      `/allowlist/sites?page=${sitesPage}&per_page=50`
    ).then((data) => {
      setSites(data.sites);
      setSitesTotal(data.total);
    });
  }, [sitesPage]);

  // Fetch sync history
  const fetchSyncs = useCallback(() => {
    apiFetch<{ total: number; syncs: SyncRecord[] }>(
      `/allowlist/syncs?page=1&per_page=20`
    ).then((data) => {
      setSyncs(data.syncs);
      setSyncsTotal(data.total);
    });
  }, []);

  // Initial load + polling
  useEffect(() => {
    fetchSites();
    fetchSyncs();
    const id = setInterval(() => {
      fetchSites();
      fetchSyncs();
    }, 30_000);
    return () => clearInterval(id);
  }, [fetchSites, fetchSyncs]);

  // Poll active sync status
  useEffect(() => {
    if (activeSyncId === null) return;
    const id = setInterval(() => {
      apiFetch<{ sync: SyncRecord | null }>(
        `/allowlist/syncs/${activeSyncId}`
      ).then((data) => {
        if (!data.sync) return;
        if (data.sync.status === "completed") {
          setSyncing(false);
          setSyncResult(
            `${data.sync.sites_enabled} enabled, ${data.sync.sites_disabled} disabled, ${data.sync.sites_failed} failed`
          );
          setActiveSyncId(null);
          fetchSites();
          fetchSyncs();
        } else if (data.sync.status === "failed") {
          setSyncing(false);
          setSyncResult(`Sync failed: ${data.sync.error_message || "unknown error"}`);
          setActiveSyncId(null);
          fetchSyncs();
        }
      });
    }, 3_000);
    return () => clearInterval(id);
  }, [activeSyncId, fetchSites, fetchSyncs]);

  // Debounced search
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

  // Handlers
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
      fetchSites();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to remove site");
    }
  }

  async function handleSyncNow() {
    setSyncing(true);
    setSyncResult("");
    setActionError(null);
    try {
      const data = await apiPost<{ sync_id: number }>("/allowlist/sync", {});
      setActiveSyncId(data.sync_id);
    } catch (err) {
      setSyncing(false);
      setActionError(err instanceof Error ? err.message : "Failed to trigger sync");
    }
  }

  async function handleExpandSync(syncId: number) {
    if (expandedSyncId === syncId) {
      setExpandedSyncId(null);
      return;
    }
    const data = await apiFetch<{ sync: SyncRecord; details: SyncDetail[] }>(
      `/allowlist/syncs/${syncId}`
    );
    setSyncDetails(data.details);
    setExpandedSyncId(syncId);
  }

  const sitesTotalPages = Math.ceil(sitesTotal / 50);

  function formatDate(iso: string | null) {
    if (!iso) return "-";
    return new Date(iso).toLocaleString();
  }

  return (
    <div className="allowlist-container">
      {/* Header */}
      <div className="allowlist-header">
        <h2 className="page-title">SharePoint Site Allow List ({sitesTotal})</h2>
        <div style={{ display: "flex", alignItems: "center" }}>
          <button
            className="sync-btn"
            onClick={handleSyncNow}
            disabled={syncing}
          >
            {syncing ? "Syncing..." : "Sync Now"}
          </button>
          {syncResult && <span className="sync-result">{syncResult}</span>}
        </div>
      </div>

      {actionError && (
        <div className="card" style={{ background: "#ffebe6", color: "#de350b", padding: "10px 14px", fontWeight: 600, marginBottom: 12 }}>
          {actionError}
        </div>
      )}

      {/* Add Site */}
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

      {/* Allow List Table */}
      <div className="table-wrapper card">
        {sites.length === 0 ? (
          <div className="empty-message">No sites in the allow list yet.</div>
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
                    <a href={site.site_url} target="_blank" rel="noopener noreferrer">
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

      {/* Pagination */}
      {sitesTotalPages > 1 && (
        <div className="pagination-bar">
          <button
            className="pagination-btn"
            disabled={sitesPage <= 1}
            onClick={() => setSitesPage((p) => p - 1)}
          >
            Prev
          </button>
          <span className="pagination-info">
            Page {sitesPage} / {sitesTotalPages}
          </span>
          <button
            className="pagination-btn"
            disabled={sitesPage >= sitesTotalPages}
            onClick={() => setSitesPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}

      {/* Sync History */}
      <div className="sync-history-section">
        <div
          className="sync-history-header"
          onClick={() => setShowHistory((v) => !v)}
        >
          <h3>Sync History ({syncsTotal})</h3>
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
                <th>Checked</th>
                <th>Disabled</th>
                <th>Enabled</th>
                <th>Failed</th>
              </tr>
            </thead>
            <tbody>
              {syncs.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty-message">
                    No sync runs yet.
                  </td>
                </tr>
              )}
              {syncs.map((sync) => (
                <Fragment key={sync.id}>
                  <tr
                    className="clickable"
                    onClick={() => handleExpandSync(sync.id)}
                  >
                    <td>{formatDate(sync.created_at)}</td>
                    <td>{sync.trigger_type}</td>
                    <td>
                      <span className={`status-badge ${sync.status}`}>
                        {sync.status}
                      </span>
                    </td>
                    <td>{sync.total_sites_checked}</td>
                    <td>{sync.sites_disabled}</td>
                    <td>{sync.sites_enabled}</td>
                    <td>{sync.sites_failed}</td>
                  </tr>
                  {expandedSyncId === sync.id && (
                    <tr className="sync-details-row">
                      <td colSpan={7}>
                        <div className="sync-details-inner">
                          {syncDetails.length === 0 ? (
                            <p className="empty-message">No site details recorded.</p>
                          ) : (
                            <table>
                              <thead>
                                <tr>
                                  <th>Site</th>
                                  <th>URL</th>
                                  <th>Previous</th>
                                  <th>Desired</th>
                                  <th>Action</th>
                                  <th>Error</th>
                                </tr>
                              </thead>
                              <tbody>
                                {syncDetails.map((d) => (
                                  <tr key={d.id}>
                                    <td>{d.site_display_name || d.site_id}</td>
                                    <td>{d.site_url}</td>
                                    <td>{d.previous_capability || "-"}</td>
                                    <td>{d.desired_capability || "-"}</td>
                                    <td>
                                      <span className={`action-badge ${d.action_taken}`}>
                                        {d.action_taken}
                                      </span>
                                    </td>
                                    <td>{d.error_message || "-"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
