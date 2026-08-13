import { API } from '../api';
import { useFetch } from '../hooks';
import { BarChart, Panel, StatCard, Table, ViewBody } from '../components';
import { fmtPct, fmtTokens } from '../format';

// Cache topology (PRD §24/§138 measurement view): per-layer change frequency
// and stability over consecutive request pairs, plus per-route tool-schema
// ordering stability. DETECT/measurement-only — nothing here reorders tools
// or rewrites prompts.
export function TopologyView() {
  const { data, error, loading } = useFetch<import('../types').TopologyReport>(API.topology);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  if (data.total_pairs === 0) {
    return (
      <div className="view">
        <Panel title="Cache topology">
          <div className="empty-state">
            No consecutive request pairs recorded yet — nothing to measure. Pairs form when a
            session issues repeated requests (PRD §24)
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="stat-grid">
        <StatCard label="Sessions" value={String(data.sessions)} />
        <StatCard label="Consecutive pairs" value={String(data.total_pairs)} />
        <StatCard label="Fingerprint churn pairs" value={String(data.churn_pairs)} />
        <StatCard label="Prefix stability" value={fmtPct(data.prefix_stability_pct)} />
        <StatCard label="Attribution gaps" value={String(data.attribution_gaps)} />
        <StatCard
          label="Unattributed loss"
          value={fmtTokens(data.unattributed_loss_tokens)}
          sub="estimated tokens"
        />
      </div>

      <Panel title="Per-layer change frequency (consecutive request pairs)">
        {data.layers.length === 0 ? (
          <div className="empty-state">No layer measurements yet</div>
        ) : (
          <>
            <BarChart
              items={data.layers.map((layer) => ({
                label: layer.attribution_based ? `${layer.layer} *` : layer.layer,
                value: layer.changes,
              }))}
              format={(value) => String(value)}
            />
            <Table
              headers={['Layer', 'Pairs', 'Changes', 'Stability', 'Est. prefix loss']}
              rows={data.layers.map((layer) => [
                layer.layer,
                String(layer.pairs),
                `${layer.change_frequency}`,
                fmtPct(layer.stability_pct),
                layer.estimated_prefix_loss_tokens === null
                  ? 'n/a'
                  : `~${fmtTokens(layer.estimated_prefix_loss_tokens)} tokens`,
              ])}
            />
          </>
        )}
        <p className="footnote">
          * Layered sub-layer rows are attributed from classified churn events (exact layered
          hashes are memory-only, PRD §30).
        </p>
      </Panel>

      <Panel title="Tool-schema ordering stability (per route)">
        {data.tool_ordering.length === 0 ? (
          <div className="empty-state">No tool-ordering measurements yet</div>
        ) : (
          <Table
            headers={['Route', 'Pairs', 'Set changes', 'Order permutations', 'Stability']}
            rows={data.tool_ordering.map((tool) => [
              tool.route ?? 'n/a',
              String(tool.pairs),
              String(tool.tool_set_changes),
              String(tool.order_permutations),
              fmtPct(tool.ordering_stability_pct),
            ])}
          />
        )}
      </Panel>
    </div>
  );
}
