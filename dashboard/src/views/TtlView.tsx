import { API } from '../api';
import { useFetch } from '../hooks';
import { LineChart, Panel, StatCard, ViewBody } from '../components';
import { fmtSeconds, fmtTime, shortHash } from '../format';

// TTL learning: route-keyed learned profiles (PRD §82) with the P11 survival
// view per profile (PRD §99/§138) — P(cache survives) over CLEAN
// observations. A profile with no clean observations shows no curve; a TTL
// beyond the observed horizon shows "unknown", never a fabricated number.
export function TtlView() {
  const { data, error, loading } = useFetch<import('../types').TtlPayload>(API.ttl);
  if (error || loading || !data) return <ViewBody error={error} loading={loading} />;

  if (data.profiles.length === 0) {
    return (
      <div className="view">
        <Panel title="TTL learning">
          <div className="empty-state">
            No TTL profiles yet — learning needs repeated observations of a stable route
            (PRD §82)
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="view">
      {data.profiles.map((profile) => {
        const survival = profile.survival;
        const curvePoints =
          survival && survival.steps.length > 0
            ? [{ x: 0, y: 1 }, ...survival.steps.map((step) => ({ x: step.age_s, y: step.survival }))]
            : [];
        return (
          <Panel
            key={profile.profile_key}
            title={`${profile.provider} · ${profile.model} · ${profile.api_mode}`}
            right={
              <span className="muted">
                route {shortHash(profile.route_hash, 8)} · endpoint {shortHash(profile.endpoint_hash, 8)}
              </span>
            }
          >
            <div className="stat-grid">
              <StatCard
                label="Estimated TTL"
                value={fmtSeconds(profile.estimated_ttl_s)}
                sub={`bounds ${fmtSeconds(profile.lower_bound_s)} – ${fmtSeconds(profile.upper_bound_s)}`}
              />
              <StatCard label="Confidence" value={profile.confidence.toFixed(2)} />
              <StatCard label="Samples" value={String(profile.sample_count)} />
              <StatCard
                label="P(survive at TTL)"
                value={survival?.p_survive_at_ttl === null || survival?.p_survive_at_ttl === undefined ? 'n/a' : survival.p_survive_at_ttl.toFixed(2)}
                sub={
                  survival === null
                    ? 'no clean observations yet'
                    : `${survival.sample_count} clean observations`
                }
              />
              <StatCard
                label="Median survival"
                value={survival?.median_s === null || survival?.median_s === undefined ? 'n/a' : fmtSeconds(survival.median_s)}
                sub={survival?.horizon_s !== null && survival?.horizon_s !== undefined ? `horizon ${fmtSeconds(survival.horizon_s)}` : undefined}
              />
            </div>
            {survival === null || curvePoints.length === 0 ? (
              <div className="empty-state">
                No survival curve yet — the P11 estimator needs CLEAN observations (verified hits
                and misses on a stable route, PRD §56)
              </div>
            ) : (
              <div className="chart-block">
                <LineChart points={curvePoints} />
                <p className="footnote">
                  Kaplan-Meier-style estimate: P(cache survives) vs cache age; MISS_REBUILT lowers
                  the curve, CONFIRMED_HIT rows are right-censored. Beyond the observed horizon the
                  estimate is undefined — shown as n/a, never extrapolated. Updated {fmtTime(profile.updated_at)}.
                </p>
              </div>
            )}
          </Panel>
        );
      })}
    </div>
  );
}
