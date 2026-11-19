"""LIVE tests: GET /api/query?relevance=... on a real BigFix server.

These are skipped unless BESAPI_LIVE_TESTS=1 is set. They use whatever
credentials besapi finds on this machine (BES_* env vars, then the besapi
config file), and only run read only session relevance.

besapi sends session relevance as a form POST, so these pin down the GET
form for anyone building the URL themselves, such as with
`BESConnection.get("query?relevance=...")`. Run them with BigFix Explorer
running and stopped:

- the root server answers some requests itself, with no engine header: 400
  for a missing, empty or repeated `relevance`, 414 for a request target
  over 32755 bytes (a request line over 32768).
- Explorer decodes the value once, like a form body, and answers 400
  "Invalid JSON" for a value that is valid JSON (`1`, `true`, `"a b"`).
- Web Reports (Explorer stopped) decodes it twice, `+` being a space only in
  the first decode. So relevance with `%` in it must be percent-encoded
  twice: `("%2541")` encoded once silently answers `A`.

- an `output` other than `json` or `xml` is answered by Web Reports, even
  while Explorer is running.

NOTE: what wasn't captured with Explorer answering skips then.

    BESAPI_LIVE_TESTS=1 python -m pytest -s tests/test_live_query_get.py
"""

import json
import os
import re
import sys
import urllib.parse

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

import besapi.plugin_utilities  # noqa: E402

EXPLORER = besapi.besapi.RELEVANCE_ENGINE_EXPLORER
WEB_REPORTS = besapi.besapi.RELEVANCE_ENGINE_WEB_REPORTS

# the longest request target (path and query) the root server accepts:
MAX_REQUEST_TARGET = 32755
# the longest string constant relevance accepts:
MAX_STRING_CONSTANT = 2048
BAD_PARAMETERS = "Bad request parameters"
NO_ENDING_QUOTE = "A string constant had no ending quotation mark."
NOT_ALLOWED = "This expression contained a character which is not allowed."
VERY_LONG_STRING = "This expression has a very long string."
# Explorer's answer to a value that is valid JSON:
INVALID_JSON = ("http", 400)


def quote(text):
    """Percent-encode everything but letters, digits and `_.-~`."""
    return urllib.parse.quote(text, safe="")


def get(conn, query, **kwargs):
    """GET /api/query?<query>, the query string sent exactly as given.

    NOTE: requests leaves a fully percent-encoded query string as it is.
    """
    return conn.get("query?" + query, **kwargs).request


def answer(response):
    """Return the JSON answer's result, ("error", message) or ("http", status)."""
    if response.status_code != 200:
        return ("http", response.status_code)
    # NOTE: decoded as UTF-8, a query answer has no Content-Type charset
    body = json.loads(response.content.decode("utf-8"))
    if "error" in body:
        return ("error", body["error"])
    return body["result"]


def expected_for(engine, expected):
    """Return the expected value for the engine answering, or skip.

    `expected` is the same for both engines, or {engine: value}, where a
    value of None is not captured yet.
    """
    if not isinstance(expected, dict):
        return expected
    if expected.get(engine) is None:
        pytest.skip(f"not captured yet with {engine} answering")
    return expected[engine]


@pytest.fixture(scope="module")
def live_conn():
    """A connection to the real server, which must answer correctly."""
    conn = besapi.plugin_utilities.get_besapi_connection(None)
    if not conn or not conn.login():
        pytest.skip("no BigFix server configured on this machine, or login failed")
    # NOTE: in parentheses, Explorer rejects a bare `1` as JSON
    response = get(conn, "relevance=" + quote("(1)") + "&output=json")
    assert response.status_code == 200, response.text
    assert answer(response) == [1]
    return conn


@pytest.fixture(scope="module")
def engine(live_conn):
    """Which engine answers /api/query now, from the answer's header."""
    response = get(live_conn, "relevance=" + quote("(1)"))
    answered_by = besapi.besapi._relevance_engine_answering(response)
    assert answered_by in (EXPLORER, WEB_REPORTS), response.headers
    print(f"\nGET /api/query is answered by: {answered_by}")
    return answered_by


