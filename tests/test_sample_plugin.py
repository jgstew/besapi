"""Run tests/plugins/sample_plugin.py as a real plugin process.

The plugin is copied to a temp folder and run from a *different* working
directory, so these tests prove that the SDK resolves paths relative to the
plugin itself and not to besapi or the current directory.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
SAMPLE_PLUGIN = os.path.join(ROOT, "tests", "plugins", "sample_plugin.py")


@pytest.fixture
def plugin_dir(tmp_path):
    """Copy the sample plugin into its own folder."""
    folder = tmp_path / "plugin"
    folder.mkdir()
    shutil.copy(SAMPLE_PLUGIN, folder / "sample_plugin.py")
    return folder


@pytest.fixture
def other_cwd(tmp_path):
    """A working directory that is not the plugin folder."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    return folder


def run_plugin(plugin_dir, cwd, *plugin_args):
    """Run the sample plugin against the local src and return the process."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    # keep any real credentials on this machine out of the test:
    # (env vars, and ~/besapi.conf or ~/.besapi.conf via an empty home)
    for name in ("BES_USER_NAME", "BES_PASSWORD", "BES_ROOT_SERVER"):
        env.pop(name, None)
    env["HOME"] = str(cwd)
    env["USERPROFILE"] = str(cwd)
    return subprocess.run(
        [sys.executable, str(plugin_dir / "sample_plugin.py"), *plugin_args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def last_json_line(stdout):
    """Parse the JSON report printed on the last line of stdout."""
    return json.loads(stdout.strip().splitlines()[-1])


def test_sample_plugin_resolves_paths_to_itself(plugin_dir, other_cwd):
    """Test that invoke folder, name and log file all point at the plugin."""
    result = run_plugin(plugin_dir, other_cwd)
    assert result.returncode == 0, result.stderr

    report = last_json_line(result.stdout)
    assert report["invoke_folder"] == str(plugin_dir)
    assert report["invoke_file_name"] == "sample_plugin"
    assert report["log_file"] == str(plugin_dir / "sample_plugin.log")
    assert report["description"] == "besapi sample plugin for tests"


def test_sample_plugin_writes_session_log(plugin_dir, other_cwd):
    """Test that the plugin's log file is created next to it with the SESSION level."""
    result = run_plugin(plugin_dir, other_cwd)
    assert result.returncode == 0, result.stderr

    log_text = (plugin_dir / "sample_plugin.log").read_text(encoding="utf-8")
    assert "SESSION:sample plugin ran" in log_text
    assert not (other_cwd / "sample_plugin.log").exists()


def test_sample_plugin_logs_session_banners(plugin_dir, other_cwd):
    """Test that the plugin session start and end banners land in the log."""
    result = run_plugin(plugin_dir, other_cwd)
    assert result.returncode == 0, result.stderr

    log_text = (plugin_dir / "sample_plugin.log").read_text(encoding="utf-8")
    assert "SESSION:----- Starting New Session" in log_text
    assert "SESSION:----- Ending Session" in log_text


def test_sample_plugin_consumes_trigger_file_next_to_it(plugin_dir, other_cwd):
    """Test that a trigger file next to the plugin is found and consumed."""
    (plugin_dir / "sample_plugin_run_now").write_text("", encoding="utf-8")

    result = run_plugin(
        plugin_dir, other_cwd, "--trigger-file", "sample_plugin_run_now"
    )
    assert result.returncode == 0, result.stderr

    assert last_json_line(result.stdout)["trigger_consumed"] is True
    assert not (plugin_dir / "sample_plugin_run_now").exists()


def test_sample_plugin_no_trigger_file(plugin_dir, other_cwd):
    """Test that a missing trigger file is reported as not consumed."""
    result = run_plugin(
        plugin_dir, other_cwd, "--trigger-file", "sample_plugin_run_now"
    )
    assert result.returncode == 0, result.stderr

    assert last_json_line(result.stdout)["trigger_consumed"] is False


def test_sample_plugin_loads_config_next_to_it(plugin_dir, other_cwd):
    """Test that sample_plugin.config.yaml next to the plugin is loaded."""
    pytest.importorskip("ruamel.yaml", reason="optional: pip install besapi[plugins]")
    (plugin_dir / "sample_plugin.config.yaml").write_text(
        "trigger_file_path: sample_plugin_run_now\n", encoding="utf-8"
    )
    result = run_plugin(plugin_dir, other_cwd, "--config")
    assert result.returncode == 0, result.stderr

    assert last_json_line(result.stdout)["config"] == {
        "trigger_file_path": "sample_plugin_run_now"
    }


def test_sample_plugin_exits_without_connection(plugin_dir, other_cwd):
    """Test that a plugin requiring a connection exits 1 when it cannot connect.

    Explicit args point at a closed local port, so no real server is used.
    """
    result = run_plugin(
        plugin_dir,
        other_cwd,
        "--connect",
        "-u",
        "fake_user",
        "-p",
        "fake_password",
        "-r",
        "https://127.0.0.1:9",
    )
    assert result.returncode == 1

    log_text = (plugin_dir / "sample_plugin.log").read_text(encoding="utf-8")
    assert "sample plugin ran" not in log_text
    assert "SESSION:----- Ending Session" in log_text


def test_sample_plugin_reports_not_connected(plugin_dir, other_cwd):
    """Test that the plugin reports no connection when none is available."""
    result = run_plugin(plugin_dir, other_cwd)
    assert result.returncode == 0, result.stderr

    assert last_json_line(result.stdout)["connected"] is False
