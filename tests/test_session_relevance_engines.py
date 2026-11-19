"""Session relevance against both BigFix query engines (issue #30).

/api/query is answered by BigFix Explorer when it is reachable, otherwise by
Web Reports. Explorer decodes a form body once, Web Reports twice. 4.3.1
fixed Web Reports and broke Explorer, because the only test modelled Web
Reports. These tests simulate both engines from live captures
(tests/fixtures/query/<engine>/corpus.json) and run every public session
relevance method against each, so a change that breaks either engine fails
here.

No network is used: FakeQueryServer is a FakeSession route that answers the
way the captured engine did.
"""

import json
import os
import re
import sys
import urllib.parse
from xml.sax.saxutils import escape

import lxml.etree
import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_besapi_sdk import make_conn  # noqa: E402

import besapi  # noqa: E402
from besapi.besapi import BESConnection  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "query")
ENGINES = ("explorer", "web_reports")
QUERY_URL = "https://bigfix.example:52311/api/query"
JSON_BODY_TYPE = "application/json+relevance"


def load_corpus(engine):
    """Load the live captures for one engine, {"<relevance> | <encoding>": [status,
    body]}.
    """
    with open(os.path.join(FIXTURES, engine, "corpus.json"), encoding="utf-8") as f:
        return json.load(f)


CORPUS = {engine: load_corpus(engine) for engine in ENGINES}

# exact texts returned by a live server (see the fixtures):
NOT_ALLOWED = "This expression contained a character which is not allowed."
NO_ENDING_QUOTE = "A string constant had no ending quotation mark."
IMPROPER_PERCENT = "A string constant had an improper %-sequence."
INVALID_JSON_BODY = (
    '{"error":"Invalid JSON: Invalid required value type, expected type: \\"object\\""}'
)
NO_EXPLORER_BODY = (
    "Cannot perform relevance query evaluation at this time because there is "
    "no reachable BigFix Explorer instance collecting data from this Server."
)
# NOTE: not captured yet (lab step L2b), a placeholder for neither engine:
NEITHER_BODY = "PLACEHOLDER: no engine is available to evaluate session relevance"

# relevance containing this gets a genuine relevance error from the fake:
GENUINE_ERROR_MARKER = "bogus_operator"
GENUINE_ERROR = f'The operator "{GENUINE_ERROR_MARKER}" is not defined.'

# the header each engine adds to its answers, see headers.json in the
# fixtures. NOTE: a live server sends no Content-Type for query answers.
ENGINE_HEADER = "x-bes-relevance-engine-type"
ENGINE_TYPES = {
    "explorer": "BigFix Explorer",
    "web_reports": "BigFix Web Reports Server",
}
NO_HEADERS: dict[str, str] = {}


def encode(relevance, encoding):
    """Build a form body the way each capture in the fixtures was sent."""
    once = "relevance=" + urllib.parse.quote(relevance, safe=":")
    return {
        "once": once,  # besapi 4.2.0 session_relevance_xml
        "twice": {"relevance": urllib.parse.quote(relevance, safe=":+")},  # 4.3.1+
        "plain": {"relevance": relevance},
        "once_json": once + "&output=json",
    }[encoding]


def looks_like_json(text):
    """Return whether Explorer's form check would read the value as JSON.

    Its parser is lenient: control characters in strings and a leading BOM.
    NaN and Infinity are relevance errors on a live server, not JSON.
    """

    def reject(constant):
        raise ValueError(constant)

    try:
        json.loads(text.lstrip("﻿"), strict=False, parse_constant=reject)
    except ValueError:
        return False
    return True


def relevance_error(text):
    """Return the error a live engine gives for the evaluated text, or None.

    Inside a string constant `%XX` is relevance's own escape; anywhere else
    a `%` is not allowed.
    """
    in_string = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            in_string = not in_string
        elif char == "%":
            if not in_string:
                return NOT_ALLOWED
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 1 : i + 3]):
                return IMPROPER_PERCENT
            i += 2
        i += 1
    if in_string:
        return NO_ENDING_QUOTE
    if GENUINE_ERROR_MARKER in text:
        return GENUINE_ERROR
    return None


def parse_answers(body):
    """Parse a captured XML answer: (resource, error, items, plurality)."""
    query = lxml.etree.fromstring(body.encode("utf-8")).find("Query")
    items = []
    for child in query.find("Result"):
        if child.tag == "Tuple":
            items.append([(a.get("type"), a.text or "") for a in child])
        else:
            items.append((child.get("type"), child.text or ""))
    error = query.find("Error")
    plurality = query.findtext("Evaluation/Plurality")
    return (
        query.get("Resource"),
        None if error is None else error.text,
        items,
        plurality,
    )


def build_answers():
    """Map each evaluated text to its answer, from every correct XML capture.

    The answer depends only on the text evaluated, not on the engine or the
    encoding: test_fixture_answers_are_consistent checks this.
    """
    answers = {}
    for corpus in CORPUS.values():
        for status, body in corpus.values():
            if status != 200 or not body.startswith("<"):
                continue
            resource, error, items, plurality = parse_answers(body)
            if error is None:
                answers.setdefault(resource, (items, plurality))
    return answers


ANSWERS = build_answers()
AS_STRING_RE = re.compile(r"^\(it as string\) of \( (.*) \)$", re.S)


def json_value(answer_type, text):
    """Convert one XML answer to the value a JSON answer carries."""
    if answer_type == "integer":
        return int(text)
    if answer_type == "boolean":
        return text == "True"
    return text


