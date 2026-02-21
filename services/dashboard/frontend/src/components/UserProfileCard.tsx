import { uvu } from "../theme";

interface UserProfile {
  user_id: string;
  display_name: string | null;
  job_title: string | null;
  department: string | null;
  mail: string | null;
  manager_name: string | null;
  photo_base64: string | null;
}

const cardStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "10px 0",
};
const avatar: React.CSSProperties = {
  width: 48,
  height: 48,
  borderRadius: "50%",
  objectFit: "cover",
  flexShrink: 0,
};
const initialsStyle: React.CSSProperties = {
  ...avatar,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: uvu.seaHaze,
  color: uvu.greenD2,
  fontWeight: 600,
  fontSize: "1rem",
};
const labelStyle: React.CSSProperties = { color: uvu.textMuted, fontSize: "0.75rem" };
const val: React.CSSProperties = { fontSize: "0.85rem", color: uvu.text };

function getInitials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join("")
    .toUpperCase();
}

export default function UserProfileCard({
  profile,
  userId,
}: {
  profile: UserProfile | null;
  userId: string;
}) {
  if (!profile) {
    return (
      <div style={{ marginBottom: 6 }}>
        <span style={labelStyle}>User: </span>
        <span style={val}>{userId}</span>
      </div>
    );
  }

  return (
    <div style={cardStyle}>
      {profile.photo_base64 ? (
        <img
          src={`data:image/jpeg;base64,${profile.photo_base64}`}
          alt={profile.display_name || userId}
          style={avatar}
        />
      ) : (
        <div style={initialsStyle}>
          {profile.display_name ? getInitials(profile.display_name) : "?"}
        </div>
      )}
      <div>
        <div style={{ fontWeight: 600, fontSize: "0.9rem", color: uvu.text }}>
          {profile.display_name || userId}
        </div>
        {profile.job_title && <div style={val}>{profile.job_title}</div>}
        {profile.department && (
          <div style={labelStyle}>{profile.department}</div>
        )}
        {profile.manager_name && (
          <div style={labelStyle}>Manager: {profile.manager_name}</div>
        )}
        <div style={{ ...labelStyle, marginTop: 2 }}>{userId}</div>
      </div>
    </div>
  );
}
