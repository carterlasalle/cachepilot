"""cachepilot_relay — CachePilot relay package (PRD §26-33; Phase 3 + Phase 4).

``cachepilotd`` is a small local provider relay: it forwards every request
verbatim to a configured upstream and returns the response unchanged (PRD
§130). Phase 4 adds read-only observation (PRD §131): correlation headers
are stripped (PRD §29), fingerprints and route identity are computed from
the physical request (PRD §22-23, §71), and usage/outcome telemetry is
written to the SQLite store — always fail-open, never touching traffic.
"""

from cachepilot_relay.config import DEFAULT_LISTEN, RelayConfig
from cachepilot_relay.server import RelayServer, create_app

__all__ = ["DEFAULT_LISTEN", "RelayConfig", "RelayServer", "create_app"]
__version__ = "0.1.0"
