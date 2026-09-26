"""Coverage of existing besapi.besapi behavior that had no offline tests.

These document behavior that already worked, so they were not red first.
They were checked by mutating the code under test to confirm each can fail.

No network is used, see the FakeSession in test_besapi_sdk.py. Response
shapes are based on a live BigFix server.
"""

import hashlib
import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_besapi_sdk import FakeResponse, make_conn  # noqa: E402

import besapi  # noqa: E402
from besapi.besapi import BESConnection  # noqa: E402

# BESConnection.login, before any test patches it out:
REAL_LOGIN = BESConnection.login

API = "https://bigfix.example:52311/api"

BES_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="BES.xsd">'
)
BESAPI_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="BESAPI.xsd">'
)


def task_xml(title):
    """Build a valid BES Task with the given (already XML safe) title."""
    return f"""{BES_HEADER}
	<Task>
		<Title>{title}</Title>
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
	</Task>
</BES>
"""


GIT_TITLE = "Update: Git v2.35.3 - Windows (x64)"
GIT_FILE = "Update_Git_v2.35.3_-_Windows_(x64)"
PY_TITLE = "Update: Python v3.10.4 - Windows"
PY_FILE = "Update_Python_v3.10.4_-_Windows"

# shaped like a live `site/custom/Demo/content` listing:
SITE_CONTENT_XML = f"""{BESAPI_HEADER}
	<Task Resource="{API}/task/custom/Demo/12724" LastModified="Thu, 05 Feb 2026 18:26:27 +0000">
		<Name>{GIT_TITLE}</Name>
		<ID>12724</ID>
	</Task>
	<Task Resource="{API}/task/custom/Demo/12725" LastModified="Thu, 05 Feb 2026 18:26:27 +0000">
		<Name>{PY_TITLE}</Name>
		<ID>12725</ID>
	</Task>
</BESAPI>
"""

# shaped like a live `sites` listing:
SITES_XML = f"""{BESAPI_HEADER}
	<ExternalSite Resource="{API}/site/external/BES%20Support">
		<Name>BES Support</Name>
	</ExternalSite>
	<CustomSite Resource="{API}/site/custom/Demo">
		<Name>Demo</Name>
	</CustomSite>
	<ActionSite Resource="{API}/site/master">
		<Name>ActionSite</Name>
	</ActionSite>
</BESAPI>
"""


# NOTE: the offline_login and export_conn fixtures are in conftest.py


def written_files(folder):
    """Relative paths of all files under folder."""
    found = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            found.append(os.path.relpath(os.path.join(dirpath, name), folder))
    return sorted(found)


# ---------- export_item_by_resource ----------


def test_export_item_by_resource_writes_title_named_file(export_conn, tmp_path):
    """Test that an item is saved as <sanitized title>.bes with its XML."""
    path = export_conn.export_item_by_resource(
        f"{API}/task/custom/Demo/12724", str(tmp_path) + "/"
    )

    assert path == f"{tmp_path}/{GIT_FILE}.bes"
    assert written_files(tmp_path) == [f"{GIT_FILE}.bes"]
    with open(path, encoding="utf-8") as bes_file:
        assert bes_file.read() == task_xml(GIT_TITLE)


def test_export_item_by_resource_type_folder_and_id(export_conn, tmp_path):
    """Test the item type folder and item id file name options."""
    path = export_conn.export_item_by_resource(
        f"{API}/task/custom/Demo/12724",
        str(tmp_path) + "/",
        include_item_type_folder=True,
        include_item_id=True,
    )

    assert path == f"{tmp_path}/Task/12724-{GIT_FILE}.bes"
    assert written_files(tmp_path) == [f"Task/12724-{GIT_FILE}.bes"]


def test_export_item_by_resource_name_trim(export_conn, tmp_path):
    """Test that long titles are trimmed before sanitizing."""
    path = export_conn.export_item_by_resource(
        f"{API}/task/custom/Demo/12724", str(tmp_path) + "/", name_trim=6
    )
    assert path == f"{tmp_path}/Update.bes"


