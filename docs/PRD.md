# CachePilot for Hermes

**Cost-Aware KV Cache Lease Optimization, Nonblocking Long-Task Runtime, Cache Intelligence, and Provider-Aware Request Replay**

**Status:** Build specification  
**Target:** Stock upstream NousResearch/hermes-agent — **no Hermes fork required**  
**Primary integration:** Hermes plugin + localhost relay  
**Secondary integration:** Plugin-only degraded mode  
**Package/runtime tooling:** `uv` for Python; `yarn` for any future web UI  
**Primary design principle:** Optimize the physical LLM request path without waking the LLM unnecessarily.

---

# 1. Executive Summary

CachePilot is an external optimization layer for Hermes Agent designed around a deceptively simple observation:

> The cheapest LLM call is the one Hermes never needs to make, and the second-cheapest is a request whose expensive prefix is already cached.

Modern agent workloads spend enormous amounts of time doing things other than generating tokens:

- running tests;
- compiling;
- waiting for another agent;
- waiting for a shell process;
- indexing a repository;
- downloading;
- building Docker images;
- waiting for CI;
- running benchmarks;
- interacting with remote systems;
- executing long tools.

A naïve agent repeatedly wakes the model to ask:

```text
Is it finished?
No.

Is it finished?
No.

Is it finished?
No.
```

This wastes tokens, pollutes the conversation, causes context growth, increases context rot, and can destroy otherwise valuable provider-side prompt/KV caches.

The first half of CachePilot solves that problem:

```text
LLM decides what work to launch
        ↓
work becomes managed background work
        ↓
local runtime monitors it
        ↓
LLM sleeps
        ↓
completion notification occurs
        ↓
LLM resumes exactly once
```

The second half handles an interesting consequence.

While Hermes is waiting, the provider-side KV/prompt cache associated with the conversation may expire.

A large session that was cheap to continue at cached-input pricing may suddenly require the provider to recompute a huge prefix.

CachePilot therefore introduces a **cache lease**.

While a background task is alive, CachePilot determines:

- what physical provider cache the session currently depends on;
- when that cache is likely to expire;
- how confident we are about that TTL;
- whether normal agent traffic has already refreshed it;
- whether a tiny warm request can preserve it;
- whether that warm request actually hit the cache;
- what the warm cost;
- what a future cold miss would cost;
- whether another warm is still economically rational.

The key distinction from the first prototype discussed in this session is:

> **CachePilot never assumes that keeping a cache alive is inherently good. It keeps it alive only when the expected future savings exceed the cost of keeping it alive.**

The full architecture is:

```text
┌─────────────────────────────────────────┐
│             Stock Hermes Agent          │
│                                         │
│ conversation / tools / delegation       │
└─────────────────┬───────────────────────┘
                  │
                  │ Hermes plugin APIs
                  ▼
┌─────────────────────────────────────────┐
│          CachePilot Hermes Plugin       │
│                                         │
│ long-task classifier                    │
│ background-work coordinator             │
│ LLM request middleware                  │
│ LLM execution middleware                │
│ cache lease manager                     │
│ cost policy                             │
│ TTL learner                             │
│ churn diagnostics                       │
└─────────────────┬───────────────────────┘
                  │ control
                  ▼
┌─────────────────────────────────────────┐
│              cachepilotd                │
│        localhost LLM relay/proxy        │
│                                         │
│ exact wire observation                  │
│ cache-equivalent request snapshots      │
│ replay/warming                          │
│ route identity                          │
│ provider adapters                       │
│ usage/cost telemetry                    │
└─────────────────┬───────────────────────┘
                  │
                  ▼
        OpenAI / Anthropic /
        OpenRouter / DeepSeek /
        compatible providers
```

Hermes stays upstream.

No recurring patch maintenance.

No monkey-patching its agent loop.

No new synthetic messages added to the conversation just to keep a cache warm.

---

# 2. Design History and Lessons That Must Be Preserved

This project evolved from several observations in this conversation. These are not incidental ideas; they should be considered requirements.

## 2.1 Cached input can be dramatically cheaper

Provider-side prompt/KV caching allows inference infrastructure to reuse intermediate attention state for a previously computed prefix instead of recomputing the entire prefix.

Conceptually:

```text
uncached request:

token 1
token 2
token 3
...
token 150,000

→ recompute KV for everything


cached request:

token 1 ───────────── token 145,000
              ↓
          KV already exists

only new suffix requires normal prefill computation
```

That explains why cached-input pricing can be substantially below normal input pricing.

The important economic insight is not simply:

> cached tokens are cheap.

It is:

> preserving a large cached prefix can sometimes cost far less than rebuilding it later.

But that statement is conditional.

---

# 3. The Critical Economic Formula

Define:

- `N` = relevant cached prefix tokens;
- `Pu` = uncached input price per token;
- `Pc` = cached input price per token;
- `W_i` = cost of warm request `i`;
- `R` = probability that the session will actually resume before being abandoned.

Then:

```text
cold_resume_cost ≈ N × Pu
```

while maintaining the cache costs approximately:

```text
maintained_cost =
Σ W_i
+
N × Pc
```

The expected benefit of one more warm is:

```text
expected_avoidable_loss =
R × (cold_resume_cost - cached_resume_cost)
```

Therefore:

```text
WARM
iff

expected_avoidable_loss
>
expected_next_warm_cost + safety_margin
```

This becomes one of CachePilot's central invariants.

A fixed watchdog such as:

```text
background job alive
→ warm forever
```

is forbidden.

A three-hour compile should not automatically trigger 36 cache refreshes simply because the job still exists.

---

# 4. What We Learned from the Ryder Design

The branch/screenshots reviewed earlier introduced several strong ideas that CachePilot should retain.

## Preserve

### A. Long tasks must not monopolize the foreground

A long-running subagent or process should not block new user input merely because the caller is waiting for it.

### B. Use notification-on-complete semantics

Instead of asking the model to poll:

```text
check
check
check
check
```

the task should produce an event when meaningful state changes.

### C. Warm using the real cache-producing request

Do not create an unrelated:

```text
"ping"
```

request.

The cache-relevant prefix must match the request that originally created or hit the cache.

### D. Bound output aggressively

A cache warm should never accidentally generate a full answer.

When supported:

```text
max_tokens = 1
```

or provider-equivalent.

### E. Real requests take priority over warm requests

A natural agent request already refreshes/reuses the cache.

Never issue a simultaneous redundant warm.

### F. Cache identity must include provider/model/request context

Do not assume every request in one Hermes conversation maps to one physical cache.

---

# 5. Problems Found in the Initial Design

CachePilot must explicitly fix the weaknesses found during our review.

## 5.1 Successful HTTP response ≠ verified cache hit

This:

```text
HTTP 200
```

does not prove:

```text
the provider reused the desired KV cache
```

A provider could have:

1. missed the cache;
2. recomputed the entire prefix;
3. returned one token;
4. still produced a completely valid response.

Therefore CachePilot distinguishes:

```text
CONFIRMED_HIT
MISS_REBUILT
SUCCESS_UNVERIFIED
FAILED
```

Never label a warm `successful` when what is really known is merely:

```text
request physically completed
```

---

## 5.2 Warm requests must appear in cost accounting

A warm call costs money.

Therefore:

```text
session cost
=
ordinary request cost
+
warm request cost
```

and:

```text
net savings
=
estimated avoided cold cost
-
warm costs
-
cache write penalties
```

Warm usage can never be hidden from session economics.

---

## 5.3 Provider TTL and refresh interval are different values

Bad:

```text
TTL = 300 seconds
warm at = second 300
```

Good:

```text
TTL = 300 seconds
safe warm point ≈ 230–250 seconds
```

The scheduler must account for:

- network latency;
- provider latency;
- queue delay;
- clock uncertainty;
- TTL estimation uncertainty.

---

## 5.4 Router identity is not necessarily physical-cache identity

A request to:

```text
OpenRouter
→ model X
```

might hit:

```text
upstream provider A
```

while the warm request hits:

```text
upstream provider B
```

Even if the top-level request is otherwise identical.

This makes route affinity and route confidence essential.

---

# 6. Current Hermes Capabilities We Should Reuse

This project should build *with* Hermes rather than duplicate Hermes.

Current Hermes has a real plugin system supporting user plugins, project plugins, bundled plugins, and packaged plugins. Plugins can register lifecycle hooks, tools, and middleware. 

More importantly, current Hermes exposes behavior-changing middleware for:

```text
llm_request
llm_execution
tool_request
tool_execution
```

The LLM request middleware may rewrite actual provider kwargs, while LLM execution middleware wraps the provider execution itself. 

Hermes also already has a process registry with:

- managed background execution;
- output buffering;
- status querying;
- waiting;
- killing;
- crash recovery;
- session scoping;
- completion notifications. 

Delegated agents already execute with isolated contexts and top-level child calls run asynchronously relative to the orchestration mechanics. 

