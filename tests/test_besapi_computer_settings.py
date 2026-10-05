"""Tests for getting / setting BigFix client (computer) settings.

See https://github.com/jgstew/besapi/issues/7 (originally CLCMacTeam/besapi#7)

No network is used, see the FakeSession in test_besapi_sdk.py. Response
shapes are from the forum example of `computer/{id}/setting/{name}`:
https://forum.bigfix.com/t/change-bes-computer-setting-with-curl/40039
"""

import os
import sys

import lxml.etree
import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_besapi_sdk import make_conn  # noqa: E402

import besapi  # noqa: E402

API = "https://bigfix.example:52311/api"

BESAPI_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="BESAPI.xsd">'
)


def setting_xml(computer_id, name, value):
    """Build a response like GET computer/{id}/setting/{name}."""
    res = f"https://localhost:52311/api/computer/{computer_id}/setting/{name}"
    return f"""{BESAPI_HEADER}
<ComputerSettings Resource="{res}">
<Setting Resource="{res}"><Name>{name}</Name><Value>{value}</Value></Setting>
</ComputerSettings>
</BESAPI>"""


SETTINGS_XML = f"""{BESAPI_HEADER}
<ComputerSettings Resource="https://localhost:52311/api/computer/123/settings">
<Setting Resource="https://localhost:52311/api/computer/123/setting/_BESClient_Log_Days"><Name>_BESClient_Log_Days</Name><Value>30</Value></Setting>
<Setting Resource="https://localhost:52311/api/computer/123/setting/My%20Tag"><Name>My Tag</Name><Value>a &amp; b</Value></Setting>
<Setting Resource="https://localhost:52311/api/computer/123/setting/Empty"><Name>Empty</Name></Setting>
</ComputerSettings>
</BESAPI>"""


@pytest.fixture
def conn(offline_login):  # pylint: disable=unused-argument
    """An offline connection with a recording session."""
    return make_conn()


# ---------- get all settings ----------


def test_get_computer_settings_returns_dict(conn):
    conn.session.routes = {f"{API}/computer/123/settings": (SETTINGS_XML, 200)}
    assert conn.get_computer_settings(123) == {
        "_BESClient_Log_Days": "30",
        "My Tag": "a & b",
        "Empty": "",
    }
    assert conn.session.calls[0][0] == "get"


def test_get_computer_settings_none_when_no_settings(conn):
    conn.session.routes = {
        f"{API}/computer/123/settings": (
            f"{BESAPI_HEADER}<ComputerSettings /></BESAPI>",
            200,
        )
    }
    assert conn.get_computer_settings("123") == {}


# ---------- get one setting ----------


def test_get_computer_setting_value(conn):
    conn.session.routes = {
        f"{API}/computer/123/setting/_BESClient_Log_Days": (
            setting_xml(123, "_BESClient_Log_Days", "30"),
            200,
        )
    }
    assert conn.get_computer_setting(123, "_BESClient_Log_Days") == "30"


def test_get_computer_setting_url_quotes_name(conn):
    url = f"{API}/computer/123/setting/My%20Tag"
    conn.session.routes = {url: (setting_xml(123, "My Tag", "x"), 200)}
    assert conn.get_computer_setting(123, "My Tag") == "x"
    assert conn.session.calls[0][1] == url


@pytest.mark.parametrize("raise_for_status", [False, True])
def test_get_computer_setting_missing_is_none(offline_login, raise_for_status):
    conn = make_conn(raise_for_status=raise_for_status)
    conn.session.response_text = "Setting not found"
    conn.session.response_status = 404
    assert conn.get_computer_setting(123, "Nope") is None


# ---------- set one setting ----------


def test_set_computer_setting_posts_xml(conn):
    conn.session.response_text = (
        f"{BESAPI_HEADER}<Action Resource='x'><ID>99</ID></Action></BESAPI>"
    )
    conn.set_computer_setting(123, "_BESClient_Log_Days", 45)

    method, url, kwargs = conn.session.calls[-1]
    assert method == "post"
    assert url == f"{API}/computer/123/setting/_BESClient_Log_Days"
    root = lxml.etree.fromstring(kwargs["data"])
    assert root.tag == "BESAPI"
    assert root.xpath("/BESAPI/ComputerSettings/Setting/Name/text()") == [
        "_BESClient_Log_Days"
    ]
    assert root.xpath("/BESAPI/ComputerSettings/Setting/Value/text()") == ["45"]
    # the body we send must be valid against the BESAPI schema:
    assert besapi.besapi.validate_xsd(kwargs["data"])


def test_set_computer_setting_escapes_value(conn):
    conn.set_computer_setting(123, "My Tag", "a & <b>")
    _, url, kwargs = conn.session.calls[-1]
    assert url == f"{API}/computer/123/setting/My%20Tag"
    root = lxml.etree.fromstring(kwargs["data"])
    assert root.xpath("//Value/text()") == ["a & <b>"]


# ---------- input validation ----------


@pytest.mark.parametrize("bad_id", ["", "12a", "../123", None, -1, True])
def test_bad_computer_id_rejected(conn, bad_id):
    with pytest.raises(ValueError):
        conn.get_computer_settings(bad_id)
    with pytest.raises(ValueError):
        conn.get_computer_setting(bad_id, "x")
    with pytest.raises(ValueError):
        conn.set_computer_setting(bad_id, "x", "1")
    assert not conn.session.calls


@pytest.mark.parametrize("bad_name", ["", None, "a/b", "  "])
def test_bad_setting_name_rejected(conn, bad_name):
    with pytest.raises(ValueError):
        conn.get_computer_setting(123, bad_name)
    with pytest.raises(ValueError):
        conn.set_computer_setting(123, bad_name, "1")
    assert not conn.session.calls
