# Hermes Integration — the Plugin Surface (PRD §15-19, §29, §125, §128, §141)

This runbook documents how CachePilot hooks **stock upstream
NousResearch/hermes-agent** — what is registered, what each surface does,
what is deliberately NOT modified, and how the relay is targeted. It
describes the code as built (`packages/hermes-plugin/src/cachepilot_hermes/`,
integration-verified against Hermes v0.20.0's plugin architecture:
`hermes_cli/plugins.py`, `hermes_cli/middleware.py`, `hermes_cli/hooks.py`).

The rule that governs everything here (AGENTS.md invariant 1, PRD §141):

> CachePilot may depend only on documented/supported plugin APIs,
> middleware contracts, configuration surfaces, and HTTP provider APIs. It
> MUST NOT import private AIAgent internals.

---

## 1. How the plugin loads

The package is discovered as a pip plugin via the `hermes_agent.plugins`
entry-point group. `PluginManager._load_plugin` imports the module named by
the entry point and calls its `register(ctx)` with a
`hermes_cli.plugins.PluginContext`:

```toml
# packages/hermes-plugin/pyproject.toml
[project.entry-points."hermes_agent.plugins"]
cachepilot = "cachepilot_hermes.plugin:register"
```

`register(ctx)` does exactly two things — register every middleware kind and
every lifecycle hook (PRD §128 step 2-3). No tools, no commands, no skills,
no config mutation, no other context surface is touched.

### Plugin manifest

`PluginManifest` (validated, mirrors Hermes' manifest fields) is built by
`cachepilot_hermes.plugin`:

| Field | Value |
|---|---|
| name | `cachepilot-hermes-plugin` |
| version | `0.1.0` |
| entrypoint | `cachepilot_hermes.plugin:register` |
| middleware | `tool_request`, `tool_execution`, `llm_request`, `llm_execution` |
| hooks | `post_tool_call`, `post_api_request`, `api_request_error`, `subagent_start`, `subagent_stop`, `on_session_start`, `on_session_end`, `on_session_reset` |

## 2. Middleware (PRD §16, §40)

Hermes v0.20.0's `VALID_MIDDLEWARE` contains all four kinds. Every callback
returns `None` (= "no change") on any failure — fail open for traffic, and
byte-identical to stock when the plugin misbehaves.

| Kind | Contract | What CachePilot does |
|---|---|---|
| `tool_request` | `(tool_name, args, original_args, **ctx) → None \| {"args", "source", "reason"}` | Terminal auto-backgrounding: the deterministic classifier (see below) may promote a call to `background=True` (+ `notify_on_complete=True`) with `source="cachepilot"`, `reason="long-running command"` (PRD §40). Only `terminal` calls are ever candidates. |
| `tool_execution` | `(tool_name, args, original_args, next_call, **ctx)` | Pure pass-through: invokes `next_call(args)` exactly once and returns its result — reproduces stock behavior by construction. |
| `llm_request` | `(request, original_request, **ctx) → None \| {"request"}` | Correlation-header injection (PRD §29, §4 below). Returns a shallow copy of the request with the `X-CachePilot-*` headers merged into its `headers` mapping when one exists; otherwise `None`. |
| `llm_execution` | `(request, original_request, next_call, **ctx)` | Pure pass-through: `next_call(request)` once, return its result. |

### The long-task classifier (PRD §39-44)

Deterministic, no LLM call, ever. Decision order (fail-safe: default is
foreground):

1. explicit `background=true` → long-running (respect the user, §44)
2. explicit `background=false` → foreground, unless
   `CACHEPILOT_LONG_TASKS_ENFORCE_FOREGROUND_HARD_POLICY=true` overrides (§44)
3. requested tool timeout ≥ `timeout_threshold_s` (20 s default) → long-running (§41)
4. learned duration p90 ≥ threshold with ≥ 2 samples → long-running (§43)
5. known-fast family (`pwd`, `ls`, `git status`, `cat`, …) → foreground (§42)
6. known-long family (`pytest`, `uv run pytest`, `docker build`, `cargo build`, `yarn build`, `go test`, …) → long-running (§42)
7. anything else → foreground

Learned durations come from `post_tool_call` (below) into
`CommandDurationHistory` (`~/.cachepilot/long_tasks.db`, overridable via
`CACHEPILOT_LONG_TASKS_DB_PATH`) — normalized command signatures only, never
command text.

## 3. Lifecycle hooks (PRD §16, §43, §46, §48)

All eight hooks in PRD §16 exist verbatim in Hermes v0.20.0's `VALID_HOOKS`
— no name mapping required. Every callback returns `None` so Hermes' hook
runner aggregates nothing and downstream behavior is byte-identical to
stock. Only safe metadata is ever logged (invariant 10).

| Hook | What CachePilot does |
|---|---|
| `post_tool_call` | Records terminal command durations into the duration learner (gated on `long_tasks.learn_command_durations`). |
| `post_api_request` | Observes API-call metadata (counts, duration, finish reason) — DEBUG log only. |
| `api_request_error` | Observes failure metadata (status, retry counts) — `error`/`reason`/`request` payloads are deliberately NOT logged (they can carry prompt content). |
| `subagent_start` | Registers a `subagent` background target (refcount +1) from the hook payload only — never inferred from conversation text (§48). |
| `subagent_stop` | Releases the target (refcount −1, clamped at zero). |
| `on_session_start` / `on_session_end` / `on_session_reset` | Session lifecycle observation — DEBUG log only. |

The `BackgroundTargetRegistry` (refcounted, thread-safe) feeds the
`X-CachePilot-Targets` header (below).

## 4. Correlation headers (PRD §29)

The `llm_request` middleware merges four headers into a **copy** of the
request's `headers` mapping when one exists (never mutating the caller's
dict, never clobbering existing values):

