import type { ReactNode } from 'react';

// Shared UI primitives for the dashboard views. All data-driven components
// render explicit empty states — an empty telemetry store never produces a
// fabricated-looking chart or table.

export function Panel({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>{title}</h2>
        {right ? <div className="panel-right">{right}</div> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function EmptyState({ what }: { what: string }) {
  return (
    <div className="empty-state" role="status">
      {what}
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="error-state" role="alert">
      Backend unreachable: {message} — is <code>dashboard/backend/server.py</code> running?
    </div>
  );
}

export function ViewBody({ error, loading }: { error: string | null; loading: boolean }) {
  if (error) return <ErrorState message={error} />;
  if (loading) return <p className="muted">Loading…</p>;
  return null;
}

export function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  );
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'ok' | 'warn' | 'bad' | 'neutral' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function BarChart({
  items,
  format,
}: {
  items: { label: string; value: number }[];
  format?: (value: number) => string;
}) {
  if (items.length === 0) return null;
  const max = Math.max(...items.map((item) => item.value), 1e-9);
  return (
    <div className="bars">
      {items.map((item) => (
        <div key={item.label} className="bar-row">
          <span className="bar-label" title={item.label}>
            {item.label}
          </span>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${(item.value / max) * 100}%` }} />
          </div>
          <span className="bar-value">{format ? format(item.value) : item.value}</span>
        </div>
      ))}
    </div>
  );
}

export function LineChart({
  points,
  width = 560,
  height = 150,
}: {
  points: { x: number; y: number }[];
  width?: number;
  height?: number;
}) {
  if (points.length === 0) return <EmptyState what="no data points yet" />;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys, 1e-9);
  const pad = 6;
  const spanX = maxX - minX || 1;
  const xAt = (x: number) => pad + ((x - minX) / spanX) * (width - pad * 2);
  const yAt = (y: number) => height - pad - (y / maxY) * (height - pad * 2);
  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xAt(p.x).toFixed(1)},${yAt(p.y).toFixed(1)}`)
    .join(' ');
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      role="img"
      aria-label="line chart"
    >
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" />
      {points.map((p, i) => (
        <circle key={i} cx={xAt(p.x)} cy={yAt(p.y)} r="2.5" fill="var(--accent)" />
      ))}
    </svg>
  );
}

export function Table({ headers, rows }: { headers: string[]; rows: ReactNode[][] }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {headers.map((header) => (
              <th key={header}>{header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
