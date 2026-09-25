"""Tests for besapi.besapi additions that help plugins.

No network is used: BESConnection.login is patched out and the HTTP session
is replaced by a stub that records each request.
"""

import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import besapi  # noqa: E402
from besapi.besapi import BESConnection  # noqa: E402

GOOD_BES_FILE = os.path.join(ROOT, "tests", "good", "ComputerGroupsExample.bes")


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, url):
        self.url = url
        self.status_code = 200
        self.text = "<BESAPI />"
        self.headers = {}


class FakeCookies:
    """Minimal stand-in for requests cookies."""

    def clear(self):
        """Clear cookies."""


class FakeSession:
    """Records every request instead of sending it."""

    def __init__(self):
        self.calls = []
        self.cookies = FakeCookies()
        self.auth = None

    def _record(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(url)

    def get(self, url, **kwargs):
        """Record a GET."""
        return self._record("get", url, **kwargs)

    def post(self, url, **kwargs):
        """Record a POST."""
        return self._record("post", url, **kwargs)

    def put(self, url, **kwargs):
        """Record a PUT."""
        return self._record("put", url, **kwargs)

    def delete(self, url, **kwargs):
        """Record a DELETE."""
        return self._record("delete", url, **kwargs)

    def close(self):
        """Close the session."""


@pytest.fixture
def offline_login(monkeypatch):
    """Skip the network login BESConnection does on creation."""
    monkeypatch.setattr(BESConnection, "login", lambda self, *a, **k: True)


def make_conn(**kwargs):
    """Create an offline BESConnection with a recording session."""
    conn = BESConnection("user", "pass", "https://bigfix.example:52311", **kwargs)
    conn.session = FakeSession()
    return conn


# ---------- D4: default request timeout ----------


@pytest.mark.parametrize("method", ["get", "delete"])
def test_default_timeout_applied(offline_login, method):
    """Test that a connection level timeout is applied to requests."""
    conn = make_conn(timeout=7)
    getattr(conn, method)("help")
    assert conn.session.calls[-1][2]["timeout"] == 7


@pytest.mark.parametrize("method", ["post", "put"])
def test_default_timeout_applied_with_data(offline_login, method):
    """Test that a connection level timeout is applied to requests with data."""
    conn = make_conn(timeout=7)
    getattr(conn, method)("help", "<BES />")
    assert conn.session.calls[-1][2]["timeout"] == 7


def test_request_timeout_overrides_default(offline_login):
    """Test that a timeout passed to a request wins over the default."""
    conn = make_conn(timeout=7)
    conn.get("help", timeout=1)
    assert conn.session.calls[-1][2]["timeout"] == 1


# ---------- D1: import BES XML from a string ----------


def read_good_bes():
    """Read a known valid BES file as bytes."""
    with open(GOOD_BES_FILE, "rb") as bes_file:
        return bes_file.read()


@pytest.mark.parametrize("as_text", [False, True])
def test_import_bes_xml_to_site_posts_to_import(offline_login, as_text):
    """Test that BES XML (bytes or str) is posted to import/<site path>."""
    conn = make_conn()
    bes_xml = read_good_bes()
    if as_text:
        bes_xml = bes_xml.decode("utf-8")

    conn.import_bes_xml_to_site(bes_xml, "custom/Demo")

    method, url, kwargs = conn.session.calls[-1]
    assert method == "post"
    assert url == "https://bigfix.example:52311/api/import/custom/Demo"
    assert kwargs["data"] == read_good_bes()


def test_import_bes_xml_to_site_rejects_invalid(offline_login):
    """Test that invalid BES XML is not posted."""
    conn = make_conn()
    assert conn.import_bes_xml_to_site("<BES><Nope/></BES>", "custom/Demo") is None
    assert conn.session.calls == []


def test_import_bes_to_site_delegates_to_xml(offline_login, monkeypatch):
    """Test that importing a file reuses import_bes_xml_to_site."""
    conn = make_conn()
    received = []
    monkeypatch.setattr(
        conn,
        "import_bes_xml_to_site",
        lambda bes_xml, site_path=None: received.append((bes_xml, site_path)),
    )

    conn.import_bes_to_site(GOOD_BES_FILE, "custom/Demo")

    assert received == [(read_good_bes(), "custom/Demo")]


# ---------- D2: escape text for a relevance string literal ----------

# verified against the BigFix client QnA, see relevance_string_escape()
RELEVANCE_ESCAPE_CASES = [
    ('say "hi"', "say %22hi%22"),
    ("100%", "100%25"),
    ("%22", "%2522"),
    ("tab\tchar", "tab%09char"),
    ("line\nbreak", "line%0Abreak"),
    ("", ""),
    (r"back\slash & <tag>", r"back\slash & <tag>"),
    ("café", "café"),
]


@pytest.mark.parametrize("value, expected", RELEVANCE_ESCAPE_CASES)
def test_relevance_string_escape(value, expected):
    """Test escaping text to embed inside a relevance "string literal"."""
    assert besapi.besapi.relevance_string_escape(value) == expected


# ---------- besapi wide default timeout ----------


def test_besapi_default_timeout_value():
    """Test the besapi wide default timeout: (connect, read) seconds."""
    assert besapi.besapi.DEFAULT_TIMEOUT == (90, 600)


def test_connection_uses_besapi_default_timeout(offline_login):
    """Test that a connection without a timeout uses the besapi default."""
    conn = make_conn()
    conn.get("help")
    assert conn.timeout == (90, 600)
    assert conn.session.calls[-1][2]["timeout"] == (90, 600)
