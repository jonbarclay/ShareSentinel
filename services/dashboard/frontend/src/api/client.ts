const BASE = "/api";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (res.status === 401) {
    window.location.href = "/api/auth/login";
    throw new Error("Unauthorized — redirecting to login");
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch(path, { method: "PATCH", body: JSON.stringify(body) });
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return apiFetch(path, { method: "POST", body: JSON.stringify(body) });
}

export function apiDelete<T>(path: string): Promise<T> {
  return apiFetch(path, { method: "DELETE" });
}
