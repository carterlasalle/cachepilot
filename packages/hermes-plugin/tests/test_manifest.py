"""PRD §128 task 1 — the plugin manifest loads and is valid.

Also verifies the Hermes entry-point wiring: the ``hermes_agent.plugins``
entry point declared in pyproject.toml resolves to a module with a callable
``register``, and the package imports with zero Hermes dependencies.
"""

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

from cachepilot_hermes.plugin import (
    PLUGIN_ENTRYPOINT,
    PLUGIN_MANIFEST,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)

PKG_ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject():
    with open(PKG_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_manifest_fields_valid():
    m = PLUGIN_MANIFEST
    assert m.name == "cachepilot-hermes-plugin"
    assert m.version == "0.1.0"
    assert m.description
    assert m.entrypoint == "cachepilot_hermes.plugin:register"
    assert len(m.hooks) == 8
    assert len(m.middleware) == 4


def test_manifest_matches_pyproject_and_version():
    project = _read_pyproject()["project"]
    assert project["name"] == PLUGIN_NAME
    assert project["version"] == PLUGIN_VERSION
    assert importlib.import_module("cachepilot_hermes").__version__ == PLUGIN_VERSION


def test_entry_point_declared_and_resolvable():
    pyproject = _read_pyproject()
    entry_points = pyproject["project"]["entry-points"]["hermes_agent.plugins"]
    assert entry_points[PLUGIN_NAME] == "cachepilot_hermes.plugin"
    module = importlib.import_module(entry_points[PLUGIN_NAME])
    assert callable(getattr(module, "register", None))
    assert PLUGIN_ENTRYPOINT.endswith(":register")


def test_plugin_imports_without_hermes_installed():
    """The plugin must import in a clean interpreter without Hermes modules.

    Proves the skeleton has no dependency on the Hermes install (it only
    needs stdlib + pydantic), which is what makes the stock-Hermes-unchanged
    test meaningful.
    """
    code = (
        "import sys\n"
        "import cachepilot_hermes.plugin\n"
        "leaked = [m for m in sys.modules if m.split('.')[0] in "
        "{'hermes', 'hermes_cli', 'agent', 'gateway', 'tools'}]\n"
        "print('LEAKED=' + ','.join(sorted(leaked)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "LEAKED=" in result.stdout
    leaked = result.stdout.split("LEAKED=", 1)[1].strip()
    assert leaked == "", f"plugin import leaked Hermes modules: {leaked}"
