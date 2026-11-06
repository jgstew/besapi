"""Tests for besapi.besapi additions that help plugins.

No network is used: BESConnection.login is patched out and the HTTP session
is replaced by a stub that records each request.
"""

import inspect
import json
import logging
import os
import sys

import lxml.etree
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

    def __init__(self, url, text="<BESAPI />", status_code=200):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        """Raise like requests does for 4xx / 5xx responses."""
        if self.status_code >= 400:
            raise besapi.besapi.requests.HTTPError(
                f"{self.status_code} Error for url: {self.url}", response=self
            )


class FakeCookies:
    """Minimal stand-in for requests cookies."""

    def clear(self):
        """Clear cookies."""


# keyword arguments requests.Session.request accepts (besides method / url):
REQUESTS_KWARGS = set(
    inspect.signature(besapi.besapi.requests.Session.request).parameters
) - {"self", "method", "url"}


class FakeSession:
    """Records every request instead of sending it."""

    def __init__(self):
        self.calls = []
        self.cookies = FakeCookies()
        self.auth = None
        self.closed = False
        # what every recorded request responds with:
        self.response_text = "<BESAPI />"
        self.response_status = 200
        # per URL responses, {url: (text, status)}, override the above:
        self.routes = {}
        self.mounted = []

    def _record(self, method, url, **kwargs):
        # like requests.Session.request, reject arguments it does not accept:
        unknown = set(kwargs) - REQUESTS_KWARGS
        if unknown:
            raise TypeError(f"unexpected keyword arguments for requests: {unknown}")
        self.calls.append((method, url, kwargs))
        text, status = self.routes.get(url, (self.response_text, self.response_status))
        return FakeResponse(url, text, status)

    def mount(self, prefix, adapter):
        """Record a mounted transport adapter."""
        self.mounted.append((prefix, adapter))

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
        self.closed = True


# NOTE: the offline_login fixture is in conftest.py


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


# ---------- E1: BESConnection works as a context manager ----------


def test_connection_context_manager_returns_self_and_logs_out(monkeypatch):
    """Test `with BESConnection(...) as conn` logs in, yields itself, logs out."""
    logins = []
    monkeypatch.setattr(
        BESConnection, "login", lambda self, *a, **k: logins.append(1) or True
    )
    conn = make_conn()
    logins.clear()

    with conn as entered:
        assert entered is conn
        assert logins == [1]
        assert conn.session.closed is False

    assert conn.session.closed is True


# ---------- E2: create_group_from_file returns the new group ----------


GROUP_BES = b"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<ComputerGroup>
		<Title>My Test Group</Title>
		<Domain>BESC</Domain>
		<JoinByIntersection>false</JoinByIntersection>
		<SearchComponentRelevance Comparison="IsTrue"><Relevance>true</Relevance></SearchComponentRelevance>
	</ComputerGroup>