class FakeQueryServer:
    """A BigFix /api/query that answers like Explorer or Web Reports did live.

    `engine` is who answers form queries: "explorer" (decodes once),
    "web_reports" (decodes twice) or "neither". It may be changed mid-test,
    such as when Explorer stops or starts. Answers for texts in the captures
    are the captured answers; any other text answers as a string equal to
    the evaluated text, so tests can assert what was evaluated.

    `headers` False is a server that doesn't say which engine answered, as
    older servers may not.
    """

    def __init__(self, engine, headers=True):
        self.engine = engine
        self.headers = headers
        self.form_requests = []  # evaluated text of each form query
        self.json_body_requests = []  # relevance of each JSON body query
        # optional: a function that rewrites the Resource of answers (tests
        # for engines that echo something else):
        self.resource_override = None
        # optional: what JSON body queries get instead, (text, status) or an
        # exception to raise, such as a proxy page or a network error:
        self.json_body_answer = None
        # optional: (text, status) for the next form query only:
        self.fail_next = None

    @property
    def requests(self):
        """Number of /api/query requests of any kind."""
        return len(self.form_requests) + len(self.json_body_requests)

    def engine_headers(self):
        """The headers of an answer from the engine answering now."""
        if not self.headers or self.engine not in ENGINE_TYPES:
            return NO_HEADERS
        return {ENGINE_HEADER: ENGINE_TYPES[self.engine]}

    def __call__(self, method, url, kwargs):
        assert method == "post" and url == QUERY_URL
        headers = kwargs.get("headers") or {}
        if headers.get("Content-Type") == JSON_BODY_TYPE:
            relevance = json.loads(kwargs["data"])["relevance"]
            self.json_body_requests.append(relevance)
            if isinstance(self.json_body_answer, Exception):
                raise self.json_body_answer
            if self.json_body_answer:
                return (*self.json_body_answer, NO_HEADERS)
            if self.engine != "explorer":
                # NOTE: no engine header, no engine answered:
                return NO_EXPLORER_BODY, 503, NO_HEADERS
            # NOTE: the JSON body has no form decoding and no JSON value check
            return self.evaluate(relevance, "json", check_json=False)

        data = kwargs["data"]
        body = data if isinstance(data, str) else urllib.parse.urlencode(data)
        form = urllib.parse.parse_qs(body)
        text = form["relevance"][0]
        if self.engine == "web_reports":
            # the second decode keeps `+`:
            text = urllib.parse.unquote(text)
        self.form_requests.append(text)
        if self.fail_next:
            # such as a proxy's error page, with no engine header:
            answer, self.fail_next = self.fail_next, None
            return (*answer, NO_HEADERS)
        if self.engine == "neither":
            return NEITHER_BODY, 503, NO_HEADERS
        return self.evaluate(text, form.get("output", ["xml"])[0])

    def evaluate(self, text, output, check_json=True):
        """Answer for the text the engine evaluates."""
        if check_json and self.engine == "explorer" and looks_like_json(text):
            return INVALID_JSON_BODY, 400, self.engine_headers()
        error = relevance_error(text)
        items, plurality = ([], None) if error else self.answer_for(text)
        if output == "json":
            return self.json_body(error, items, plurality), 200, self.engine_headers()
        resource = text
        if self.resource_override:
            resource = self.resource_override(text)
        return (
            self.xml_body(resource, error, items, plurality),
            200,
            self.engine_headers(),
        )

    @staticmethod
    def answer_for(text):
        """Look up the captured answer, else answer the text as a string."""
        if text in ANSWERS:
            return ANSWERS[text]
        # parentheses do not change an answer, such as `(1)` for `1`:
        if text.startswith("(") and text.endswith(")") and text[1:-1] in ANSWERS:
            return ANSWERS[text[1:-1]]
        match = AS_STRING_RE.match(text)
        if match:
            items, plurality = FakeQueryServer.answer_for(match.group(1))
            as_strings = []
            for item in items:
                if isinstance(item, list):
                    as_strings.append(("string", ", ".join(t for _, t in item)))
                else:
                    as_strings.append(("string", item[1]))
            return as_strings, plurality
        return [("string", text)], "Singular"

    @staticmethod
    def xml_body(resource, error, items, plurality):
        """Build an XML answer in the captured format."""
        attr = escape(resource, {'"': "&quot;"})
        if error:
            return (
                f'<BESAPI><Query Resource="{attr}"><Result></Result>'
                f"<Error>{escape(error)}</Error></Query></BESAPI>"
            )
        result = ""
        for item in items:
            if isinstance(item, list):
                result += "<Tuple>"
                result += "".join(
                    f'<Answer type="{t}">{escape(v)}</Answer>' for t, v in item
                )
                result += "</Tuple>"
            else:
                result += f'<Answer type="{item[0]}">{escape(item[1])}</Answer>'
        return (
            f'<BESAPI><Query Resource="{attr}"><Result>{result}</Result>'
            f"<Evaluation><Time>0.05ms</Time><Plurality>{plurality}</Plurality>"
            "</Evaluation></Query></BESAPI>"
        )

    def json_body(self, error, items, plurality):
        """Build a JSON answer, with the engine fingerprint.

        Explorer answers carry `count` and a fractional `evaltime_ms`, Web
        Reports answers have no `count` and a whole `evaltime_ms`.
        """
        if error:
            return json.dumps({"result": [], "error": error}, separators=(",", ":"))
        result = []
        for item in items:
            if isinstance(item, list):
                result.append([json_value(t, v) for t, v in item])
            else:
                result.append(json_value(*item))
        types = [
            (
                "( " + ", ".join(t for t, _ in item) + " )"
                if isinstance(item, list)
                else item[0]
            )
            for item in items
        ]
        answer = {"result": result}
        if self.engine == "explorer":
            answer["count"] = len(result)
        answer["plural"] = plurality == "Plural"
        answer["type"] = types[0] if types else "string"
        answer["evaltime_ms"] = 0.05 if self.engine == "explorer" else 0
        return json.dumps(answer, separators=(",", ":"), ensure_ascii=False)


def engine_conn(engine, headers=True, **kwargs):
    """Create an offline connection answered by a FakeQueryServer."""
    conn = make_conn(**kwargs)
    server = FakeQueryServer(engine, headers)
    conn.session.routes[QUERY_URL] = server
    return conn, server


# ---------- the fake must match the live captures ----------

TIME_RE = re.compile(r"\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}")


def normalize(body):
    """Remove what changes between runs: timings and `now` answers."""
    if body.startswith("<"):
        body = re.sub(r"<Time>[^<]*</Time>", "<Time/>", body)
        return TIME_RE.sub("NOW", body)
    try:
        answer = json.loads(body)
    except ValueError:
        return body
    if isinstance(answer, dict):
        answer.pop("evaltime_ms", None)
        answer["result"] = [
            TIME_RE.sub("NOW", v) if isinstance(v, str) else v
            for v in answer.get("result", [])
        ]
    return answer


CAPTURES = [(engine, key) for engine in ENGINES for key in CORPUS[engine]]


@pytest.mark.parametrize(("engine", "key"), CAPTURES)
def test_fake_query_server_matches_live_capture(engine, key):
    """Test that the fake answers every captured request as the live server did.

    The Resource attribute is the text the live engine evaluated, so this
    checks the fake decodes each body as that engine does.
    """
    relevance, encoding = key.rsplit(" | ", 1)
    status, body = CORPUS[engine][key]

    text, fake_status, _ = FakeQueryServer(engine)(
        "post", QUERY_URL, {"data": encode(relevance, encoding)}
    )

    assert fake_status == status
    assert normalize(text) == normalize(body)
    if encoding == "once_json" and status == 200 and "error" not in body:
        # the engine fingerprint, which besapi reads:
        evaltime = json.loads(body)["evaltime_ms"]
        assert isinstance(evaltime, float) == (engine == "explorer")
        assert ("count" in json.loads(body)) == (engine == "explorer")


def test_fixture_answers_are_consistent():
    """Test that an evaluated text has one answer across engines and encodings."""
    seen = {}
    for engine, key in CAPTURES:
        status, body = CORPUS[engine][key]
        if status != 200 or not body.startswith("<"):
            continue
        resource, error, items, plurality = parse_answers(normalize(body))
        if error is None:
            assert seen.setdefault(resource, (items, plurality)) == (
                items,
                plurality,
            ), key


def test_fixtures_cover_both_engines_and_encodings():
    """Test that each engine has every relevance in all four encodings."""
    for engine in ENGINES:
        keys = {key.rsplit(" | ", 1)[0] for key in CORPUS[engine]}
        assert len(keys) == 23
        assert len(CORPUS[engine]) == 23 * 4


def load_headers(engine):
    """Load the live captures of each engine's answer headers, headers.json."""
    with open(os.path.join(FIXTURES, engine, "headers.json"), encoding="utf-8") as f:
        return json.load(f)


HEADERS = {engine: load_headers(engine) for engine in ENGINES}