Hermes's Anthropic prompt caching implementation already does sophisticated cache segmentation:

- static system prefix;
- system suffix;
- recent conversation boundaries;
- tool schemas;
- up to four cache-control breakpoints;
- native Anthropic and compatible layouts;
- 5-minute and 1-hour TTL modes. 

Hermes's OpenAI-compatible transport also supports content-addressed `prompt_cache_key` generation when the destination supports it, incorporating stable prompt instructions, tools, and normalized session scope. 

Therefore:

> **CachePilot does not replace Hermes prompt caching. It optimizes the lifecycle and economics of the caches Hermes already makes possible.**

---

# 7. Product Requirements Document

## 7.1 Product Name

Working name:

**CachePilot**

Expanded:

**CachePilot for Hermes Agent**

---

# 8. Product Mission

Make long-running Hermes sessions:

- cheaper;
- less context-heavy;
- less interruptive;
- more cache-efficient;
- more observable;
- more predictable;

without modifying Hermes core.

---

# 9. Primary User

A Hermes user who:

- runs coding or research agents;
- delegates to subagents;
- frequently starts long shell commands;
- uses large contexts;
- uses providers with prompt caching;
- cares about cost;
- switches among providers or models;
- may route through OpenRouter or other gateways;
- wants stock Hermes upgrades without maintaining a fork.

---

# 10. Primary Use Cases

## UC-1: Long test suite

Hermes runs:

```bash
uv run pytest
```

Expected duration: 12 minutes.

Desired behavior:

```text
Hermes launches background process
        ↓
returns control to user
        ↓
CachePilot observes cache lease
        ↓
one economically justified warm may occur
        ↓
pytest finishes
        ↓
Hermes receives one completion event
        ↓
model resumes with cached context
```

---

## UC-2: Docker build

```bash
docker build ...
```

Build lasts 20 minutes.

CachePilot must:

- avoid LLM polling;
- keep user interaction available;
- detect normal LLM traffic while build runs;
- treat that normal traffic as cache activity;
- avoid redundant warms.

---

## UC-3: Background subagent

Parent agent delegates repository analysis.

Subagent runs for ten minutes.

Parent must not repeatedly ask:

```text
is delegate_task finished?
```

CachePilot should observe subagent lifecycle events and maintain/cache only when necessary.

---

## UC-4: Very long process

Task lasts three hours.

If maintaining the prompt cache would cost more than one eventual cold miss:

```text
CachePilot intentionally allows cache expiration.
```

This is correct behavior.

---

## UC-5: Router instability

OpenRouter routes successive requests to different upstreams.

CachePilot notices:

```text
same logical request fingerprint
+
unexpected cache misses
+
changed physical route identity
```

and classifies the issue as likely route instability rather than incorrectly learning an extremely short TTL.

---

# 11. Goals

## P0 goals

CachePilot MUST:

1. run without a Hermes fork;
2. integrate using supported plugin/middleware surfaces;
3. eliminate unnecessary model-driven polling of background work;
4. identify active cache leases;
5. observe real model requests;
6. give ordinary model requests priority over warms;
7. perform bounded cache-equivalent warm requests;
8. measure warm cost;
9. distinguish verified hits from unverified successes;
10. stop warming when warming is economically irrational;
11. keep the relay private/local;
12. never persist API credentials;
13. never persist full prompts by default.

---

# 12. Non-Goals

CachePilot is not:

- an LLM router;
- a memory system;
- a replacement for Hermes context compression;
- a replacement for Hermes prompt caching;
- a generic semantic cache;
- an OpenRouter response cache;
- an MCP server that the model manually calls;
- an agent prompt telling Hermes to behave differently;
- a reason to fork Hermes.

---

# 13. Success Metrics

After enabling CachePilot on long-task workloads:

### Token efficiency

```text
LLM polling calls ↓ ≥ 95%
```

for eligible managed background tasks.

### Context growth

Synthetic check-in messages:

```text
≈ 0
```

### Cache behavior

For eligible background waits whose duration crosses one cache TTL:

```text
cache-hit-at-resume rate
```

should materially exceed baseline.

### Economics

For warming-enabled workloads:

```text
net_cache_savings_usd >= 0
```

over statistically meaningful test runs.

### Correctness

No warm request may:

- modify the durable conversation;
- generate visible assistant output;
- execute tools;
- race a real request;
- change active model state.

---

# 14. Architecture

The system contains six primary components.

```text
1. Hermes Plugin
2. Cache Lease Manager
3. cachepilotd Relay
4. Provider Adapter Layer
5. Persistent Telemetry Store
6. CachePilot CLI
```

Optional seventh component:

```text
7. Dashboard
```

---

# 15. Component 1 — Hermes Plugin

The Hermes plugin is responsible for understanding **agent semantics**.

The relay understands HTTP.

The plugin understands:

```text
this is session A

this shell command is long-running

subagent B is alive

process C just finished

this model call belongs to turn D

the user switched model

this session ended
```

That separation is important.

---

# 16. Plugin Hooks and Middleware

Register:

```python
ctx.register_middleware("tool_request", ...)
ctx.register_middleware("llm_request", ...)
ctx.register_middleware("llm_execution", ...)

ctx.register_hook("post_tool_call", ...)
ctx.register_hook("post_api_request", ...)
ctx.register_hook("api_request_error", ...)
ctx.register_hook("subagent_start", ...)
ctx.register_hook("subagent_stop", ...)
ctx.register_hook("on_session_start", ...)
ctx.register_hook("on_session_end", ...)
ctx.register_hook("on_session_reset", ...)
```

The upstream plugin architecture is specifically designed to expose these extension points without changing call sites. 

---

# 17. Why an MCP Is Not the Primary Integration

An MCP requires the model to decide:

```text
I should call cache_manager now.
```

That creates exactly the behavior CachePilot wants to eliminate.

Cache management is infrastructure.

It must be:

```text
automatic
deterministic
below the model
```

Therefore:

```text
MCP: no
skill: no
prompt instructions: no
plugin middleware: yes
```

---

# 18. Why Hooks Alone Are Insufficient

Lifecycle hooks are useful for observation.

But exact cache optimization needs to operate on:

```text
the request actually sent to the provider
```

not merely:

```text
the conceptual conversation
```

This is why `llm_request` and `llm_execution` middleware are essential.

---

# 19. Why a Relay Is Still Necessary

Hermes execution middleware wraps the real provider call, but the `next_call` callback is intentionally single-use. It cannot safely be saved and invoked again several minutes later to warm the cache. 

Therefore the plugin can observe the request but needs another component capable of reproducing it later.

That component is:

```text
cachepilotd
```

---

# 20. Component 2 — Cache Lease Manager

A cache lease represents:

> A physical prompt-cache opportunity that is valuable while one or more background operations may eventually need the same conversational prefix again.

Example:

```python
@dataclass
class CacheLease:
    lease_id: str
    session_id: str

    provider: str
    model: str
    api_mode: str
    base_url: str

    auth_scope_hash: str

    route_fingerprint: str | None

    request_fingerprint: str
    cache_fingerprint: str

    system_fingerprint: str
    tools_fingerprint: str
    history_prefix_fingerprint: str

    last_real_request_at: float
    last_cache_touch_at: float | None
    last_confirmed_hit_at: float | None

    estimated_ttl_s: float
    ttl_confidence: float

    active_targets: set[str]

    generation: int

    warm_count: int
    warm_cost_usd: float

    estimated_cold_resume_cost_usd: float | None
    estimated_cached_resume_cost_usd: float | None

    state: LeaseState
```

---

# 21. Lease Ownership

One session may have multiple leases.

Example:

```text
session
 ├── OpenRouter / DeepSeek / route-A
 ├── Anthropic / Claude / native
 └── OpenAI / GPT / Responses
```

A model switch must not refresh the old model's lease.

---

# 22. Cache Identity

Never use:

```text
session_id
```

alone as cache identity.

Physical identity should include at least:

```text
provider
model
API mode
normalized base URL
auth/profile scope
route identity if available
stable prompt prefix
tools/schema
cache-control layout
prompt-cache key
provider-specific routing values
```

Canonical representation:

```python
CacheIdentity(
    provider=...,
    model=...,
    api_mode=...,
    endpoint=...,
    auth_scope=...,
    route=...,
    prompt_key=...,
    system_hash=...,
    tools_hash=...,
)
```

---

# 23. Two Fingerprints, Not One

CachePilot MUST distinguish:

## Full request fingerprint

Hash of the entire canonical provider request.

```text
request_fingerprint
```

Used for:

- debugging;
- duplicate detection;
- response-cache analysis.

## Cache fingerprint

Hash only of fields relevant to provider prefix-cache identity.

```text
cache_fingerprint
```

This intentionally excludes fields such as:

```text
max_tokens
stream
client timeout
trace identifiers
```