</BES>
"""


def test_create_group_from_file_returns_created_group(
    offline_login, monkeypatch, tmp_path
):
    """Test that the group is looked up by (name, site) after creating it."""
    conn = make_conn()
    group_file = tmp_path / "group.bes"
    group_file.write_bytes(GROUP_BES)
    lookups = []

    def fake_get_computergroup(group_name, site_path=None):
        lookups.append((group_name, site_path))
        # not found before creation, found after:
        if len(lookups) == 1:
            return None
        return f"GROUP:{group_name}@{site_path}"

    monkeypatch.setattr(conn, "get_computergroup", fake_get_computergroup)

    result = conn.create_group_from_file(str(group_file), "custom/Demo")

    assert lookups == [("My Test Group", "custom/Demo")] * 2
    assert result == "GROUP:My Test Group@custom/Demo"


# ---------- E3: upload reports the missing file clearly ----------


def test_upload_missing_file_names_the_path(offline_login, tmp_path, caplog):
    """Test that uploading a missing file raises and logs with the path."""
    conn = make_conn()
    missing = str(tmp_path / "missing_file.bin")

    with pytest.raises(FileNotFoundError, match="missing_file.bin"):
        conn.upload(missing)

    assert any(
        "missing_file.bin" in r.getMessage() and "not readable" in r.getMessage()
        for r in caplog.records
    )


# ---------- E4: session relevance uses the connection timeout ----------


def test_session_relevance_json_uses_timeout(offline_login):
    """Test that JSON session relevance queries apply the default timeout."""
    conn = make_conn(timeout=7)
    conn.session.response_text = '{"result": [1]}'
    conn.session_relevance_json("1")
    assert conn.session.calls[-1][2]["timeout"] == 7


def test_session_relevance_xml_uses_timeout(offline_login):
    """Test that XML session relevance queries apply the default timeout."""
    conn = make_conn(timeout=7)
    conn.session_relevance_xml("1")
    assert conn.session.calls[-1][2]["timeout"] == 7


# ---------- E5: generated XML escapes text ----------

UNSAFE_TEXT = 'Fish & Chips <b>"quoted"</b>'


def test_dashboard_variable_value_is_escaped(offline_login):
    """Test that dashboard variable values with XML characters stay intact."""
    conn = make_conn()
    conn.set_dashboard_variable_value("Dash.ojo", "my_var", UNSAFE_TEXT)

    posted = conn.session.calls[-1][2]["data"]
    tree = lxml.etree.fromstring(posted.encode("utf-8"))
    assert tree.findtext("DashboardData/Value") == UNSAFE_TEXT


def test_target_xml_computer_names_are_escaped():
    """Test that computer names with XML characters stay intact."""
    names = ["pc&1", "<pc2>"]
    target_xml = besapi.besapi.get_target_xml(names)

    tree = lxml.etree.fromstring(f"<Target>{target_xml}</Target>")
    assert [e.text for e in tree.findall("ComputerName")] == names


TASK_WITH_UNSAFE_TEXT = b"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<Task>
		<Title>Fish &amp; Chips &lt;test&gt;</Title>
		<Description>test</Description>
		<Relevance>true</Relevance>
		<Category></Category>
		<Source>Internal</Source>
		<SourceID></SourceID>
		<SourceReleaseDate>2024-01-01</SourceReleaseDate>
		<SourceSeverity></SourceSeverity>
		<CVENames></CVENames>
		<SANSID></SANSID>
		<Domain>BESC</Domain>
		<DefaultAction ID="Action1">
			<Description><PreLink>Click </PreLink><Link>here</Link><PostLink> to deploy.</PostLink></Description>
			<ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[// before ]]]]><![CDATA[> after]]></ActionScript>
		</DefaultAction>
	</Task>
</BES>
"""


def test_action_xml_from_bes_file_escapes_title_and_cdata(tmp_path):
    """Test that the title and an actionscript containing ]]> survive."""
    bes_file = tmp_path / "task.bes"
    bes_file.write_bytes(TASK_WITH_UNSAFE_TEXT)

    action_xml = besapi.besapi.action_xml_from_bes_file(str(bes_file))

    tree = lxml.etree.fromstring(action_xml.encode("utf-8"))
    assert tree.findtext("SingleAction/Title") == "Fish & Chips <test>"
    assert "// before ]]> after" in tree.findtext("SingleAction/ActionScript")


# ---------- issue 34: action_xml_from_bes_file keeps the script type ----------
# The actionscript_keeps_markers, missing_mimetype_defaults_to_actionscript,
# prefers_default_action and positional_targets_still_work tests cover
# existing behavior the fix must keep, so they passed before it; they were
# mutation-checked instead.

TASK_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<Task>
		<Title>test task</Title>
		<Description>test</Description>
		<Relevance>true</Relevance>
		<Category></Category>
		<Source>Internal</Source>
		<SourceID></SourceID>
		<SourceReleaseDate>2026-01-01</SourceReleaseDate>
		<SourceSeverity></SourceSeverity>
		<CVENames></CVENames>
		<SANSID></SANSID>
		<Domain>BESC</Domain>
{actions}
	</Task>