def test_export_item_by_resource_forbidden_returns_none(export_conn, tmp_path):
    """Test that a 403 item is skipped: nothing written, None returned."""
    export_conn.session.routes[f"{API}/task/custom/Demo/12724"] = ("Forbidden", 403)

    assert (
        export_conn.export_item_by_resource(
            f"{API}/task/custom/Demo/12724", str(tmp_path) + "/"
        )
        is None
    )
    assert written_files(tmp_path) == []


# ---------- get_content_by_resource ----------


def test_get_content_by_resource_upgrades_http(export_conn):
    """Test that http:// resources are requested over https://."""
    content = export_conn.get_content_by_resource(
        "http://bigfix.example:52311/api/task/custom/Demo/12724"
    )

    assert export_conn.session.calls[-1][1] == f"{API}/task/custom/Demo/12724"
    assert str(content.besobj.Task.Title) == GIT_TITLE


# ---------- save_item_to_besfile ----------


def test_save_item_to_besfile(tmp_path):
    """Test saving an XML string to a new folder, named by its title."""
    folder = tmp_path / "new_folder"
    path = BESConnection.save_item_to_besfile(None, task_xml(PY_TITLE), str(folder))

    assert path == f"{folder}/{PY_FILE}.bes"
    with open(path, encoding="utf-8") as bes_file:
        assert bes_file.read() == task_xml(PY_TITLE)


# ---------- export_site_contents ----------


@pytest.mark.parametrize(
    "options, expected",
    [
        (
            {},
            [
                f"custom-Demo/Task/12724-{GIT_FILE}.bes",
                f"custom-Demo/Task/12725-{PY_FILE}.bes",
            ],
        ),
        (
            {"include_item_ids": False},
            [f"custom-Demo/Task/{GIT_FILE}.bes", f"custom-Demo/Task/{PY_FILE}.bes"],
        ),
        (
            {"include_site_folder": False},
            [f"Task/12724-{GIT_FILE}.bes", f"Task/12725-{PY_FILE}.bes"],
        ),
        (
            {"include_site_folder": False, "include_item_ids": False},
            [f"Task/{GIT_FILE}.bes", f"Task/{PY_FILE}.bes"],
        ),
    ],
)
def test_export_site_contents_paths(export_conn, tmp_path, options, expected):
    """Test the export folder layout for each combination of options."""
    export_conn.export_site_contents("custom/Demo", str(tmp_path) + "/", **options)
    assert written_files(tmp_path) == expected


def test_export_site_contents_file_content(export_conn, tmp_path):
    """Test that exported files contain each item's XML."""
    export_conn.export_site_contents("custom/Demo", str(tmp_path) + "/")

    git_file = tmp_path / "custom-Demo" / "Task" / f"12724-{GIT_FILE}.bes"
    assert git_file.read_text(encoding="utf-8") == task_xml(GIT_TITLE)


def test_export_site_contents_skips_forbidden_items(export_conn, tmp_path):
    """Test that an item that cannot be read is skipped, others still export."""
    export_conn.session.routes[f"{API}/task/custom/Demo/12724"] = ("Forbidden", 403)

    export_conn.export_site_contents("custom/Demo", str(tmp_path) + "/")

    assert written_files(tmp_path) == [f"custom-Demo/Task/12725-{PY_FILE}.bes"]


def test_export_site_contents_missing_site_writes_nothing(export_conn, tmp_path):
    """Test that a non 200 site listing exports nothing."""
    export_conn.session.routes[f"{API}/site/custom/Demo/content"] = (
        "Requested resource does not exist.",
        404,
    )
    export_conn.export_site_contents("custom/Demo", str(tmp_path) + "/")
    assert written_files(tmp_path) == []


def test_export_site_contents_uses_current_site_path(export_conn, tmp_path):
    """Test that the connection's site path is used when none is given."""
    export_conn.site_path = "custom/Demo"
    export_conn.export_site_contents(export_folder=str(tmp_path) + "/")
    assert len(written_files(tmp_path)) == 2


# ---------- export_all_sites ----------


@pytest.fixture
def sites_conn(offline_login, monkeypatch):
    """A connection with a sites listing, recording export_site_contents calls."""
    conn = make_conn()
    conn.session.routes = {f"{API}/sites": (SITES_XML, 200)}
    exported = []
    monkeypatch.setattr(
        conn, "export_site_contents", lambda *args, **kwargs: exported.append(args)
    )
    return conn, exported


