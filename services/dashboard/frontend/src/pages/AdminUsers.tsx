import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { useIsAdmin } from "../context/AuthContext";
import "./AdminUsers.css";

interface DashboardUser {
  oid: string;
  email: string;
  display_name: string;
  roles: string[];
  last_seen_at: string;
  first_seen_at: string;
}

interface UsersResponse {
  total: number;
  page: number;
  per_page: number;
  users: DashboardUser[];
}

export default function AdminUsers() {
  const isAdmin = useIsAdmin();
  const [users, setUsers] = useState<DashboardUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const perPage = 50;

  useEffect(() => {
    setLoading(true);
    apiFetch<UsersResponse>(`/admin/users?page=${page}&per_page=${perPage}`)
      .then((data) => {
        setUsers(data.users);
        setTotal(data.total);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [page]);

  if (!isAdmin) {
    return <div className="admin-forbidden"><h2>Access Denied</h2><p>You need admin privileges to access this page.</p></div>;
  }

  const totalPages = Math.ceil(total / perPage);

  return (
    <div className="admin-users-container">
      <h2>Dashboard Users</h2>

      <div className="users-info-banner">
        Roles are determined by Entra ID group membership and cannot be changed here.
        To modify a user's role, add or remove them from the appropriate Entra ID security group.
      </div>

      {loading ? (
        <div className="admin-loading">Loading users...</div>
      ) : (
        <>
          <div className="users-card">
            <table className="users-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Roles</th>
                  <th>Last Seen</th>
                  <th>First Seen</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.oid}>
                    <td>{u.display_name}</td>
                    <td>{u.email}</td>
                    <td>
                      {(Array.isArray(u.roles) ? u.roles : []).map((role) => (
                        <span key={role} className={`role-badge role-${role}`}>{role}</span>
                      ))}
                    </td>
                    <td>{new Date(u.last_seen_at).toLocaleString()}</td>
                    <td>{new Date(u.first_seen_at).toLocaleString()}</td>
                  </tr>
                ))}
                {users.length === 0 && (
                  <tr><td colSpan={5} className="empty-message">No users have logged in yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="pagination">
              <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>
                Previous
              </button>
              <span>Page {page} of {totalPages}</span>
              <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}>
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
