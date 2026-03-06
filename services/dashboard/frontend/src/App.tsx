import { Routes, Route, NavLink } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import Dashboard from "./pages/Dashboard";
import EventList from "./pages/EventList";
import EventDetail from "./pages/EventDetail";
import Statistics from "./pages/Statistics";
import AllowList from "./pages/AllowList";
import AdminSettings from "./pages/AdminSettings";
import AdminUsers from "./pages/AdminUsers";
import "./App.css";

function Nav() {
  const { user, logout } = useAuth();

  return (
    <nav className="top-nav glass-effect">
      <div className="nav-content">
        <NavLink to="/" className="brand-logo" style={{ textDecoration: 'none' }} end>
          <img src="/logo.png" alt="" className="brand-logo-img" />
          ShareSentinel
        </NavLink>
        <div className="nav-links">
          <NavLink to="/" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")} end>
            Dashboard
          </NavLink>
          <NavLink to="/events" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            Events
          </NavLink>
          <NavLink to="/statistics" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            Statistics
          </NavLink>
          <NavLink to="/allowlist" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            Allow List
          </NavLink>
          {user?.roles?.includes("admin") && (
            <>
              <NavLink to="/admin/settings" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
                Settings
              </NavLink>
              <NavLink to="/admin/users" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
                Users
              </NavLink>
            </>
          )}
        </div>
        {user && (
          <div className="nav-user">
            <span className="nav-user-name">{user.name}</span>
            <button onClick={logout} className="nav-logout-btn">
              Logout
            </button>
          </div>
        )}
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <div className="app-container">
        <Nav />
        <main className="main-content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/events" element={<EventList />} />
            <Route path="/events/:eventId" element={<EventDetail />} />
            <Route path="/statistics" element={<Statistics />} />
            <Route path="/allowlist" element={<AllowList />} />
            <Route path="/admin/settings" element={<AdminSettings />} />
            <Route path="/admin/users" element={<AdminUsers />} />
          </Routes>
        </main>
      </div>
    </AuthProvider>
  );
}
