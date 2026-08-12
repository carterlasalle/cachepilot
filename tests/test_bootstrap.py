"""Bootstrap smoke test — the workspace is importable and the version exists."""

import cachepilot


def test_version():
    assert cachepilot.__version__
