"""LIVE tests: run the sample plugin against a real BigFix server.

These are skipped unless BESAPI_LIVE_TESTS=1 is set. They use whatever
credentials besapi finds on this machine (BES_* env vars, then the besapi
config file), and only run read only session relevance.

    BESAPI_LIVE_TESTS=1 python -m pytest tests/test_live_sample_plugin.py
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BESAPI_LIVE_TESTS") != "1",
    reason="live BigFix server tests, set BESAPI_LIVE_TESTS=1 to run",
)

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


def run_live_plugin(plugin_dir, *plugin_args):
    """Run the sample plugin with this machine's real environment and creds."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    return subprocess.run(
        [
            sys.executable,
            str(plugin_dir / "sample_plugin.py"),
            "--connect",
            *plugin_args,
        ],
        cwd=plugin_dir.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def last_json_line(stdout):
    """Parse the JSON report printed on the last line of stdout."""
    return json.loads(stdout.strip().splitlines()[-1])


def test_live_sample_plugin_connects(plugin_dir):
    """Test that the plugin connects with this machine's credentials."""
    result = run_live_plugin(plugin_dir)
    assert result.returncode == 0, result.stderr

    report = last_json_line(result.stdout)
    assert report["connected"] is True
    assert report["timeout"] is not None


@pytest.mark.parametrize(
    "text",
    ['say "hi"', "100%", "%22 literal", 'mixed "50%" off', r"back\slash & <tag>"],
)
def test_live_relevance_escape_round_trip(plugin_dir, text):
    """Test that escaped text survives a round trip through session relevance."""
    result = run_live_plugin(plugin_dir, "--relevance-echo", text)
    assert result.returncode == 0, result.stderr

    report = last_json_line(result.stdout)
    assert report["relevance_echo"] == text
    assert report["relevance_echo_length"] == len(text)
