"""Tests for the plugin SDK helpers in besapi.plugin_utilities.

These cover the helpers that plugins would otherwise copy into every script.
See also tests/test_sample_plugin.py which runs a real plugin end to end.
"""

import argparse
import logging
import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import besapi
from besapi import plugin_utilities


@pytest.fixture
def fake_main(monkeypatch, tmp_path):
    """Pretend the running `__main__` script is tmp_path/my_plugin.py."""
    plugin_path = tmp_path / "my_plugin.py"
    plugin_path.write_text("# fake plugin\n", encoding="utf-8")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(
        sys.modules["__main__"], "__file__", str(plugin_path), raising=False
    )
    return plugin_path


# ---------- A1: invoke path helpers resolve the plugin, not besapi ----------


def test_get_invoke_path_uses_main_file(fake_main):
    """Test that the invoke path is the running __main__ script."""
    assert plugin_utilities.get_invoke_path() == str(fake_main)


def test_get_invoke_path_frozen_uses_executable(monkeypatch, tmp_path):
    """Test that a frozen (PyInstaller) plugin resolves to its executable."""
    exe_path = str(tmp_path / "my_plugin.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe_path)
    assert plugin_utilities.get_invoke_path() == exe_path


def test_get_invoke_path_falls_back_to_argv(monkeypatch, tmp_path):
    """Test the fallback to sys.argv[0] when __main__ has no __file__."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys.modules["__main__"], "__file__", raising=False)
    script = str(tmp_path / "argv_plugin.py")
    monkeypatch.setattr(sys, "argv", [script])
    assert plugin_utilities.get_invoke_path() == script


def test_get_invoke_folder_is_plugin_folder(fake_main):
    """Test that the invoke folder is the plugin's folder, not besapi's."""
    assert plugin_utilities.get_invoke_folder() == str(fake_main.parent)


def test_get_invoke_file_name_is_plugin_name(fake_main):
    """Test that the invoke file name is the plugin's name without extension."""
    assert plugin_utilities.get_invoke_file_name() == "my_plugin"


# ---------- A2: default log file goes next to the plugin ----------


def test_default_log_path_is_next_to_plugin(fake_main):
    """Test that with no path the log file is <plugin folder>/<plugin>.log."""
    cfg = plugin_utilities.get_plugin_logging_config(verbose=0, console=False)
    file_handler = cfg["handlers"][0]
    try:
        assert file_handler.baseFilename == str(fake_main.parent / "my_plugin.log")
    finally:
        file_handler.close()


# ---------- A3: only a trailing /api is stripped from the REST URL ----------


class RecordingConnection:
    """Stand-in for BESConnection that records the root server it was given."""

    rootservers: list = []

    def __init__(self, username, password, rootserver, **kwargs):
        RecordingConnection.rootservers.append(rootserver)


@pytest.fixture
def recording_connection(monkeypatch):
    """Replace BESConnection so no network connection is attempted."""
    RecordingConnection.rootservers = []
    monkeypatch.setattr(besapi.besapi, "BESConnection", RecordingConnection)
    return RecordingConnection


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


@pytest.mark.parametrize(
    "rest_url, expected",
    [
        ("https://api.example.com:52311/api", "https://api.example.com:52311"),
        ("https://bigfix.example.com:52311/api/", "https://bigfix.example.com:52311"),
    ],
)
def test_rest_url_only_trailing_api_stripped(recording_connection, rest_url, expected):
    """Test that only a trailing /api is removed, never /api inside the host."""
    args = make_args(user="me", password="pw", rest_url=rest_url)
    plugin_utilities.get_besapi_connection_args(args)
    assert recording_connection.rootservers == [expected]


# ---------- A4: never prompt for a password without a terminal ----------


class NoTTY:
    """Stand-in for sys.stdin when running as a service."""

    @staticmethod
    def isatty():
        return False


def test_no_password_prompt_without_tty(monkeypatch, recording_connection):
    """Test that a service without a terminal is not blocked on getpass."""

    def fail_getpass(*_args, **_kwargs):
        raise AssertionError("getpass must not be called without a tty")

    monkeypatch.setattr(plugin_utilities.getpass, "getpass", fail_getpass)
    monkeypatch.setattr(plugin_utilities, "get_root_server_rest_pass", lambda: None)
    monkeypatch.setattr(sys, "stdin", NoTTY())

    args = make_args(user="me", rest_url="https://bigfix:52311")
    assert plugin_utilities.get_besapi_connection_args(args) is None
    assert recording_connection.rootservers == []


# ---------- A5: the library does not print unless verbose ----------


def test_logging_config_quiet_stdout_when_not_verbose(tmp_path, capsys):
    """Test that console logging does not print to stdout when not verbose."""
    cfg = plugin_utilities.get_plugin_logging_config(
        str(tmp_path / "quiet.log"), verbose=0, console=True
    )
    cfg["handlers"][0].close()
    assert capsys.readouterr().out == ""


# ---------- B2: argparse description ----------


def test_setup_plugin_argparse_custom_description():
    """Test that plugins can set their own --help description."""
    parser = plugin_utilities.setup_plugin_argparse(description="My plugin")
    assert parser.description == "My plugin"


# ---------- B3: SESSION log level constant ----------


def test_session_log_level_constant():
    """Test that the SESSION log level is exported as a named constant."""
    assert plugin_utilities.SESSION_LOG_LEVEL == 99
    assert logging.getLevelName(plugin_utilities.SESSION_LOG_LEVEL) == "SESSION"


# ---------- C1: plugin_session context manager ----------


def session_messages(caplog):
    """Messages logged at the SESSION level."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == plugin_utilities.SESSION_LOG_LEVEL
    ]


def test_plugin_session_logs_start_versions_and_end(caplog, fake_main):
    """Test that a session logs start and end banners plus version info."""
    caplog.set_level(logging.DEBUG)
    with plugin_utilities.plugin_session("1.2.3"):
        logging.info("doing plugin work")

    banners = session_messages(caplog)
    assert len(banners) == 2
    assert "Starting New Session" in banners[0]
    assert "Ending Session" in banners[1]
    assert "1.2.3" in caplog.text
    assert besapi.besapi.__version__ in caplog.text


def test_plugin_session_logs_exception_and_still_ends(caplog, fake_main):
    """Test that an uncaught error is logged, re-raised, and the session still ends."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match="plugin blew up"):
        with plugin_utilities.plugin_session("1.2.3"):
            raise ValueError("plugin blew up")

    assert any(
        r.levelno == logging.ERROR and r.exc_info for r in caplog.records
    ), "exception should be logged with traceback"
    assert "Ending Session" in session_messages(caplog)[-1]


# ---------- C3: resolve_plugin_path ----------


def test_resolve_plugin_path_finds_file_next_to_plugin(
    fake_main, tmp_path, monkeypatch
):
    """Test that a relative path is found next to the plugin, not the cwd."""
    (fake_main.parent / "settings.yaml").write_text("x: 1\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert plugin_utilities.resolve_plugin_path("settings.yaml") == str(
        fake_main.parent / "settings.yaml"
    )


def test_resolve_plugin_path_prefers_path_as_given(fake_main, tmp_path, monkeypatch):
    """Test that a path that exists as given (relative to cwd) wins."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "settings.yaml").write_text("x: 2\n", encoding="utf-8")
    (fake_main.parent / "settings.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    assert plugin_utilities.resolve_plugin_path("settings.yaml") == str(
        cwd / "settings.yaml"
    )


def test_resolve_plugin_path_missing_returns_none(fake_main):
    """Test that a path found nowhere returns None."""
    assert plugin_utilities.resolve_plugin_path("does_not_exist.yaml") is None


# ---------- C5: consume_trigger_file ----------


def test_consume_trigger_file_present(fake_main):
    """Test that an existing trigger file returns True and is deleted."""
    trigger = fake_main.parent / "run_now"
    trigger.write_text("", encoding="utf-8")

    assert plugin_utilities.consume_trigger_file("run_now") is True
    assert not trigger.exists()


def test_consume_trigger_file_missing(fake_main):
    """Test that a missing trigger file returns False."""
    assert plugin_utilities.consume_trigger_file("run_now") is False


# ---------- C6: find_executable ----------


def make_executable(path):
    """Create a small executable file."""
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="uses posix executable bits")
def test_find_executable_on_path(tmp_path, monkeypatch):
    """Test that an executable on PATH is found."""
    tool = make_executable(tmp_path / "besapi_test_tool")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert plugin_utilities.find_executable("besapi_test_tool") == str(tool)


@pytest.mark.skipif(os.name == "nt", reason="uses posix executable bits")
def test_find_executable_extra_paths(tmp_path, monkeypatch):
    """Test that extra paths are used when not on PATH."""
    tool = make_executable(tmp_path / "custom_tool")
    monkeypatch.setenv("PATH", "")
    found = plugin_utilities.find_executable(
        "besapi_test_tool", extra_paths=[str(tmp_path / "missing"), str(tool)]
    )
    assert found == str(tool)


def test_find_executable_default(monkeypatch):
    """Test that the default is returned when nothing is found."""
    monkeypatch.setenv("PATH", "")
    assert (
        plugin_utilities.find_executable("besapi_test_tool", default="fallback")
        == "fallback"
    )


# ---------- C7: run_logged ----------

PY_OUT_ERR = "import sys; print('to-stdout'); sys.stderr.write('to-stderr')"
PY_FAIL = "import sys; sys.stderr.write('it-broke'); sys.exit(3)"


def test_run_logged_logs_stdout_and_stderr(caplog):
    """Test that both stdout and stderr are logged and the result is returned."""
    caplog.set_level(logging.DEBUG)
    result = plugin_utilities.run_logged([sys.executable, "-c", PY_OUT_ERR])

    assert result.returncode == 0
    assert result.stdout.strip() == "to-stdout"
    assert "to-stdout" in caplog.text
    assert "to-stderr" in caplog.text


def test_run_logged_failure_check_true_logs_and_raises(caplog):
    """Test that a failing command logs its stderr as a warning then raises."""
    caplog.set_level(logging.DEBUG)
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        plugin_utilities.run_logged([sys.executable, "-c", PY_FAIL])

    assert any(
        r.levelno >= logging.WARNING and "it-broke" in r.getMessage()
        for r in caplog.records
    )


def test_run_logged_failure_check_false_returns(caplog):
    """Test that check=False returns the failed result with stderr warned."""
    caplog.set_level(logging.DEBUG)
    result = plugin_utilities.run_logged([sys.executable, "-c", PY_FAIL], check=False)

    assert result.returncode == 3
    assert any(
        r.levelno >= logging.WARNING and "it-broke" in r.getMessage()
        for r in caplog.records
    )


# ---------- C2: init_plugin one-call bootstrap ----------


@pytest.fixture
def reset_root_logging():
    """Init_plugin reconfigures the root logger, restore it after the test."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers, root.level = saved_handlers, saved_level


def test_init_plugin_yields_args_and_connection(
    monkeypatch, fake_main, reset_root_logging
):
    """Test that init_plugin parses args, logs to the plugin log, and connects."""
    monkeypatch.setattr(sys, "argv", ["my_plugin.py", "-v", "--extra", "yes"])
    monkeypatch.setattr(
        plugin_utilities, "get_besapi_connection", lambda args: "CONNECTION"
    )
    parser = plugin_utilities.setup_plugin_argparse()
    parser.add_argument("--extra")

    with plugin_utilities.init_plugin("1.2.3", parser=parser) as (args, bes_conn):
        assert bes_conn == "CONNECTION"
        assert args.verbose == 1
        assert args.extra == "yes"

    log_text = (fake_main.parent / "my_plugin.log").read_text(encoding="utf-8")
    assert "Starting New Session" in log_text
    assert "Ending Session" in log_text


def test_init_plugin_exits_when_connection_required(
    monkeypatch, fake_main, reset_root_logging
):
    """Test that no connection logs an error, ends the session, and exits 1."""
    monkeypatch.setattr(sys, "argv", ["my_plugin.py"])
    monkeypatch.setattr(plugin_utilities, "get_besapi_connection", lambda args: None)

    with pytest.raises(SystemExit) as exit_info:
        with plugin_utilities.init_plugin("1.2.3"):
            raise AssertionError("plugin body must not run without a connection")

    assert exit_info.value.code == 1
    log_text = (fake_main.parent / "my_plugin.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text
    assert "Ending Session" in log_text


def test_init_plugin_connection_optional(monkeypatch, fake_main, reset_root_logging):
    """Test that require_connection=False lets the plugin run without one."""
    monkeypatch.setattr(sys, "argv", ["my_plugin.py"])
    monkeypatch.setattr(plugin_utilities, "get_besapi_connection", lambda args: None)

    with plugin_utilities.init_plugin("1.2.3", require_connection=False) as (
        _args,
        bes_conn,
    ):
        assert bes_conn is None


# ---------- C4: get_plugin_config ----------


def test_get_plugin_config_default_name(fake_main):
    """Test that <plugin>.config.yaml next to the plugin is loaded by default."""
    pytest.importorskip("ruamel.yaml", reason="optional: pip install besapi[plugins]")
    (fake_main.parent / "my_plugin.config.yaml").write_text(
        "bigfix:\n  sites:\n    - name: Demo\n", encoding="utf-8"
    )
    config = plugin_utilities.get_plugin_config()
    assert config == {"bigfix": {"sites": [{"name": "Demo"}]}}


def test_get_plugin_config_explicit_name(fake_main):
    """Test that an explicit config file name is resolved next to the plugin."""
    pytest.importorskip("ruamel.yaml", reason="optional: pip install besapi[plugins]")
    (fake_main.parent / "other.yaml").write_text("key: value\n", encoding="utf-8")
    assert plugin_utilities.get_plugin_config("other.yaml") == {"key": "value"}


def test_get_plugin_config_missing_raises(fake_main):
    """Test that a missing config file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        plugin_utilities.get_plugin_config()


def test_get_plugin_config_without_yaml_library(fake_main, monkeypatch):
    """Test a clear install hint when the optional YAML library is missing."""
    (fake_main.parent / "my_plugin.config.yaml").write_text("a: 1\n", encoding="utf-8")
    # a None entry in sys.modules makes that import raise ImportError:
    monkeypatch.setitem(sys.modules, "ruamel", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)

    with pytest.raises(ImportError, match=r"besapi\[plugins\]"):
        plugin_utilities.get_plugin_config()


# ---------- B1: explicit args win over root server credentials ----------


def test_explicit_args_win_over_root_server(monkeypatch):
    """Test that explicit -u/-p are used even when running on a root server."""
    monkeypatch.setattr(plugin_utilities, "get_besconn_root_server", lambda: "ROOT")
    monkeypatch.setattr(
        plugin_utilities, "get_besapi_connection_args", lambda args: "ARGS"
    )
    args = make_args(user="me", password="pw", rest_url="https://bigfix:52311")
    assert plugin_utilities.get_besapi_connection(args) == "ARGS"


def test_root_server_is_fallback_after_explicit_args(monkeypatch):
    """Test that the root server is tried only after explicit args fail."""
    attempts = []

    def args_conn(_args):
        attempts.append("args")

    def root_conn():
        attempts.append("root")
        return "ROOT"

    monkeypatch.setattr(plugin_utilities, "get_besapi_connection_args", args_conn)
    monkeypatch.setattr(plugin_utilities, "get_besconn_root_server", root_conn)
    args = make_args(user="me", password="pw", rest_url="https://bigfix:52311")

    assert plugin_utilities.get_besapi_connection(args) == "ROOT"
    assert attempts == ["args", "root"]


# ---------- C2 + D4: init_plugin gives connections a default timeout ----------


class TimeoutConnection:
    """Stand-in connection with no timeout set yet."""

    timeout = None


def test_init_plugin_sets_default_timeout(monkeypatch, fake_main, reset_root_logging):
    """Test that plugin connections never wait forever by default."""
    monkeypatch.setattr(sys, "argv", ["my_plugin.py"])
    monkeypatch.setattr(
        plugin_utilities, "get_besapi_connection", lambda args: TimeoutConnection()
    )

    with plugin_utilities.init_plugin("1.2.3") as (_args, bes_conn):
        assert bes_conn.timeout == plugin_utilities.DEFAULT_PLUGIN_TIMEOUT
        assert bes_conn.timeout is not None


def test_default_plugin_timeout_value():
    """Test the default plugin timeout: (connect, read) seconds."""
    assert plugin_utilities.DEFAULT_PLUGIN_TIMEOUT == (30, 600)


class BesapiDefaultTimeoutConnection:
    """Stand-in connection that already has the besapi wide default timeout."""

    timeout = (90, 600)


def test_init_plugin_timeout_overrides_besapi_default(
    monkeypatch, fake_main, reset_root_logging
):
    """Test that the plugin timeout replaces the besapi wide default."""
    monkeypatch.setattr(sys, "argv", ["my_plugin.py"])
    monkeypatch.setattr(
        plugin_utilities,
        "get_besapi_connection",
        lambda args: BesapiDefaultTimeoutConnection(),
    )

    with plugin_utilities.init_plugin("1.2.3") as (_args, bes_conn):
        assert bes_conn.timeout == (30, 600)
