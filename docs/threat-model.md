# CachePilot Threat Model

This document enumerates the security-relevant assets, trust boundaries,
threats, and mitigations of the CachePilot system as built. Each mitigation
is mapped to an AGENTS.md invariant and to the code that implements it.
Companion documents: [SECURITY.md](../SECURITY.md) (policy + reporting) and
[docs/architecture.md](architecture.md) (component walkthrough). The
authoritative invariants are AGENTS.md §"Non-Negotiable Architecture Rules".

## 1. System summary

Three processes plus one store:

```text
stock Hermes (plugin loaded)  →  cachepilotd relay (127.0.0.1:8787)  →  provider API
        │                                    │
        │ X-CachePilot-* correlation headers │ SQLite WAL telemetry store
        │ (stripped before upstream)         │ (~/.hermes/cachepilot/cachepilot.db)
        └────────────────────────────────────┘ memory-only request snapshots
```

- **Hermes plugin** (`packages/hermes-plugin`): middleware + lifecycle hooks
  inside the Hermes process. Observes agent semantics; injects correlation
  headers; maintains background-target refcounts and the duration store
  (`~/.cachepilot/long_tasks.db`).
- **Relay** (`packages/relay`): standalone localhost proxy. Forwards
  verbatim, observes, fingerprints, schedules and executes bounded warm
  replays, writes telemetry.
- **Telemetry store**: SQLite (WAL) with hashes/timestamps/usage/prices/
  route identities/outcomes only.
- **Provider API**: the upstream the relay forwards to and warms against.

## 2. Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Provider API keys / Authorization headers | CRITICAL — full account access | Hermes config / env; forwarded through the relay in-flight; **never** logged, persisted, or snapshotted |
| Conversation prompts, tool arguments, tool output | HIGH — user data | In-flight request/response bodies through the relay; memory-only snapshots; **never** written to disk by CachePilot |
| Request snapshots (cache-producing bodies) | HIGH — replayable prompts | Memory-only `SnapshotStore` (PRD §30); destroyed on lease invalidation/relay restart |
| Telemetry DB | LOW-MED — hashes, usage, costs, outcomes, route identities | `~/.hermes/cachepilot/cachepilot.db` (WAL, safe fallback) |
| Cache identity / route identity hashes | LOW — derived digests | telemetry DB + lease records |
| Relay control surface (TCP listener) | MED — can send provider traffic | `127.0.0.1:8787` loopback only |
| Command duration history | LOW — normalized signatures only, no command text | `~/.cachepilot/long_tasks.db` |

## 3. Trust boundaries

1. **Hermes ↔ plugin**: the plugin trusts Hermes' documented context
   objects and hook payloads; it must never import private AIAgent
   internals (invariant 1, PRD §141). The plugin's outputs (correlation
   headers) are advisory metadata, not cache identity.
2. **Plugin ↔ relay**: the relay trusts the plugin's `X-CachePilot-*`
   headers only for *correlation and target counting* — never for cache
   identity. Cache identity is derived from the physical request by the
   relay itself (invariant 7).
3. **Relay ↔ telemetry store**: the relay writes only the invariant-10
   allowlist (hashes, timestamps, usage, prices, route identities,
   outcomes). The store is not an authorization boundary — it is local
   same-user data.
4. **Relay ↔ provider**: TLS to the provider; the provider necessarily
   observes request/response content (that is the service contract).
5. **Same-user local processes**: NOT a trust boundary. Any process running
   as the same user can already read the user's provider credentials; the
   relay adds no new cross-user exposure (loopback-only bind).

## 4. Threats and mitigations

### T1 — Secret exfiltration (API keys / auth headers)

An attacker (compromised dependency, malicious local process, log sink)
obtains the provider credentials that flow through the system.

Mitigations (mapped):

- **Invariant 10 / PRD §83**: the relay never logs or persists
  Authorization headers or API keys. `auth_scope` is stored as a **stable
  hash** of the Authorization header (`relay-default` when absent) —
  `packages/core/src/cachepilot_core/identity.py`,
  `cachepilot_relay/observation.py`.
