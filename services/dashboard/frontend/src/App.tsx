import { Routes, Route, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import EventList from "./pages/EventList";
import EventDetail from "./pages/EventDetail";
import Statistics from "./pages/Statistics";
import "./App.css";

function Nav() {
  return (
    <nav className="top-nav glass-effect">
      <div className="nav-content">
        <strong className="brand-logo">
          ShareSentinel
        </strong>
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
        </div>
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <div className="app-container">
      <Nav />
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/events" element={<EventList />} />
          <Route path="/events/:eventId" element={<EventDetail />} />
          <Route path="/statistics" element={<Statistics />} />
        </Routes>
      </main>
    </div>
  );
}