</BES>
"""


def fixlet_action(tag, script, mime=None, action_id="Action1", settings=""):
    """Return one DefaultAction or Action element for TASK_TEMPLATE."""
    mime_attr = "" if mime is None else f' MIMEType="{mime}"'
    return (
        f'\t\t<{tag} ID="{action_id}">\n'
        f"\t\t\t<ActionScript{mime_attr}><![CDATA[{script}]]></ActionScript>\n"
        f"{settings}"
        f"\t\t</{tag}>"
    )


def convert_task(tmp_path, *actions, **kwargs):
    """Write a Task with these actions and convert it to action XML."""
    bes_file = tmp_path / "task.bes"
    # bytes, so Windows does not turn the script's \r\n into \r\r\n:
    bes_file.write_bytes(
        TASK_TEMPLATE.format(actions="\n".join(actions)).encode("utf-8")
    )
    assert besapi.besapi.validate_xml_bes_file(str(bes_file))
    return besapi.besapi.action_xml_from_bes_file(str(bes_file), **kwargs)


def converted_script(action_xml):
    """Return (MIMEType, script) of a converted action; check it is valid."""
    assert besapi.besapi.validate_xsd(action_xml.encode("utf-8"))
    elem = lxml.etree.fromstring(action_xml.encode("utf-8")).find(
        "SingleAction/ActionScript"
    )
    return elem.get("MIMEType"), elem.text


def test_action_xml_keeps_powershell_mimetype_without_markers(tmp_path):
    """Test that a PowerShell DefaultAction stays PowerShell, unwrapped."""
    script = 'Write-Output "hi"\r\nexit 0'
    action_xml = convert_task(
        tmp_path,
        fixlet_action(
            "DefaultAction", script, "application/x-Fixlet-Windows-PowerShell"
        ),
    )
    mime, text = converted_script(action_xml)
    assert mime == "application/x-Fixlet-Windows-PowerShell"
    assert text.replace("\r\n", "\n") == script.replace("\r\n", "\n")
    assert "// Start:" not in text and "// End" not in text


def test_action_xml_keeps_sh_mimetype_and_script(tmp_path):
    """Test that an application/x-sh script is copied unchanged."""
    script = "#!/bin/sh\necho hi"
    action_xml = convert_task(
        tmp_path, fixlet_action("DefaultAction", script, "application/x-sh")
    )
    assert converted_script(action_xml) == ("application/x-sh", script)


def test_action_xml_actionscript_keeps_markers(tmp_path):
    """Test that ActionScript output is unchanged: same type, with markers."""
    action_xml = convert_task(
        tmp_path,
        fixlet_action("DefaultAction", "// hi", "application/x-Fixlet-Windows-Shell"),
    )
    assert converted_script(action_xml) == (
        "application/x-Fixlet-Windows-Shell",
        "// Start:\n// hi\n// End",
    )


def test_action_xml_missing_mimetype_defaults_to_actionscript(tmp_path):
    """Test that an ActionScript with no MIMEType uses the XSD default."""
    action_xml = convert_task(tmp_path, fixlet_action("DefaultAction", "// hi"))
    assert converted_script(action_xml) == (
        "application/x-Fixlet-Windows-Shell",
        "// Start:\n// hi\n// End",
    )


def test_action_xml_uses_first_action_without_default_action(tmp_path):
    """Test that a Task with only <Action> elements uses the first one."""
    action_xml = convert_task(
        tmp_path,
        fixlet_action("Action", "// first", action_id="Action1"),
        fixlet_action("Action", "// second", action_id="Action2"),
    )
    assert converted_script(action_xml)[1] == "// Start:\n// first\n// End"


def test_action_xml_prefers_default_action(tmp_path):
    """Test that DefaultAction wins over other actions."""
    action_xml = convert_task(
        tmp_path,
        fixlet_action("DefaultAction", "// default", action_id="Action1"),
        fixlet_action("Action", "// other", action_id="Action2"),
    )
    assert converted_script(action_xml)[1] == "// Start:\n// default\n// End"


def test_action_xml_action_id_picks_action(tmp_path):
    """Test that action_id picks that action, with its own settings."""
    settings = (
        "\t\t\t<Settings><HasEndTime>true</HasEndTime>"
        "<EndDateTimeLocalOffset>P2D</EndDateTimeLocalOffset></Settings>\n"
    )
    action_xml = convert_task(
        tmp_path,
        fixlet_action("DefaultAction", "// default", action_id="Action1"),
        fixlet_action(
            "Action",
            "#!/bin/sh\necho two",
            "application/x-sh",
            action_id="Action2",
            settings=settings,
        ),
        action_id="Action2",
    )
    assert converted_script(action_xml) == ("application/x-sh", "#!/bin/sh\necho two")
    tree = lxml.etree.fromstring(action_xml.encode("utf-8"))
    assert tree.findtext("SingleAction/Settings/EndDateTimeLocalOffset") == "P2D"


def test_action_xml_positional_targets_still_work(tmp_path):
    """Test that existing callers passing (file_path, targets) keep working."""
    bes_file = tmp_path / "task.bes"
    bes_file.write_bytes(
        TASK_TEMPLATE.format(actions=fixlet_action("DefaultAction", "// hi")).encode(
            "utf-8"
        )
    )
    action_xml = besapi.besapi.action_xml_from_bes_file(str(bes_file), 12345)
    tree = lxml.etree.fromstring(action_xml.encode("utf-8"))
    assert tree.findtext("SingleAction/Target/ComputerID") == "12345"


def test_action_xml_settings_have_no_namespace_declaration(tmp_path):
    """Test that the copied <Settings> does not carry xmlns:xsi."""
    settings = "\t\t\t<Settings><HasEndTime>false</HasEndTime></Settings>\n"
    action_xml = convert_task(
        tmp_path, fixlet_action("DefaultAction", "// hi", settings=settings)
    )
    assert besapi.besapi.validate_xsd(action_xml.encode("utf-8"))
    assert "<Settings><HasEndTime>false</HasEndTime></Settings>" in action_xml


@pytest.mark.parametrize(
    "actions, kwargs, message",
    [
        ([fixlet_action("DefaultAction", "// hi")], {"action_id": "Nope"}, "Nope"),
        (
            [fixlet_action("DefaultAction", "https://x.test/", "text/x-uri")],
            {},
            "x-uri",
        ),
        ([], {}, "no action"),
        ([fixlet_action("DefaultAction", "")], {}, "empty"),
    ],
    ids=["unknown-action-id", "text-x-uri", "no-action", "empty-actionscript"],
)
def test_action_xml_rejects_unusable_actions(tmp_path, actions, kwargs, message):
    """Test that unusable actions raise ValueError, not IndexError."""
    with pytest.raises(ValueError, match=message):
        convert_task(tmp_path, *actions, **kwargs)


ANALYSIS_BES = b"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<Analysis>
		<Title>test analysis</Title>
		<Description>test</Description>
		<Relevance>true</Relevance>
		<Source>Internal</Source>
		<SourceReleaseDate>2026-01-01</SourceReleaseDate>
		<Domain>BESC</Domain>
		<Property Name="test" ID="1" EvaluationPeriod="PT1H">true</Property>
	</Analysis>
</BES>
"""


