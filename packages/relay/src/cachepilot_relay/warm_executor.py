"""HTTP warm executor — PRD §147 transport for bounded warm replay (Phase 6).

The relay's concrete :class:`~cachepilot_core.adapters.WarmExecutor`: sends
one bounded cache-equivalent warm request to the upstream over httpx and
returns the parsed usage + honestly-classified outcome + cost.

Warm safety (PRD §32, §97):

- the warm is sent DIRECTLY to the upstream with a plain httpx call — it
  never re-enters the relay's observation or forwarding path (no recursive
  lease tracking, no re-observation, no telemetry recording, no tool
  execution);
- generated content is discarded: only usage/outcome/cost are returned;
- an uncertain warm (the adapter declined to bound the request) is skipped
  — nothing is sent, nothing is paid for (fail closed for warming,
  AGENTS.md invariant 9);
- a bounded per-request timeout keeps a hung upstream from stalling the
  scheduler; transport errors classify as FAILED (fail open for traffic —
  normal forwarding never goes through this path).
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
from cachepilot_core.adapters import (
    CacheProviderAdapter,
    WarmResult,
)
from cachepilot_core.pricing import PricingTable, estimate_cost
from cachepilot_core.snapshots import RequestSnapshot
from cachepilot_core.telemetry import Outcome
from cachepilot_core.usage import TokenUsage

logger = logging.getLogger("cachepilot_relay.warm_executor")


class HttpWarmExecutor:
    """Transport + adapter combo for one bounded warm replay (PRD §147)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        adapter: CacheProviderAdapter,
        *,
        pricing: PricingTable | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._client = client
        self._adapter = adapter
        self._pricing = pricing
        self._timeout_s = timeout_s

    async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
        """Build the bounded warm from the snapshot and send it upstream."""
        body = self._adapter.build_warm_request(snapshot.body)
        if body is None:
            # Uncertain warm → skip (invariant 9). Nothing was sent.
            return WarmResult(outcome=None, usage=TokenUsage(), cost_usd=Decimal(0))
        headers = {"content-type": "application/json"}
        if snapshot.authorization:
            headers["authorization"] = snapshot.authorization
        try:
            response = await self._client.post(
                snapshot.upstream_url,
                json=body,
                headers=headers,
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "warm request failed (cache_fp=%s): %s",
                snapshot.cache_fingerprint[:12],
                exc,
            )
            return WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal(0))
        usage = self._adapter.parse_usage(response)
        outcome = self._adapter.classify_cache_result(usage, response)
        return WarmResult(outcome=outcome, usage=usage, cost_usd=self._resolve_cost(usage))

    def _resolve_cost(self, usage: TokenUsage) -> Decimal:
        """PRD §65 priority for the warm's cost: provider-returned, else the
        configured pricing snapshot, else 0 (recorded as visible zero — the
        cost is never claimed, invariant 4)."""
        if usage.cost is not None:
            return usage.cost
        if self._pricing is not None:
            return estimate_cost(usage, self._pricing)
        return Decimal(0)