when the relevant provider adapter knows those fields do not affect prompt-cache identity.

This lets the warm request differ only in safe output-control parameters while retaining the same cache identity.

---

# 24. Cache Topology

Do not treat a prompt as one undifferentiated blob.

Track:

```text
┌──────────────────────────────┐
│ static system prefix         │
├──────────────────────────────┤
│ dynamic system suffix        │
├──────────────────────────────┤
│ tool schemas                 │
├──────────────────────────────┤
│ historical conversation      │
├──────────────────────────────┤
│ recent conversation tail     │
└──────────────────────────────┘
```

Record hashes for each layer.

This enables diagnostics such as:

```text
cache miss cause:

system          unchanged
tools           unchanged
history prefix  unchanged
route           changed

LIKELY CAUSE:
router affinity loss
```

or:

```text
system changed every request

LIKELY CAUSE:
volatile value inserted into prompt prefix
```

---

# 25. Cache Churn Detector

A major feature of CachePilot should be identifying accidental cache destruction.

Examples:

```text
timestamps
random UUIDs
session timestamps
dynamic ordering
non-deterministic tool schemas
changing memory prefixes
provider failover
tool list mutation
route mutation
compression events
```

Output:

```text
Cache churn detected

Static system prefix:
  changed 11/12 requests

First divergent byte:
  near "...Current time: 3:14 PM..."

Estimated reusable prefix lost:
  ~22,400 tokens/request
```

P0 should **detect**, not automatically rewrite.

Automatic canonicalization belongs later.

---

# 26. Component 3 — cachepilotd Relay

`cachepilotd` is a small local provider relay.

Default:

```text
127.0.0.1:8787
```

Never:

```text
0.0.0.0
```

unless explicitly configured.

---

# 27. Data Plane

Normal request:

```text
Hermes
   ↓
cachepilotd
   ↓
provider
```

The relay:

1. receives request;
2. correlates it with Hermes session metadata;
3. calculates physical fingerprint;
4. forwards request;
5. observes response;
6. records usage/cache metadata;
7. returns the response unchanged.

---

# 28. Control Plane

Plugin communicates separately with relay.

Preferred:

```text
Unix domain socket
~/.hermes/cachepilot/control.sock
```

Fallback:

```text
127.0.0.1:<private-control-port>
```

authenticated using a random local control token.

---

# 29. Correlating Hermes Requests with Relay Requests

The plugin knows:

```text
session_id
turn_id
api_request_id
provider
model
```

The relay sees the actual HTTP request.

Use two correlation mechanisms.

## Primary

Internal headers:

```text
X-CachePilot-Session
X-CachePilot-Request
X-CachePilot-Turn
```

The relay MUST strip these before forwarding upstream.

They must never affect provider cache identity.

## Fallback

Plugin sends:

```text
register_expected_request(
    session_id,
    api_request_id,
    canonical_fingerprint
)
```

to the control plane immediately before `next_call`.

Relay matches the incoming request against the expected fingerprint.

---

# 30. Request Snapshot Policy

The relay maintains:

```text
last known cache-producing request
```

for an active lease.

The complete request body stays:

```text
IN MEMORY ONLY
```

by default.

Do not persist:

- prompts;
- conversation history;
- API keys;
- authorization headers;
- tool arguments containing secrets.

Persistent storage contains only:

```text
hashes
timestamps
usage
prices
route identities
cache outcomes
numeric metrics
```

If the relay restarts:

```text
active request snapshots disappear
→ leases become non-warmable
→ no unsafe reconstruction
```

That is acceptable because provider caches are ephemeral anyway.

---

# 31. Warm Requests

A warm request is best described as:

> **cache-equivalent replay**

not:

> exact identical request.

Start from the actual request snapshot.

Then modify only provider-approved output bounding fields.

Example:

```python
warm = deepcopy(snapshot)

if "max_tokens" in warm:
    warm["max_tokens"] = 1

elif "max_completion_tokens" in warm:
    warm["max_completion_tokens"] = 1

elif "max_output_tokens" in warm:
    warm["max_output_tokens"] = 1

else:
    adapter.use_verified_stream_cancel_strategy(...)
```

Never invent a field the provider did not support.

---

# 32. Warm Safety Rules

A warm MUST NOT:

- execute tools;
- mutate session history;
- produce visible content;
- trigger another agent turn;
- save assistant output;
- trigger memory extraction;
- trigger skill learning;
- trigger downstream hooks as though it were a normal conversation step.

The relay discards generated content.

Only cache/usage metadata matters.

---

# 33. Warm Request Tool Choice

If the original request includes tools, maintain them if they are part of provider cache identity.

But ensure:

```text
tool_choice = none
```

only if doing so does not alter relevant cache identity.

This is provider-specific.

If uncertain:

```text
do not mutate tool choice
```

and discard any attempted tool output without executing the tool.

The provider adapter must define this behavior.

---

# 34. Component 4 — Provider Adapters

Interface:

```python
class CacheProviderAdapter(Protocol):

    def canonical_cache_identity(
        self,
        request: PhysicalRequest,
        response: PhysicalResponse | None,
    ) -> CacheIdentity:
        ...

    def cache_fingerprint(
        self,
        request: PhysicalRequest,
    ) -> str:
        ...

    def build_warm_request(
        self,
        original: PhysicalRequest,
    ) -> PhysicalRequest:
        ...

    def parse_usage(
        self,
        response: PhysicalResponse,
    ) -> Usage:
        ...

    def classify_cache_result(
        self,
        usage: Usage,
        response: PhysicalResponse,
    ) -> CacheResult:
        ...

    def extract_route_identity(
        self,
        response: PhysicalResponse,
    ) -> str | None:
        ...

    def ttl_hint(
        self,
        request: PhysicalRequest,
    ) -> TTLHint | None:
        ...

    def can_pin_route(self) -> bool:
        ...

    def apply_route_affinity(
        self,
        request: PhysicalRequest,
        route: str,
    ) -> PhysicalRequest:
        ...
```

---

# 35. Provider Capabilities

Each adapter exposes capabilities:

```python
@dataclass
class CacheCapabilities:
    supports_cache_telemetry: bool
    supports_cache_write_telemetry: bool

    supports_prompt_cache_key: bool
    supports_explicit_cache_control: bool

    supports_output_bound: bool
    supports_stream_cancel: bool

    read_refreshes_ttl: Literal[
        "yes",
        "no",
        "unknown",
    ]

    route_identity_available: bool
    route_affinity_available: bool
```

No assumptions based solely on:

```text
"OpenAI-compatible"
```

Different compatible providers behave differently.

---

# 36. Initial Adapter Set

P0:

```text
OpenAI-compatible generic
OpenRouter
DeepSeek-compatible
OpenAI
Anthropic
```

Later:

```text
Fireworks
DeepInfra
Together
Kimi
Qwen
Gemini
Nous
```

Do not create twelve nearly identical adapters prematurely.

Shared wire behavior belongs in base classes.

---

# 37. Hermes Prompt Caching Must Remain Authoritative

For Anthropic, Hermes already builds cache-control plans with stable system-prefix and recent-message breakpoints. CachePilot should observe the resulting cache structure instead of replacing it. 

Likewise, when Hermes generates a provider-supported `prompt_cache_key`, preserve it byte-for-byte. 

---

# 38. Distinguish Cache Types

CachePilot MUST explicitly distinguish:

### Prefix/KV cache

Reusable prompt computation.

```text
TARGET OF CACHE LEASING
```

### Explicit Anthropic prompt cache

Provider-managed prefix cache using `cache_control`.

```text
TARGET OF CACHE LEASING
```

### OpenAI prompt cache routing

Prefix caching influenced by provider cache key/routing.

```text
TARGET OF CACHE LEASING
```

### OpenRouter response cache

Cache of an **identical entire request/response**.

```text
ORTHOGONAL
```

Changing output-token limits for a warm may cause a different response-cache identity while still preserving the desired prefix cache.

Do not mix these metrics.

Current Hermes configuration explicitly treats OpenRouter response caching separately from prompt caching. 

---

# 39. Component 5 — Long-Task Manager

The largest guaranteed cost saving comes from avoiding LLM polling entirely.

This should be implemented before cache warming.

---

# 40. Terminal Auto-Backgrounding

`tool_request` middleware observes terminal calls.

Example:

```python
def tool_request(tool_name, args, **ctx):
    if tool_name != "terminal":
        return None

    decision = classifier.classify(args)

    if decision == LONG_RUNNING:
        updated = dict(args)
        updated["background"] = True
        updated["notify_on_complete"] = True

        return {
            "args": updated,
            "source": "cachepilot",
            "reason": "long-running command",
        }

    return None
```

Hermes's middleware contract explicitly supports replacing the effective tool arguments before normal tool execution. 

---

# 41. Long-Task Classifier

The classifier should be deterministic.

No LLM call.

Inputs:

```text
tool name
command
requested timeout
known command family
historical execution duration
explicit user flags
environment
```

---

# 42. Static Long-Running Hints

Common long-running commands:

```text
pytest
uv run pytest
yarn test
yarn build
yarn lint --heavy...
docker build
docker compose build
cargo build
cargo test
make
cmake --build
ninja
git clone
large package installs
benchmark suites
repository indexing
agent harnesses
```

Likely-fast commands:

```text
pwd
ls
git status
git diff --stat
cat
head
tail
rg
sed
which
echo
```

Static classifications are hints, not immutable truth.

---

# 43. Duration Learner

Track:

```text
normalized command signature
median runtime
p90 runtime
sample count
```

Example:

```text
uv run pytest tests/unit
median = 24 sec

uv run pytest
median = 611 sec
```

The second should automatically background.

---

# 44. Explicit User Intent Wins

If Hermes/model explicitly says:

```text
background=true
```

respect it.

If explicitly requesting foreground execution:

```text
background=false
```

do not override unless configured to enforce a hard timeout policy.

---

# 45. Do Not Poll with the LLM

Absolute invariant:

```text
No periodic agent turn exists solely to ask
whether background work is still running.
```

Local process monitoring is fine.

LLM monitoring is not.

---

# 46. Background Target Abstraction

Normalize:

```python
BackgroundTarget(
    id=...,
    kind="process" | "subagent" | "external",
    session_id=...,
    started_at=...,
    expected_completion=True,
)
```

A lease remains relevant while:

```text
active_targets > 0
```

---

# 47. Completion

When all relevant background targets finish:

```text
Cache lease no longer needs watchdog warming
```

The resulting normal Hermes notification/resumption request itself becomes the final cache consumer.

---

# 48. Subagent Handling

Use:

```text
subagent_start
subagent_stop
```

to update target counts.

Never infer subagent existence from conversation text.

The actual Hermes delegation runtime already isolates child context from the parent's full transcript and tracks active child runs. 

---

# 49. Cache Lease State Machine

States:

```text
INACTIVE
ARMED
WARM_SCHEDULED
WARMING
CONFIRMED_HIT
SUCCESS_UNVERIFIED
MISS_REBUILT
ECONOMIC_STOP
EXPIRED
INVALIDATED
FAILED
```

---

# 50. State Transitions

```text
background target starts
        ↓
ARMED

normal LLM request succeeds
        ↓
ARMED
deadline reset

deadline approaches
        ↓
WARM_SCHEDULED

no real request racing
+
economics positive
        ↓
WARMING

cache telemetry shows read
        ↓
CONFIRMED_HIT
        ↓
ARMED

provider returns successfully,
no cache telemetry
        ↓
SUCCESS_UNVERIFIED
        ↓
ARMED with lower confidence

cache read = 0
        ↓
MISS_REBUILT
        ↓
update TTL/route model

economics negative
        ↓
ECONOMIC_STOP

model/provider/cache identity changes
        ↓
INVALIDATED

all targets complete
        ↓
INACTIVE
```

---

# 51. Real-Request-Wins Rule

Every natural Hermes request increments:

```text
lease.generation
```

Scheduler captures generation:

```python
scheduled_generation = lease.generation
```

Immediately before warming:

```python
if lease.generation != scheduled_generation:
    return SKIPPED_STALE
```

Also maintain:

```text
real_request_in_flight
```

Warm may execute only if:

```text
real_request_in_flight == false
warm_request_in_flight == false
```

---

# 52. Locking

Per cache identity:

```python
asyncio.Lock
```

or equivalent single-owner coordinator.

Never use one global lock for every model request.

Independent cache leases must proceed independently.

---

# 53. Warm Deadline

Do not schedule at TTL expiry.

Define:

```python
network_margin = max(
    minimum_margin_s,
    latency_p95_s * latency_multiplier,
)

safe_deadline = min(
    last_touch + ttl * warm_fraction,
    last_touch + ttl - network_margin,
)
```

Defaults:

```yaml
warm_fraction: 0.80
minimum_margin_s: 10
latency_multiplier: 2.0
jitter_fraction: 0.03
```

Example:

```text
TTL                    300s
80%                    240s
p95 latency              4s
2 × p95                  8s
minimum margin           10s

safe warm ≈ 240s
```

---

# 54. Jitter

Apply deterministic per-lease jitter:

```text
±3%
```

based on cache fingerprint.

This prevents many active sessions from warming simultaneously.

---

# 55. TTL Learning

Hard-coded TTLs should only bootstrap the system.

CachePilot should learn actual observed behavior.

Suppose:

```text
cache hit at idle age 183s
```

Then:

```text
TTL > 183s
```

Later:

```text
miss at idle age 302s
```

Then:

```text
TTL ∈ (183s, 302s]
```

Maintain:

```python
TTLProfile(
    lower_bound_s,
    upper_bound_s,
    estimated_ttl_s,
    confidence,
    sample_count,
)
```

---

# 56. TTL Learning Must Separate Route Changes

A miss cannot automatically mean:

```text
TTL expired
```

Potential causes:

```text
route changed
prompt changed
tool schema changed
model changed
auth scope changed
provider cache eviction
TTL expired
```

Only add a clean TTL miss observation when cache identity remained stable.

---

# 57. TTL Estimate

One possible estimator:

```python
if upper_bound is not None:
    estimate = lower_bound + (
        upper_bound - lower_bound
    ) * 0.35
else:
    estimate = max(adapter_hint, lower_bound)
```

Favor the lower side of the interval for safe warming.

---

# 58. TTL Confidence

Confidence increases with:

- repeated consistent observations;
- unchanged route;
- verified cache-read telemetry;
- stable fingerprints.

Confidence decreases with:

- router changes;
- unverified responses;
- provider failovers;
- inconsistent hits.

---

# 59. TTL Override Hierarchy

Configuration:

```yaml
ttl:
  force_seconds: null
```

Priority:

```text
1. force_seconds if explicitly set
2. high-confidence learned TTL
3. provider adapter observed/default hint
4. warming unavailable
```

Do not silently guess unknown TTL values.

---

# 60. Economic Controller

This is the most important improvement over a simple KV watchdog.

For each lease calculate:

```python
avoidable_loss = (
    cold_resume_cost
    - cached_resume_cost
)
```

Then apply continuation probability:

```python
expected_value = resume_probability * avoidable_loss
```

Warm budget:

```python
max_warm_budget = (
    expected_value
    * budget_ratio
)
```

Default:

```yaml
budget_ratio: 0.70
```

Keep 30% safety headroom.

---

# 61. Warm Decision

```python
remaining_budget = (
    max_warm_budget
    - cumulative_warm_cost
)

should_warm = (
    next_warm_expected_cost
    < remaining_budget
    and expected_net_savings
    >= minimum_expected_savings
)
```

---

# 62. Example

```text
Cold future prefix cost          $0.80
Cached future prefix cost        $0.08
Avoidable cost                   $0.72

Budget ratio                     70%
Warm budget                      $0.504

Warm #1                          $0.11
Warm #2                          $0.11
Warm #3                          $0.11
Warm #4                          $0.11

Spent after #4                   $0.44

Predicted warm #5                $0.11

$0.44 + $0.11 > $0.504

Decision:
LET CACHE EXPIRE
```

Correct.

---

# 63. Resume Probability

P0:

```text
background target with notify_on_complete
→ R = high
```

Example:

```text
0.95
```

Task explicitly detached/no expected continuation:

```text
R = low
```

Example:

```text
0.20
```

Do not attempt complicated ML initially.

---

# 64. Natural Requests Are Free Heartbeats

If the user sends another message while a task is running:

```text
normal model request
```

already touches/reuses the cache.

Therefore:

```text
cancel scheduled warm
reset lease age
```

This may eliminate most heartbeat requests during active conversations.

---

# 65. Cost Resolver

Cost resolution priority:

```text
1. provider-returned monetary usage
2. provider usage × live pricing metadata
3. configured price override
4. unknown
```

If cost is unknown:

```text
do not claim net savings
```

Warming can either:

```text
a. operate conservatively under explicit user policy
b. disable economic warming
```

Default:

```text
disable economically unbounded repeated warming
```

---

# 66. No Permanent Hard-Coded Prices

Provider pricing changes.

Never ship:

```python
DEEPSEEK_CACHE_PRICE = ...
```

as long-term authority.

Pricing adapters may include fallback snapshots with:

```text
as_of timestamp
```

but live/provider-supplied usage should win.

---

# 67. Cache Verification

Classification enum:

```python
class CacheOutcome(Enum):
    CONFIRMED_HIT = ...
    MISS_REBUILT = ...
    SUCCESS_UNVERIFIED = ...
    SKIPPED_BUSY = ...
    SKIPPED_STALE = ...
    SKIPPED_ECONOMIC = ...
    SKIPPED_NO_TARGETS = ...
    SKIPPED_UNSUPPORTED = ...
    FAILED = ...
```

