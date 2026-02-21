const BASE = "/api";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch(path, { method: "PATCH", body: JSON.stringify(body) });
}