def twice(relevance):
    """A relevance value quoted once more, as for Web Reports."""
    return urllib.parse.quote(relevance, safe=":+")


# how each case in headers.json was sent: a form dict, or a JSON body
HEADER_CASES = {
    "form once ok": {"relevance": "number of bes sites"},
    "form once json ok": {"relevance": "1 + 1", "output": "json"},
    "form once error": {"relevance": GENUINE_ERROR_MARKER},
    "form once json error": {"relevance": GENUINE_ERROR_MARKER, "output": "json"},
    "form once %22": {"relevance": '("a%22b")'},
    "form once %2541": {"relevance": '("%2541")'},
    "form twice %22": {"relevance": twice('("a%22b")')},
    "form twice %2541 json": {"relevance": twice('("%2541")'), "output": "json"},
    "form json-shaped 1": {"relevance": "1"},
    "form json-shaped (1)": {"relevance": "(1)"},
    "json body ok": '("%2541")',
    "json body error": GENUINE_ERROR_MARKER,
}


@pytest.mark.parametrize("case", HEADER_CASES)
@pytest.mark.parametrize("engine", ENGINES)
def test_fake_query_server_matches_live_headers(engine, case):
    """Test the fake says which engine answered, as each live engine did.

    Every answer from an engine has its header, errors and HTTP 400 too.
    Only the 503 to a JSON body, which no engine answered, has none.
    """
    live = HEADERS[engine][case]
    sent = HEADER_CASES[case]
    if isinstance(sent, str):
        request = {
            "data": json.dumps({"relevance": sent}),
            "headers": {"Content-Type": JSON_BODY_TYPE},
        }
    else:
        request = {"data": sent}

    text, status, headers = FakeQueryServer(engine)("post", QUERY_URL, request)

    assert status == live["status"]
    assert headers.get(ENGINE_HEADER) == live["engine_type"]
    # a live server sends no Content-Type, so besapi can't rely on one:
    assert "content-type" not in {h.lower() for h in headers}
    if "body" not in live:
        return  # cut short in the capture
    if live["body"].startswith("<"):
        assert (
            parse_answers(normalize(text))[:3]
            == parse_answers(normalize(live["body"]))[:3]
        )
    else:
        assert normalize(text) == normalize(live["body"])


def test_header_fixtures_cover_every_case():
    """Test both engines' header captures have the same cases."""
    for engine in ENGINES:
        assert set(HEADERS[engine]) - {"GET query"} == set(HEADER_CASES)


def test_fake_query_server_json_body_needs_explorer():
    """Test the application/json+relevance answers, which need Explorer."""
    request = {
        "data": json.dumps({"relevance": '("%2541")'}),
        "headers": {"Content-Type": JSON_BODY_TYPE},
    }
    text, status, _ = FakeQueryServer("explorer")("post", QUERY_URL, request)
    assert status == 200
    assert json.loads(text)["result"] == ["%41"]
    for engine in ("web_reports", "neither"):
        assert FakeQueryServer(engine)("post", QUERY_URL, request)[:2] == (
            NO_EXPLORER_BODY,
            503,
        )


# ---------- the regression matrix: every method, both engines ----------

# the correct answers, from the live captures (Web Reports decoding the
# twice encoded body, Explorer the once encoded one, agree on every row).
# Columns: session_relevance_xml (parsed answers), _array, _string, _json
# (["result"]), _json_string. NOW stands for the time `now` answered.
# NOTE: _array returning <Nothing> for tuples, and _json_string printing
# tuples as Python lists, are existing behavior and not part of issue #30.
NOTHING = ["<Nothing> Nothing returned, but no error."]
EXPECTED = {
    "now": ([("time", "NOW")], ["NOW"], "NOW", ["NOW"], "NOW"),
    "number of bes sites": ([("integer", "6")], ["6"], "6", [6], "6"),
    '(1, "a")': (
        [[("integer", "1"), ("string", "a")]],
        NOTHING,
        "1, a",
        [[1, "a"]],
        "[1, 'a']",
    ),
    "1 + 1": ([("integer", "2")], ["2"], "2", [2], "2"),
    '("a+b")': ([("string", "a+b")], ["a+b"], "a+b", ["a+b"], "a+b"),
    '("a&b=c")': ([("string", "a&b=c")], ["a&b=c"], "a&b=c", ["a&b=c"], "a&b=c"),
    '("a b")': ([("string", "a b")], ["a b"], "a b", ["a b"], "a b"),
    '("é")': ([("string", "é")], ["é"], "é", ["é"], "é"),
    '((1, "a"); (2, "b"))': (
        [[("integer", "1"), ("string", "a")], [("integer", "2"), ("string", "b")]],
        NOTHING,
        "1, a\n2, b",
        [[1, "a"], [2, "b"]],
        "[1, 'a']\n[2, 'b']",
    ),
    'lengths of first matches (regex " +") of "        "': (
        [("integer", "8")],
        ["8"],
        "8",
        [8],
        "8",
    ),
    '("a%22b")': ([("string", 'a"b')], ['a"b'], 'a"b', ['a"b'], 'a"b'),
    '"100%25 of " & "5+5"': (
        [("string", "100% of 5+5")],
        ["100% of 5+5"],
        "100% of 5+5",
        ["100% of 5+5"],
        "100% of 5+5",
    ),
    '("x%22y")': ([("string", 'x"y')], ['x"y'], 'x"y', ['x"y'], 'x"y'),
    '("100%25 done")': (
        [("string", "100% done")],
        ["100% done"],
        "100% done",
        ["100% done"],
        "100% done",
    ),
    # the silent one: Web Reports answers `A` when this is encoded once
    '("%2541")': ([("string", "%41")], ["%41"], "%41", ["%41"], "%41"),
    '("%2522")': ([("string", "%22")], ["%22"], "%22", ["%22"], "%22"),
    '("%41")': ([("string", "A")], ["A"], "A", ["A"], "A"),
    '(it as string) of ( "a%22b" )': (
        [("string", 'a"b')],
        ['a"b'],
        'a"b',
        ['a"b'],
        'a"b',
    ),
    # valid JSON: Explorer answers HTTP 400 unless sent in parentheses
    '"now"': ([("string", "now")], ["now"], "now", ["now"], "now"),
    "1": ([("integer", "1")], ["1"], "1", [1], "1"),
    "true": ([("boolean", "True")], ["True"], "True", [True], "True"),
    "(true)": ([("boolean", "True")], ["True"], "True", [True], "True"),
    '("now")': ([("string", "now")], ["now"], "now", ["now"], "now"),
}
JSON_SHAPED = {'"now"', "1", "true"}
ENTRIES = ("xml", "array", "string", "json", "json_array", "json_string", "bescli")


def expected(entry, relevance):
    """The correct answer of one method for one relevance."""
    xml, array, string, json_result, json_string = EXPECTED[relevance]
    return {
        "xml": xml,
        "array": array,
        "string": string,
        "json": json_result,
        "json_array": json_result,
        "json_string": json_string,
        "bescli": f"Q: {relevance}\nA: \n{string}\n",
    }[entry]


def evaluated_as_asked(resource, relevance):
    """Whether the engine evaluated the relevance the caller asked for.

    Relevance that is valid JSON may be sent in parentheses, which does not
    change what it means.
    """
    return resource == relevance or (
        relevance in JSON_SHAPED and resource == f"({relevance})"
    )


