"""Coverage of existing bescli behavior.

These document behavior that already worked, so they were not red first.
They were checked by mutating the code under test to confirm each can fail.

No network is used: HOME and the working directory point at an empty temp
folder so no real besapi.conf is found, and connections are offline fakes.
"""

import io
import logging
import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from test_besapi_coverage import (  # noqa: E402
    API,
    GIT_FILE,
    GIT_TITLE,
    SITES_XML,
    task_xml,
)
from test_besapi_sdk import QUERY_ANSWERS_XML, make_conn  # noqa: E402

from bescli import bescli  # noqa: E402

# NOTE: the offline_login and export_conn fixtures are in conftest.py


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Make sure no real config file can be found."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    if os.path.exists("/etc/besapi.conf"):
        pytest.skip("/etc/besapi.conf exists, cannot isolate bescli config")
    return tmp_path


@pytest.fixture
def cli(isolated):
    """A bescli interface with no config and no connection.

    NOTE: its own stdout, since cmd2 binds the output stream when created.
    """
    return bescli.BESCLInterface(stdout=io.StringIO())


def output(cli_obj):
    """Everything the interface wrote with poutput / pfeedback."""
    return cli_obj.stdout.getvalue()


@pytest.fixture
def connected_cli(cli, offline_login):
    """A bescli interface with an offline connection."""
    cli.bes_conn = make_conn()
    cli.BES_ROOT_SERVER = "https://bigfix.example:52311"
    cli.BES_USER_NAME = "me"
    cli.BES_PASSWORD = "<PASSWORD>"
    return cli