---

# 68. CONFIRMED_HIT

Requires provider-specific evidence such as:

```text
cache_read_tokens > 0
```

or documented equivalent.

---

# 69. MISS_REBUILT

Evidence:

```text
provider returned
+
relevant input > threshold
+
cache_read_tokens == 0
```

Use carefully on providers whose telemetry semantics differ.

---

# 70. SUCCESS_UNVERIFIED

Provider accepted request but provided no trustworthy cache telemetry.

This outcome may reset the heartbeat only according to adapter policy.

Some providers may refresh cache despite hiding telemetry.

Others may not.

---

# 71. Route Affinity

Cache route identity:

```python
RouteIdentity(
    gateway="openrouter",
    upstream_provider=...,
    endpoint=...,
    region=...,
    deployment=...,
)
```

Only fields actually observable should be populated.

---

# 72. Router Policy

When:

```text
route changed
```

CachePilot should:

1. record event;
2. invalidate route-specific confidence;
3. avoid treating the miss as TTL evidence;
4. optionally request affinity to the previous route if supported.

---

# 73. Route Pinning Must Be Economic

Never pin to an expensive provider indefinitely merely to preserve cache.

Evaluate:

```text
extra route cost
vs
cache recompute savings
```

This creates:

```text
route affinity economics
```

not blind stickiness.

---

# 74. OpenRouter Integration

Current Hermes already exposes provider-routing controls such as:

```text
sort
only
ignore
order
require_parameters
```

at the configuration layer. 

CachePilot should not overwrite global user routing.

Instead adapter-level affinity should be:

```text
lease scoped
temporary
reversible
```

and used only when a compatible per-request mechanism is available.

---

# 75. Cache Churn Diagnostics

Store transitions:

```python
FingerprintDelta(
    timestamp,
    request_id,
    system_changed,
    tools_changed,
    history_changed,
    route_changed,
    model_changed,
    cache_key_changed,
)
```

CLI:

```bash
cachepilot explain-miss <request>
```

Possible output:

```text
Request req_1842 cache miss

Stable:
  provider
  model
  tools
  system prefix
  prompt-cache key

Changed:
  upstream provider

Likely cause:
  router backend changed

Confidence:
  0.92
```

---

# 76. Observability

CachePilot needs excellent observability because otherwise cost optimization becomes folklore.

Provide:

```bash
cachepilot status
cachepilot leases
cachepilot costs
cachepilot ttl
cachepilot routes
cachepilot churn
cachepilot doctor
cachepilot benchmark
```

---

# 77. `cachepilot status`

Example:

```text
CachePilot 0.1.0

Mode: relay
Relay: healthy
Hermes plugin: active

Active leases: 2
Background targets: 3

Current session:
  provider        openrouter
  model           deepseek/...
  prefix          128,441 tokens
  estimated TTL   287s ± 18s
  confidence      0.84
  next warm       2m 51s
  warm budget     $0.43
  spent           $0.07
  net savings     +$0.31
```

---

# 78. `cachepilot leases`

```text
LEASE       TARGETS  CACHE AGE  TTL    STATE
a8f11       2        81s        287s   ARMED
7ac42       1        243s       300s   WARMING
```

---

# 79. `cachepilot costs`

```text
Session economics

Normal uncached input        $1.842
Normal cached input          $0.171
Cache writes                 $0.093
Warm calls                   $0.067

Estimated cold misses saved  $0.482

Net CachePilot savings       $0.415
```

Never show:

```text
money saved
```

when cost data are incomplete.

Instead:

```text
net savings: unknown
```

---

# 80. Telemetry Events

Structured event schema:

```json
{
  "event": "cachepilot.warm.completed",
  "timestamp": "...",
  "session_hash": "...",
  "lease_id": "...",
  "provider": "...",
  "model": "...",
  "cache_outcome": "confirmed_hit",
  "cache_read_tokens": 123411,
  "cache_write_tokens": 0,
  "warm_cost_usd": 0.0182,
  "lease_age_s": 231.4,
  "estimated_ttl_s": 287,
  "route_hash": "..."
}
```

No raw prompt content.

---

# 81. Persistent Storage

Use SQLite.

Path:

```text
~/.hermes/cachepilot/cachepilot.db
```

Journal:

```text
WAL
```

with safe fallback if the filesystem cannot support it.

Hermes itself uses WAL as its normal SQLite default and falls back when the backing filesystem is unsuitable. 

---

# 82. Database Schema

## `provider_profiles`

```sql
provider
model
api_mode
endpoint_hash
route_hash

ttl_lower_s
ttl_upper_s
ttl_estimate_s
ttl_confidence

latency_p50_ms
latency_p95_ms

sample_count

updated_at
```

---

## `request_events`

```sql
id
session_hash
timestamp

provider
model
route_hash

request_fingerprint
cache_fingerprint

system_hash
tools_hash
history_hash

input_tokens
output_tokens
cache_read_tokens
cache_write_tokens

cost_usd

request_kind
```

`request_kind`:

```text
normal
warm
```

---

## `warm_events`

```sql
id
lease_id
timestamp

scheduled_age_s
actual_age_s

outcome

input_tokens
output_tokens
cache_read_tokens
cache_write_tokens

cost_usd

decision_reason
```

---

## `churn_events`

```sql
id
timestamp
session_hash

previous_cache_fingerprint
new_cache_fingerprint

system_changed
tools_changed
history_changed
route_changed
cache_key_changed
model_changed
```

---

## `command_history`

```sql
signature
sample_count

runtime_p50
runtime_p90
runtime_p95

background_success_rate

updated_at
```

---

# 83. What Is Never Persisted

Default persistence MUST exclude:

```text
Authorization headers
API keys
raw prompts
raw messages
tool output
raw tool schemas
user content
raw provider responses
```

Hashes are sufficient.

---

# 84. Configuration

Proposed:

```yaml
cachepilot:
  enabled: true

  mode: relay

  long_tasks:
    enabled: true

    auto_background: true

    timeout_threshold_s: 20

    learn_command_durations: true

    notify_on_complete: true

    known_long_commands:
      - pytest
      - docker build
      - yarn build
      - cargo build

    known_foreground_commands:
      - pwd
      - ls
      - git status

  cache:
    warming: true

    scheduling:
      warm_fraction: 0.80
      minimum_margin_s: 10
      latency_multiplier: 2.0
      jitter_fraction: 0.03

    economics:
      enabled: true
      budget_ratio: 0.70
      minimum_expected_savings_usd: 0.01

    ttl_learning:
      enabled: true
      minimum_samples: 3

    verification:
      prefer_provider_telemetry: true

    route_affinity:
      enabled: true
      economic_gate: true

  relay:
    listen: "127.0.0.1:8787"

    control_socket:
      "~/.hermes/cachepilot/control.sock"

    persist_request_bodies: false

  telemetry:
    sqlite: true
    json_log: true

    retain_days: 30

    store_prompt_content: false
```

---

# 85. Hermes Configuration Strategy

Hermes already supports configurable provider/base URL behavior and custom OpenAI-compatible endpoints. 

Installation should generate the appropriate relay route for each configured provider.

Conceptually:

```text
Hermes provider identity remains intact

provider base URL
      ↓
localhost CachePilot
      ↓
actual upstream provider
```

Preserving provider identity is important so Hermes keeps applying the correct:

- prompt-cache behavior;
- transport;
- token normalization;
- request quirks.

---

# 86. Relay Routing Configuration

Example internal CachePilot config:

```yaml
upstreams:
  openrouter:
    upstream: "https://openrouter.ai/api/v1"

  anthropic:
    upstream: "https://api.anthropic.com"

  openai:
    upstream: "https://api.openai.com/v1"
```

The real addresses should be generated/discovered from the original Hermes configuration, not duplicated manually where possible.

---

# 87. Plugin-Only Mode

Support:

```yaml
mode: plugin_only
```

Features still available:

```text
long-task backgrounding
poll suppression
subagent lifecycle tracking
cache fingerprinting
cache churn diagnostics
normal request telemetry
TTL inference
cost analysis
```

Potential best-effort warm:

```text
ctx.llm.complete(...)
```

Hermes exposes host-owned LLM access to plugins and returns cache-read/cache-write usage and cost metadata. 

But plugin-only warming must be classified:

```text
BEST_EFFORT
```

because it cannot guarantee exact physical request equivalence.

---

# 88. Relay Mode

Relay mode is:

```text
RECOMMENDED
```

because it sees:

```text
the actual provider request
```

and can reproduce its cache-relevant prefix.

---

# 89. Security Architecture

Relay listens:

```text
127.0.0.1 only
```

Control plane:

```text
Unix socket
```

Permissions:

```text
0600
```

Runtime state directory:

```text
0700
```

Never expose relay externally.

---

# 90. Authorization Handling

The relay must forward upstream authorization but must:

