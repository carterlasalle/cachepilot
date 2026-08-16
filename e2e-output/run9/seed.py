"""E2E-001 Run 9 seed helper — writes a seeded telemetry DB for CLI/API checks.

Reuses dashboard/backend/smoke_test.py's seed_store() so the live dashboard +
CLI read the exact same fixture the smoke test asserts against.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dashboard.backend.smoke_test import seed_store


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("e2e-output/run9/telemetry.db")
    seed_store(db)
    print(f"seeded {db.resolve()} ({db.stat().st_size} bytes)")


if __name__ == "__main__":
    main()