def test_export_all_sites_skips_external_by_default(sites_conn):
    """Test that only custom and master sites are exported by default."""
    conn, exported = sites_conn
    conn.export_all_sites()
    assert exported == [("custom/Demo", "./", 70, False), ("master", "./", 70, False)]


def test_export_all_sites_include_external(sites_conn):
    """Test that external sites are exported when asked, with their URL path."""
    conn, exported = sites_conn
    conn.export_all_sites(include_external=True, export_folder="out/", name_trim=9)
    assert [args[0] for args in exported] == [
        "external/BES%20Support",
        "custom/Demo",
        "master",
    ]
    assert all(args[1:3] == ("out/", 9) for args in exported)


def test_export_all_sites_failed_listing(sites_conn):
    """Test that nothing is exported if the sites listing fails."""
    conn, exported = sites_conn
    conn.session.routes[f"{API}/sites"] = ("error", 500)
    conn.export_all_sites()
    assert exported == []


# ---------- get_bes_conn_using_config_file / env ----------


class RecordingConnection:
    """Stand-in for BESConnection that records how it was created."""

    created: list = []

    def __init__(self, username, password, rootserver, **kwargs):
        RecordingConnection.created.append((username, password, rootserver))


@pytest.fixture
def recording_connection(monkeypatch):
    """Replace BESConnection so no network connection is attempted."""
    RecordingConnection.created = []
    monkeypatch.setattr(besapi.besapi, "BESConnection", RecordingConnection)
    return RecordingConnection


