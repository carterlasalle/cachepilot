import { API } from '../api';
import { useFetch } from '../hooks';
import { Badge, Panel, StatCard, Table, ViewBody } from '../components';
import { fmtCost } from '../format';

// Overview: cache health aggregates + relay/plugin state + per-provider
// summary. Mirrors `cachepilot status` — the hit rate only counts requests
// with trustworthy cache telemetry (CONFIRMED_HIT + MISS_REBUILT).
export function StatusView() {
  const { data, error, loading } = useFetch<import('../types').StatusPayload>(API.status, 10000);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;
  const stats = data.stats;

  const relayTone = data.relay === 'healthy' ? 'ok' : data.relay.startsWith('unreachable') ? 'warn' : 'neutral';
  const pluginTone = data.plugin.startsWith('inactive') ? 'warn' : 'ok';

  return (
    <div className="view">
      <div className="stat-grid">
        <StatCard label="Requests recorded" value={String(stats.total)} />
        <StatCard
          label="Cache hit rate"
          value={stats.hit_rate === null ? 'n/a' : `${(stats.hit_rate * 100).toFixed(1)}%`}
          sub={`${stats.telemetry_observed} requests with cache telemetry`}
        />
        <StatCard label="CONFIRMED_HIT" value={String(stats.confirmed_hits)} />
        <StatCard label="MISS_REBUILT" value={String(stats.misses)} />
        <StatCard label="SUCCESS_UNVERIFIED" value={String(stats.unverified)} />
        <StatCard label="FAILED" value={String(stats.failed)} />
        <StatCard label="Churn events" value={String(stats.churn_events)} />
        <StatCard label="Route changes" value={String(stats.route_changes)} />
      </div>

      <div className="stat-grid">
        <StatCard label="Relay" value={data.relay} />
        <StatCard label="Hermes plugin" value={data.plugin} />
      </div>

      <Panel title="Per-provider summary">
        {data.providers.length === 0 ? (
          <div className="empty-state">No provider telemetry recorded yet</div>
        ) : (
          <Table
            headers={['Provider', 'Requests', 'Recorded cost']}
            rows={data.providers.map((provider) => [
              <span key="p">{provider.provider}</span>,
              String(provider.requests),
              provider.recorded_cost_usd === null ? (
                <span key="c" className="muted">
                  unknown
                </span>
              ) : (
                fmtCost(provider.recorded_cost_usd)
              ),
            ])}
          />
        )}
        <p className="footnote">
          Recorded-cost-only (PRD §79): providers without a provider-returned cost show{' '}
          <Badge tone="neutral">unknown</Badge> — never zero. Relay/plugin badges:
          <Badge tone={relayTone}>{data.relay}</Badge>
          <Badge tone={pluginTone}>{data.plugin}</Badge>
        </p>
      </Panel>
    </div>
  );
}
