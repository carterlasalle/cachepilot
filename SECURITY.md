# Security Policy

CachePilot is an optimization layer for Hermes Agent. It observes and replays
LLM provider traffic on the local machine, so its security posture is
deliberately conservative: **it must never be able to make Hermes less safe
or less available than stock Hermes is.** The full reasoning, asset model,
threat list, and control mapping live in [docs/threat-model.md](docs/threat-model.md).

## Supported versions

Security fixes land on the `main` branch and are backported on request.
This project is pre-1.0 (0.1.0); any release may carry security fixes.

## Reporting a vulnerability

Do **not** open a public issue for a suspected vulnerability. Report it
privately to the maintainers (repo owner / fleet foreman). Include:

- affected component (plugin / relay / CLI / core) and version (package
  version or commit SHA);
- a minimal repro: configuration, upstream provider, request shape;
- impact: what an attacker could read, modify, or deny.

You will get an acknowledgement within 3 business days and a fix plan within
10 business days. Fixes are committed as ordinary tasks through the GitReins
judge (which runs secret scans + the security rule scanner on every commit).

## Security posture

### Local-only relay

- `cachepilotd` binds `127.0.0.1:8787` by default and **refuses wildcard
  binds** (`0.0.0.0` / `::`) at config validation
  (`packages/relay/src/cachepilot_relay/config.py`) unless
  `CACHEPILOT_RELAY_ALLOW_EXTERNAL_BIND=1` is explicitly set — that override
  is for test harnesses and is never the default. The bind policy is
  enforced by a dedicated test (`test_bind.py`).
- The relay is a local process serving Hermes on the same machine (PRD §26,
  §89). It is never meant to be exposed externally.

### No secret or prompt persistence

- API keys, authorization headers, raw prompts, raw messages, tool output,
  raw tool schemas, user content, and raw provider responses are **never
  persisted** (PRD §83, AGENTS.md invariant 10). The SQLite telemetry store
  holds only hashes, timestamps, usage, prices, route identities, and
  outcomes. The CLI shows `n/a` rather than fabricating values.
- The request **snapshot store is memory-only** (PRD §30): it dies on relay
  restart and is never written to disk. When a lease is invalidated the
  snapshot reference is dropped.
- The plugin's structured debug emitter reduces containers to
  `Type(len=N)` summaries and never logs payload values, error messages, or
  headers (`emit_debug` in `packages/hermes-plugin/src/cachepilot_hermes/config.py`).
- Authorization is hashed into `auth_scope_hash` for cache identity; the
  header itself is forwarded only to the upstream it was meant for.

### Fail-open traffic, fail-closed warming

- **Normal traffic always forwards.** Every observation/telemetry error —
  including an unusable telemetry path at startup — logs a warning and
  leaves forwarding untouched (AGENTS.md invariant 9). Hermes correctness
  never depends on CachePilot.
- **Uncertain warm = skip.** A warm request is only built when the adapter
  can bound output with certainty; `build_warm_request` returns `None`
  otherwise (`SKIPPED_UNSUPPORTED`). Warm content is discarded; only
  usage/outcome/cost are recorded.
- Warm requests are sent directly to the upstream and never re-enter the
  forwarding/observation path (no recursive lease tracking, no tool
  execution).

### Circuit breakers

- **Warm circuit breaker** (implemented, PRD §94): after 2 consecutive warm
  outcomes that did not verify a cache touch, warming stops for that lease
  (`SKIPPED_CIRCUIT_OPEN`) until a normal request produces fresh cache
  evidence.
- **Relay-attributable failure isolation** (PRD §93): upstream transport
  failures are logged and answered with 502 without breaking forwarding;
  the per-route optimization-disable breaker is a specified follow-up (see
  threat model §mitigations).

### Stock Hermes stays upstream

- Integration is limited to documented plugin surfaces (middleware + hooks);
  there is no fork, no monkey-patching, and no import of private AIAgent
  internals (PRD §141). A CI test asserts zero Hermes source modifications.

### Secret hygiene for contributors

- Never commit real API keys or long-lived tokens — the GitReins secrets
  guard (gitleaks + regex scanner) **blocks** commits containing them.
- Test fixtures use obviously fake keys; do not make them look like valid
  `sk-…` keys of plausible length.
- `.env` / credential files are never committed; add them to `.gitignore`.

## Threat model

Assets, trust boundaries, threat list (secret exfiltration, prompt
persistence, MITM on the relay, cache poisoning, warm-request confusion, DoS
via warm loop), and the control mapping are documented in
[docs/threat-model.md](docs/threat-model.md). Please read it before
reporting — it may already cover the behavior you found.