def run_entry(conn, entry, relevance):
    """Run one public method, its answer with times replaced by NOW."""
    if entry == "xml":
        result = conn.session_relevance_xml(relevance)
        resource, error, items, _ = parse_answers(result.text)
        if error is not None or not evaluated_as_asked(resource, relevance):
            return ("Resource", resource, "Error", error)
        value = items
    elif entry == "array":
        value = conn.session_relevance_array(relevance)
    elif entry == "string":
        value = conn.session_relevance_string(relevance)
    elif entry == "json":
        value = conn.session_relevance_json(relevance)["result"]
    elif entry == "json_array":
        value = conn.session_relevance_json_array(relevance)
    elif entry == "json_string":
        value = conn.session_relevance_json_string(relevance)
    else:
        from bescli import bescli  # pylint: disable=import-outside-toplevel

        cli = bescli.BESCLInterface(stdout=__import__("io").StringIO())
        cli.feedback_to_output = True
        cli.bes_conn = conn
        cli.onecmd("query " + relevance)
        value = cli.stdout.getvalue()
    return json.loads(TIME_RE.sub("NOW", json.dumps(value)), object_hook=None)


def as_compared(value):
    """Compare tuples and lists alike, as JSON round trips them."""
    return json.loads(json.dumps(value))


def check_row(engine, relevance, entry, raise_for_status=False, headers=True):
    """Run a relevance twice on a fresh connection, return what was wrong.

    The first query on a connection may need one retry to learn which
    engine answers, the second must not. Relevance without `%` must never
    need more than one request.
    """
    conn, server = engine_conn(
        engine, headers=headers, raise_for_status=raise_for_status
    )
    problems = []
    for attempt in ("first", "again"):
        before = server.requests
        try:
            value = run_entry(conn, entry, relevance)
        except Exception as err:  # pylint: disable=broad-except
            problems.append(f"{attempt}: raised {err!r}")
            break
        if value != as_compared(expected(entry, relevance)):
            problems.append(f"{attempt}: answered {value!r}")
        used = server.requests - before
        budget = 2 if "%" in relevance and attempt == "first" else 1
        if used > budget:
            problems.append(f"{attempt}: {used} requests, at most {budget}")
    if "%" not in relevance and server.json_body_requests:
        problems.append("relevance without % sent an extra request")
    if entry in XML_ENTRIES and server.json_body_requests:
        problems.append("the XML path sent a JSON body query")
    return problems


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Keep bescli from reading a real config file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
@pytest.mark.parametrize("raise_for_status", [False, True])
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("relevance", list(EXPECTED))
@pytest.mark.parametrize("engine", ENGINES)
def test_regression_matrix(
    offline_login, isolated_home, engine, relevance, entry, raise_for_status, headers
):
    """Test every method answers correctly whichever engine answers.

    With the engine header, as live servers send, and without, as older
    servers may.
    """
    assert check_row(engine, relevance, entry, raise_for_status, headers) == []


def test_matrix_covers_every_capture():
    """Test the matrix runs every relevance captured from both engines."""
    for engine in ENGINES:
        assert {key.rsplit(" | ", 1)[0] for key in CORPUS[engine]} == set(EXPECTED)


# ---------- the matrix must catch the regressions that shipped ----------


def legacy_420_xml(self, relevance, **kwargs):
    """Besapi 4.2.0 session_relevance_xml: encoded once, wrong on Web Reports."""
    return self.post(
        "query",
        data=f"relevance={urllib.parse.quote(relevance, safe=':')}",
        **kwargs,
    )


def legacy_431_xml(self, relevance, **kwargs):
    """Besapi 4.3.1 to 4.4.6 session_relevance_xml: twice, wrong on Explorer."""
    return self.post(
        "query",
        data={"relevance": urllib.parse.quote(relevance, safe=":+")},
        **kwargs,
    )


def legacy_json(self, relevance, **kwargs):
    """Besapi 4.2.0 to 4.4.6 session_relevance_json: twice, wrong on Explorer."""
    rel_data = {"output": "json", "relevance": urllib.parse.quote(relevance, safe=":+")}
    result = self.post("query", data=rel_data, **kwargs)
    return json.loads(result.text)


def matrix_failures(engine):
    """Return the (relevance, method) rows of the matrix that fail."""
    return {
        (relevance, entry)
        for relevance in EXPECTED
        for entry in ENTRIES
        if check_row(engine, relevance, entry)
    }


XML_ENTRIES = {"xml", "array", "string", "bescli"}
JSON_ENTRIES = {"json", "json_array", "json_string"}


def test_matrix_catches_431_on_explorer(offline_login, isolated_home, monkeypatch):
    """Test the matrix fails 4.3.1 to 4.4.6 with Explorer, the bug in #30.

    Only `now` has nothing to quote, so it still worked, except through
    _string and bescli, which wrap it in `(it as string) of ( ... )`.
    """
    monkeypatch.setattr(BESConnection, "session_relevance_xml", legacy_431_xml)
    monkeypatch.setattr(BESConnection, "session_relevance_json", legacy_json)
    still_worked = {("now", entry) for entry in ENTRIES} - {
        ("now", "string"),
        ("now", "bescli"),
    }
    every_row = {(relevance, entry) for relevance in EXPECTED for entry in ENTRIES}
    assert matrix_failures("explorer") == every_row - still_worked


def test_matrix_passes_431_on_web_reports(offline_login, isolated_home, monkeypatch):
    """Test the matrix agrees 4.3.1 to 4.4.6 was right with Web Reports."""
    monkeypatch.setattr(BESConnection, "session_relevance_xml", legacy_431_xml)
    monkeypatch.setattr(BESConnection, "session_relevance_json", legacy_json)
    assert matrix_failures("web_reports") == set()


def test_matrix_catches_420_on_web_reports(offline_login, isolated_home, monkeypatch):
    """Test the matrix fails 4.2.0 with Web Reports, which 4.3.1 fixed.

    That includes the silent wrong answers, `A` for ("%2541"). ("%41")
    answers `A` either way, but its Resource shows Web Reports evaluated
    ("A"), so only session_relevance_xml, which checks it, fails there.
    """
    monkeypatch.setattr(BESConnection, "session_relevance_xml", legacy_420_xml)
    monkeypatch.setattr(BESConnection, "session_relevance_json", legacy_json)
    broken = {
        '("a%22b")',
        '"100%25 of " & "5+5"',
        '("x%22y")',
        '("100%25 done")',
        '("%2541")',
        '("%2522")',
        '(it as string) of ( "a%22b" )',
    }
    assert matrix_failures("web_reports") == {
        (relevance, entry) for relevance in broken for entry in XML_ENTRIES
    } | {('("%41")', "xml")}


# ---------- engines that change mid-session ----------

