"""LIVE tests: get / set a client setting on a real lab computer.

These are skipped unless BESAPI_LIVE_TESTS=1 is set. They use whatever
credentials besapi finds on this machine (BES_* env vars, then the besapi
config file).

WARNING: this WRITES a harmless custom setting `_besapi_live_test` to the
target computer, by an action the root server creates. Only use lab computers.

The target is BESAPI_TEST_COMPUTER_ID if set, otherwise the first docker
container found: a computer whose name contains "docker", or is a 12 hex
character container id (the default docker hostname).

    BESAPI_LIVE_TESTS=1 python -m pytest tests/test_live_computer_settings.py

BESAPI_TEST_SETTING_WAIT is how long (seconds, default 600) to wait for the
client to apply the setting and report it back.
"""

import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BESAPI_LIVE_TESTS") != "1",
    reason="live BigFix server tests, set BESAPI_LIVE_TESTS=1 to run",
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import besapi  # noqa: E402

SETTING_NAME = "_besapi_live_test"

# docker containers: name contains docker, or is a 12 hex char container id
DOCKER_COMPUTERS_RELEVANCE = (
    '(id of it as string & "|" & name of it) of bes computers whose '
    '(name of it as lowercase contains "docker" or '
    "(length of it = 12 and it as lowercase = "
    'concatenation of characters whose (it is contained by "0123456789abcdef") '
    "of (it as lowercase)) of name of it)"
)


@pytest.fixture(scope="module")
def conn():
    """A live connection, using this machine's besapi credentials."""
    with besapi.besapi.get_bes_conn_using_config_file() as live_conn:
        yield live_conn


@pytest.fixture(scope="module")
def computer_id(conn):  # pylint: disable=redefined-outer-name
    """The lab computer to test against."""
    if os.environ.get("BESAPI_TEST_COMPUTER_ID"):
        return os.environ["BESAPI_TEST_COMPUTER_ID"]
    found = conn.session_relevance_array(DOCKER_COMPUTERS_RELEVANCE)
    found = [row for row in found if "|" in row]
    if not found:
        pytest.fail(
            "no docker container computers visible to these creds, "
            "set BESAPI_TEST_COMPUTER_ID"
        )
    print(f"testing against computer: {found[0]}")
    return found[0].split("|", 1)[0]


def test_live_get_computer_settings(
    conn, computer_id
):  # pylint: disable=redefined-outer-name
    settings = conn.get_computer_settings(computer_id)
    assert isinstance(settings, dict)
    print(f"computer {computer_id} has {len(settings)} settings")


def test_live_get_missing_setting_is_none(
    conn, computer_id
):  # pylint: disable=redefined-outer-name
    assert conn.get_computer_setting(computer_id, "_besapi_does_not_exist") is None


def test_live_set_then_get_computer_setting(
    conn, computer_id
):  # pylint: disable=redefined-outer-name
    value = time.strftime("%Y%m%d%H%M%S")
    result = conn.set_computer_setting(computer_id, SETTING_NAME, value)
    print(f"set result:\n{result}")
    assert result.request.status_code < 400

    # the root server applies it by action, wait for the client to report
    deadline = time.time() + float(os.environ.get("BESAPI_TEST_SETTING_WAIT", "600"))
    current = None
    while time.time() < deadline:
        current = conn.get_computer_setting(computer_id, SETTING_NAME)
        if current == value:
            break
        time.sleep(15)
    assert current == value