def test_action_xml_rejects_non_actionable_bes_file(tmp_path):
    """Test that an Analysis raises ValueError, not IndexError."""
    bes_file = tmp_path / "analysis.bes"
    bes_file.write_bytes(ANALYSIS_BES)
    assert besapi.besapi.validate_xml_bes_file(str(bes_file))
    with pytest.raises(ValueError, match="Fixlet, Task or SingleAction"):
        besapi.besapi.action_xml_from_bes_file(str(bes_file))


# ---------- PR 35 review: action_xml_from_bes_file edge cases ----------


def test_action_xml_settings_with_comment(tmp_path):
    """Test that a comment inside <Settings> does not break conversion."""
    settings = (
        "\t\t\t<Settings><!-- keep for 2 days --><HasEndTime>true</HasEndTime>"
        "<EndDateTimeLocalOffset>P2D</EndDateTimeLocalOffset></Settings>\n"
    )
    action_xml = convert_task(
        tmp_path, fixlet_action("DefaultAction", "// hi", settings=settings)
    )
    assert besapi.besapi.validate_xsd(action_xml.encode("utf-8"))
    tree = lxml.etree.fromstring(action_xml.encode("utf-8"))
    assert tree.findtext("SingleAction/Settings/EndDateTimeLocalOffset") == "P2D"


