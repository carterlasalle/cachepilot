import { API } from '../api';
import { useFetch } from '../hooks';
import { Badge, Panel, Table, ViewBody } from '../components';
import { fmtCost, fmtSeconds, fmtTime, shortHash } from '../format';

const LEASE_POLL_MS = 5000;

function stateTone(state: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (state === 'armed' || state === 'confirmed_hit') return 'ok';
  if (state === 'economic_stop' || state === 'miss_rebuilt') return 'bad';
  if (state === 'warming' || state === 'warm_scheduled') return 'warn';
  return 'neutral';
}

// Live leases: polls the store every 5s. Every column is a real stored
// lease field (or a direct derivation like cache age) — never fabricated.
export function LeasesView() {
  const { data, error, loading } = useFetch<import('../types').LeasesPayload>(API.leases, LEASE_POLL_MS);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  return (
    <div className="view">
      <Panel
        title="Live leases"
        right={<span className="muted">polls every {LEASE_POLL_MS / 1000}s</span>}
      >
        {data.leases.length === 0 ? (
          <div className="empty-state">
            No active leases recorded yet — leases appear once the relay persists a lease for a
            running background task
          </div>
        ) : (
          <Table
            headers={['Lease', 'Provider / model', 'State', 'Targets', 'Cache age', 'TTL', 'Conf.', 'Warms']}
            rows={data.leases.map((lease) => [
              <span key="id" title={lease.lease_id}>
                {shortHash(lease.lease_id, 8)}
              </span>,
              <span key="pm">
                {lease.provider} / {lease.model}
              </span>,
              <Badge key="s" tone={stateTone(lease.state)}>
                {lease.state}
              </Badge>,
              String(lease.active_targets.length),
              fmtSeconds(lease.cache_age_s),
              fmtSeconds(lease.estimated_ttl_s),
              lease.ttl_confidence.toFixed(2),
              String(lease.warm_count),
            ])}
          />
        )}
        <p className="footnote">
          Cache age = now − last cache touch (unknown when the cache was never touched). Warm cost
          is recorded and visible on the lease row — never hidden (AGENTS.md invariant 4).
        </p>
      </Panel>
      {data.leases.length > 0 && (
        <Panel title="Lease details">
          <Table
            headers={['Updated', 'Session', 'Cache fp', 'Generation', 'Warm cost', 'Est. cold / cached resume']}
            rows={data.leases.map((lease) => [
              fmtTime(lease.updated_at),
              shortHash(lease.session_hash, 10),
              shortHash(lease.cache_fingerprint, 10),
              String(lease.generation),
              fmtCost(lease.warm_cost_usd),
              `${fmtCost(lease.estimated_cold_resume_cost_usd)} / ${fmtCost(
                lease.estimated_cached_resume_cost_usd,
              )}`,
            ])}
          />
        </Panel>
      )}
    </div>
  );
}
