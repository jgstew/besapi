import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from besapi import plugin_utilities


def test_setup_plugin_argparse_defaults():
    """Test that default args are set correctly."""
    parser = plugin_utilities.setup_plugin_argparse(plugin_args_required=False)
    # ensure parser returns expected arguments when not required
    args = parser.parse_args([])
    assert args.verbose == 0
    assert args.console is False
    assert args.besserver is None
    assert args.rest_url is None
    assert args.user is None
    assert args.password is None


def test_setup_plugin_argparse_required_flags():
    """Test that required args cause SystemExit when missing."""
    parser = plugin_utilities.setup_plugin_argparse(plugin_args_required=True)
    # when required, missing required args should cause SystemExit
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_get_plugin_args_parses_known_args(monkeypatch):
    """Test that known command line args are parsed correctly."""
    # simulate command line args
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "-v",
            "-c",
            "--rest-url",
            "https://example:52311",
            "--user",
            "me",
            "--password",
            "pw",
        ],
    )
    args = plugin_utilities.get_plugin_args(plugin_args_required=False)
    assert args.verbose == 1
    assert args.console is True
    assert args.rest_url == "https://example:52311"
    assert args.user == "me"
    assert args.password == "pw"
    assert args.besserver is None
