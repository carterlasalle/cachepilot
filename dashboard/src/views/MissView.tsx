import { API } from '../api';
import { useFetch } from '../hooks';
import { Badge, Panel, StatCard, ViewBody } from '../components';
import { fmtTokens, fmtTime, shortHash } from '../format';

// Miss explanation (PRD §75, §137): the LATEST churn event — the stored
// moment a reusable prefix was destroyed — with the layers that changed, the
// likely cause, confidence and estimated prefix loss. Mirrors
// `cachepilot explain-miss`.
export function MissView() {
  const { data, error, loading } = useFetch<import('../types').MissPayload>(API.miss);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  const event = data.event;

  return (
    <div className="view">
      <Panel title="Miss explanation (latest churn event)">
        {event === null ? (
          <div className="empty-state">
            No churn events recorded — nothing to explain. A miss explanation exists only once the
            detector records a real cache-fingerprint transition (PRD §75)
          </div>
        ) : (
          <>
            <div className="stat-grid">
              <StatCard label="Churn event" value={event.id === null ? 'n/a' : `#${event.id}`} sub={fmtTime(event.timestamp)} />
              <StatCard label="Session" value={shortHash(event.session_hash, 12)} />
              <StatCard
                label="Cache key"
                value={`${shortHash(event.previous_cache_fingerprint, 8)} → ${shortHash(
                  event.new_cache_fingerprint,
                  8,
                )}`}
              />
              <StatCard
                label="Confidence"
                value={event.confidence === null ? 'n/a' : event.confidence.toFixed(2)}
              />
              <StatCard
                label="Est. prefix lost"
                value={event.estimated_prefix_loss_tokens === null ? 'n/a' : `~${fmtTokens(event.estimated_prefix_loss_tokens)} tokens`}
              />
            </div>

            <div className="layer-groups">
              <div className="layer-group">
                <h3>Stable</h3>
                {data.stable.length === 0 ? (
                  <p className="muted">(none)</p>
                ) : (
                  <div className="chips">
                    {data.stable.map((layer) => (
                      <Badge key={layer} tone="ok">
                        {layer}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
              <div className="layer-group">
                <h3>Changed</h3>
                {data.changed.length === 0 ? (
                  <p className="muted">(none)</p>
                ) : (
                  <div className="chips">
                    {data.changed.map((layer) => (
                      <Badge key={layer} tone="bad">
                        {layer}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="cause-block">
              <h3>Likely cause</h3>
              <p className="cause-text">{event.likely_cause ?? 'n/a (not classified)'}</p>
              {event.first_divergent_offset !== null && event.first_divergent_layer !== null && (
                <p className="footnote">
                  First divergent byte: offset ~{event.first_divergent_offset} within{' '}
                  <code>{event.first_divergent_layer}</code>
                </p>
              )}
            </div>
          </>
        )}
      </Panel>
      <p className="footnote">
        Rows recorded before Phase 10 (or with content unavailable at record time) show n/a for
        the classifier fields — honest unknowns, never guesses (AGENTS.md invariant 3).
      </p>
    </div>
  );
}
