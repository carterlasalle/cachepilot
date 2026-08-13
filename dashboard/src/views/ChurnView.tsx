import { API } from '../api';
import { useFetch } from '../hooks';
import { BarChart, Panel, Table, ViewBody } from '../components';
import { fmtTime, shortHash } from '../format';

// Cache churn (PRD §25, §76): per-layer change frequency over recorded
// churn events + the most common classifier diagnoses.
export function ChurnView() {
  const { data, error, loading } = useFetch<import('../types').ChurnPayload>(API.churn);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  return (
    <div className="view">
      <Panel title="Per-layer change frequency">
        {data.events.length === 0 ? (
          <div className="empty-state">
            No churn events — the detector records a row only when a cache fingerprint actually
            changes (PRD §25)
          </div>
        ) : (
          <BarChart
            items={data.layers.map((layer) => ({
              label: layer.layer,
              value: layer.changed,
            }))}
            format={(value) => `${value}/${data.events.length}`}
          />
        )}
      </Panel>

      <Panel title="Most common likely causes">
        {data.top_causes.length === 0 ? (
          <div className="empty-state">
            No classified causes yet — events recorded before Phase 10 (or without content
            available) carry no diagnosis
          </div>
        ) : (
          <Table
            headers={['Count', 'Cause']}
            rows={data.top_causes.map((cause) => [String(cause.count), cause.cause])}
          />
        )}
      </Panel>

      <Panel title={`Recent churn events (${data.events.length})`}>
        {data.events.length === 0 ? (
          <div className="empty-state">No churn events recorded yet</div>
        ) : (
          <Table
            headers={['Time (UTC)', 'Session', 'Cache key', 'Changed', 'Likely cause']}
            rows={data.events.map((event) => [
              fmtTime(event.timestamp),
              shortHash(event.session_hash, 10),
              <span key="fp">
                {shortHash(event.previous_cache_fingerprint, 8)} →{' '}
                {shortHash(event.new_cache_fingerprint, 8)}
              </span>,
              <span key="ch">
                {[
                  event.system_changed && 'system',
                  event.tools_changed && 'tools',
                  event.history_changed && 'history',
                  event.route_changed && 'route',
                  event.cache_key_changed && 'cache key',
                  event.model_changed && 'model',
                ]
                  .filter(Boolean)
                  .join(', ') || '—'}
              </span>,
              event.likely_cause ?? <span className="muted">n/a (not classified)</span>,
            ])}
          />
        )}
      </Panel>

      {data.events.some((event) => event.estimated_prefix_loss_tokens !== null) && (
        <p className="footnote">
          Estimated reusable-prefix loss is only shown per event when the classifier had content
          available; the exact figures are on the Miss explanation view.
        </p>
      )}
    </div>
  );
}
