"""GET /api/query?relevance=... as each engine answered it live.

besapi sends session relevance as a form POST, but a GET query string is the
same `application/x-www-form-urlencoded` format as a form body, so these
captures (tests/fixtures/query/<engine>/get.json) check the engine models
FakeQueryServer uses for every session relevance test:

- BigFix Explorer decodes the value once, and rejects a value that is valid
  JSON (`1`, `true`, `"text"`) with HTTP 400.
- Web Reports (Explorer stopped) decodes it twice, `+` being a space only in
  the first decode.

The captures' `query` is exactly what was sent after `?`.
"""

import json
import os
import re
import sys
from xml.sax.saxutils import unescape

import pytest
import requests

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_session_relevance_engines import (  # noqa: E402
    ENGINE_HEADER,
    ENGINE_TYPES,
    ENGINES,
    FIXTURES,
    QUERY_URL,
    FakeQueryServer,
)

import besapi  # noqa: E402


def load_get(engine):
    """Load one engine's GET captures, {"<case>|<encoding>|<output>": capture}."""
    with open(os.path.join(FIXTURES, engine, "get.json"), encoding="utf-8") as f:
        return json.load(f)


GET = {engine: load_get(engine) for engine in ENGINES}

RESOURCE_RE = re.compile(r'<Query Resource="([^"]*)"')
# answered by the root server itself, before any engine:
ROOT_SERVER_STATUSES = (400, 414)
RELEVANCE_ENGINES = {
    "explorer": besapi.besapi.RELEVANCE_ENGINE_EXPLORER,
    "web_reports": besapi.besapi.RELEVANCE_ENGINE_WEB_REPORTS,
}


def resource(body):
    """Return the text the engine evaluated, from an XML answer, or None."""
    match = RESOURCE_RE.search(body)
    return unescape(match.group(1), {"&quot;": '"'}) if match else None


def answered_by_engine(engine):
    """Return the cases an engine answered, not the root server itself."""
    return sorted(case for case, live in GET[engine].items() if live["engine_type"])


EVALUATED = {
    engine: {
        case: resource(live["body"])
        for case, live in GET[engine].items()
        if live["status"] == 200 and resource(live["body"]) is not None
    }
    for engine in ENGINES
}


@pytest.mark.parametrize("engine", ENGINES)
def test_get_captures_cover_encodings_and_outputs(engine):
    """Test the captures have each encoding, both outputs, and the 400s/414."""
    assert len(EVALUATED[engine]) >= 50
    for encoding in ("once", "twice", "plus"):
        for output in ("xml", "json"):
            assert any(c.endswith(f"|{encoding}|{output}") for c in GET[engine])
    statuses = {live["status"] for live in GET[engine].values()}
    assert set(ROOT_SERVER_STATUSES) <= statuses


@pytest.mark.parametrize(
    "engine, case",
    [(engine, case) for engine in ENGINES for case in sorted(EVALUATED[engine])],
)
def test_fake_decodes_get_query_strings_like_live(engine, case):
    """Test the fake evaluates what the live engine did for each query string.

    Includes Web Reports' silent case: `("%2541")` sent encoded once is
    evaluated as `("%41")`, which answers `A`.

    NOTE: requests re-quotes a query string with an invalid %-sequence (`%zz`,
    a lone `%`) whole, `%` as `%25`. The captures have what was really sent.
    """
    server = FakeQueryServer(engine)

    server("post", QUERY_URL, {"data": GET[engine][case]["query"]})

    assert server.form_requests == [EVALUATED[engine][case]]


@pytest.mark.parametrize(
    "engine, case",
    [(engine, case) for engine in ENGINES for case in answered_by_engine(engine)],
)
def test_fake_answers_get_query_strings_with_the_live_status(engine, case):
    """Test the fake gives each value the live status: Explorer's 400 for a
    value that is valid JSON, 200 otherwise.
    """
    live = GET[engine][case]
    if live["query"] is None:
        pytest.skip("too long to keep in the captures")

    _text, status, headers = FakeQueryServer(engine)(
        "post", QUERY_URL, {"data": live["query"]}
    )

    assert status == live["status"]
    assert headers[ENGINE_HEADER] == live["engine_type"]


@pytest.mark.parametrize(
    "engine, case",
    [(engine, case) for engine in ENGINES for case in sorted(GET[engine])],
)
def test_engine_of_each_get_answer(engine, case):
    """Test besapi reads which engine answered from each live GET answer.

    The root server's own 400 and 414 answers have no engine header. Only
    Explorer's JSON answers have `count`, the fingerprint besapi falls back
    on, and not with an error.
    """
    live = GET[engine][case]
    response = requests.Response()
    response.status_code = live["status"]
    if live["engine_type"]:
        response.headers[ENGINE_HEADER] = live["engine_type"]

    answered_by = besapi.besapi._relevance_engine_answering(response)

    if live["engine_type"] is None:
        assert live["status"] in ROOT_SERVER_STATUSES
        assert answered_by is None
    else:
        assert live["engine_type"] == ENGINE_TYPES[engine]
        assert answered_by == RELEVANCE_ENGINES[engine]
    if live["status"] == 200 and live["body"].startswith("{"):
        answer = json.loads(live["body"])
        assert ("count" in answer) == (engine == "explorer" and "error" not in answer)