def test_action_xml_uses_relevance_of_converted_item_only(tmp_path):
    """Test that a file with two Tasks uses only the first Task's relevance."""
    one_task = TASK_TEMPLATE.format(actions=fixlet_action("DefaultAction", "// hi"))
    task_start = one_task.index("\t<Task>")
    task_end = one_task.index("</Task>") + len("</Task>\n")
    second_task = (
        one_task[task_start:task_end]
        .replace("<Title>test task</Title>", "<Title>second task</Title>")
        .replace("<Relevance>true</Relevance>", "<Relevance>false</Relevance>")
    )
    bes_file = tmp_path / "two_tasks.bes"
    bes_file.write_bytes(
        (one_task[:task_end] + second_task + one_task[task_end:]).encode("utf-8")
    )
    assert besapi.besapi.validate_xml_bes_file(str(bes_file))

    action_xml = besapi.besapi.action_xml_from_bes_file(str(bes_file))

    tree = lxml.etree.fromstring(action_xml.encode("utf-8"))
    assert tree.findtext("SingleAction/Title") == "test task"
    assert tree.findtext("SingleAction/Relevance") == "true"


@pytest.mark.parametrize(
    "mime, expected_mime, expected_script",
    [
        (
            "application/x-fixlet-windows-shell",
            "application/x-fixlet-windows-shell",
            "// Start:\n// hi\n// End",
        ),
        (" application/x-Fixlet-Windows-Shell ", None, "// Start:\n// hi\n// End"),
        ("Application/x-sh", "Application/x-sh", "// hi"),
        (" application/x-sh", None, "// hi"),
    ],
    ids=["lowercase-actionscript", "padded-actionscript", "uppercase-sh", "padded-sh"],
)
def test_action_xml_mimetype_case_and_whitespace(
    tmp_path, mime, expected_mime, expected_script
):
    """Test that MIMEType checks ignore case and surrounding whitespace."""
    action_xml = convert_task(tmp_path, fixlet_action("DefaultAction", "// hi", mime))
    out_mime, out_script = converted_script(action_xml)
    assert out_mime.strip() == (expected_mime or mime.strip())
    assert out_script == expected_script