def write_config(path, **values):
    """Write a besapi config file with the given keys."""
    lines = ["[besapi]"] + [f"{key} = {value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_config_file_connection(recording_connection, tmp_path):
    """Test that all three config values are used to connect."""
    conf = write_config(
        tmp_path / "besapi.conf",
        BES_ROOT_SERVER="https://bigfix.example:52311",
        BES_USER_NAME="me",
        BES_PASSWORD="<PASSWORD>",
    )
    conn = besapi.besapi.get_bes_conn_using_config_file(conf)

    assert isinstance(conn, RecordingConnection)
    assert recording_connection.created == [
        ("me", "<PASSWORD>", "https://bigfix.example:52311")
    ]


@pytest.mark.parametrize(
    "missing", ["BES_ROOT_SERVER", "BES_USER_NAME", "BES_PASSWORD"]
)
def test_config_file_missing_value(recording_connection, tmp_path, missing):
    """Test that a config file missing any value does not connect."""
    values = {
        "BES_ROOT_SERVER": "https://bigfix.example:52311",
        "BES_USER_NAME": "me",
        "BES_PASSWORD": "<PASSWORD>",
    }
    del values[missing]
    conf = write_config(tmp_path / "besapi.conf", **values)

    assert besapi.besapi.get_bes_conn_using_config_file(conf) is None
    assert recording_connection.created == []


def test_config_file_not_found(recording_connection, tmp_path):
    """Test that a missing config file does not connect."""
    assert (
        besapi.besapi.get_bes_conn_using_config_file(str(tmp_path / "nope.conf"))
        is None
    )
    assert recording_connection.created == []


def test_env_connection(recording_connection, monkeypatch):
    """Test that BES_* env vars are used to connect."""
    monkeypatch.setenv("BES_USER_NAME", "env_user")
    monkeypatch.setenv("BES_PASSWORD", "<PASSWORD>")
    monkeypatch.setenv("BES_ROOT_SERVER", "https://env.example:52311")

    assert isinstance(besapi.besapi.get_bes_conn_using_env(), RecordingConnection)
    assert recording_connection.created == [
        ("env_user", "<PASSWORD>", "https://env.example:52311")
    ]


def test_env_connection_incomplete(recording_connection, monkeypatch):
    """Test that incomplete env vars do not connect."""
    monkeypatch.setenv("BES_USER_NAME", "env_user")
    monkeypatch.delenv("BES_PASSWORD", raising=False)
    monkeypatch.setenv("BES_ROOT_SERVER", "https://env.example:52311")

    assert besapi.besapi.get_bes_conn_using_env() is None
    assert recording_connection.created == []


# ---------- get_upload / upload / parse_upload_result_to_prefetch ----------

SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"

# shaped like a live upload result, which the server sends as application/xml:
UPLOAD_XML = f"""{BESAPI_HEADER}
	<FileUpload Resource="{API}/upload/{SHA1}/file.txt">
		<Available>1</Available>
		<URL>http://bigfix.example:52311/Uploads/{SHA1}/file.txt.bfswd</URL>
		<Name>file.txt</Name>
		<Size>11</Size>
		<SHA1>{SHA1}</SHA1>
		<SHA256>{SHA256}</SHA256>
	</FileUpload>
</BESAPI>
"""


def upload_result():
    """A RESTResult for UPLOAD_XML, as the server sends it."""
    response = FakeResponse(f"{API}/upload", UPLOAD_XML)
    response.headers = {"content-type": "application/xml"}
    return besapi.besapi.RESTResult(response)


@pytest.mark.parametrize(
    "file_name, file_hash, message",
    [
        ("file.txt", "abc", "Invalid SHA1"),
        ("file name.txt", SHA1, "cannot contain spaces"),
        ("", SHA1, "No file_name"),
    ],
)
def test_get_upload_rejects_bad_input(offline_login, file_name, file_hash, message):
    """Test input validation before any request is made."""
    conn = make_conn()
    with pytest.raises(ValueError, match=message):
        conn.get_upload(file_name, file_hash)
    assert conn.session.calls == []


def test_get_upload_not_found(offline_login):
    """Test that a missing upload returns None."""
    conn = make_conn()
    conn.session.response_text = "Upload not found"
    assert conn.get_upload("file.txt", SHA1) is None
    assert conn.session.calls[-1][1] == f"{API}/upload/{SHA1}/file.txt"


def test_get_upload_found(offline_login):
    """Test that an existing upload's result is returned."""
    conn = make_conn()
    conn.session.response_text = UPLOAD_XML
    assert conn.get_upload("file.txt", SHA1).text == UPLOAD_XML


def test_upload_new_file_posts_with_hash_lookup(offline_login, tmp_path):
    """Test a new upload: sha1 is computed, checked, then the file is posted."""
    local_file = tmp_path / "my file.txt"
    local_file.write_bytes(b"hello world")
    sha1 = hashlib.sha1(b"hello world").hexdigest()  # nosec B324
    conn = make_conn()
    conn.session.routes = {
        f"{API}/upload/{sha1}/my_file.txt": ("Upload not found", 200),
        f"{API}/upload": (UPLOAD_XML, 200),
    }

    conn.upload(str(local_file))

    (_, check_url, _), (method, post_url, kwargs) = conn.session.calls
    # spaces are replaced in the upload file name:
    assert check_url == f"{API}/upload/{sha1}/my_file.txt"
    assert (method, post_url) == ("post", f"{API}/upload")
    assert kwargs["headers"] == {
        "Content-Disposition": 'attachment; filename="my_file.txt"'
    }


def test_upload_existing_file_is_not_posted(offline_login, tmp_path):
    """Test that an upload that already exists on the server is reused."""
    local_file = tmp_path / "file.txt"
    local_file.write_bytes(b"hello world")
    conn = make_conn()
    conn.session.response_text = UPLOAD_XML

    result = conn.upload(str(local_file), file_hash=SHA1)

    assert result.text == UPLOAD_XML
    assert [call[0] for call in conn.session.calls] == ["get"]


@pytest.mark.parametrize(
    "use_localhost, use_https, url",
    [
        (True, True, f"https://localhost:52311/Uploads/{SHA1}/file.txt.bfswd"),
        (False, True, f"https://bigfix.example:52311/Uploads/{SHA1}/file.txt.bfswd"),
        (False, False, f"http://bigfix.example:52311/Uploads/{SHA1}/file.txt.bfswd"),
    ],
)
def test_parse_upload_result_to_prefetch(offline_login, use_localhost, use_https, url):
    """Test the prefetch statement built from an upload result."""
    conn = make_conn()
    prefetch = conn.parse_upload_result_to_prefetch(
        upload_result(), use_localhost=use_localhost, use_https=use_https
    )
    assert prefetch == (f"prefetch file.txt sha1:{SHA1} size:11 {url} sha256:{SHA256}")


# ---------- login ----------


def test_login_success_sets_connected_and_mounts_upload(offline_login):
    """Test the first login: GET login, record the time, mount the upload adapter."""
    conn = make_conn()
    conn.last_connected = None

    assert REAL_LOGIN(conn) is True

    assert conn.session.calls[-1][:2] == ("get", f"{API}/login")
    assert conn.session.calls[-1][2]["timeout"] == (3, 20)
    assert conn.last_connected is not None
    assert conn.session.mounted[-1][0] == f"{API}/upload"


def test_login_already_connected_skips_request(offline_login):
    """Test that an existing connection does not log in again."""
    conn = make_conn()
    conn.get("help")
    calls = len(conn.session.calls)

    assert REAL_LOGIN(conn) is True
    assert len(conn.session.calls) == calls


def test_login_failure_raises(offline_login):
    """Test that a failed login raises the HTTP error."""
    conn = make_conn()
    conn.last_connected = None
    conn.session.response_status = 401

    with pytest.raises(besapi.besapi.requests.HTTPError):
        REAL_LOGIN(conn)
    assert conn.last_connected is None

    # a later login must try again, not report the failed one as connected:
    calls = len(conn.session.calls)
    with pytest.raises(besapi.besapi.requests.HTTPError):
        REAL_LOGIN(conn)
    assert len(conn.session.calls) == calls + 1


# ---------- am_i_main_operator ----------


def test_am_i_main_operator_true_and_cached(offline_login):
    """Test that access to webreports means main operator, checked once."""
    conn = make_conn()
    assert conn.am_i_main_operator() is True
    assert conn.am_i_main_operator() is True
    assert [call[1] for call in conn.session.calls] == [f"{API}/webreports"]


def test_am_i_main_operator_forbidden(offline_login):
    """Test that a 403 on webreports means not main operator."""
    conn = make_conn()
    conn.session.response_status = 403
    assert conn.am_i_main_operator() is False


def test_am_i_main_operator_unknown_on_error(offline_login, monkeypatch):
    """Test that other errors leave it unknown (None)."""
    conn = make_conn()

    def broken_get(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(conn, "get", broken_get)
    assert conn.am_i_main_operator() is None


# ---------- site paths ----------


def test_validate_site_path_checks_site_exists(offline_login):
    """Test that an existing site is confirmed with a GET."""
    conn = make_conn()
    assert conn.validate_site_path("custom/Demo") == "custom/Demo"
    assert conn.session.calls[-1][1] == f"{API}/site/custom/Demo"


def test_validate_site_path_missing_site(offline_login):
    """Test a missing site: None, or ValueError with raise_error."""
    conn = make_conn()
    conn.session.response_status = 404

    assert conn.validate_site_path("custom/Nope") is None
    with pytest.raises(ValueError, match="does not exist"):
        conn.validate_site_path("custom/Nope", raise_error=True)


def test_validate_site_path_none_without_raise(offline_login):
    """Test that None and empty site paths return None without raise_error."""
    conn = make_conn()
    assert conn.validate_site_path(None) is None
    assert conn.validate_site_path("  ") is None


def test_set_current_site_path(offline_login):
    """Test that only an existing site becomes the current site path."""
    conn = make_conn()
    assert conn.set_current_site_path("custom/Demo") == "custom/Demo"
    assert conn.site_path == "custom/Demo"

    conn.session.response_status = 404
    assert conn.set_current_site_path("custom/Nope") is None
    assert conn.site_path == "custom/Demo"


def test_get_current_site_path(offline_login):
    """Test the default, an explicit path, and a missing context."""
    conn = make_conn()
    assert conn.get_current_site_path() == "master"
    assert conn.get_current_site_path("operator/me") == "operator/me"

    conn.site_path = ""
    with pytest.raises(ValueError, match="Site Path context not set"):
        conn.get_current_site_path()


# ---------- users ----------

OPERATOR_XML = f"""{BESAPI_HEADER}
	<Operator>
		<Name>test_user</Name>
		<Password>x</Password>
		<MasterOperator>false</MasterOperator>
	</Operator>
</BESAPI>
"""


def test_get_user_found(offline_login):
    """Test that an existing operator is returned."""
    conn = make_conn()
    conn.session.response_text = OPERATOR_XML
    assert conn.get_user("test_user").text == OPERATOR_XML
    assert conn.session.calls[-1][1] == f"{API}/operator/test_user"


def test_get_user_not_found(offline_login):
    """Test that a missing operator returns None."""
    conn = make_conn()
    conn.session.response_text = "Operator does not exist"
    assert conn.get_user("test_user") is None


@pytest.fixture
def operator_file(tmp_path):
    """A BESAPI operator definition file."""
    path = tmp_path / "operator.xml"
    path.write_text(OPERATOR_XML, encoding="utf-8")
    return str(path)


def test_create_user_from_file_existing(offline_login, monkeypatch, operator_file):
    """Test that an existing user is returned without creating it."""
    conn = make_conn()
    monkeypatch.setattr(conn, "get_user", lambda name: f"USER:{name}")

    assert conn.create_user_from_file(operator_file) == "USER:test_user"
    assert conn.session.calls == []


def test_create_user_from_file_new(offline_login, monkeypatch, operator_file):
    """Test that a new user is posted to operators, then looked up."""
    conn = make_conn()
    lookups = []

    def fake_get_user(name):
        lookups.append(name)
        return None if len(lookups) == 1 else f"USER:{name}"

    monkeypatch.setattr(conn, "get_user", fake_get_user)

    assert conn.create_user_from_file(operator_file) == "USER:test_user"
    assert conn.session.calls[-1][:2] == ("post", f"{API}/operators")
    assert b"<Name>test_user</Name>" in conn.session.calls[-1][2]["data"]


# ---------- sites ----------

CUSTOM_SITE_BES = f"""{BES_HEADER}
	<CustomSite>
		<Name>NewSite</Name>
		<Description>test</Description>
		<GlobalReadPermission>false</GlobalReadPermission>
		<Subscription><Mode>None</Mode></Subscription>
	</CustomSite>
</BES>
"""


@pytest.fixture
def site_file(tmp_path):
    """A BES custom site definition file."""
    path = tmp_path / "site.bes"
    path.write_text(CUSTOM_SITE_BES, encoding="utf-8")
    return str(path)


def test_create_site_from_file_new(offline_login, site_file):
    """Test that a site that does not exist yet is posted to sites."""
    conn = make_conn()
    conn.session.routes = {f"{API}/site/custom/NewSite": ("does not exist", 404)}

    conn.create_site_from_file(site_file)

    assert conn.session.calls[-1][:2] == ("post", f"{API}/sites")
    assert b"<Name>NewSite</Name>" in conn.session.calls[-1][2]["data"]


def test_create_site_from_file_existing(offline_login, site_file):
    """Test that an existing site is not created again."""
    conn = make_conn()
    assert conn.create_site_from_file(site_file) is None
    assert [call[0] for call in conn.session.calls] == ["get"]


# ---------- computer groups ----------

GROUPS_FILE = os.path.join(ROOT, "tests", "good", "ComputerGroupsExample.bes")


def test_get_computergroup(offline_login):
    """Test finding a group by name in a site's group listing."""
    conn = make_conn()
    with open(GROUPS_FILE, encoding="utf-8") as groups:
        conn.session.response_text = groups.read()

    group = conn.get_computergroup("Docker - Hosts - Linux", "custom/Public")

    assert conn.session.calls[-1][1] == f"{API}/computergroups/custom/Public"
    assert str(group.ID) == "6818"
    assert conn.get_computergroup("No Such Group", "custom/Public") is None


# ---------- small helpers ----------


def test_session_relevance_json_array_and_string(offline_login):
    """Test the JSON result helpers."""
    conn = make_conn()
    conn.session.response_text = '{"result": ["a", 2]}'
    assert conn.session_relevance_json_array("x") == ["a", 2]
    assert conn.session_relevance_json_string("x") == "a\n2"


def test_url_keeps_absolute_urls(offline_login):
    """Test that url() only prefixes relative paths."""
    conn = make_conn()
    assert conn.url("sites") == f"{API}/sites"
    assert conn.url(f"{API}/sites") == f"{API}/sites"