SWITCH_RELEVANCE = ['("%2541")', '("a%22b")', '("%41")', "number of bes sites", "true"]


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("relevance", SWITCH_RELEVANCE)
@pytest.mark.parametrize(
    ("before", "after"),
    [("explorer", "web_reports"), ("web_reports", "explorer")],
    ids=["explorer_stops", "explorer_starts"],
)
def test_engine_switch_mid_session(
    offline_login, isolated_home, before, after, relevance, entry, headers
):
    """Test the call after Explorer stops or starts is still right.

    Never a wrong answer: a wrong encoding is detected from the answer and
    retried once within the same call.
    """
    conn, server = engine_conn(before, headers=headers)
    want = as_compared(expected(entry, relevance))
    assert run_entry(conn, entry, relevance) == want
    server.engine = after
    requests_before = server.requests
    assert run_entry(conn, entry, relevance) == want
    # NOTE: relevance that is valid JSON is sent as is for Web Reports, so
    # Explorer starting costs it one resend too:
    retry_needed = "%" in relevance or relevance in JSON_SHAPED
    assert server.requests - requests_before <= (2 if retry_needed else 1)
    # and the call after that has learned the new engine:
    requests_before = server.requests
    assert run_entry(conn, entry, relevance) == want
    assert server.requests - requests_before == 1


def outcome(func):
    """What a call did: its value, or the type of exception it raised."""
    try:
        return ("value", func())
    except Exception as err:  # pylint: disable=broad-except
        return ("raised", type(err).__name__)


@pytest.mark.parametrize("raise_for_status", [False, True])
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("relevance", SWITCH_RELEVANCE)
def test_neither_engine_behaves_as_before(
    offline_login, isolated_home, monkeypatch, relevance, entry, raise_for_status
):
    """Test that with no engine answering, besapi fails as 4.4.6 did.

    No encoding can work then. besapi must not loop or raise anything new.
    """
    conn, server = engine_conn("neither", raise_for_status=raise_for_status)
    new = outcome(lambda: run_entry(conn, entry, relevance))
    assert server.requests <= (2 if "%" in relevance else 1)

    monkeypatch.setattr(BESConnection, "session_relevance_xml", legacy_431_xml)
    monkeypatch.setattr(BESConnection, "session_relevance_json", legacy_json)
    legacy_conn, _ = engine_conn("neither", raise_for_status=raise_for_status)
    assert new == outcome(lambda: run_entry(legacy_conn, entry, relevance))


# ---------- unit tests: the bug as reported ----------


def test_session_relevance_string_true_with_explorer(offline_login):
    """Test the reported symptom: every _string query failed with Explorer."""
    conn, _ = engine_conn("explorer")
    assert conn.session_relevance_string("true") == "True"


def test_session_relevance_json_with_explorer(offline_login):
    """Test _json with Explorer, broken in every release checked."""
    conn, _ = engine_conn("explorer")
    assert conn.session_relevance_json("number of bes sites")["result"] == [6]


def test_json_shaped_relevance_with_explorer(offline_login):
    """Test relevance that is valid JSON, which Explorer rejected with 400."""
    conn, server = engine_conn("explorer")
    result = conn.session_relevance_xml('"now"')
    assert result.request.status_code == 200
    assert parse_answers(result.text)[2] == [("string", "now")]
    assert server.form_requests == ['("now")']


# ---------- unit tests: requests sent ----------

NO_PERCENT = [r for r in EXPECTED if "%" not in r and r not in JSON_SHAPED]
PERCENT = [r for r in EXPECTED if "%" in r]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("relevance", NO_PERCENT)
def test_no_percent_is_one_request_encoded_once(offline_login, engine, relevance):
    """Test relevance without % is sent as 4.2.0 did, once, in one request."""
    conn, server = engine_conn(engine)
    conn.session_relevance_xml(relevance)
    conn.session_relevance_json(relevance)
    assert [call[2]["data"] for call in conn.session.calls] == [
        {"relevance": relevance},
        {"relevance": relevance, "output": "json"},
    ]
    assert server.form_requests == [relevance, relevance]


@pytest.mark.parametrize("relevance", PERCENT)
def test_percent_with_explorer_is_one_request(offline_login, relevance):
    """Test % relevance with Explorer: encoded once, no retry, no check."""
    conn, server = engine_conn("explorer")
    conn.session_relevance_xml(relevance)
    assert server.form_requests == [relevance]
    assert server.json_body_requests == []


@pytest.mark.parametrize("raise_for_status", [False, True])
def test_web_reports_is_learned_from_the_answer(offline_login, raise_for_status):
    """Test the fallback: once (wrong), then twice (right), then twice only."""
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    conn.session_relevance_xml('("a%22b")')
    # what Web Reports evaluated: once encoded, then twice encoded
    assert server.form_requests == ['("a"b")', '("a%22b")']
    conn.session_relevance_xml('("a%22b")')
    assert server.form_requests[2:] == ['("a%22b")']


@pytest.mark.parametrize("raise_for_status", [False, True])
def test_explorer_is_learned_back_from_the_answer(offline_login, raise_for_status):
    """Test Explorer starting while the connection assumes Web Reports."""
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    conn.session_relevance_xml('("a%22b")')
    server.engine = "explorer"
    server.form_requests.clear()
    assert parse_answers(conn.session_relevance_xml('("a%22b")').text)[2] == [
        ("string", 'a"b')
    ]
    # twice (not allowed), then once:
    assert server.form_requests == ["%28%22a%2522b%22%29", '("a%22b")']
    conn.session_relevance_xml('("a%22b")')
    assert server.form_requests[2:] == ['("a%22b")']


@pytest.mark.parametrize("relevance", ['("%2541")', '("%2522")'])
def test_silent_wrong_answer_is_detected_from_resource(offline_login, relevance):
    """Test Web Reports answering `A` for ("%2541") once is never returned.

    The answer has no error: only its Resource shows a different text was
    evaluated. The XML path never needs a check request for this.
    """
    conn, server = engine_conn("web_reports")
    assert conn.session_relevance_array(relevance) == EXPECTED[relevance][1]
    assert len(server.form_requests) == 2
    assert server.json_body_requests == []


# ---------- unit tests: genuine errors ----------

BROKEN = '("a%22b") ' + GENUINE_ERROR_MARKER


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_genuine_error_with_explorer(offline_login, headers):
    """Test a real relevance error with Explorer: one request, no retry.

    With the header, Explorer said it answered what was sent for it. Without,
    the Resource shows the relevance was evaluated as sent.
    """
    conn, server = engine_conn("explorer", headers=headers)
    result = conn.session_relevance_xml(BROKEN)
    assert parse_answers(result.text)[1] == GENUINE_ERROR
    assert server.form_requests == [BROKEN]
    # still assumes Explorer: the next query is sent once first
    conn.session_relevance_xml('("a%22b")')
    assert server.form_requests[1:] == ['("a%22b")']


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_genuine_error_raises_once(offline_login, headers):
    """Test raise_errors raises the error, after one request."""
    conn, server = engine_conn("explorer", headers=headers)
    with pytest.raises(ValueError, match=GENUINE_ERROR):
        conn.session_relevance_array(BROKEN, raise_errors=True)
    assert len(server.form_requests) == 1


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_genuine_error_with_web_reports(offline_login, headers):
    """Test a real relevance error once Web Reports was learned: one request."""
    conn, server = engine_conn("web_reports", headers=headers)
    conn.session_relevance_xml('("a%22b")')
    server.form_requests.clear()
    result = conn.session_relevance_xml(BROKEN)
    assert parse_answers(result.text)[1] == GENUINE_ERROR
    assert server.form_requests == [BROKEN]


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_genuine_error_on_a_fresh_web_reports_connection(offline_login, headers):
    """Test a real error while Web Reports isn't known yet: the wrong encoding.

    Sent once, Web Reports evaluates ("a"b") instead: the header, or the
    Resource, shows that. Sent twice, the error is genuine: returned, and
    Web Reports is learned from it.
    """
    conn, server = engine_conn("web_reports", headers=headers)
    result = conn.session_relevance_xml(BROKEN)
    assert parse_answers(result.text)[:2] == (BROKEN, GENUINE_ERROR)
    assert len(server.form_requests) == 2
    conn.session_relevance_xml('("a%22b")')
    assert server.form_requests[2:] == ['("a%22b")']


