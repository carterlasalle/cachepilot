// Backend endpoints (dashboard/backend/server.py). In dev the Vite proxy
// forwards /api to http://127.0.0.1:8788; in production the backend serves
// this app's dist/ build same-origin, so /api works either way.
export const API = {
  status: '/api/status',
  leases: '/api/leases',
  costs: '/api/costs',
  ttl: '/api/ttl',
  routes: '/api/routes',
  churn: '/api/churn',
  miss: '/api/miss',
  topology: '/api/topology',
  health: '/api/health',
} as const;

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    throw new Error(`${path}: HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}