- **Invariant 10, plugin logging**: every plugin log line goes through
  `emit_debug` (`cachepilot_hermes/config.py`), which reduces containers to
  `Type(len=N)` summaries and never accepts payload values; error hooks
  deliberately exclude `error`/`reason`/`request` fields
  (`lifecycle.py::make_api_request_error`).
- **Invariant 9**: an observation error logs a warning and forwards anyway —
  a failure in the logging path cannot cause header redaction to fail
  silently; forwarding never depends on CachePilot.
- **No fork / no monkey patch (invariant 1)**: the plugin never reaches
  into Hermes' request objects beyond documented middleware contracts.

### T2 — Prompt persistence (raw prompts / tool output on disk)

CachePilot or a bug writes conversation content, tool output, or raw tool
schemas to the telemetry store or logs.

Mitigations:

- **Invariant 10 / PRD §83**: the SQLite schema stores only hashes
  (`system_hash`, `tools_hash`, `history_hash`, fingerprints), usage
  numbers, prices, route identities and outcomes — never content
  (`packages/core/src/cachepilot_core/storage.py`, §82 schema).
- **PRD §30**: request snapshots live in a memory-only `SnapshotStore`
  (keyed by cache fingerprint; nothing in a snapshot is ever persisted, and
  a fresh relay start has no snapshots at all). The churn classifier's
  request-body cache is additionally bounded — ≤1 MiB per body, ≤32
  sessions (`cachepilot_relay/observation.py`). Churn classification's
  divergence snippet is bounded, in-memory, and never written to
  `churn_events` (only the numeric offset + layer name are stored).
- **Duration history** records normalized command *signatures* (e.g.
  `uv run pytest`), never command text or tool output
  (`duration_history.py`).
- **CLI honesty**: empty/unavailable data prints `n/a` or "no telemetry
  recorded yet" — the absence of evidence is never fabricated.

### T3 — MITM on the relay (loopback listener abuse)

A local process connects to `127.0.0.1:8787` and either reads traffic or
forges requests that the relay forwards with Hermes' credentials.

Mitigations:

- **Loopback-only bind**: default `127.0.0.1:8787`; wildcard binds
  (`0.0.0.0` / `::`) are **refused at config validation** unless
  `CACHEPILOT_RELAY_ALLOW_EXTERNAL_BIND=1` is set, and the refusal is
  itself a tested behavior (`packages/relay/tests/test_bind.py`,
  `config.py::_refuse_wildcard_bind`).
- **Same-user model**: the relay is a local optimization for the same user
  (PRD §26); it does not widen the trust surface beyond what the user's own
  shell already has.
- **Host header rewrite**: the client `Host` is dropped and rebuilt from the
  upstream URL; hop-by-hop headers are stripped per RFC 7230 §6.1
  (`proxy.py`).
- **PRD §93 relay failure isolation**: upstream transport errors are logged
  and answered 502 without breaking forwarding. The specified per-route
  optimization-disable breaker (3 consecutive relay-attributable failures)
  is a documented follow-up, not yet implemented — forwarding fail-open is
  the current control.

### T4 — Cache poisoning (malicious or accidental warm)

A warm replay causes the provider to cache content that should not be
cached, or a local attacker induces the relay to warm an attacker-chosen
prompt, polluting the provider cache and burning billable tokens.

Mitigations:

- **Invariant 8 / PRD §23**: a warm is byte-identical to the original
  cache-producing request except for safe output-bounding fields
  (`max_tokens=1` family); `build_warm_request` returns `None` rather than
  inventing fields (`adapters.py`, `docs/provider-adapters.md`).
- **Invariant 3 / PRD §67-70**: warm outcomes are classified
  CONFIRMED_HIT / MISS_REBUILT / SUCCESS_UNVERIFIED / FAILED — never
  "200 = hit". `last_cache_touch_at` advances only on a verified cache
  touch.
- **Warm output is discarded**: generated content never reaches Hermes and
  is never executed as tools (PRD §32); only usage/outcome/cost are
  recorded.
