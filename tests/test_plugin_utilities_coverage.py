"""Coverage of existing plugin_utilities connection behavior.

These document behavior that already worked, so they were not red first.
They were checked by mutating the code under test to confirm each can fail.
"""

import argparse
import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import besapi  # noqa: E402
from besapi import plugin_utilities  # noqa: E402

REST_URL = "https://rest.example:52311"
BES_SERVER = "https://besserver.example:52311"


class FakeConnection:
    """Stand-in for BESConnection, raising for root servers in `fail_for`."""

    attempts: list = []
    fail_for: dict = {}

    def __init__(self, username, password, rootserver, **kwargs):
        FakeConnection.attempts.append((username, password, rootserver))
        error = FakeConnection.fail_for.get(rootserver)
        if error:
            raise error


@pytest.fixture
def fake_connection(monkeypatch):
    """Replace BESConnection, and make sure no root server creds are found."""
    FakeConnection.attempts = []
    FakeConnection.fail_for = {}
    monkeypatch.setattr(besapi.besapi, "BESConnection", FakeConnection)
    monkeypatch.setattr(plugin_utilities, "get_root_server_rest_pass", lambda: None)
    return FakeConnection


def make_args(**overrides):
    """Build plugin args like setup_plugin_argparse would."""
    values = {
        "verbose": 0,
        "console": False,
        "besserver": None,
        "rest_url": None,
        "user": None,
        "password": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_connects_with_rest_url(fake_connection):
    """Test the normal case: user, password and rest url."""
    args = make_args(user="me", password="pw", rest_url=REST_URL)
    assert isinstance(plugin_utilities.get_besapi_connection_args(args), FakeConnection)
    assert fake_connection.attempts == [("me", "pw", REST_URL)]


def test_no_user_does_not_connect(fake_connection):
    """Test that without a user no connection is attempted."""
    args = make_args(password="pw", rest_url=REST_URL)
    assert plugin_utilities.get_besapi_connection_args(args) is None
    assert fake_connection.attempts == []


def test_missing_rest_url_falls_back_to_besserver(fake_connection):
    """Test that besserver is used when no rest url is given."""
    args = make_args(user="me", password="pw", besserver=BES_SERVER)
    assert isinstance(plugin_utilities.get_besapi_connection_args(args), FakeConnection)
    assert fake_connection.attempts == [("me", "pw", BES_SERVER)]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        besapi.besapi.requests.exceptions.ConnectionError("no route"),
    ],
)
def test_failed_rest_url_falls_back_to_besserver(fake_connection, error):
    """Test that a connection error on the rest url tries besserver next."""
    fake_connection.fail_for = {REST_URL: error}
    args = make_args(user="me", password="pw", rest_url=REST_URL, besserver=BES_SERVER)

    assert isinstance(plugin_utilities.get_besapi_connection_args(args), FakeConnection)
    assert [a[2] for a in fake_connection.attempts] == [REST_URL, BES_SERVER]


def test_failed_rest_url_without_besserver(fake_connection):
    """Test that a failed rest url with no besserver returns None."""
    fake_connection.fail_for = {REST_URL: ConnectionRefusedError("refused")}
    args = make_args(user="me", password="pw", rest_url=REST_URL)

    assert plugin_utilities.get_besapi_connection_args(args) is None
    assert [a[2] for a in fake_connection.attempts] == [REST_URL]


def test_besserver_unknown_error_returns_none(fake_connection):
    """Test that an unexpected error from besserver returns None."""
    fake_connection.fail_for = {
        REST_URL: ConnectionRefusedError("refused"),
        BES_SERVER: RuntimeError("unexpected"),
    }
    args = make_args(user="me", password="pw", rest_url=REST_URL, besserver=BES_SERVER)

    assert plugin_utilities.get_besapi_connection_args(args) is None
    assert [a[2] for a in fake_connection.attempts] == [REST_URL, BES_SERVER]


def test_password_from_root_server(fake_connection, monkeypatch):
    """Test that a missing password is read from the local root server."""
    monkeypatch.setattr(
        plugin_utilities, "get_root_server_rest_pass", lambda: "root_server_pw"
    )
    args = make_args(user="me", rest_url=REST_URL)

    plugin_utilities.get_besapi_connection_args(args)
    assert fake_connection.attempts == [("me", "root_server_pw", REST_URL)]


class TTY:
    """Stand-in for an interactive sys.stdin."""

    @staticmethod
    def isatty():
        return True


def test_password_prompt_with_tty(fake_connection, monkeypatch):
    """Test that an interactive run prompts for a missing password."""
    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setattr(plugin_utilities.getpass, "getpass", lambda *a: "typed_pw")
    args = make_args(user="me", rest_url=REST_URL)

    plugin_utilities.get_besapi_connection_args(args)
    assert fake_connection.attempts == [("me", "typed_pw", REST_URL)]


def test_get_besapi_connection_no_args_uses_env_then_config(monkeypatch):
    """Test that without args (and no root server) env then config are used."""
    monkeypatch.setattr(plugin_utilities, "get_besconn_root_server", lambda: None)
    tried = []
    monkeypatch.setattr(
        besapi.besapi, "get_bes_conn_using_env", lambda: tried.append("env")
    )
    monkeypatch.setattr(
        besapi.besapi,
        "get_bes_conn_using_config_file",
        lambda: tried.append("config") or "CONFIG_CONN",
    )

    assert plugin_utilities.get_besapi_connection() == "CONFIG_CONN"
    assert tried == ["env", "config"]


def test_get_besapi_connection_env_wins(monkeypatch):
    """Test that an env connection is used without reading the config file."""
    monkeypatch.setattr(plugin_utilities, "get_besconn_root_server", lambda: None)
    monkeypatch.setattr(besapi.besapi, "get_bes_conn_using_env", lambda: "ENV_CONN")

    def config_must_not_be_read():
        raise AssertionError("config file should not be read")

    monkeypatch.setattr(
        besapi.besapi, "get_bes_conn_using_config_file", config_must_not_be_read
    )
    assert plugin_utilities.get_besapi_connection() == "ENV_CONN"


def test_get_besapi_connection_user_failed_no_fallback_to_env(monkeypatch):
    """Test that a failed explicit user never silently connects via env/config."""
    monkeypatch.setattr(plugin_utilities, "get_besapi_connection_args", lambda a: None)
    monkeypatch.setattr(plugin_utilities, "get_besconn_root_server", lambda: None)

    def must_not_be_called():
        raise AssertionError("env / config must not be used for an explicit user")

    monkeypatch.setattr(
        plugin_utilities, "get_besapi_connection_env_then_config", must_not_be_called
    )
    args = make_args(user="me", password="pw", rest_url=REST_URL)
    assert plugin_utilities.get_besapi_connection(args) is None
