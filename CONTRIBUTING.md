# Contributing to CachePilot

CachePilot is an external optimization layer for **stock** Hermes Agent — no
fork, no monkey patches, no recurring patch maintenance. Every contribution
must preserve the invariants in [AGENTS.md](AGENTS.md) and stay within the
documented plugin surfaces.

## Developer setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/) (workspace tooling).

```bash
git clone <repo-url> && cd cachepilot
uv sync --group dev          # installs all workspace packages + dev deps
uv run pytest                # sanity check
```

The repo is a uv workspace with four packages plus the root `cachepilot`
meta-package. `uv run` from the root resolves every package; there is no
per-package venv hopping.

## Workspace layout

```
packages/core/            cachepilot_core — pure, offline-testable engine
                          (identity, fingerprint, usage, pricing, economics,
                          leases, ttl, churn, route_intel, route_affinity,
                          adapters, snapshots, telemetry, storage,
                          fake_provider research harness)
packages/hermes-plugin/   cachepilot_hermes — external Hermes plugin
                          (manifest, middleware, lifecycle hooks, long-task
                          classifier, duration history, target registry)
packages/relay/           cachepilot_relay — cachepilotd localhost relay
                          (server, proxy, observation, lease controller,
                          warm executor)
packages/cli/             cachepilot_cli — cachepilot status/leases/costs/
                          ttl/routes/churn/explain-miss
docs/                     PRD.md (authoritative 169-section spec) + runbooks
                          (architecture, provider-adapters, cache-economics,
                          threat-model, hermes-integration)
```

Layering: `core` has no dependencies on the other packages; `relay` and `cli`
depend on `core`; `hermes-plugin` depends only on `core`-adjacent helpers and
never imports `AIAgent` internals (PRD §141). Match the existing import
order (stdlib → third-party → local), pydantic v2 models for schemas, and
`asyncio`/httpx for anything I/O-bound.

## Quality gate (must pass before commit)

```bash
uv run ruff check .                     # lint (line-length 100, py312)
uvx mypy --python-executable .venv/bin/python <changed files>   # types
uv run --group dev pytest -x --tb=short # full suite, stop on first failure
```

Notes:

- `uv run --group dev` matters: the dev group carries the test plugins —
  bare `uv run pytest` can resync to default groups and drop them.
- The full suite spans `packages/*/tests` plus root `tests/`; pytest 9
  requires unique test module basenames across the whole tree.
- `benchmarks/` (if present) is not part of the test suite.
- mypy on the full tree is currently blocked by a pre-existing PEP-695
  syntax error in `packages/core/src/cachepilot_core/adapters.py:57` — run
  mypy per-file with `--follow-imports=skip` when the whole-tree run fails
  for that reason.

## CI (GitHub Actions)

Two workflows run on every push to `main` (and on PRs) —
`.github/workflows/`:

**ci.yml** — per-push quality gate (PRD §140):

| Job | Command |
|---|---|
| lint | `uv run --group dev ruff check .` |
| typecheck | `uvx mypy --python-executable .venv/bin/python --native-parser --python-version 3.12 --follow-imports=skip src packages` |
| test | `uv run --group dev pytest -x --tb=short` — the suite includes the race tests (lease/warm-executor interleavings) and the relay differential integration tests, so PRD §140's race/integration requirement runs here |
| coverage | `uv run --group dev pytest --cov=cachepilot_core --cov=cachepilot_hermes --cov=cachepilot_relay --cov=cachepilot_cli --cov-fail-under=90 --cov-report=term-missing` |
| audit | `uv audit` (uv 0.12 prints an experimental warning on stderr; exit 0 still means the lockfile is clean) |

**compat.yml** — Hermes compatibility matrix (PRD §140). Two cells install a
real Hermes agent into a dedicated venv, set `HERMES_AGENT_DIR` to that
install root (`site-packages`), and run `pytest packages/hermes-plugin/tests
-q` so `test_stock_hermes_unchanged.py` actually runs instead of skipping:

- `latest-release` — `pip install hermes-agent` from PyPI (currently 0.19.0)
- `current-main` — editable install of a fresh clone of
  `https://github.com/NousResearch/hermes-agent.git` (hermes-agent's build
  backend refuses wheel/sdist builds by design; `pip install -e` is the
  sanctioned dev path)

Each cell also asserts the plugin entry-point group is still
`hermes_agent.plugins` (drift guard). The weekly scheduled run (Mon 03:00 UTC,
`0 3 * * 1`) repeats the `current-main` cell to catch Hermes API drift
between releases; the workflow is also `workflow_dispatch`-able. `current-main`
runs on PRs / the cron / manual dispatch but not on push: an upstream
hermes-main breakage must never block the push fast-gate (ci.yml +
`latest-release` stay green), while the scheduled job still surfaces drift
weekly.

Hermes goes into a dedicated venv, not the project `.venv`: the stock-unchanged
test snapshots the install tree, and its ignore list skips any path containing
`.venv` — pointing `HERMES_AGENT_DIR` at the project venv's site-packages
would produce an empty snapshot and fail the test outright. As of 2026-08-13
the `current-main` cell's entry-point check is red upstream: hermes main's
`hermes_cli/plugins.py` imports `registration_lifecycle`, a top-level module
missing from its own `pyproject.toml` `py-modules` list — exactly the drift
this job exists to surface.

