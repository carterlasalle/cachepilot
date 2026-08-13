// JSON shapes served by the dashboard backend (dashboard/backend/server.py).
// Every shape mirrors the cachepilot CLI's query surface — nothing here is
// ever invented by the frontend; empty collections render empty states.

export interface HealthStats {
  total: number;
  confirmed_hits: number;
  misses: number;
  unverified: number;
  failed: number;
  churn_events: number;
  route_changes: number;
  telemetry_observed: number;
  hit_rate: number | null;
}

export interface ProviderSummary {
  provider: string;
  requests: number;
  recorded_cost_usd: number | null;
}

export interface StatusPayload {
  stats: HealthStats;
  relay: string;
  plugin: string;
  providers: ProviderSummary[];
}

export interface Lease {
  id: number;
  lease_id: string;
  session_hash: string | null;
  provider: string;
  model: string;
  api_mode: string;
  base_url_hash: string;
  auth_scope_hash: string;
  route_fingerprint: string | null;
  request_fingerprint: string;
  cache_fingerprint: string;
  system_fingerprint: string;
  tools_fingerprint: string;
  history_prefix_fingerprint: string;
  last_real_request_at: number;
  last_cache_touch_at: number | null;
  last_confirmed_hit_at: number | null;
  estimated_ttl_s: number;
  ttl_confidence: number;
  active_targets: string[];
  generation: number;
  warm_count: number;
  warm_cost_usd: number;
  estimated_cold_resume_cost_usd: number | null;
  estimated_cached_resume_cost_usd: number | null;
  state: string;
  updated_at: string;
  cache_age_s: number | null;
}

export interface LeasesPayload {
  leases: Lease[];
}

export interface CostPoint {
  timestamp: string;
  provider: string;
  cost_usd: number;
}

export interface CostsPayload {
  total_usd: number;
  per_provider: Record<string, number>;
  note: string;
  recent: CostPoint[];
}

export interface SurvivalStep {
  age_s: number;
  survival: number;
  at_risk: number;
  events: number;
}

export interface ProfileSurvival {
  sample_count: number;
  horizon_s: number | null;
  p_survive_at_ttl: number | null;
  median_s: number | null;
  steps: SurvivalStep[];
}

export interface TTLProfile {
  provider: string;
  model: string;
  api_mode: string;
  endpoint_hash: string;
  route_hash: string | null;
  lower_bound_s: number | null;
  upper_bound_s: number | null;
  estimated_ttl_s: number | null;
  confidence: number;
  sample_count: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  updated_at: string | null;
  profile_key: string;
  survival: ProfileSurvival | null;
}

export interface TtlPayload {
  profiles: TTLProfile[];
}

export interface RouteEvent {
  timestamp: string;
  session_hash: string | null;
  cache_fingerprint: string;
  request_fingerprint: string | null;
  previous_route_hash: string | null;
  new_route_hash: string | null;
  gateway: string | null;
  upstream_provider: string | null;
  endpoint: string | null;
  region: string | null;
  deployment: string | null;
  verdict: string;
  id: number | null;
}

export interface RouteIntelStats {
  route_switches: number;
  instability_verdicts: number;
  short_ttl_verdicts: number;
  last_switch_at: string | null;
}

export interface RoutesPayload {
  events: RouteEvent[];
  stats: RouteIntelStats;
}

export interface ChurnEvent {
  timestamp: string;
  session_hash: string | null;
  previous_cache_fingerprint: string;
  new_cache_fingerprint: string;
  provider: string | null;
  model: string | null;
  route_hash: string | null;
  system_changed: boolean;
  tools_changed: boolean;
  history_changed: boolean;
  route_changed: boolean;
  cache_key_changed: boolean;
  model_changed: boolean;
  likely_cause: string | null;
  confidence: number | null;
  estimated_prefix_loss_tokens: number | null;
  first_divergent_offset: number | null;
  first_divergent_layer: string | null;
  id: number | null;
}

export interface ChurnLayer {
  layer: string;
  changed: number;
  total: number;
  frequency: string;
}

export interface ChurnCause {
  cause: string;
  count: number;
}

export interface ChurnPayload {
  events: ChurnEvent[];
  layers: ChurnLayer[];
  top_causes: ChurnCause[];
}

export interface MissPayload {
  event: ChurnEvent | null;
  stable: string[];
  changed: string[];
}

export interface TopologyLayerStats {
  layer: string;
  pairs: number;
  changes: number;
  stability_pct: number | null;
  change_frequency: string;
  estimated_prefix_loss_tokens: number | null;
  attribution_based: boolean;
}

export interface ToolOrderingStats {
  route: string | null;
  pairs: number;
  tool_set_changes: number;
  order_permutations: number;
  ordering_stability_pct: number | null;
}

export interface TopologyReport {
  layers: TopologyLayerStats[];
  tool_ordering: ToolOrderingStats[];
  sessions: number;
  total_pairs: number;
  churn_pairs: number;
  prefix_stability_pct: number | null;
  attribution_gaps: number;
  unattributed_loss_tokens: number;
}
