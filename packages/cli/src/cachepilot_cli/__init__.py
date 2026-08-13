"""cachepilot_cli — CachePilot observability CLI (PRD §76-79, §126).

Phase 4 ships ``status`` (relay/plugin/cache health from the telemetry
store) and ``costs`` (recorded-cost-only aggregates, PRD §79); Phase 5
replaces the ``leases`` placeholder with a real listing of the lease rows
the relay persists (PRD §78 — never fabricated).
"""

__version__ = "0.1.0"