Notes:

- The typecheck job requires `--native-parser --python-version 3.12`:
  mypy's default parser rejects the pre-existing PEP-695 `type X =` aliases
  in `packages/core/src/cachepilot_core/adapters.py:57` (mypy 2.3.0).
- `mypy` and `pytest-cov` are pinned in the `[dependency-groups] dev` group
  so every CI command is reproducible locally with `uv sync --group dev`.

## Task lifecycle (GitReins)

Every change is a GitReins task; every commit is judged by the Tier-2 LLM
evaluator. Do not skip the judge — a commit that is not judged is not done.

```bash
# 1. Create a task with explicit, verifiable criteria
gitreins task create <task-id> "description" "file X exists with feature Y" "tests pass"

# 2. Mark in progress and do the work
gitreins task start <task-id>
# ... implement, with tests ...

# 3. Run the judge — agentic LLM evaluates each criterion against the code
gitreins task complete <task-id>     # auto-runs the evaluator

# 4. Stage and commit — the pre-commit hook runs Tier-1 guards (secrets/lint/tests)
#    and BLOCKS on failures. Re-stage first: the judge resets the git index.
git add <files>
git commit -m "type: description. Addresses <task-id>."

# 5. Browse verdicts
gitreins report
```

Guard details (`.gitreins/config.yaml`):

- **secrets** (BLOCKS): gitleaks + built-in regex scanner (sk-, ghp_, glpat-,
  AKIA, AIza, JWT, password patterns). Test secrets stay in tests and never
  look like real keys.
- **lint** (WARNS): ruff. `.md`-only changes are exempt in practice.
- **tests** (BLOCKS): diff-mode maps changed files to test files by basename;
  unmapped changes (config, docs) fall back to the full suite.
  `test_command: uv run pytest -x --tb=short`, timeout 900s.

Known pitfalls (learned the hard way):

- Run `gitreins guard` after `git add` — with nothing staged it passes
  vacuously (no files scanned).
- If the pre-commit hook file is missing, guards never run on commit — run
  `gitreins guard` manually.
- The judge caps input tokens/iterations; complex tasks may abort INCOMPLETE
  with a cap message — raise `GITREINS_MAX_*` env or the `evaluator:` config
  (doc-heavy runs: `MAX_INPUT_TOKENS=5M MAX_OUTPUT_TOKENS=1M
  MAX_ITERATIONS=120`) and re-run.
- The judge's tier-1 lint step runs bare `ruff` — export
  `PATH="$PWD/.venv/bin:$PATH"` inline with the judge command if ruff is not
  on PATH.
- Never commit `.gitreins/tasks.yaml` (local state) or `.venv/` /
  `__pycache__/`.

## Phase / PR workflow

Phases are sequential (PRD §127-139), one PR per phase, each judged. Before
starting a phase:

1. Read `docs/PRD.md` for the phase's sections and the current docs runbooks.
2. Identify the owning module (core / hermes-plugin / relay / cli).
3. Write tests first where the behavior is spec'd (RED-GREEN-REFACTOR),
   including fake-provider integration tests — never "200 = success".
4. Run the full quality gate, `gitreins task complete`, then commit with
   `type: description. Addresses <task-id>.` (types: feat, fix, docs, test,
   refactor, chore).
5. Do not push — the foreman handles push and the post-merge judge pass.

Definition of Done per PR (AGENTS.md): stock Hermes unchanged (CI test
asserts zero source modifications), code + tests + docs updated, ruff/mypy/
pytest pass, fake-provider integration coverage, race tests for
warm-vs-real / warm-vs-complete / model-switch invalidation, economics +
fingerprint logic unit-tested offline, no architectural invariant weakened.

## AGENTS.md invariants (summary)

1. **No Hermes fork / monkey patches** — only documented middleware + hooks.
2. **No LLM polling** — local process monitoring only; "check again in N
   seconds" turns are forbidden.
3. **HTTP 200 ≠ cache hit** — outcomes are CONFIRMED_HIT / MISS_REBUILT /
   SUCCESS_UNVERIFIED / FAILED.
4. **Warm costs are visible** — session cost = ordinary + warm; never claim
   "money saved" with incomplete data.
5. **Warming is economic, not a watchdog** — WARM iff
   `expected_avoidable_loss > expected_next_warm_cost + safety_margin`.
6. **Real requests win** — natural traffic refreshes the cache and cancels
   scheduled warms (generation counters, per-identity locks).
7. **Cache identity is physical** — provider, model, api_mode, endpoint,
   auth scope, route, prompt/system/tools hashes; `session_id` alone is
   never identity.
8. **Two fingerprints** — `request_fingerprint` (full) vs
   `cache_fingerprint` (prefix-cache-relevant; excludes output-bounding
   fields).
9. **Fail open for traffic, fail closed for warming** — plus warm circuit
   breaker (2 misses) and relay-attributable failure isolation.
10. **Never persist secrets or prompts by default** — hashes, timestamps,
    usage, prices, route identities, outcomes only.

The full text — including the guard hierarchy and the absolute anti-patterns
list — is in [AGENTS.md](AGENTS.md). If you think an invariant needs to
change, raise it with the foreman; do not weaken one silently.