def test_action_xml_value_error_is_not_logged(tmp_path, caplog):
    """Test that a ValueError the caller can handle is not also logged."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match="no action"):
        convert_task(tmp_path)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


# ---------- E6: unfinished update_item_from_file ----------


def test_update_item_from_file_not_implemented(offline_login, capsys):
    """Test that the unfinished method raises instead of printing."""
    conn = make_conn()
    with pytest.raises(NotImplementedError):
        conn.update_item_from_file(GOOD_BES_FILE, "custom/Demo")
    assert capsys.readouterr().out == ""


# ---------- E7: XML session relevance keeps %XX escapes ----------


def server_decoded_relevance(data):
    """Decode a posted query body the way the BigFix server does: twice.

    Verified against a live server: the JSON query path (which double
    encodes) keeps `%22` escapes, while a single encoded body loses them.
    """
    import urllib.parse

    body = data if isinstance(data, str) else urllib.parse.urlencode(data)
    form = urllib.parse.parse_qs(body)
    return urllib.parse.unquote(form["relevance"][0])


@pytest.mark.parametrize(
    "relevance",
    ['(it as string) of ( "a%22b" )', '"100%25 of " & "5+5"'],
)
def test_session_relevance_xml_body_survives_double_decode(offline_login, relevance):
    """Test that relevance escapes like %22 reach the evaluator intact."""
    conn = make_conn()
    conn.session_relevance_xml(relevance)
    assert server_decoded_relevance(conn.session.calls[-1][2]["data"]) == relevance


# ---------- P1: XSD schemas are compiled once, not per call ----------


def test_validate_xsd_does_not_recompile_schemas(monkeypatch):
    """Test that repeated validation reuses the compiled XSD schemas."""
    real_schema = lxml.etree.XMLSchema
    compiled = []

    def counting_schema(*args, **kwargs):
        compiled.append(1)
        return real_schema(*args, **kwargs)

    monkeypatch.setattr(lxml.etree, "XMLSchema", counting_schema)

    # an invalid document is checked against every schema:
    invalid = b"<BESAPI><Nope/></BESAPI>"
    besapi.besapi.validate_xsd(invalid)
    compiled.clear()

    for _ in range(4):
        assert besapi.besapi.validate_xsd(invalid) is False
    assert besapi.besapi.validate_xsd(read_good_bes()) is True

    assert compiled == []


# ---------- P2: RESTResult validates lazily ----------


@pytest.fixture
def count_validate_xsd(monkeypatch):
    """Count calls to the module level validate_xsd."""
    calls = []
    real_validate = besapi.besapi.validate_xsd

    def counting_validate(doc):
        calls.append(1)
        return real_validate(doc)

    monkeypatch.setattr(besapi.besapi, "validate_xsd", counting_validate)
    return calls


def test_rest_result_construction_does_not_validate(count_validate_xsd):
    """Test that creating a RESTResult defers XSD validation until needed."""
    result = besapi.besapi.RESTResult(FakeResponse("https://h/api/x", "<BESAPI />"))
    assert count_validate_xsd == []
    # still computed on access (an empty BESAPI element is valid):
    assert result.valid is True
    assert len(count_validate_xsd) == 1


def test_rest_result_json_response_skips_validation(count_validate_xsd):
    """Test that JSON responses are never run through XSD validation."""
    response = FakeResponse("https://h/api/query", '{"result": []}')
    response.headers = {"content-type": "application/json; charset=UTF-8"}

    result = besapi.besapi.RESTResult(response)

    assert result.valid is False
    assert count_validate_xsd == []


# ---------- B5: opt-in raise_for_status ----------
# live server check: missing resources return 404, unknown endpoints 400,
# and besapi returned them silently as results


@pytest.mark.parametrize("status", [400, 404, 500])
@pytest.mark.parametrize("method", ["get", "delete"])
def test_raise_for_status_raises_on_errors(offline_login, method, status):
    """Test that opted in connections raise on 4xx / 5xx responses."""
    conn = make_conn(raise_for_status=True)
    conn.session.response_status = status
    with pytest.raises(besapi.besapi.requests.HTTPError):
        getattr(conn, method)("site/custom/does_not_exist")


@pytest.mark.parametrize("method", ["post", "put"])
def test_raise_for_status_raises_on_errors_with_data(offline_login, method):
    """Test that opted in POST / PUT raise on error responses."""
    conn = make_conn(raise_for_status=True)
    conn.session.response_status = 404
    with pytest.raises(besapi.besapi.requests.HTTPError):
        getattr(conn, method)("x", "<BES />")


def test_raise_for_status_applies_to_session_relevance(offline_login):
    """Test that session relevance queries also raise when opted in."""
    conn = make_conn(raise_for_status=True)
    conn.session.response_status = 400
    with pytest.raises(besapi.besapi.requests.HTTPError):
        conn.session_relevance_xml("number of bes sites")


def test_raise_for_status_keeps_permission_error_for_403(offline_login):
    """Test that 403 still raises PermissionError, as without the option."""
    conn = make_conn(raise_for_status=True)
    conn.session.response_status = 403
    with pytest.raises(PermissionError):
        conn.get("webreports")


def test_raise_for_status_ok_response(offline_login):
    """Test that 2xx responses are returned normally when opted in."""
    conn = make_conn(raise_for_status=True)
    assert conn.get("help").request.status_code == 200


def test_raise_for_status_off_returns_error_results(offline_login):
    """Test that without the option, error responses are still returned."""
    conn = make_conn(raise_for_status=False)
    conn.session.response_status = 404
    assert conn.get("x").request.status_code == 404


# ---------- B6: opt-in raise_errors for session relevance ----------

# exact body returned by a live server for a bad query, with HTTP 200:
QUERY_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
	<Query Resource="this is not valid relevance">
		<Result></Result>
		<Error>The operator "this" is not defined.</Error>
	</Query>
</BESAPI>
"""

