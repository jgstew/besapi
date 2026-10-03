"""Every way to start bescli must reach BESCLInterface().cmdloop().

Entry points covered:
  * `python -m besapi`    (src/besapi/__main__.py)
  * `python -m bescli`    (src/bescli/__main__.py)
  * `python -m bescli.bescli` (the module's own __main__ guard)
  * console scripts `besapi` and `bescli` declared in setup.cfg
    (what `uvx besapi` / `uvx --from besapi bescli` run)

No network: cmdloop is replaced with a recorder, so no prompt is started.
"""

import configparser
import importlib
import os
import runpy
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from bescli import bescli as bescli_module


@pytest.fixture
def cmdloop_calls(monkeypatch):
    """Replace the interactive loop with a recorder."""
    calls = []
    # patch the base class: runpy re-executes bescli.bescli as a fresh module,
    # which defines a new BESCLInterface that still inherits cmdloop from here
    monkeypatch.setattr(
        bescli_module.BESCLInterface.__mro__[1],
        "cmdloop",
        lambda self, *a, **k: calls.append(1),
    )
    return calls


def _console_scripts():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(ROOT, "setup.cfg"))
    raw = cfg.get("options.entry_points", "console_scripts", fallback="")
    pairs = (line.split("=", 1) for line in raw.splitlines() if line.strip())
    return {name.strip(): target.strip() for name, target in pairs}


@pytest.mark.parametrize("module", ["besapi", "bescli", "bescli.bescli"])
def test_python_dash_m_runs_cmdloop(module, cmdloop_calls):
    """`python -m <module>` starts the command loop exactly once."""
    sys.modules.pop(module + ".__main__", None)
    runpy.run_module(module, run_name="__main__", alter_sys=False)
    assert cmdloop_calls == [1]


@pytest.mark.parametrize("script", ["besapi", "bescli"])
def test_console_script_declared(script):
    """Setup.cfg declares both scripts, pointing at the single main()."""
    assert _console_scripts().get(script) == "bescli.bescli:main"


@pytest.mark.parametrize("script", ["besapi", "bescli"])
def test_console_script_target_runs_cmdloop(script, cmdloop_calls):
    """The declared target resolves to a callable that starts the loop."""
    target = _console_scripts()[script]
    mod_name, func_name = target.split(":")
    func = getattr(importlib.import_module(mod_name), func_name)
    func()
    assert cmdloop_calls == [1]