BROKEN_JSON = '("a%22b") ' + GENUINE_ERROR_MARKER


@pytest.mark.parametrize("engine", ENGINES)
def test_genuine_json_error_with_the_header_is_one_request(offline_login, engine):
    """Test a real error through _json once the engine is known: no retry."""
    conn, server = engine_conn(engine)
    conn.session_relevance_json("number of bes sites")
    server.form_requests.clear()
    assert conn.session_relevance_json(BROKEN_JSON)["error"] == GENUINE_ERROR
    assert len(server.form_requests) == 1


def test_genuine_json_error_without_the_header_is_retried(offline_login):
    """Test a real error through _json, no header: JSON has no Resource.

    So any error may be the encoding, and is retried once.
    """
    conn, server = engine_conn("explorer", headers=False)
    assert conn.session_relevance_json(BROKEN_JSON)["error"] == GENUINE_ERROR
    assert len(server.form_requests) == 2


def test_resource_mismatch_both_times_returns_first(offline_login, caplog):
    """Test an engine whose Resource never matches: no loop, a warning."""
    conn, server = engine_conn("explorer", headers=False)
    server.resource_override = lambda text: text + " (rewritten)"
    with caplog.at_level("WARNING", logger="besapi"):
        result = conn.session_relevance_xml('("a%22b")')
    assert parse_answers(result.text)[0] == '("a%22b") (rewritten)'
    assert len(server.form_requests) == 2
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "(rewritten)" in warnings[0].getMessage()


def test_resource_mismatch_without_percent_is_not_retried(offline_login):
    """Test that without % a different Resource can't be the encoding."""
    conn, server = engine_conn("explorer", headers=False)
    server.resource_override = lambda text: text + " (rewritten)"
    conn.session_relevance_xml("number of bes sites")
    assert len(server.form_requests) == 1


@pytest.mark.parametrize("relevance", ['("a&b=c%22")', '"%41"'])
def test_resource_compared_unescaped_after_parentheses(offline_login, relevance):
    """Test that &quot; / &amp; and the added parentheses don't cause a retry."""
    conn, server = engine_conn("explorer", headers=False)
    conn.session_relevance_xml(relevance)
    assert len(server.form_requests) == 1


@pytest.mark.parametrize("raise_for_status", [False, True])
def test_first_http_error_is_a_trigger(offline_login, raise_for_status):
    """Test an HTTP error on the first % request is retried, first error wins."""
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    server.fail_next = ("Internal error", 500)
    # the retry (twice encoded) answers correctly:
    assert conn.session_relevance_array('("a%22b")') == ['a"b']
    assert len(server.form_requests) == 2

    conn, server = engine_conn("explorer", raise_for_status=raise_for_status)
    server.fail_next = ("Internal error", 500)
    # the retry (twice encoded) fails too, so the first error is returned:
    if raise_for_status:
        with pytest.raises(besapi.besapi.requests.HTTPError, match="500"):
            conn.session_relevance_xml('("a%22b")')
    else:
        assert conn.session_relevance_xml('("a%22b")').request.status_code == 500
    assert len(server.form_requests) == 2


# ---------- unit tests: session_relevance_json and %25XX ----------


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_json_percent_25xx_with_explorer_is_one_request(offline_login, headers):
    """Test %25XX through _json with Explorer: one JSON body query, no form."""
    conn, server = engine_conn("explorer", headers=headers)
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.json_body_requests == ['("%2541")']
    assert server.form_requests == []


def test_json_percent_25xx_once_the_header_was_seen(offline_login):
    """Test %25XX through _json once an answer said who answers: the form.

    The header shows whether Web Reports answered a once encoded query,
    which it answers wrongly, so no JSON body query is needed.
    """
    conn, server = engine_conn("explorer")
    conn.session_relevance_json("number of bes sites")
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.form_requests == ["number of bes sites", '("%2541")']
    assert server.json_body_requests == []


def test_json_percent_25xx_once_the_header_was_seen_explorer_stops(offline_login):
    """Test the silent `A` from Web Reports is never returned, with the header.

    Explorer was learned, then stops: the once encoded answer says Web
    Reports answered it, so it is sent again twice encoded.
    """
    conn, server = engine_conn("explorer")
    conn.session_relevance_json("number of bes sites")
    server.engine = "web_reports"
    server.form_requests.clear()
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    # what Web Reports evaluated: ("%41"), answered `A`, then right:
    assert server.form_requests == ['("%41")', '("%2541")']
    assert server.json_body_requests == []


def test_json_percent_25xx_with_web_reports(offline_login):
    """Test %25XX through _json with Web Reports: 503, then twice encoded."""
    conn, server = engine_conn("web_reports")
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.json_body_requests == ['("%2541")']
    assert server.form_requests == ['("%2541")']
    # learned, so no JSON body query next time:
    conn.session_relevance_json('("%2541")')
    assert len(server.json_body_requests) == 1
    assert server.form_requests[1:] == ['("%2541")']


def test_json_percent_25xx_after_explorer_stops(offline_login):
    """Test %25XX through _json never answers `A` after Explorer stops.

    The JSON answer has no Resource to compare, so each %25XX query asks
    Explorer directly, which Web Reports can never answer.
    """
    conn, server = engine_conn("explorer")
    conn.session_relevance_json('("%2541")')
    server.engine = "web_reports"
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]


@pytest.mark.parametrize(
    "answer",
    [
        ("<html>Sign in</html>", 200),
        ('{"other": 1}', 200),
        ("", 400),
        ("", 401),
        ("", 404),
        ("", 500),
    ],
)
@pytest.mark.parametrize("raise_for_status", [False, True])
def test_json_body_not_answered_by_explorer(offline_login, answer, raise_for_status):
    """Test any JSON body answer but Explorer's falls back to twice encoded."""
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    server.json_body_answer = answer
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    conn.session_relevance_json('("%2541")')
    assert len(server.json_body_requests) == 1


def test_json_body_network_error(offline_login):
    """Test a network error on the JSON body query falls back to twice."""
    conn, server = engine_conn("web_reports")
    server.json_body_answer = besapi.besapi.requests.ConnectionError("reset")
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]


def test_json_body_network_error_does_not_mean_web_reports(offline_login):
    """Test a network error alone doesn't make besapi assume Web Reports.

    Without the header, the twice encoded fallback fails loudly with
    Explorer, and the next query asks Explorer again rather than encoding
    twice.
    """
    conn, server = engine_conn("explorer", headers=False)
    server.json_body_answer = besapi.besapi.requests.ConnectionError("reset")
    assert "error" in conn.session_relevance_json('("%2541")')
    server.json_body_answer = None
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert len(server.json_body_requests) == 2