def write_conf(folder, **values):
    """Write a besapi config file."""
    path = folder / "test.conf"
    lines = ["[besapi]"] + [f"{key} = {value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ---------- config ----------


def test_starts_without_config(cli):
    """Test a fresh interface with no config found."""
    assert cli.bes_conn is None
    assert cli.BES_USER_NAME is None
    assert cli.prompt == "BigFix> "


def test_conf_loads_values_and_logs_in(cli, isolated, monkeypatch):
    """Test that a complete config file is loaded and triggers a login."""
    logins = []
    monkeypatch.setattr(cli, "do_login", lambda *a: logins.append(1))
    conf = write_conf(
        isolated,
        BES_ROOT_SERVER="https://bigfix.example:52311",
        BES_USER_NAME="me",
        BES_PASSWORD="<PASSWORD>",
    )

    cli.do_conf(conf)

    assert (cli.BES_ROOT_SERVER, cli.BES_USER_NAME, cli.BES_PASSWORD) == (
        "https://bigfix.example:52311",
        "me",
        "<PASSWORD>",
    )
    assert cli.conf_path == conf
    assert logins == [1]


def test_conf_incomplete_does_not_log_in(cli, isolated, monkeypatch):
    """Test that a config file missing the password does not log in."""
    logins = []
    monkeypatch.setattr(cli, "do_login", lambda *a: logins.append(1))
    conf = write_conf(isolated, BES_ROOT_SERVER="https://h:52311", BES_USER_NAME="me")

    cli.do_conf(conf)

    assert cli.BES_PASSWORD is None
    assert logins == []


def test_saveconf_writes_config(connected_cli, isolated):
    """Test that the current config is saved to the config file path."""
    connected_cli.CONFPARSER.add_section("besapi")
    connected_cli.CONFPARSER.set("besapi", "BES_USER_NAME", "me")
    connected_cli.conf_path = str(isolated / "saved.conf")

    connected_cli.do_saveconf()

    assert "BES_USER_NAME = me" in (isolated / "saved.conf").read_text(
        encoding="utf-8"
    ).replace("bes_user_name", "BES_USER_NAME")


# ---------- login ----------


class FakeBESConnection:
    """Stand-in for BESConnection with a configurable login result."""

    login_result = True
    error = None

    def __init__(self, user, password, root_server, **kwargs):
        self.args = (user, password, root_server)
        if FakeBESConnection.error:
            raise FakeBESConnection.error

    def login(self):
        """Return the configured login result."""
        return FakeBESConnection.login_result


@pytest.fixture
def fake_besconnection(monkeypatch):
    """Replace BESConnection used by bescli."""
    FakeBESConnection.login_result = True
    FakeBESConnection.error = None
    monkeypatch.setattr(bescli.besapi, "BESConnection", FakeBESConnection)
    return FakeBESConnection


def set_creds(cli_obj):
    """Give the interface complete credentials."""
    cli_obj.BES_USER_NAME = "me"
    cli_obj.BES_PASSWORD = "<PASSWORD>"
    cli_obj.BES_ROOT_SERVER = "https://bigfix.example:52311"


def test_login_success(cli, fake_besconnection):
    """Test a login with complete credentials."""
    set_creds(cli)
    cli.do_login()
    assert cli.bes_conn.args == ("me", "<PASSWORD>", "https://bigfix.example:52311")


def test_login_failed_clears_password(cli, fake_besconnection):
    """Test that a failed login clears the password and connection."""
    fake_besconnection.login_result = False
    set_creds(cli)
    cli.do_login()
    assert cli.bes_conn is None
    assert cli.BES_PASSWORD is None


def test_login_http_error_clears_password(cli, fake_besconnection):
    """Test that an HTTP error (bad password) clears the password and counts."""
    fake_besconnection.error = bescli.requests.exceptions.HTTPError("401")
    set_creds(cli)
    cli.do_login()
    assert cli.bes_conn is None
    assert cli.BES_PASSWORD is None
    assert cli.BES_ROOT_SERVER == "https://bigfix.example:52311"
    assert cli.num_errors == 1


def test_login_connection_error_clears_root_server(cli, fake_besconnection):
    """Test that a connection error clears the root server and counts."""
    fake_besconnection.error = bescli.requests.exceptions.ConnectionError("no route")
    set_creds(cli)
    cli.do_login()
    assert cli.bes_conn is None
    assert cli.BES_ROOT_SERVER is None
    assert cli.BES_PASSWORD == "<PASSWORD>"
    assert cli.num_errors == 1


def test_login_prompts_for_missing_values(cli, fake_besconnection, monkeypatch):
    """Test that missing user, root server and password are prompted for."""
    answers = iter(["typed_user", "https://typed.example:52311"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(bescli.getpass, "getpass", lambda *a: "typed_pw")

    cli.do_login()

    assert cli.bes_conn.args == (
        "typed_user",
        "typed_pw",
        "https://typed.example:52311",
    )
    assert cli.CONFPARSER.get("besapi", "BES_USER_NAME") == "typed_user"


# ---------- clear / ls / logout ----------


@pytest.mark.parametrize(
    "arg, expected",
    [
        ("root", (None, "me", "<PASSWORD>")),
        ("user", ("https://bigfix.example:52311", None, "<PASSWORD>")),
        ("pass", ("https://bigfix.example:52311", "me", None)),
        ("", (None, None, None)),
    ],
)
def test_clear(connected_cli, arg, expected):
    """Test clearing one or all saved parameters."""
    connected_cli.do_clear(arg)
    assert (
        connected_cli.BES_ROOT_SERVER,
        connected_cli.BES_USER_NAME,
        connected_cli.BES_PASSWORD,
    ) == expected


def test_ls_connected(connected_cli):
    """Test the status listing of a connected interface."""
    connected_cli.do_ls()
    out = output(connected_cli)
    assert "Connected: True" in out
    assert "BES_ROOT_SERVER: https://bigfix.example:52311" in out
    assert "BES_USER_NAME: me" in out
    assert "Password Length: 10" in out
    assert "Current Site Path: master" in out


def test_ls_not_connected(cli):
    """Test the status listing without a connection."""
    cli.do_ls()
    out = output(cli)
    assert "Connected: False" in out
    assert "Password Length: 0" in out
    assert "Current Site Path" not in out


def test_logout_closes_session(connected_cli):
    """Test that logout closes the connection's session."""
    connected_cli.do_logout()
    assert connected_cli.bes_conn.session.closed is True


# ---------- requests ----------


def test_get_not_logged_in(cli):
    """Test get without a connection."""
    cli.do_get("sites")
    assert "Not currently logged in" in output(cli)


def test_get_strips_root_server_prefix(connected_cli):
    """Test that a full URL is reduced to the api path."""
    connected_cli.bes_conn.session.response_text = SITES_XML
    connected_cli.do_get(f"{API}/sites")

    assert connected_cli.bes_conn.session.calls[-1][1] == f"{API}/sites"
    assert "<Name>Demo</Name>" in output(connected_cli)


def test_get_object_path(connected_cli):
    """Test `get path.Element.Child` output of an objectified attribute."""
    connected_cli.bes_conn.session.response_text = SITES_XML
    connected_cli.do_get("sites.CustomSite.Name")
    assert output(connected_cli).strip() == "Demo"


def test_delete(connected_cli):
    """Test delete requests, with any root server prefix replaced.

    The prefix may name the server differently (such as by IP address).
    """
    connected_cli.do_delete("https://10.0.0.5:52311/api/action/123")
    assert connected_cli.bes_conn.session.calls[-1][:2] == (
        "delete",
        f"{API}/action/123",
    )


def test_delete_not_logged_in(cli):
    """Test delete without a connection."""
    cli.do_delete("action/123")
    assert "Not currently logged in" in output(cli)


def test_query(connected_cli):
    """Test a session relevance query through the command parser."""
    connected_cli.bes_conn.session.response_text = QUERY_ANSWERS_XML
    connected_cli.onecmd("query names of bes sites")

    # feedback (Q: / A:) and the answers all go to stdout in cmd2 4:
    assert output(connected_cli) == "Q: names of bes sites\nA: \nBES Support\nDemo\n"
    posted = connected_cli.bes_conn.session.calls[-1][2]["data"]["relevance"]
    assert "names of bes sites" in posted.replace("%20", " ")


def test_query_not_logged_in(cli, monkeypatch, capsys):
    """Test that query tries to log in, then reports the error."""
    monkeypatch.setattr(cli, "do_login", lambda *a: None)
    cli.onecmd("query names of bes sites")
    assert "can't query without login" in capsys.readouterr().err


def test_am_i_main_operator(connected_cli):
    """Test the main operator check output."""
    connected_cli.do_am_i_main_operator()
    assert "Am I Main Operator? True" in output(connected_cli)


def test_site_path_commands(connected_cli):
    """Test getting and setting the current site path."""
    connected_cli.do_set_current_site("custom/Demo")
    connected_cli.do_get_current_site()
    out = output(connected_cli)
    assert "New Site Path: `custom/Demo`" in out
    assert "Current Site Path: `custom/Demo`" in out


def test_get_action_and_operator(connected_cli):
    """Test the action and operator lookups request the right paths."""
    connected_cli.do_get_action("123")
    connected_cli.do_get_operator("someone")
    urls = [call[1] for call in connected_cli.bes_conn.session.calls]
    assert urls == [f"{API}/action/123", f"{API}/operator/someone"]


def test_serverinfo(connected_cli):
    """Test that server info JSON is pretty printed."""
    connected_cli.bes_conn.session.response_text = '{"version": "11.0"}'
    connected_cli.do_serverinfo()
    out = output(connected_cli)
    assert "Server Info for https://bigfix.example:52311" in out
    assert '{\n  "version": "11.0"\n}' in out


def test_export_item_by_resource_command(cli, export_conn, isolated):
    """Test exporting one item to the current folder."""
    cli.bes_conn = export_conn
    cli.do_export_item_by_resource(f"{API}/task/custom/Demo/12724")

    assert (isolated / f"{GIT_FILE}.bes").read_text(encoding="utf-8") == task_xml(
        GIT_TITLE
    )
    assert f"{GIT_FILE}.bes" in output(cli)


def test_export_site_command(cli, export_conn, isolated):
    """Test exporting a site to the current folder without site folder or ids."""
    cli.bes_conn = export_conn
    cli.do_export_site("custom/Demo")
    assert (isolated / "Task" / f"{GIT_FILE}.bes").exists()


@pytest.mark.parametrize(
    "command", ["do_upload", "do_create_group", "do_create_user", "do_create_site"]
)
def test_file_commands_unreadable_file(connected_cli, command):
    """Test that file based commands report an unreadable file."""
    getattr(connected_cli, command)("/no/such/file.bes")
    assert "/no/such/file.bes is not a readable file" in output(connected_cli)
    assert connected_cli.bes_conn.session.calls == []


# ---------- misc ----------


def test_debug_sets_logger_level(cli):
    """Test that debug mode switches the besapi logger level."""
    cli.do_debug("1")
    assert logging.getLogger("besapi").level == logging.DEBUG
    cli.do_debug("")
    assert logging.getLogger("besapi").level == logging.WARNING


def test_parse_help_resources(connected_cli):
    """Test that api resources are parsed from the server's help output."""
    connected_cli.bes_conn.session.response_text = (
        "GET https://bigfix.example:52311/api/sites\n"
        "POST https://bigfix.example:52311/api/query \n"
        "other text\n"
    )
    assert connected_cli.parse_help_resources() == ["sites", "query"]


def test_parse_help_resources_offline_defaults(cli):
    """Test the default resource list without a connection."""
    assert "sites" in cli.parse_help_resources()


def test_complete_api_resources(connected_cli):
    """Test tab completion of api resources."""
    connected_cli.api_resources = ["sites", "serverinfo", "query"]
    assert connected_cli.complete_api_resources("s", "get s", 4, 5) == [
        "sites",
        "serverinfo",
    ]


def test_error_count_and_version(cli):
    """Test the error count and version output."""
    cli.num_errors = 2
    cli.do_error_count()
    cli.do_version()
    out = output(cli)
    assert "Error Count: 2" in out
    assert f"besapi version: {bescli.__version__}" in out