- never log it;
- never persist it;
- redact headers before error logging;
- zero/remove references when snapshots expire.

---

# 91. Snapshot Lifetime

A snapshot should be destroyed when:

```text
lease inactive
session ends
model changes
auth profile changes
provider changes
relay restarts
economic stop and cache no longer needed
```

---

# 92. Failure Isolation

CachePilot is an optimization.

Hermes correctness cannot depend upon it.

If relay optimization fails:

```text
normal request forwarding must continue
```

If plugin fails:

```text
Hermes continues normally
```

CachePilot should fail open for ordinary traffic and fail closed for warming.

Meaning:

```text
normal provider request:
  forward

uncertain warm:
  skip
```

---

# 93. Relay Circuit Breaker

If upstream proxying produces repeated errors:

```text
3 consecutive relay-attributable failures
```

disable optimization for that route.

Normal forwarding remains enabled.

---

# 94. Warm Circuit Breaker

Example:

```text
2 consecutive warm misses
```

should temporarily stop warming that lease until a normal request produces new cache evidence.

This avoids repeatedly paying to discover that the cache cannot be preserved.

---

# 95. Context Rot Prevention

CachePilot itself must not inject heartbeat messages into Hermes history.

Forbidden:

```text
user: Check the background job.
assistant: Still running.
```

No cache management event belongs in conversational context.

Operational information should live in:

```text
telemetry
status UI
logs
```

---

# 96. Request Concurrency

Maintain:

```text
normal_request_active_count
warm_request_active
lease_generation
```

Warm allowed only when:

```text
normal_request_active_count == 0
AND
warm_request_active == false
AND
lease generation unchanged
AND
targets still active
```

---

# 97. Warm Cancellation

If a real request begins while a warm is:

### not sent yet

Cancel it.

### queued

Cancel it.

### network request already sent

If safely cancellable:

```text
cancel/close
```

otherwise let it finish but do not start another.

---

# 98. User Input During Background Work

User input always wins.

CachePilot must never delay a user request to preserve its own warm schedule.

Priority:

```text
1. user/real model request
2. task completion notification
3. cache warm
4. diagnostics
```

---

# 99. Cache Eviction vs TTL Expiration

Provider caches can be evicted before nominal TTL.

Therefore learned data should model:

```text
survival probability
```

eventually, not merely one deterministic TTL.

P0 can use bounds.

P2 could estimate:

```text
P(cache survives | age)
```

---

# 100. Future Probabilistic Scheduler

Later:

```text
expected_warm_value(age)
=
P(cache still alive at age)
×
P(session will resume)
×
avoidable_future_cost
-
warm_cost
```

Warm at the age maximizing expected value.

This is superior to a fixed 80% timer but unnecessary for first implementation.

---

# 101. Testing Strategy

The test suite is critical.

Never validate this project based on:

```text
"provider returned 200"
```

---

# 102. Unit Tests — Fingerprinting

Test:

```text
identical requests
→ identical cache fingerprint
```

Change:

```text
max_tokens
```

Expected:

```text
request fingerprint changes
cache fingerprint unchanged
```

Change:

```text
system prefix
```

Expected:

```text
cache fingerprint changes
```

Change:

```text
tool schema
```

Expected:

```text
cache fingerprint changes
```

---

# 103. Unit Tests — Economics

Cases:

### One cheap warm

```text
warm cost < avoidable miss
→ WARM
```

### Too many warms

```text
cumulative warm cost > budget
→ ECONOMIC_STOP
```

### Unknown pricing

```text
repeated warming disabled by default
```

### Zero probability of continuation

```text
never warm
```

---

# 104. Unit Tests — Scheduler

Check:

```text
TTL = 300
fraction = .8
→ due ≈ 240
```

and:

```text
large provider latency
→ earlier deadline
```

---

# 105. Unit Tests — Race Conditions

Simulate:

```text
warm scheduled
real request begins
warm aborted
```

Simulate:

```text
warm due
background target completes
warm aborted
```

Simulate:

```text
model changes
old warm aborted
```

Simulate:

```text
session reset
all old leases invalidated
```

---

# 106. Unit Tests — TTL Learning

```text
hit at 180
miss at 300

→ lower >= 180
→ upper <= 300
```

Route change during miss:

```text
must NOT tighten TTL upper bound
```

---

# 107. Unit Tests — Cache Verification

Provider telemetry:

```text
cache_read_tokens = 120000
→ CONFIRMED_HIT
```

```text
cache_read_tokens = 0
→ MISS_REBUILT
```

No telemetry:

```text
→ SUCCESS_UNVERIFIED
```

---

# 108. Unit Tests — Long Task Classifier

```text
pwd
→ foreground
```

```text
pytest with historic p90 8m
→ background
```

Explicit user background false:

```text
→ foreground unless hard policy enabled
```

---

# 109. Integration Tests — Fake Provider

Build a deterministic fake LLM provider.

It simulates:

```text
KV cache
TTL
cache_read_tokens
cache_write_tokens
variable latency
route identity
pricing
```

Example cache map:

```python
cache[fingerprint] = expires_at
```

If request arrives before expiration:

```text
cache_read_tokens = prefix_tokens
```

else:

```text
cache_write_tokens = prefix_tokens
```

This makes the entire system testable without real API costs.

---

# 110. Integration Scenario — No Warm Needed

```text
background task duration = 120s
TTL = 300s
```

Expected:

```text
0 warm calls
resume cache hit
```

---

# 111. Integration Scenario — One Warm

```text
task = 480s
TTL = 300s
```

Expected:

```text
1 warm
resume hit
```

---

# 112. Integration Scenario — Natural Request

```text
task starts
200s later user sends message
```

Expected:

```text
natural request resets deadline
scheduled warm cancelled
```

---

# 113. Integration Scenario — Economic Stop

```text
task = 3h
warm price high
```

Expected:

```text
some early warms
then ECONOMIC_STOP
cache allowed to expire
```

---

# 114. Integration Scenario — Route Changes

```text
request 1 → backend A
warm → backend B
```

Expected:

```text
cache miss classified route-related
TTL estimate unchanged
route confidence reduced
```

---

# 115. Hermes Integration Tests

Run against stock upstream Hermes.

Tests must prove:

```text
Hermes source tree unchanged
```

and:

```text
plugin installed externally
```

Test:

- plugin loads;
- middleware receives provider request;
- terminal arguments can be rewritten;
- normal model response unchanged;
- session reset cleans leases;
- relay failure does not corrupt session.

---

# 116. Benchmark Suite

Compare five modes:

```text
A. stock Hermes
B. CachePilot long-task manager only
C. plugin + relay, warming disabled
D. fixed-TTL warming
E. adaptive economic warming
```

---

# 117. Benchmark Workloads

### Workload 1

Short shell calls.

Expected:

```text
no behavioral difference
```

### Workload 2

8-minute tests.

### Workload 3

20-minute build.

### Workload 4

one-hour job.

### Workload 5

background job + active user chat.

### Workload 6

multiple subagents.

### Workload 7

router backend changes.

### Workload 8

context compression occurs mid-job.

### Workload 9

model switch.

### Workload 10

provider failover.

---

# 118. Benchmark Metrics

Capture:

```text
provider calls
LLM polling calls

input tokens
output tokens
cache-read tokens
cache-write tokens

warm input tokens
warm output tokens

normal request cost
warm cost
cold cost avoided
net savings

wall-clock runtime
time-to-user-response

context token growth

resume cache-hit rate

route change count

warm attempts
confirmed hits
unverified successes
misses
economic skips
```

---

# 119. Benchmark Success Condition

CachePilot is successful only if:

```text
cost_with_cachepilot
<
baseline_cost
```

on the workload categories it chooses to optimize.

If a workload loses money:

```text
policy should eventually learn not to warm it.
```

---

# 120. Project Repository

Proposed:

```text
cachepilot/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── AGENTS.md
│
├── packages/
│   ├── core/
│   │   └── cachepilot_core/
│   │
│   ├── hermes-plugin/
│   │   └── cachepilot_hermes/
│   │
│   ├── relay/
│   │   └── cachepilot_relay/
│   │
│   └── cli/
│       └── cachepilot_cli/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── hermes/
│   ├── providers/
│   ├── race/
│   └── benchmarks/
│
├── fixtures/
│   ├── providers/
│   └── requests/
│
├── scripts/
│
├── docs/
│   ├── architecture.md
│   ├── provider-adapters.md
│   ├── cache-economics.md
│   ├── threat-model.md
│   └── hermes-integration.md
│
└── dashboard/            # later
    ├── package.json
    └── yarn.lock
```

---

# 121. Python Project Setup

Use a `uv` workspace.

Conceptually:

```toml
[tool.uv.workspace]
members = [
    "packages/core",
    "packages/hermes-plugin",
    "packages/relay",
    "packages/cli",
]
```

Development:

```bash
uv sync
uv run pytest
```