def test_json_body_network_error_with_the_header(offline_login):
    """Test the twice encoded fallback after a network error, with the header.

    Explorer's answer says it answered, so the query is sent again for it.
    """
    conn, server = engine_conn("explorer")
    server.json_body_answer = besapi.besapi.requests.ConnectionError("reset")
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.form_requests == ["%28%22%252541%22%29", '("%2541")']


def test_json_percent_25xx_after_explorer_starts(offline_login):
    """Test %25XX through _json once Explorer is back: one retry, as JSON body."""
    conn, server = engine_conn("web_reports", headers=False)
    conn.session_relevance_json('("%2541")')
    server.engine = "explorer"
    server.form_requests.clear()
    server.json_body_requests.clear()
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert len(server.form_requests) == 1
    assert server.json_body_requests == ['("%2541")']


def test_json_percent_25xx_after_explorer_starts_with_the_header(offline_login):
    """Test %25XX through _json once Explorer is back, with the header."""
    conn, server = engine_conn("web_reports")
    conn.session_relevance_json('("%2541")')
    server.engine = "explorer"
    server.form_requests.clear()
    server.json_body_requests.clear()
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    # twice (not allowed), then once:
    assert server.form_requests == ["%28%22%252541%22%29", '("%2541")']
    assert server.json_body_requests == []


def test_json_percent_without_25xx_uses_the_form(offline_login):
    """Test other % relevance through _json: the error drives the fallback."""
    conn, server = engine_conn("web_reports")
    assert conn.session_relevance_json('("a%22b")')["result"] == ['a"b']
    assert server.form_requests == ['("a"b")', '("a%22b")']
    assert server.json_body_requests == []


def test_state_is_shared_by_xml_and_json(offline_login):
    """Test Web Reports learned by an XML query applies to a JSON one."""
    conn, server = engine_conn("web_reports")
    conn.session_relevance_xml('("%2541")')
    server.form_requests.clear()
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.form_requests == ['("%2541")']
    assert server.json_body_requests == []


def test_json_fingerprint_learns_web_reports(offline_login):
    """Test a JSON answer without `count` means Web Reports answered."""
    conn, server = engine_conn("web_reports", headers=False)
    conn.session_relevance_json("number of bes sites")
    conn.session_relevance_xml('("a%22b")')
    # sent twice encoded first, no retry:
    assert server.form_requests[1:] == ['("a%22b")']


def test_json_fingerprint_learns_explorer(offline_login):
    """Test a JSON answer with `count` means Explorer answered."""
    conn, server = engine_conn("web_reports", headers=False)
    conn.session_relevance_xml('("a%22b")')
    server.engine = "explorer"
    conn.session_relevance_json("number of bes sites")
    server.form_requests.clear()
    conn.session_relevance_xml('("a%22b")')
    assert server.form_requests == ['("a%22b")']


# ---------- unit tests: relevance that is valid JSON ----------


JSON_SHAPED_MORE = [
    '"now"',
    "1",
    "-1",
    "1.5",
    "true",
    "null",
    '"x" ',
    '"a\tb"',
    '{"a": 1}',
]


@pytest.mark.parametrize("relevance", JSON_SHAPED_MORE)
@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
@pytest.mark.parametrize("raise_for_status", [False, True])
def test_json_shaped_relevance_in_parentheses_for_explorer(
    offline_login, relevance, headers, raise_for_status
):
    """Test relevance that is valid JSON is sent in parentheses, for Explorer."""
    conn, server = engine_conn(
        "explorer", headers=headers, raise_for_status=raise_for_status
    )
    conn.session_relevance_xml(relevance)
    conn.session_relevance_json(relevance)
    assert server.form_requests == [f"({relevance})"] * 2


@pytest.mark.parametrize("relevance", JSON_SHAPED_MORE)
@pytest.mark.parametrize("raise_for_status", [False, True])
def test_json_shaped_relevance_unchanged_for_web_reports(
    offline_login, relevance, raise_for_status
):
    """Test relevance that is valid JSON is sent as is once Web Reports answers.

    Only Explorer needs the parentheses. A fresh connection assumes
    Explorer, so the first query has them; the header then says Web Reports
    answered, and the parentheses are harmless, so it isn't sent again.
    """
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    conn.session_relevance_xml(relevance)
    conn.session_relevance_json(relevance)
    conn.session_relevance_xml(relevance)
    assert server.form_requests == [f"({relevance})", relevance, relevance]


@pytest.mark.parametrize("raise_for_status", [False, True])
def test_json_shaped_relevance_after_explorer_starts(offline_login, raise_for_status):
    """Test Explorer's HTTP 400 to valid JSON, sent as is for Web Reports.

    Its header says Explorer answered, so it is sent again in parentheses.
    """
    conn, server = engine_conn("web_reports", raise_for_status=raise_for_status)
    conn.session_relevance_xml("number of bes sites")
    server.engine = "explorer"
    server.form_requests.clear()
    assert conn.session_relevance_array("1") == ["1"]
    assert server.form_requests == ["1", "(1)"]


@pytest.mark.parametrize("relevance", ['"a" & "b"', "01", "now", "NaN", "(1)"])
def test_not_json_relevance_unchanged(offline_login, relevance):
    """Test relevance that is not valid JSON is sent unchanged."""
    conn, server = engine_conn("explorer")
    conn.session_relevance_xml(relevance)
    assert server.form_requests == [relevance]


# ---------- unit tests: state per connection and root server ----------


def test_login_resets_the_state(monkeypatch):
    """Test that a real login forgets which engine answered."""
    from test_besapi_sdk import FakeSession  # pylint: disable=import-outside-toplevel

    # a real login, against a recording session:
    monkeypatch.setattr(besapi.besapi.requests, "Session", FakeSession)
    conn = BESConnection("user", "pass", "https://bigfix.example:52311")
    server = FakeQueryServer("web_reports")
    conn.session.routes[QUERY_URL] = server
    conn.session_relevance_xml('("a%22b")')
    assert len(server.form_requests) == 2

    conn.last_connected = None
    assert conn.login()
    conn.session_relevance_xml('("a%22b")')
    # learned again: once (wrong), then twice
    assert len(server.form_requests) == 4


def other_root_server(conn, engine, headers=True):
    """Point a connection at another root server, answered by `engine`."""
    other = FakeQueryServer(engine, headers)
    conn.rootserver = "https://other.example:52311"

    def answer(method, url, kwargs):
        return other(method, QUERY_URL, kwargs)

    conn.session.routes["https://other.example:52311/api/query"] = answer
    return other


def test_rootserver_change_keeps_the_state(offline_login):
    """Test the state isn't tied to the root server: no extra request.

    A wrong state only costs one retry, see the next test.
    """
    conn, _ = engine_conn("web_reports")
    conn.session_relevance_xml('("a%22b")')
    other = other_root_server(conn, "web_reports")
    conn.session_relevance_xml('("a%22b")')
    assert other.form_requests == ['("a%22b")']


