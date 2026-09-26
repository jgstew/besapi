import os
import sys
import types

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


def make_failing_utilities():
    """Build a stand-in platform utilities module where every function raises."""

    def raise_error():
        raise RuntimeError("CryptoUtility exploded")

    return types.SimpleNamespace(
        __name__="fake_platform_utilities",
        get_linux_credentials_rest_pass=raise_error,
        get_besconn_root_linux=raise_error,
        get_win_registry_rest_pass=raise_error,
        get_besconn_root_windows_registry=raise_error,
    )


def test_get_root_server_rest_pass_swallows_errors(monkeypatch):
    """Test that a failure in the root server password lookup does not raise."""
    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_failing_utilities()
    )
    assert plugin_utilities.get_root_server_rest_pass() is None


def test_get_besconn_root_server_swallows_errors(monkeypatch):
    """Test that a failure in the root server connection does not raise."""
    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_failing_utilities()
    )
    assert plugin_utilities.get_besconn_root_server() is None


def test_get_besapi_connection_falls_back_when_root_server_errors(monkeypatch):
    """Test that get_besapi_connection falls back when the root server lookup
    raises.
    """
    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_failing_utilities()
    )
    # env/config fallback is stubbed out so no real connection is attempted:
    monkeypatch.setattr(
        plugin_utilities, "get_besapi_connection_env_then_config", lambda: "fallback"
    )
    assert plugin_utilities.get_besapi_connection() == "fallback"


def test_get_root_server_rest_pass_no_module(monkeypatch):
    """Test that a missing platform module is handled without raising.

    This is the normal case on macOS, where the module is never imported.
    """
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", None)
    assert plugin_utilities.get_root_server_rest_pass() is None
    assert plugin_utilities.get_besconn_root_server() is None


def test_platform_utilities_not_imported_on_darwin():
    """Test that the linux utilities are only used on linux."""
    if sys.platform.startswith("linux"):
        assert plugin_utilities.PLATFORM_UTILITIES is not None
    elif os.name != "nt":
        # macOS and other non-linux posix: no platform utilities at all
        assert plugin_utilities.PLATFORM_UTILITIES is None
