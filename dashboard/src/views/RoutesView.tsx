import { API } from '../api';
import { useFetch } from '../hooks';
import { Badge, Panel, StatCard, Table, ViewBody } from '../components';
import { fmtTime, shortHash } from '../format';

function verdictTone(verdict: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (verdict === 'clean') return 'ok';
  if (verdict === 'route_instability') return 'bad';
  if (verdict === 'short_ttl') return 'warn';
  return 'neutral';
}

// Route changes: observed route identities + instability aggregates (PRD §71,
// UC-5). Only observable fields are shown; unknowns render as n/a.
export function RoutesView() {
  const { data, error, loading } = useFetch<import('../types').RoutesPayload>(API.routes);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  return (
    <div className="view">
      <div className="stat-grid">
        <StatCard label="Route switches" value={String(data.stats.route_switches)} />
        <StatCard label="Instability verdicts" value={String(data.stats.instability_verdicts)} />
        <StatCard label="Short-TTL verdicts" value={String(data.stats.short_ttl_verdicts)} />
        <StatCard label="Last switch" value={fmtTime(data.stats.last_switch_at)} />
      </div>

      <Panel title="Observed route changes">
        {data.events.length === 0 ? (
          <div className="empty-state">
            No observed route changes yet — route intelligence records switches between repeated
            logical requests (UC-5)
          </div>
        ) : (
          <Table
            headers={['Time (UTC)', 'Verdict', 'Route', 'Gateway', 'Upstream', 'Endpoint', 'Region']}
            rows={data.events.map((event) => [
              fmtTime(event.timestamp),
              <Badge key="v" tone={verdictTone(event.verdict)}>
                {event.verdict}
              </Badge>,
              <span key="r">
                {shortHash(event.previous_route_hash, 8)} → {shortHash(event.new_route_hash, 8)}
              </span>,
              event.gateway ?? <span key="n" className="muted">n/a</span>,
              event.upstream_provider ?? <span key="n" className="muted">n/a</span>,
              event.endpoint ?? <span key="n" className="muted">n/a</span>,
              event.region ?? <span key="n" className="muted">n/a</span>,
            ])}
          />
        )}
        <p className="footnote">
          Route instability = a miss caused by a physical route switch on a warm cache (never
          short-TTL evidence); short-TTL = hit-then-miss on the same route. Verdicts come from the
          router-miss classifier (PRD UC-5).
        </p>
      </Panel>
    </div>
  );
}
