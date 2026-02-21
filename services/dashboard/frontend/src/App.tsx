import { Routes, Route, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import EventList from "./pages/EventList";
import EventDetail from "./pages/EventDetail";
import Statistics from "./pages/Statistics";
import { uvu } from "./theme";

const navStyle: React.CSSProperties = {
  display: "flex",
  gap: "1.5rem",
  padding: "0.85rem 2rem",
  background: uvu.greenD2,
  color: "#fff",
  alignItems: "center",
  borderBottom: `3px solid ${uvu.green}`,
};

const linkStyle: React.CSSProperties = {
  color: "rgba(255,255,255,.7)",
  textDecoration: "none",
  fontSize: "0.9rem",
  fontWeight: 500,
  padding: "4px 0",
  borderBottom: "2px solid transparent",
};
const activeStyle: React.CSSProperties = {
  color: uvu.gold,
  borderBottomColor: uvu.gold,
};

function Nav() {
  const style = ({ isActive }: { isActive: boolean }) =>
    isActive ? { ...linkStyle, ...activeStyle } : linkStyle;
  return (
    <nav style={navStyle}>
      <strong style={{ marginRight: "auto", fontSize: "1.05rem", color: "#fff", letterSpacing: "-0.01em" }}>
        ShareSentinel
      </strong>
      <NavLink to="/" style={style} end>Dashboard</NavLink>
      <NavLink to="/events" style={style}>Events</NavLink>
      <NavLink to="/statistics" style={style}>Statistics</NavLink>
    </nav>
  );
}

export default function App() {
  return (
    <>
      <Nav />
      <main style={{ padding: "1.5rem 2rem", maxWidth: 1400, margin: "0 auto" }}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/events" element={<EventList />} />
          <Route path="/events/:eventId" element={<EventDetail />} />
          <Route path="/statistics" element={<Statistics />} />
        </Routes>
      </main>
    </>
  );
}