# exact body returned by a live server for a query with no results:
QUERY_EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
	<Query Resource="nothing">
		<Result></Result>
		<Evaluation>
			<Time>0.73ms</Time>
			<Plurality>Plural</Plurality>
		</Evaluation>
	</Query>
</BESAPI>
"""


def test_session_relevance_array_raise_errors(offline_login):
    """Test that a query error raises with the server's message when opted in."""
    conn = make_conn()
    conn.session.response_text = QUERY_ERROR_XML
    with pytest.raises(ValueError, match='The operator "this" is not defined.'):
        conn.session_relevance_array("this is not valid relevance", raise_errors=True)


def test_session_relevance_string_raise_errors(offline_login):
    """Test that session_relevance_string passes raise_errors through."""
    conn = make_conn()
    conn.session.response_text = QUERY_ERROR_XML
    with pytest.raises(ValueError, match="is not defined"):
        conn.session_relevance_string("this is not valid relevance", raise_errors=True)


def test_session_relevance_array_raise_errors_empty_is_not_error(offline_login):
    """Test that an empty result is not treated as an error."""
    conn = make_conn()
    conn.session.response_text = QUERY_EMPTY_XML
    result = conn.session_relevance_array("nothing", raise_errors=True)
    assert result == ["<Nothing> Nothing returned, but no error."]


# ---------- RESTResult parsing paths (coverage of existing behavior) ----------
# These document behavior that already worked, so they were not red first.
# They were checked by mutating RESTResult to confirm each one can fail.

SITES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
	<ExternalSite Resource="https://h:52311/api/site/external/BES%20Support">
		<Name>BES Support</Name>
	</ExternalSite>
	<CustomSite Resource="https://h:52311/api/site/custom/Demo">
		<Name>Demo</Name>
	</CustomSite>