@pytest.fixture(scope="module")
def web_reports(live_conn, engine):
    """Skip unless Web Reports answers, the only engine this was captured with."""
    if engine != WEB_REPORTS:
        pytest.skip(f"only captured with Web Reports answering, not {engine}")
    return live_conn


@pytest.mark.parametrize(
    "query, body",
    [
        ("output=json", BAD_PARAMETERS),
        ("", BAD_PARAMETERS),
        ("relevance=", BAD_PARAMETERS),
        ("relevance=&output=json", BAD_PARAMETERS),
        # the parameter name is case sensitive:
        ("Relevance=1", BAD_PARAMETERS),
        (
            "relevance=1&relevance=2",
            'More than one value exists for parameter "relevance" when only one was expected.',
        ),
    ],
)
def test_live_get_bad_parameters(live_conn, query, body):
    """Test the root server answers a bad query string itself with 400."""
    response = get(live_conn, query)

    assert response.status_code == 400
    assert response.text == body
    assert besapi.besapi.RELEVANCE_ENGINE_HEADER not in response.headers


def request_target_query(conn, target_length):
    """A query string making the request target `target_length` bytes long.

    `xxx...` is a relevance error, which is fine: only the length counts.
    """
    path = urllib.parse.urlsplit(conn.url("query")).path + "?relevance="
    return "relevance=" + "x" * (target_length - len(path))


def test_live_get_request_target_too_long(live_conn):
    """Test a request target over 32755 bytes gets 414 from the root server."""
    response = get(live_conn, request_target_query(live_conn, MAX_REQUEST_TARGET + 1))

    assert response.status_code == 414
    assert json.loads(response.text) == {"error": "Request-URI Too Long"}
    assert besapi.besapi.RELEVANCE_ENGINE_HEADER not in response.headers


def test_live_get_request_target_longest(web_reports):
    """Test a request target of exactly 32755 bytes is evaluated.

    NOTE: Web Reports only: Explorer stopped answering during the probe that
    sent requests this long, so it isn't run against Explorer.
    """
    response = get(web_reports, request_target_query(web_reports, MAX_REQUEST_TARGET))

    assert response.status_code == 200
    assert besapi.besapi.RELEVANCE_ENGINE_HEADER in response.headers


@pytest.mark.parametrize(
    "query, expected",
    [
        # no `%`:
        ("relevance=" + quote('("a b")'), ["a b"]),
        ("relevance=" + quote('("a+b")'), ["a+b"]),
        (
            "relevance=" + quote(quote('("a b")')),
            {WEB_REPORTS: ["a b"], EXPLORER: ("error", NOT_ALLOWED)},
        ),
        (
            "relevance=" + quote(quote('("a+b")')),
            {WEB_REPORTS: ["a+b"], EXPLORER: ("error", NOT_ALLOWED)},
        ),
        # a value that is valid JSON:
        ("relevance=" + quote('"a b"'), {WEB_REPORTS: ["a b"], EXPLORER: INVALID_JSON}),
        (
            "relevance=" + urllib.parse.quote_plus('"a b"'),
            {WEB_REPORTS: ["a b"], EXPLORER: INVALID_JSON},
        ),
        (
            "relevance=" + quote('"café"'),
            {WEB_REPORTS: ["café"], EXPLORER: INVALID_JSON},
        ),
        ("relevance=true", {WEB_REPORTS: [True], EXPLORER: INVALID_JSON}),
        # `%`: encoded once for Explorer, twice for Web Reports
        (
            "relevance=" + quote('("a%22b")'),
            {WEB_REPORTS: ("error", NO_ENDING_QUOTE), EXPLORER: ['a"b']},
        ),
        (
            "relevance=" + quote(quote('("a%22b")')),
            {WEB_REPORTS: ['a"b'], EXPLORER: ("error", NOT_ALLOWED)},
        ),
        # NOTE: silently wrong with Web Reports, evaluated as ("%41"):
        ("relevance=" + quote('("%2541")'), {WEB_REPORTS: ["A"], EXPLORER: ["%41"]}),
        (
            "relevance=" + quote(quote('("%2541")')),
            {WEB_REPORTS: ["%41"], EXPLORER: ("error", NOT_ALLOWED)},
        ),
        # a raw `+` in the URL is a space, `%2B` is a plus:
        ("relevance=1+%2B+1", [2]),
        ("relevance=1%2B1", [2]),
        ("relevance=(%22a+b%22)", ["a b"]),
        (
            "relevance=(%2522a%252Bb%2522)",
            {WEB_REPORTS: ["a+b"], EXPLORER: ("error", NOT_ALLOWED)},
        ),
    ],
)
def test_live_get_decoding(live_conn, engine, query, expected):
    """Test how many times each engine decodes the GET value."""
    expected = expected_for(engine, expected)

    response = get(live_conn, query + "&output=json")

    assert answer(response) == expected