No Poetry.

No raw `pip` workflow for development.

---

# 122. Dashboard

Not P0.

If added:

```text
React/TypeScript
```

with:

```bash
yarn
```

not npm/pnpm.

Potential views:

```text
live leases
cache topology
cost graph
TTL learning
route changes
miss explanation
provider health
```

---

# 123. Internal Core Modules

```text
cachepilot_core/
├── identity.py
├── fingerprint.py
├── lease.py
├── scheduler.py
├── economics.py
├── ttl.py
├── routing.py
├── usage.py
├── pricing.py
├── telemetry.py
├── storage.py
├── command_history.py
└── types.py
```

---

# 124. Relay Modules

```text
cachepilot_relay/
├── app.py
├── proxy.py
├── snapshots.py
├── control.py
├── upstreams.py
├── streaming.py
└── adapters/
    ├── base.py
    ├── openai_compat.py
    ├── openai.py
    ├── anthropic.py
    ├── openrouter.py
    └── deepseek.py
```

---

# 125. Hermes Plugin Modules

```text
cachepilot_hermes/
├── plugin.py
├── config.py
├── lifecycle.py
├── tool_middleware.py
├── llm_middleware.py
├── targets.py
└── relay_client.py
```

---

# 126. CLI Modules

```text
cachepilot_cli/
├── main.py
├── status.py
├── doctor.py
├── leases.py
├── costs.py
├── ttl.py
├── routes.py
├── churn.py
└── benchmark.py
```

---

# 127. Phase 0 — Research Harness

Before implementing warming:

Build fake provider.

Build:

```text
canonical request representation
usage normalization
cache fingerprint
cache simulator
economic calculator
```

Acceptance:

```text
all economics + fingerprint logic testable offline
```

---

# 128. Phase 1 — Hermes Plugin Skeleton

Implement external Hermes plugin.

Tasks:

1. plugin manifest;
2. register middleware;
3. register lifecycle hooks;
4. print structured debug logs;
5. verify against stock Hermes;
6. add CI test that checks no Hermes source modifications.

Acceptance:

```text
Hermes normal behavior identical with plugin installed.
```

---

# 129. Phase 2 — Long-Task Runtime

Implement:

```text
terminal long-task classifier
automatic background promotion
completion notifications
subagent target tracking
target refcount
command runtime history
```

Do not implement cache warming yet.

Benchmark against stock Hermes.

Goal:

```text
prove polling reduction independently.
```

---

# 130. Phase 3 — Relay Pass-Through

Implement `cachepilotd`.

First version:

```text
100% pass-through
0 cache modification
```

Acceptance:

```text
same provider response
same streaming behavior
same tool calls
same errors
same headers where relevant
```

Create golden differential tests.

---

# 131. Phase 4 — Physical Request Observation

Add:

```text
correlation IDs
request fingerprint
cache fingerprint
usage parsing
cache telemetry
route extraction
```

No warming.

CLI should now show:

```text
current cache health
cache hit percentage
churn events
route changes
```

---

# 132. Phase 5 — Lease Manager

Connect background targets to observed cache identities.

Implement:

```text
lease arm
lease invalidate
lease completion
generation counter
normal-request-reset
scheduler
```

Still no actual warm network request.

Use dry-run output:

```text
WOULD WARM IN 47s
```

Run this for real workloads to validate scheduling.

---

# 133. Phase 6 — Cache Warming

Enable actual bounded warm replay.

Initially support only:

```text
one known-good OpenAI-compatible provider adapter
```

Then:

```text
OpenRouter
DeepSeek-compatible
Anthropic
OpenAI
```

Every new adapter requires integration tests.

---

# 134. Phase 7 — Economic Controller

Add:

```text
cost estimation
warm budget
expected savings
economic stop
```

This phase turns:

```text
KV watchdog
```

into:

```text
KV optimizer
```

---

# 135. Phase 8 — TTL Learning

Replace fixed TTL hints with:

```text
route-aware learned bounds
confidence
observational refinement
```

Add:

```text
cachepilot ttl
```

---

# 136. Phase 9 — Route Intelligence

Add:

```text
route identity
router miss analysis
optional route affinity
route economics
```

Do not attempt this until cache fingerprinting and TTL inference are trustworthy.

---

# 137. Phase 10 — Churn Intelligence

Implement:

```text
system diff classification
tool schema churn
cache key churn
route churn
history-boundary churn
```

Provide:

```bash
cachepilot explain-miss
```

---

# 138. Phase 11 — Advanced Optimizations

Only after measurement.

Candidates:

### Stable tool ordering

Only if proven semantically safe.

### Volatile prompt isolation

Identify timestamps/dynamic content that unnecessarily destroy a reusable prefix.

### Cross-request prefix topology

Determine which prefix layers provide the most economic value.

### Survival model

Move from:

```text
estimated TTL
```

to:

```text
P(cache survives at age t)
```

---

# 139. Phase 12 — Optional UI

Dashboard only when CLI and telemetry are complete.

The core product cannot depend on the dashboard.

---

# 140. CI/CD

Required:

```text
ruff
mypy or pyright
pytest
coverage
dependency audit
race/integration tests
```

CI matrix:

```text
Hermes current main
Hermes latest release
```

A scheduled compatibility job should catch plugin API changes.

---

# 141. Hermes Compatibility Contract

CachePilot may depend only on:

```text
documented/supported plugin APIs
middleware contracts
configuration surfaces
HTTP provider APIs
```

It MUST NOT import:

```text
private AIAgent internals
private conversation loop functions
private mutable global request state
```

unless specifically isolated behind an optional compatibility adapter.

---

# 142. Compatibility Guard

On startup:

```bash
cachepilot doctor
```

must verify:

```text
required middleware exists
required hooks exist
relay reachable
Hermes provider routing valid
plugin enabled
control socket permissions correct
```

If incompatible:

```text
disable optimization
do not break Hermes
```

---

# 143. Logging

Structured logs:

```text
~/.hermes/cachepilot/logs/cachepilot.jsonl
```

Levels:

```text
ERROR
WARN
INFO
DEBUG
TRACE
```

Default INFO should log:

```text
lease creation
warm decisions
warm outcomes
economic stop
route changes
errors
```

Not every provider token.

---

# 144. Debug Mode

```bash
CACHEPILOT_DEBUG=1
```

Adds:

```text
fingerprint transitions
scheduling math
economic calculations
TTL updates
route decisions
```

Still no credentials or raw prompts.

---

# 145. Explainability Requirement

Every warm decision must be explainable.

Store:

```python
WarmDecision(
    action="warm",
    age_s=237,
    estimated_ttl_s=291,
    ttl_confidence=.87,
    predicted_warm_cost=.012,
    remaining_budget=.089,
    expected_avoidable_loss=.144,
    reason="due_and_economically_positive",
)
```

Likewise skips:

```text
SKIPPED_ECONOMIC
SKIPPED_BUSY
SKIPPED_NO_TARGET
SKIPPED_ROUTE_UNCERTAIN
```

---

# 146. Core Algorithm

```python
async def evaluate_lease(lease):

    if not lease.active_targets:
        return stop("no_targets")

    if lease.real_request_active:
        return skip("busy")

    if lease.warm_request_active:
        return skip("already_warming")

    if lease.cache_identity_invalid:
        return stop("identity_invalid")

    ttl = ttl_estimator.resolve(lease)

    if ttl is None:
        return skip("unknown_ttl")

    due_at = scheduler.next_deadline(
        last_touch=lease.last_cache_touch,
        ttl=ttl,
        provider_latency=lease.latency_profile,
    )

    if now() < due_at:
        return schedule(due_at)

    economics = economic_controller.evaluate(lease)

    if not economics.should_warm:
        lease.state = ECONOMIC_STOP
        return skip("economic")

    generation = lease.generation

    if lease.real_request_active:
        return skip("race")

    if lease.generation != generation:
        return skip("stale")

    return await warm(lease)
```

---

# 147. Warm Algorithm

```python
async def warm(lease):

    async with lease.lock:

        if lease.real_request_active:
            return SKIPPED_BUSY

        if not lease.active_targets:
            return SKIPPED_NO_TARGETS

        snapshot = snapshot_store.get(
            lease.cache_fingerprint
        )

        if snapshot is None:
            return SKIPPED_UNSUPPORTED

        request = adapter.build_warm_request(
            snapshot
        )

        started = monotonic()

        response = await upstream.send(request)

        usage = adapter.parse_usage(response)

        outcome = adapter.classify_cache_result(
            usage,
            response,
        )

        cost = pricing.calculate(
            request,
            usage,
        )

        lease.warm_count += 1
        lease.warm_cost_usd += cost

        ttl_learner.observe(
            lease,
            age=started - lease.last_cache_touch,
            outcome=outcome,
        )

        economics.observe_warm(
            lease,
            cost,
        )

        return outcome
```

---

# 148. Normal Request Algorithm