@pytest.mark.parametrize("headers", [True, False], ids=["header", "no_header"])
def test_rootserver_change_to_another_engine(offline_login, headers):
    """Test a root server answered by another engine is learned in one call."""
    conn, _ = engine_conn("web_reports", headers=headers)
    conn.session_relevance_xml('("a%22b")')
    other = other_root_server(conn, "explorer", headers)
    assert conn.session_relevance_array('("a%22b")') == ['a"b']
    assert len(other.form_requests) == 2
    conn.session_relevance_array('("a%22b")')
    assert len(other.form_requests) == 3


def test_state_is_per_connection(offline_login):
    """Test two connections keep their own state, even when equal."""
    conn_wr, server_wr = engine_conn("web_reports")
    conn_ex, server_ex = engine_conn("explorer")
    assert conn_wr == conn_ex
    conn_wr.session_relevance_xml('("a%22b")')
    conn_ex.session_relevance_xml('("a%22b")')
    assert len(server_wr.form_requests) == 2
    assert server_ex.form_requests == ['("a%22b")']


# ---------- unit tests: the engine header ----------


def test_header_teaches_the_engine_without_percent(offline_login):
    """Test any answer says who answers, so the first % query is right."""
    conn, server = engine_conn("web_reports")
    conn.session_relevance_array("number of bes sites")
    assert conn.session_relevance_array('("a%22b")') == ['a"b']
    assert server.form_requests == ["number of bes sites", '("a%22b")']


@pytest.mark.parametrize("value", ["BigFix Something Else", "", "explorer"])
def test_unknown_header_value_is_no_header(offline_login, value):
    """Test an engine type besapi doesn't know is treated as no header."""
    conn, server = engine_conn("explorer")
    server.engine_headers = lambda: {ENGINE_HEADER: value}
    conn.session_relevance_json("number of bes sites")
    server.form_requests.clear()
    # no header to trust: %25XX through _json asks Explorer with a JSON body
    assert conn.session_relevance_json('("%2541")')["result"] == ["%41"]
    assert server.json_body_requests == ['("%2541")']


def test_header_engine_changing_again_is_not_retried_twice(offline_login):
    """Test the engine changing between the query and its retry: no loop."""
    conn, server = engine_conn("explorer")
    answered = []
    real_call = server.__call__

    def flapping(method, url, kwargs):
        answer = real_call(method, url, kwargs)
        answered.append(server.engine)
        server.engine = "explorer" if server.engine == "web_reports" else "web_reports"
        return answer

    server.engine = "web_reports"
    conn.session.routes[QUERY_URL] = flapping
    conn.session_relevance_xml('("a%22b")')
    assert answered == ["web_reports", "explorer"]
    assert len(server.form_requests) == 2


def test_header_on_an_http_error(offline_login):
    """Test the header is read from an HTTPError under raise_for_status."""
    conn, server = engine_conn("explorer", raise_for_status=True)
    conn.session_relevance_xml("number of bes sites")
    server.engine = "web_reports"
    conn.session_relevance_xml("number of bes sites")  # learns Web Reports
    server.engine = "explorer"
    server.form_requests.clear()
    # sent as is for Web Reports, Explorer answers HTTP 400 with its header:
    assert conn.session_relevance_string("1") == "1"


@pytest.mark.parametrize(
    "relevance", [b'("a%22b")', b"number of bes sites", '("é")'.encode()]
)
@pytest.mark.parametrize("engine", ENGINES)
def test_bytes_relevance(offline_login, engine, relevance):
    """Test relevance as bytes works as its UTF-8 text does, as in 4.4.6."""
    text = relevance.decode("utf-8")
    conn, server = engine_conn(engine)
    assert conn.session_relevance_array(relevance) == conn.session_relevance_array(text)
    assert conn.session_relevance_json(relevance) == conn.session_relevance_json(text)


def test_login_forgets_the_header(monkeypatch):
    """Test a real login forgets an engine learned from the header."""
    from test_besapi_sdk import FakeSession  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(besapi.besapi.requests, "Session", FakeSession)
    conn = BESConnection("user", "pass", "https://bigfix.example:52311")
    server = FakeQueryServer("explorer")
    conn.session.routes[QUERY_URL] = server
    conn.session_relevance_json("number of bes sites")
    conn.last_connected = None
    assert conn.login()
    # nothing is known again: %25XX through _json asks with a JSON body
    conn.session_relevance_json('("%2541")')
    assert server.json_body_requests == ['("%2541")']


# ---------- unit tests: request arguments ----------


def test_kwargs_reach_every_request(offline_login):
    """Test timeout and headers go to the query, the retry and the JSON body."""
    conn, server = engine_conn("web_reports", timeout=9)
    conn.session_relevance_xml('("a%22b")', timeout=7, headers={"X-Test": "1"})
    conn2, server2 = engine_conn("web_reports")
    conn2.session_relevance_json('("%2541")', timeout=7, headers={"X-Test": "1"})
    calls = conn.session.calls + conn2.session.calls
    assert len(calls) == 4
    for _, _, kwargs in calls:
        assert kwargs["timeout"] == 7
        assert kwargs["headers"]["X-Test"] == "1"
        # nothing new reaches requests:
        assert set(kwargs) <= {"data", "verify", "timeout", "headers"}
    # the JSON body query adds its content type, without changing the caller's:
    assert conn2.session.calls[0][2]["headers"]["Content-Type"] == JSON_BODY_TYPE
    assert "Content-Type" not in conn2.session.calls[1][2]["headers"]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\t"])
def test_resource_compared_as_xml_normalizes_whitespace(offline_login, newline):
    """Test multi-line % relevance isn't retried for its line breaks.

    An XML parser turns a line break or tab in an attribute into a space,
    so the Resource of multi-line relevance never equals it exactly.
    NOTE: whether a live server escapes them in Resource isn't captured.
    """
    conn, server = engine_conn("explorer", headers=False)
    relevance = f'("a%22b"){newline}& ("c")'
    conn.session_relevance_xml(relevance)
    assert len(server.form_requests) == 1


def test_header_answer_is_trusted_over_resource(offline_login):
    """Test that with the header, the Resource isn't needed: one request."""
    conn, server = engine_conn("explorer")
    server.resource_override = lambda text: text + " (rewritten)"
    conn.session_relevance_xml('("a%22b")')
    assert len(server.form_requests) == 1


def test_resource_check_reads_only_the_query_tag(offline_login, monkeypatch):
    """Test the check without the header doesn't parse the whole answer.

    Only the Query tag's Resource decides it. A full parse, when needed, is
    the lazy one of the result.
    """
    conn, server = engine_conn("explorer", headers=False)
    parses = []
    real_fromstring = besapi.besapi.lxml.etree.fromstring

    def counting_fromstring(*args, **kwargs):
        parses.append(args)
        return real_fromstring(*args, **kwargs)

    monkeypatch.setattr(besapi.besapi.lxml.etree, "fromstring", counting_fromstring)
    conn.session_relevance_xml('("%2541")')
    assert len(server.form_requests) == 1
    assert parses == []


def test_answer_without_query_is_retried_once(offline_login):
    """Test a % query answered by XML with no Query: one retry, then returned."""
    conn = make_conn()
    conn.session.response_text = "<BESAPI />"
    result = conn.session_relevance_xml('("a%22b")')
    assert result.text == "<BESAPI />"
    assert len(conn.session.calls) == 2