- **Snapshots are memory-only and short-lived** (T2): a stale or
  attacker-planted snapshot cannot be replayed after a restart.
- **Real-request-wins (invariant 6)**: generation counters and per-identity
  locks ensure a warm can never race or clobber a real request's cache
  evidence.

### T5 — Warm-request confusion (warm mistaken for real traffic)

A warm request is mistaken for a natural request (or vice versa), corrupting
lease state, TTL learning, or cost accounting.

Mitigations:

- **Invariant 3**: outcome classification is separate from request kind;
  `request_kind` distinguishes warm from normal in telemetry.
- **PRD §147**: the warm executor sends directly to the upstream and never
  re-enters the forwarding/observation path — no recursive lease tracking,
  no re-observation, no tool execution (`relay/warm_executor.py`).
- **Invariant 6**: `real_request_active` / `warm_request_active` flags and
  generation counters serialize warm-vs-real and warm-vs-complete races;
  race tests cover all three pairings (`test_leases.py`, `test_warm_executor.py`).
- **Correlation headers are stripped** before the upstream sees them, so a
  warm replay can never carry stale correlation metadata into provider
  cache identity.

### T6 — DoS via warm loop (unbounded warming)

A bug or misconfiguration keeps issuing warm requests forever, burning
money and hammering the provider.

Mitigations:

- **Invariant 5 / PRD §61-62**: the economic gate enforces the warm budget —
  cumulative spend can never exceed `expected_value × budget_ratio`; the
  lease enters `ECONOMIC_STOP` on exhaustion. A 3-hour compile cannot
  trigger 36 refreshes.
- **PRD §94 warm circuit breaker**: 2 consecutive warm outcomes that did
  not verify a cache touch open the circuit (`SKIPPED_CIRCUIT_OPEN`) until
  a normal request produces new cache evidence (`leases.py`).
- **PRD §53-54**: scheduling uses safe deadlines with jitter; a due warm is
  a single bounded request, not a burst.
- **Bounded warm transport**: the warm executor enforces a per-request
  timeout (30 s default); transport errors classify FAILED, never
  "refreshed".
- **PRD §65 default**: economically unbounded repeated warming is disabled
  by default — unknown pricing ⇒ `SKIP_UNKNOWN_PRICING`, no warm.

## 5. Residual risks (accepted)

| Risk | Why accepted |
|---|---|
| Same-user local processes can forge requests through the relay | The relay exists to serve the same user's Hermes; loopback-only bind contains it. Cross-user protection is the OS user boundary. |
| The relay's TCP port has no per-request authentication | It is bound to loopback only; the upstream credentials it forwards are the user's own. |
| Per-route relay circuit breaker (PRD §93) not yet implemented | Forwarding already fails open; optimization-disable per route is scheduled with P11-adjacent work. |
| Streaming responses are recorded SUCCESS_UNVERIFIED with zero usage | The stream is never consumed (read-only observation); usage parsing for streams is deferred by design. |

## 6. Invariant → control index

| AGENTS.md invariant | Primary controls |
|---|---|
| 1 no fork / no monkey patches | plugin-only integration; CI test asserts zero Hermes source modifications (`test_stock_hermes_unchanged.py`) |
| 2 no LLM polling | local scheduler + completion notification; no heartbeat turns |
| 3 HTTP 200 ≠ hit | outcome classification; verified-touch-only refresh |
| 4 warm costs visible | `warm_count` / `warm_cost_usd` on every lease; recorded-cost-only CLI |
| 5 economic warming | `EconomicController` + budget + `ECONOMIC_STOP` |
| 6 real requests win | generation counters, per-identity locks, `real_request_active` |
| 7 physical cache identity | identity derived from provider/model/endpoint/auth-scope/route/hashes |
| 8 two fingerprints | request vs cache fingerprint; warm differs only in bounding |
| 9 fail open / fail closed | forwarding never depends on CachePilot; uncertain warm = skip; warm circuit breaker |
| 10 no secret/prompt persistence | invariant-10 allowlist storage; memory-only snapshots; secret-safe logging |