```python
def before_normal_request(lease):

    lease.real_request_active = True

    lease.generation += 1

    scheduler.cancel_pending_warm(lease)
```

After success:

```python
def after_normal_request(lease, response):

    lease.real_request_active = False

    outcome = adapter.classify_cache_result(...)

    ttl_learner.observe(...)

    lease.last_cache_touch = now()

    scheduler.rearm(lease)
```

After failure:

```python
lease.real_request_active = False
```

Do not automatically treat a failed provider call as cache refresh.

---

# 149. Background Target Algorithm

```python
def target_started(target):

    lease.active_targets.add(target.id)

    if len(lease.active_targets) == 1:
        lease.arm()
```

Completion:

```python
def target_finished(target):

    lease.active_targets.discard(target.id)

    if not lease.active_targets:
        scheduler.cancel(lease)
        lease.disarm()
```

---

# 150. Anti-Polling Guard

CachePilot should additionally detect pathological repeated checks.

Example:

```text
terminal poll proc_123
terminal poll proc_123
terminal poll proc_123
```

If the LLM repeatedly requests equivalent status checks within a very short interval:

```text
intercept
return locally cached current status
+
guidance that completion notification is armed
```

without creating another provider call solely for waiting.

Use cautiously; do not block legitimate diagnostic inspections.

---

# 151. Avoiding Context Growth

Repeated local status responses can still enlarge context.

Therefore where Hermes already supports completion notification:

```text
prefer no check-in at all
```

The agent should return to user/idle rather than continue looping.

---

# 152. Provider Failover

If provider changes mid-session:

```text
invalidate physical lease
```

Do not warm old provider unless a still-running child specifically depends on it.

Create a new lease for the new provider/cache identity.

---

# 153. Model Switch

Same rule:

```text
model A cache
≠
model B cache
```

even on same provider.

---

# 154. Compression

Context compression changes the request prefix.

When Hermes compresses:

```text
old cache fingerprint
→ invalidate

new request
→ create new cache identity
```

Do not attempt to preserve an obsolete pre-compression cache.

---

# 155. Tool Schema Changes

If tool set changes:

```text
tools_fingerprint changes
```

Treat as possible cache boundary change.

Adapter determines whether the provider's cache includes tool definitions.

---

# 156. Authentication Scope

Different provider accounts may use completely distinct physical cache namespaces.

Therefore include a one-way:

```text
auth_scope_hash
```

derived from stable account/profile identity.

Never hash actual API key directly if avoidable.

Prefer Hermes profile/account identity.

---

# 157. Multiple Active Background Tasks

If:

```text
pytest
docker build
subagent
```

all use the same parent cache lease:

```text
active_targets = 3
```

One warm maintains the lease for all of them.

Do not warm once per background process.

---

# 158. Multiple Leases

If separate models/providers are involved:

```text
lease A
lease B
```

schedule independently.

---

# 159. Cross-Session Cache Sharing

P0:

```text
DISABLED
```

Even when identical stable prefixes exist.

Reasons:

- routing ambiguity;
- auth scope;
- privacy boundaries;
- prompt-cache-key session scoping;
- difficult economics.

Later research may identify safe shared static-prefix opportunities.

---

# 160. Provider Cache Write Economics

Some caching systems charge extra for cache writes.

Therefore:

```text
cache_write_cost
```

must be separately tracked.

A miss during a warm might be substantially more expensive than expected because it causes:

```text
cache rebuild/write
```

That should immediately affect economics.

---

# 161. Miss Rebuild Protection

If a warm unexpectedly produces:

```text
MISS_REBUILT
```

do not blindly continue the warm schedule.

Re-evaluate:

```text
did TTL expire?
did route change?
did prefix change?
is read-refresh unsupported?
```

Until explained:

```text
warming confidence reduced
```

---

# 162. Cache Warm Eligibility

Warm only when all are true:

```text
background target active

cache identity known

warm strategy supported

snapshot available

TTL known enough

no real request active

economic value positive

route confidence acceptable

circuit breaker closed
```

---

# 163. Provider Adapter Certification

Adapters start as:

```text
experimental
```

Promote to:

```text
verified
```

only after live tests prove:

1. warm request reuses cache;
2. warm request extends useful cache lifetime if claimed;
3. output bounding works;
4. usage telemetry parsing works;
5. no accidental tool execution occurs.

---

# 164. Feature Flags

Every significant subsystem:

```yaml
long_tasks.enabled
cache.warming
cache.economics.enabled
cache.ttl_learning.enabled
cache.route_affinity.enabled
cache.churn_detection.enabled
```

Independent toggles.

This makes debugging practical.

---

# 165. Safe Rollout Sequence

Recommended first production deployment:

```text
Week/Phase 1:
telemetry only

Phase 2:
long-task backgrounding

Phase 3:
warm dry-run

Phase 4:
warming on one provider

Phase 5:
economics

Phase 6:
adaptive TTL

Phase 7:
additional providers
```

Never enable every optimization simultaneously before getting baseline measurements.

---

# 166. Definition of Done — V1

V1 is complete when:

- [ ] stock Hermes runs with zero source modifications
- [ ] CachePilot loads as external plugin
- [ ] relay runs independently
- [ ] long terminal jobs are backgrounded appropriately
- [ ] background task completion generates one meaningful resumption path
- [ ] model polling is eliminated for managed wait periods
- [ ] actual physical provider requests are fingerprinted
- [ ] real requests cancel redundant warms
- [ ] cache-equivalent replay works for at least one provider
- [ ] warm output is bounded and discarded
- [ ] cache hits are verified where telemetry exists
- [ ] warm costs are separately recorded
- [ ] net savings are calculated
- [ ] economic stop prevents unbounded warming
- [ ] TTL observations are learned
- [ ] route changes do not corrupt TTL learning
- [ ] raw prompts are not persisted
- [ ] relay binds to localhost
- [ ] fake-provider integration suite passes
- [ ] race tests pass
- [ ] Hermes compatibility tests pass
- [ ] benchmark shows positive savings for at least the intended workload class

---

# 167. Absolute Anti-Patterns

Do not implement:

```text
LLM heartbeat prompts
```

Do not implement:

```text
"check again in 30 seconds" agent turns
```

Do not implement:

```text
warm forever while process alive
```

Do not implement:

```text
HTTP 200 = cache confirmed
```

Do not implement:

```text
hidden warm cost
```

Do not implement:

```text
provider-wide TTL when route/model behavior differs
```

Do not implement:

```text
session_id = cache identity
```

Do not implement:

```text
arbitrary OpenAI-compatible fields on every provider
```

Do not implement:

```text
raw prompt persistence by default
```

Do not implement:

```text
Hermes core monkey patches
```

Do not implement:

```text
fork required
```

---

# 168. Guiding Hierarchy

When optimizing, prioritize in this order:

```text
1. Eliminate unnecessary model calls

2. Preserve prefix stability

3. Reuse natural model calls as cache activity

4. Preserve cache with a warm only when needed

5. Warm only when expected economics are positive

6. Improve route affinity when economically useful

7. Learn provider behavior from evidence

8. Automate more only after measurement
```

This hierarchy matters.

A perfect cache warmer attached to an agent that wastes ten unnecessary LLM turns is still a bad system.

---

# 169. Final Product Vision

The finished system should make Hermes feel like it understands that an LLM call is an expensive stateful operation rather than an ordinary polling function.

Instead of:

```text
agent
  ↓
tool
  ↓
agent
  ↓
check
  ↓
agent
  ↓
check
  ↓
agent
  ↓
cache expired
  ↓
huge cold request
```

the execution model becomes:

```text
agent
  │
  ├──── launch work ──────────────────────┐
  │                                       │
  ▼                                       ▼
idle / user remains interactive        background work
  │                                       │
  │          CachePilot lease             │
  │        ┌──────────────────┐            │
  │        │                  │            │
  │   real request?        cache near TTL? │
  │        │                  │            │
  │       yes                yes           │
  │        │                  │            │
  │ refresh naturally   economics positive?
  │                           │
  │                          yes
  │                           │
  │                    tiny cache warm
  │                           │
  └───────────────────────────┴────────────┘
                              │
                       work completes
                              │
                              ▼
                       one real resume
                              │
                              ▼
                       cached context
```

And eventually CachePilot should be able to answer, empirically:

```text
Why did this cache miss?

How long does this provider's cache really survive?

Which part of my prompt keeps changing?

Did OpenRouter move me to another backend?

How much did preserving this cache cost?

How much would letting it expire have cost?

Did warming actually save money?

Should I warm it again?

Why did CachePilot decide not to?
```

That is the target.

Not merely:

> a KV-cache heartbeat.

The product should become a **cache intelligence and execution-efficiency layer for autonomous agents**.

Hermes decides what work should happen.

The provider performs inference.

CachePilot sits between those layers and makes sure neither one does expensive work unnecessarily.