| Header | Value | Semantics |
|---|---|---|
| `X-CachePilot-Session` | per-process cached `uuid4` | Identifies the Hermes process/session to the relay |
| `X-CachePilot-Request` | fresh `uuid4` per `llm_request` call | Identifies one physical request |
| `X-CachePilot-Turn` | `uuid5(session:request)` — deterministic | Retries/duplicates of one request stay correlated to one turn across processes |
| `X-CachePilot-Targets` | active background-target count for the session | Phase 5 bridge: tells the relay to keep the session's cache lease armed while background work may still need the same prefix (PRD §46, §132) |

The relay **strips all four before forwarding** (`strip_correlation_headers`
in `cachepilot_relay/observation.py`), so they never reach the upstream and
never affect provider cache identity. Injection is gated by
`CACHEPILOT_CORRELATION_HEADERS` (default true) and is strictly fail-open:
no headers mapping on the request ⇒ pass through unchanged.

## 5. How the relay is targeted

Hermes' provider base URL points at the local relay; the relay forwards to
the real upstream. Provider identity stays intact (PRD §85):

```text
Hermes provider base URL  →  http://127.0.0.1:8787  →  real upstream
                              (cachepilotd, CACHEPILOT_UPSTREAM)
```

The relay does not trust any header for cache identity. From the observed
physical request it derives provider (upstream host), model/api_mode (body +
path), auth scope (hash of the Authorization header), route identity, and
both fingerprints — physical cache identity per invariant 7. The plugin's
headers only contribute **correlation** and the **target count**; the relay
keys leases by the physical cache fingerprint, not by session id.

`cachepilot status` verifies relay health with an HTTP probe of the relay's
local control endpoint (`GET /cachepilot/health` — answered by the relay
itself, never forwarded upstream) at `CACHEPILOT_RELAY_LISTEN` (default
`127.0.0.1:8787`): 'healthy' requires the relay's distinctive body, so a
foreign process squatting on the port reads 'occupied by another service'.
It also reports the plugin state from `CACHEPILOT_ENABLED` plus telemetry
evidence.

## 6. What is NOT modified (and how that is enforced)

- **No fork.** Hermes source is never touched; the repo pins no Hermes
  fork and no recurring patch maintenance exists.
- **No monkey patches.** No import of `AIAgent` internals, private
  conversation-loop functions, or mutable global request state (PRD §141).
- **No `next_call` reuse.** The `llm_execution` middleware invokes
  `next_call` exactly once — it is intentionally single-use (PRD §19). The
  relay reproduces requests later via its own snapshots; the plugin never
  stashes the callback.
- **No context rot.** CachePilot never injects heartbeat messages
  ("Check the background job." / "Still running.") into conversation
  history (PRD §95). Operational information lives in telemetry, the CLI,
  and logs only.
- **Enforced by CI**: `packages/hermes-plugin/tests/test_stock_hermes_unchanged.py`
  asserts stock Hermes source is byte-identical after plugin load — a
  failing assertion blocks the commit via the GitReins guard.

## 7. Plugin configuration

All `CACHEPILOT_*` environment variables, read at plugin load
(`cachepilot_hermes/config.py`); malformed values fall back to defaults so
a bad variable can never break Hermes (fail open):

| Variable | Default | Meaning |
|---|---|---|
| `CACHEPILOT_ENABLED` | `true` | Master switch (still registers everything; gates logs/observation) |
| `CACHEPILOT_LOG_LEVEL` / `CACHEPILOT_LOG_FORMAT` | `DEBUG` / `kv` | Structured debug emitter (values reduced to `Type(len=N)`; never payloads) |
| `CACHEPILOT_CORRELATION_HEADERS` | `true` | Gates correlation-header injection |
| `CACHEPILOT_LONG_TASKS_ENABLED` | `true` | Long-task runtime master switch |
| `CACHEPILOT_LONG_TASKS_AUTO_BACKGROUND` | `true` | Enables `tool_request` promotion |
| `CACHEPILOT_LONG_TASKS_TIMEOUT_THRESHOLD_S` | `20` | Timeout-based long-running hint |
| `CACHEPILOT_LONG_TASKS_LEARN_COMMAND_DURATIONS` | `true` | Duration learning into `~/.cachepilot/long_tasks.db` |
| `CACHEPILOT_LONG_TASKS_NOTIFY_ON_COMPLETE` | `true` | Adds `notify_on_complete=True` on promotion |
| `CACHEPILOT_LONG_TASKS_KNOWN_LONG_COMMANDS` / `_KNOWN_FOREGROUND_COMMANDS` | built-ins | Comma-separated family overrides |
| `CACHEPILOT_LONG_TASKS_ENFORCE_FOREGROUND_HARD_POLICY` | `false` | PRD §44 override of explicit `background=false` |

## 8. Degraded modes

- **Plugin disabled** (`CACHEPILOT_ENABLED=false`): registration still
  happens (deterministic, zero traffic impact); no logs, no observation,
  no promotion. Hermes behaves exactly as stock.
- **Relay unreachable / observation off**: the plugin keeps injecting
  correlation headers (cheap, local); the relay ignores them. Traffic
  never depends on the relay.
- **Correlation headers off**: the plugin is a pure long-task observer;
  the relay runs pass-through with no session correlation.

## 9. Compatibility contract (PRD §141-142)

CachePilot depends only on documented plugin APIs, middleware contracts,
configuration surfaces, and HTTP provider APIs. The PRD §142 `cachepilot
doctor` startup compatibility guard is a specified follow-up; the
CI-enforced zero-modification test is the current guard. If a future Hermes
version renames a hook or middleware kind, registration fails open — the
plugin logs and continues, never breaking Hermes.