@pytest.mark.parametrize(
    "output, is_json",
    [
        ("&output=json", True),
        ("&output=Json", True),
        # Web Reports' value is case sensitive, Explorer's isn't:
        ("&output=JSON", {WEB_REPORTS: False, EXPLORER: True}),
        ("&output=xml", False),
        ("&output=XML", False),
        ("&output=", False),
        ("", False),
    ],
)
def test_live_get_output(live_conn, engine, output, is_json):
    """Test which `output` values get a JSON answer, the others XML."""
    is_json = expected_for(engine, is_json)

    response = get(live_conn, "relevance=" + quote("(1)") + output)

    assert response.status_code == 200
    assert response.text.startswith("{") == is_json
    assert besapi.besapi._relevance_engine_answering(response) == engine


def test_live_get_output_first(live_conn):
    """Test the order of the parameters doesn't matter."""
    response = get(live_conn, "output=json&relevance=" + quote("(1)"))

    assert answer(response) == [1]


@pytest.mark.parametrize("output", ["foo", "jsonx", "csv"])
def test_live_get_unknown_output_goes_to_web_reports(live_conn, output):
    """Test another `output` value is answered by Web Reports, in XML, even
    while Explorer is running.

    A form POST does the same.
    """
    response = get(live_conn, "relevance=" + quote("(1)") + "&output=" + output)

    assert response.status_code == 200
    assert response.text.startswith("<?xml")
    assert besapi.besapi._relevance_engine_answering(response) == WEB_REPORTS


def test_live_get_ignores_accept(live_conn):
    """Test an Accept header doesn't change the answer, still XML."""
    response = get(
        live_conn, "relevance=" + quote("(1)"), headers={"Accept": "application/json"}
    )

    assert response.text.startswith("<?xml")


def test_live_get_string_constant_limit(live_conn):
    """Test a string constant has at most 2048 characters, not the expression."""

    def length_of(constant):
        relevance = f"length of {constant}"
        return answer(get(live_conn, f"relevance={quote(relevance)}&output=json"))

    longest = '"' + "x" * MAX_STRING_CONSTANT + '"'
    too_long = '"' + "x" * (MAX_STRING_CONSTANT + 1) + '"'
    two_constants = '("' + "x" * 1500 + '" & "' + "x" * 1500 + '")'

    assert length_of(longest) == [MAX_STRING_CONSTANT]
    assert length_of(too_long) == ("error", VERY_LONG_STRING)
    assert length_of(two_constants) == [3000]


@pytest.mark.parametrize(
    "query, has_resource",
    [
        ("relevance=" + quote("number of bes sites"), True),
        ("relevance=" + quote("number of bes sites\n"), False),
        # the text evaluated has the control character itself:
        ("relevance=" + quote('("a\x01b")'), False),
    ],
)
def test_live_get_resource(live_conn, engine, query, has_resource):
    """Test the XML Resource is left out when the text evaluated has a line
    break or a control character.
    """
    has_resource = expected_for(engine, has_resource)

    response = get(live_conn, query)

    assert response.status_code == 200
    assert (b"<Query Resource=" in response.content) == has_resource


def test_live_get_control_character(live_conn, engine):
    """Test a control character in an XML answer comes after a backslash.

    That makes the XML invalid (issue #38), from both engines. The JSON
    answer is right.
    """
    relevance = quote('("a%01b")')
    query = "relevance=" + (quote(relevance) if engine == WEB_REPORTS else relevance)

    xml = get(live_conn, query)
    json_answer = get(live_conn, query + "&output=json")

    assert re.search(rb"<Answer[^>]*>a\\\x01b</Answer>", xml.content)
    assert answer(json_answer) == ["a\x01b"]
