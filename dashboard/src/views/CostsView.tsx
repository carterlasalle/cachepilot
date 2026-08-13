import { API } from '../api';
import { useFetch } from '../hooks';
import { BarChart, LineChart, Panel, StatCard, ViewBody } from '../components';
import { fmtCost } from '../format';

// Cost graph: recorded provider-returned costs from request_events. The
// cumulative line is built from the most recent 200 recorded-cost rows —
// honest recorded cost only, never "money saved" (PRD §79, invariant 4).
export function CostsView() {
  const { data, error, loading } = useFetch<import('../types').CostsPayload>(API.costs);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  const providerBars = Object.entries(data.per_provider).map(([provider, cost]) => ({
    label: provider,
    value: cost,
  }));

  // Cumulative recorded cost over the recent series (chronological order),
  // indexed 1..N so the x-axis is request order.
  let running = 0;
  const indexed = [...data.recent].reverse().map((point, index) => {
    running += point.cost_usd;
    return { x: index + 1, y: running };
  });

  return (
    <div className="view">
      <div className="stat-grid">
        <StatCard label="Total recorded cost" value={fmtCost(data.total_usd)} />
        <StatCard label="Providers with recorded cost" value={String(providerBars.length)} />
      </div>

      <Panel title="Recorded cost by provider">
        {providerBars.length === 0 ? (
          <div className="empty-state">
            No recorded costs yet — request_events rows without a provider-returned cost are
            unknown, never counted as zero
          </div>
        ) : (
          <BarChart items={providerBars} format={(value) => fmtCost(value)} />
        )}
      </Panel>

      <Panel title="Cumulative recorded cost — most recent 200 requests">
        {indexed.length < 2 ? (
          <div className="empty-state">
            Not enough recorded-cost data points yet to draw the cumulative series
          </div>
        ) : (
          <LineChart points={indexed} />
        )}
      </Panel>

      <p className="footnote">{data.note}</p>
    </div>
  );
}