</BESAPI>
"""

NOT_XML_TEXT = "Requested resource does not exist."


def make_result(text, headers=None, status_code=200):
    """Build a RESTResult from a fake response."""
    response = FakeResponse("https://h:52311/api/x", text, status_code)
    response.headers = headers or {}
    return besapi.besapi.RESTResult(response)


def test_rest_result_valid_xml_without_header_parses(count_validate_xsd):
    """Test that valid XML with no content-type is validated, then parsed."""
    result = make_result(SITES_XML)

    assert result.valid is True
    assert len(count_validate_xsd) == 1
    assert [str(site.Name) for site in result.besobj.iterchildren()] == [
        "BES Support",
        "Demo",
    ]


def test_rest_result_call_returns_besobj():
    """Test that calling the result returns the objectified XML."""
    result = make_result(SITES_XML)
    assert result() is result.besobj
    assert str(result().CustomSite.Name) == "Demo"


def test_rest_result_besxml_and_str_for_valid_xml():
    """Test besxml bytes and str() for a valid XML result."""
    result = make_result(SITES_XML)

    assert result.besxml.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert b"<Name>Demo</Name>" in result.besxml
    assert str(result) == result.besxml.decode("utf-8")


def test_rest_result_besdict_and_besjson_for_valid_xml():
    """Test dict and json representations of a valid XML result."""
    result = make_result(SITES_XML)

    assert result.besdict == {
        "ExternalSite": {"Name": "BES Support"},
        "CustomSite": {"Name": "Demo"},
    }
    assert json.loads(result.besjson) == result.besdict


def test_rest_result_xml_content_type_skips_validation(count_validate_xsd):
    """Test that an application/xml content-type is trusted without XSD checks."""
    result = make_result(SITES_XML, headers={"content-type": "application/xml"})

    assert result.valid is True
    assert str(result.besobj.CustomSite.Name) == "Demo"
    assert count_validate_xsd == []


def test_rest_result_non_xml_text():
    """Test that a non-XML body is invalid and every XML view is empty."""
    result = make_result(NOT_XML_TEXT, status_code=404)

    assert result.valid is False
    assert result.besobj is None
    assert result.besxml is None
    assert result() is None
    assert str(result) == NOT_XML_TEXT
    assert result.besdict == {"text": NOT_XML_TEXT}


def test_rest_result_well_formed_but_not_bes_xml():
    """Test that XML which does not match the BES schemas is not parsed."""
    result = make_result("<html><body>error page</body></html>")

    assert result.valid is False
    assert result.besobj is None
    assert str(result) == "<html><body>error page</body></html>"


def test_rest_result_bytes_text(count_validate_xsd):
    """Test that a bytes body is validated as bytes."""
    result = make_result(SITES_XML.encode("utf-8"))

    assert result.valid is True
    assert len(count_validate_xsd) == 1
    assert str(result.besobj.CustomSite.Name) == "Demo"


def test_rest_result_validates_and_parses_once(count_validate_xsd):
    """Test that validity and the parsed object are cached after first use."""
    result = make_result(SITES_XML)

    first = result.besobj
    for _ in range(3):
        assert result.valid is True
        assert result.besobj is first
        str(result)

    assert len(count_validate_xsd) == 1


def test_rest_result_valid_setter_overrides():
    """Test that setting valid overrides the computed value."""
    result = make_result(SITES_XML)
    result.valid = False

    assert result.valid is False
    assert result.besobj is None
    assert str(result) == SITES_XML


def test_rest_result_validate_xsd_method_uses_cached_validity(count_validate_xsd):
    """Test the RESTResult.validate_xsd method before and after validity is known."""
    result = make_result(SITES_XML)

    # not known yet: validates the given document
    assert result.validate_xsd(NOT_XML_TEXT) is False
    assert len(count_validate_xsd) == 1

    result.valid = True
    # known: returns it without validating again
    assert result.validate_xsd(NOT_XML_TEXT) is True
    assert len(count_validate_xsd) == 1


def test_rest_result_403_raises_permission_error():
    """Test that a 403 raises PermissionError when the result is created."""
    with pytest.raises(PermissionError, match="403"):
        make_result("Forbidden", status_code=403)


# ---------- session_relevance_array default parsing (existing behavior) ----------

QUERY_ANSWERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
	<Query Resource="names of bes sites">
		<Result>
			<Answer type="string">BES Support</Answer>
			<Answer type="string">Demo</Answer>
		</Result>
		<Evaluation>
			<Time>0.50ms</Time>
			<Plurality>Plural</Plurality>
		</Evaluation>
	</Query>
</BESAPI>
"""


def test_session_relevance_array_answers(offline_login):
    """Test that each Answer becomes one list item."""
    conn = make_conn()
    conn.session.response_text = QUERY_ANSWERS_XML
    assert conn.session_relevance_array("names of bes sites") == ["BES Support", "Demo"]


def test_session_relevance_string_joins_answers(offline_login):
    """Test that session_relevance_string joins answers with newlines."""
    conn = make_conn()
    conn.session.response_text = QUERY_ANSWERS_XML
    assert conn.session_relevance_string("names of bes sites") == "BES Support\nDemo"


def test_session_relevance_array_error_default(offline_login):
    """Test that without raise_errors, a query error is returned as a string."""
    conn = make_conn()
    conn.session.response_text = QUERY_ERROR_XML
    assert conn.session_relevance_array("this is not valid relevance") == [
        'ERROR: The operator "this" is not defined.'
    ]


def test_session_relevance_array_empty_default(offline_login):
    """Test that an empty result is reported, not treated as an error."""
    conn = make_conn()
    conn.session.response_text = QUERY_EMPTY_XML
    assert conn.session_relevance_array("nothing") == [
        "<Nothing> Nothing returned, but no error."
    ]
