"""cachepilot_relay — CachePilot relay package (PRD §26-33; Phase 3 pass-through).

``cachepilotd`` is a small local provider relay: it forwards every request
verbatim to a configured upstream and returns the response unchanged. First
version is 100% pass-through with 0 cache modification (PRD §130) — no
correlation headers, no fingerprints, no usage parsing, no warming.
"""

from cachepilot_relay.config import DEFAULT_LISTEN, RelayConfig
from cachepilot_relay.server import RelayServer, create_app

__all__ = ["DEFAULT_LISTEN", "RelayConfig", "RelayServer", "create_app"]
__version__ = "0.1.0"
