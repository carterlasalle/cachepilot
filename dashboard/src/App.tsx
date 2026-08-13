import { useEffect, useState } from 'react';
import { API, getJSON } from './api';
import { StatusView } from './views/StatusView';
import { LeasesView } from './views/LeasesView';
import { TopologyView } from './views/TopologyView';
import { CostsView } from './views/CostsView';
import { TtlView } from './views/TtlView';
import { RoutesView } from './views/RoutesView';
import { ChurnView } from './views/ChurnView';
import { MissView } from './views/MissView';

type ViewKey =
  | 'status'
  | 'leases'
  | 'topology'
  | 'costs'
  | 'ttl'
  | 'routes'
  | 'churn'
  | 'miss';

const NAV: { key: ViewKey; label: string }[] = [
  { key: 'status', label: 'Overview' },
  { key: 'leases', label: 'Live leases' },
  { key: 'topology', label: 'Cache topology' },
  { key: 'costs', label: 'Cost graph' },
  { key: 'ttl', label: 'TTL learning' },
  { key: 'routes', label: 'Route changes' },
  { key: 'churn', label: 'Churn' },
  { key: 'miss', label: 'Miss explanation' },
];

function useBackendHealth(): 'checking' | 'ok' | 'down' {
  const [health, setHealth] = useState<'checking' | 'ok' | 'down'>('checking');
  useEffect(() => {
    let cancelled = false;
    async function probe(): Promise<void> {
      try {
        await getJSON<{ ok: boolean }>(API.health);
        if (!cancelled) setHealth('ok');
      } catch {
        if (!cancelled) setHealth('down');
      }
    }
    void probe();
    const id = window.setInterval(() => void probe(), 10000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);
  return health;
}

export function App() {
  const [view, setView] = useState<ViewKey>('status');
  const health = useBackendHealth();

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">◈</span>
          <div>
            <div className="brand-name">CachePilot</div>
            <div className="brand-sub">dashboard</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item${view === item.key ? ' active' : ''}`}
              onClick={() => setView(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          {health === 'ok' ? (
            <span className="health-ok">● backend connected</span>
          ) : health === 'down' ? (
            <span className="health-down">● backend unreachable</span>
          ) : (
            <span className="health-checking">● checking backend…</span>
          )}
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <h1>{NAV.find((item) => item.key === view)?.label}</h1>
          <span className="muted">read-only view of the telemetry store · PRD §122/§139</span>
        </header>
        <div className="content">
          {view === 'status' && <StatusView />}
          {view === 'leases' && <LeasesView />}
          {view === 'topology' && <TopologyView />}
          {view === 'costs' && <CostsView />}
          {view === 'ttl' && <TtlView />}
          {view === 'routes' && <RoutesView />}
          {view === 'churn' && <ChurnView />}
          {view === 'miss' && <MissView />}
        </div>
      </main>
    </div>
  );
}
