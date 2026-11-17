"""LIVE tests: session relevance encoding against a real BigFix server (#30).

These are skipped unless BESAPI_LIVE_TESTS=1 is set. They use whatever
credentials besapi finds on this machine (BES_* env vars, then the besapi
config file), and only run read only session relevance.

They pass whichever engine answers /api/query: BigFix Explorer, or Web
Reports when Explorer is stopped. Run them in both states before a release.

    BESAPI_LIVE_TESTS=1 python -m pytest -s tests/test_live_session_relevance_encoding.py
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BESAPI_LIVE_TESTS") != "1",
    reason="live BigFix server tests, set BESAPI_LIVE_TESTS=1 to run",
)

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_session_relevance_engines import (  # noqa: E402
    ENTRIES,
    EXPECTED,
    as_compared,
    expected,
    run_entry,
)

import besapi.plugin_utilities  # noqa: E402

# answers that don't depend on the server's content (`number of bes sites`)
# or the time (`now`):
PORTABLE = [r for r in EXPECTED if r not in ("now", "number of bes sites")]


@pytest.fixture(scope="module")
def live_conn():
    """A connection to the real server, which must answer correctly.

    NOTE: fails rather than skips when relevance comes back as an error,
    since that is exactly how issue #30 looked: as if there were no server.
    """
    conn = besapi.plugin_utilities.get_besapi_connection(None)
    if not conn or not conn.login():
        pytest.skip("no BigFix server configured on this machine, or login failed")
    answer = conn.session_relevance_string("true")
    assert answer == "True", f"the server answered `true` with: {answer}"
    return conn


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Keep bescli from reading a real config file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_live_engine_detected(live_conn):
    """Report which engine answers, from the JSON answer's fingerprint."""
    # pylint: disable=protected-access
    answer = live_conn.session_relevance_json("1")
    engine = "explorer" if "count" in answer else "web_reports"
    print(f"\n/api/query is answered by: {engine}")
    assert live_conn._get_relevance_engine() == (
        besapi.besapi.RELEVANCE_ENGINE_EXPLORER
        if engine == "explorer"
        else besapi.besapi.RELEVANCE_ENGINE_WEB_REPORTS
    )


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("relevance", PORTABLE)
def test_live_session_relevance(live_conn, isolated_home, relevance, entry):
    """Test every method answers correctly on the real server."""
    assert run_entry(live_conn, entry, relevance) == as_compared(
        expected(entry, relevance)
    )


@pytest.mark.parametrize("entry", ENTRIES)
def test_live_session_relevance_now(live_conn, isolated_home, entry):
    """Test `now`, whose answer is the time."""
    answer = run_entry(live_conn, entry, "now")
    assert "NOW" in str(answer), answer
