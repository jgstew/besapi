"""Tests for examples/bigfix_root_server_upgrade_win.py, on any OS.

The BigFix REST API, the Windows registry, PowerShell and sqlcmd are all faked.
"""

import asyncio
import datetime
import importlib.util
import io
import json
import logging
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_PATH = os.path.join(ROOT, "examples", "bigfix_root_server_upgrade_win.py")
COMPAT_PATH = os.path.join(
    ROOT, "examples", "bigfix_root_server_upgrade_win_compat.yaml"
)

ES_KEY = r"SOFTWARE\Wow6432Node\BigFix\Enterprise Server"


@pytest.fixture
def upgrade():
    """Load the example script as a module, without running main()."""
    spec = importlib.util.spec_from_file_location(
        "root_server_upgrade_win", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def compat(upgrade):
    """The real compatibility data file."""
    return upgrade.load_compat(COMPAT_PATH)


# a small made up compatibility table, so path search tests don't depend on
# the real data changing over time:
FIXTURE_COMPAT = {
    "bigfix_server": {
        "1.0": {
            "windows": {"A": "1.0.0", "B": "1.0.0"},
            "mssql": {"S1": "1.0.0", "S2": "1.0.0"},
            "min_db_compat_level": 110,
        },
        "2.0": {
            "windows": {"B": "2.0.0", "C": "2.0.3"},
            "mssql": {"S2": "2.0.0", "S3": "2.0.0"},
            "min_db_compat_level": 120,
        },
    },
    "bigfix_upgrade_paths": {
        "2.0": {"min_from": "1.0.7", "prerequisites": ["enable the thing"]}
    },
    "mssql_on_windows": {
        "versions": {
            "S1": {"A": "SP3"},
            "S2": {"A": "RTM", "B": "RTM"},
            "S3": {"B": "RTM", "C": "RTM"},
        }
    },
    "mssql_upgrade_paths": {"versions": {"S2": {"S1": "SP3"}, "S3": {"S2": "RTM"}}},
    "windows_upgrade_paths": {"versions": {"A": ["B"], "B": ["C"]}},
    "mssql_max_compat_level": {"versions": {"S1": 100, "S2": 140, "S3": 160}},
}


class FakeConnection:
    """Answers session relevance and REST GETs from dicts of query/path -> result.

    A result that is an Exception instance is raised instead of returned.
    """

    def __init__(self, answers, get_responses=None, main_operator=False):
        self.answers = answers
        self.get_responses = get_responses or {}
        self.main_operator = main_operator

    def session_relevance_json(self, relevance, **kwargs):
        answer = self.answers.get(relevance)
        if answer is None:
            return {"result": [], "error": "The operator is not defined."}
        return {"result": answer}

    def get(self, path, **kwargs):
        response = self.get_responses[path]
        if isinstance(response, Exception):
            raise response
        return types.SimpleNamespace(text=response)

    def am_i_main_operator(self):
        return self.main_operator


class FakeHost:
    """Stands in for the local Windows host: registry, PowerShell, sqlcmd, ports."""

    def __init__(
        self,
        registry=None,
        powershell=None,
        sql=None,
        files=(),
        ports=(),
        admin=True,
        windows=True,
        sql_handler=None,
    ):
        # registry: {key path: {value name: data}}, subkeys derived from paths
        self.registry = registry or {}
        self.powershell = powershell or {}
        self.sql = sql or {}
        # called for queries not in `sql`, like backups with generated paths:
        self.sql_handler = sql_handler
        self.files = set(files)
        self.ports = set(ports)
        self.admin = admin
        self.windows = windows
        self.ran = []
        self.sql_ran = []
        self.shares = []
        # share tooling:
        self.share_errors = {}
        self.disconnected = []
        self.reachable = set()
        self.dirs = set()
        self.write_errors = {}
        self.free_bytes = 500 * 1024**3
        self.users = {}
        self.user_passwords = {}
        self.run_handler = None
        self.secret_runs = []
        self.captured = []
        self.capture_result = (0, "Status Name\nRunning BESClient\n")

    def is_windows(self):
        return self.windows

    def is_admin(self):
        return self.admin

    def reg_values(self, path):
        if path not in self.registry:
            return None
        return dict(self.registry[path])

    def reg_subkeys(self, path):
        prefix = path + "\\"
        names = {
            key[len(prefix) :].split("\\")[0]
            for key in self.registry
            if key.startswith(prefix)
        }
        if not names and path not in self.registry:
            return None
        return sorted(names)

    def powershell_json(self, script):
        result = self.powershell[script]
        if isinstance(result, Exception):
            raise result
        return result

    def sqlcmd(self, server, query):
        self.sql_ran.append(query)
        if query not in self.sql and self.sql_handler:
            return self.sql_handler(server, query)
        result = self.sql[query]
        if isinstance(result, Exception):
            raise result
        return result

    def port_open(self, port):
        return port in self.ports

    def file_exists(self, path):
        return path in self.files

    def walk(self, top):
        """Like os.walk over `files`, honouring edits to the yielded dirnames."""
        # like os.walk, a drive root keeps its backslash:
        stack = [top if top.endswith(":\\") else top.rstrip("\\")]
        while stack:
            folder = stack.pop()
            prefix = ntpath.join(folder, "").lower()
            dirnames, filenames = set(), []
            for path in self.files:
                if path.lower().startswith(prefix):
                    parts = path[len(prefix) :].split("\\")
                    if len(parts) == 1:
                        filenames.append(parts[0])
                    else:
                        dirnames.add(parts[0])
            dirnames = sorted(dirnames)
            yield folder, dirnames, sorted(filenames)
            stack.extend(ntpath.join(folder, name) for name in reversed(dirnames))

    def connect_share(self, share_root, user, password):
        self.shares.append((share_root, user, password))
        error = self.share_errors.get(share_root)
        if error:
            raise error

    def disconnect_share(self, share_root):
        self.disconnected.append(share_root)

    def tcp_connect(self, server, port, timeout=5):
        if (server, port) in self.reachable:
            return None
        return "timed out"

    def dir_exists(self, path):
        return path in self.dirs or any(
            f.lower().startswith(path.lower().rstrip("\\") + "\\") for f in self.files
        )

    def make_dirs(self, path):
        self.dirs.add(path)

    def write_probe(self, folder):
        error = self.write_errors.get(folder)
        if error:
            raise error

    def disk_free(self, path):
        return self.free_bytes

    def local_user(self, name):
        return self.users.get(name.lower())

    def create_local_user(self, name, password, comment):
        self.users[name.lower()] = {"name": name, "comment": comment}
        self.user_passwords[name.lower()] = password

    def set_local_user_password(self, name, password):
        self.user_passwords[name.lower()] = password

    def delete_local_user(self, name):
        self.users.pop(name.lower(), None)

    def run(self, cmd):
        self.ran.append(cmd)
        return self.run_handler(cmd) if self.run_handler else ""

    def run_secret(self, cmd):
        self.secret_runs.append(cmd)

    def run_capture(self, cmd, timeout=300):
        self.captured.append(cmd)
        return self.capture_result


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "version,expected",
    [
        ("10.50.2500.0", "2008 R2"),
        ("10.51.2500.0", "2008 R2"),
        ("10.0.6000.29", "2008"),
        ("11.0.7001.0", "2012"),
        ("14.0.3456.2", "2017"),
        ("16.0.4135.4", "2022"),
        ("17.0.1000.7", "2025"),
        ("garbage", None),
        (None, None),
    ],
)
def test_sql_major_from_version(upgrade, version, expected):
    """Test SQL Server build numbers map to their product version."""
    assert upgrade.sql_major_from_version(version) == expected


@pytest.mark.parametrize(
    "os_name,expected",
    [
        ("Win2012R2 6.3.9600", "2012 R2"),
        ("Win2019 10.0.17763.1234", "2019"),
        ("Win2025 10.0.26100", "2025"),
        ("Windows Server 2012 R2 Standard", "2012 R2"),
        ("Windows Server 2022 Datacenter", "2022"),
        ("Win10 10.0.19045", None),
        (None, None),
    ],
)
def test_windows_server_version(upgrade, os_name, expected):
    """Test BigFix OS property values and registry product names are parsed."""
    assert upgrade.windows_server_version(os_name) == expected


def test_version_tuple_compares_numerically(upgrade):
    """Test 10.0.10 sorts after 10.0.7, which a string compare gets wrong."""
    assert upgrade.version_tuple("10.0.10") > upgrade.version_tuple("10.0.7.52")
    assert upgrade.bigfix_line("10.0.7.52") == "10.0"


@pytest.mark.parametrize(
    "level,minimum,expected",
    [("SP1", "SP3", False), ("SP3", "SP3", True), ("SP4", "RTM", True)],
)
def test_servicing_level_at_least(upgrade, level, minimum, expected):
    """Test SQL servicing levels compare RTM < SP1 < ... < SP4."""
    assert upgrade.servicing_level_at_least(level, minimum) is expected


def test_servicing_level_unknown(upgrade):
    """Test an unknown servicing level is unknown, not a pass or a fail."""
    assert upgrade.servicing_level_at_least(None, "SP3") is None


# ---------------------------------------------------------------- compat


def test_check_state_supported(upgrade):
    """Test a combination that every source supports has no problems."""
    result = upgrade.check_state(
        FIXTURE_COMPAT, {"bigfix": "1.0.7", "windows": "B", "mssql": "S2"}
    )
    assert result == {"supported": True, "problems": [], "prerequisites": []}


def test_check_state_problems(upgrade):
    """Test each unsupported part of a combination is reported."""
    result = upgrade.check_state(
        FIXTURE_COMPAT, {"bigfix": "2.0.1", "windows": "C", "mssql": "S1"}
    )
    assert result["supported"] is False
    problems = "\n".join(result["problems"])
    assert "SQL Server S1 is not supported on Windows Server C" in problems
    assert "BigFix 2.0.1 does not support Windows Server C (needs 2.0.3" in problems
    assert "BigFix 2.0.1 does not support SQL Server S1" in problems


def test_check_state_servicing_level(upgrade):
    """Test a known servicing level below the minimum is a problem, unknown is a
    prerequisite.
    """
    known = upgrade.check_state(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "A", "mssql": "S1", "mssql_level": "SP1"},
    )
    unknown = upgrade.check_state(
        FIXTURE_COMPAT, {"bigfix": "1.0.7", "windows": "A", "mssql": "S1"}
    )
    assert known["supported"] is False
    assert any("SP3" in problem for problem in known["problems"])
    assert unknown["supported"] is True
    assert any("SP3" in item for item in unknown["prerequisites"])


def test_check_state_db_compat_level(upgrade):
    """Test a database compatibility level below BigFix's minimum is a problem."""
    result = upgrade.check_state(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "B", "mssql": "S2", "db_compat_level": 100},
    )
    assert result["supported"] is False
    assert any("compatibility level 100" in p for p in result["problems"])


def test_find_upgrade_path_all_states_supported(upgrade):
    """Test the path found only passes through supported combinations."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "A", "mssql": "S1"},
        {"windows": "C", "mssql": "S3"},
    )
    assert result["reachable"] is True
    assert [(s["component"], s["to"]) for s in result["steps"]] == [
        ("mssql", "S2"),
        ("windows", "B"),
        ("bigfix", "2.0.3"),
        ("mssql", "S3"),
        ("windows", "C"),
    ]
    for state in result["states"][1:]:
        assert upgrade.check_state(FIXTURE_COMPAT, state)["supported"] is True


def test_find_upgrade_path_prerequisites(upgrade):
    """Test step prerequisites: source servicing level and BigFix upgrade notes."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "A", "mssql": "S1"},
        {"windows": "C", "mssql": "S3"},
    )
    steps = {(s["component"], s["to"]): s for s in result["steps"]}
    assert any("SP3" in p for p in steps[("mssql", "S2")]["prerequisites"])
    assert "enable the thing" in steps[("bigfix", "2.0.3")]["prerequisites"]
    # the S1 databases are at 100, BigFix 2.0 needs 120:
    assert any("120" in p for p in steps[("bigfix", "2.0.3")]["prerequisites"])


def test_find_upgrade_path_rejected_first_steps(upgrade):
    """Test each blocked first step is reported with its reasons."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "A", "mssql": "S1"},
        {"windows": "C", "mssql": "S3"},
    )
    rejected = {(r["component"], r["to"]): r["reasons"] for r in result["rejected"]}
    assert ("windows", "B") in rejected
    assert any("S1" in reason for reason in rejected[("windows", "B")])


def test_find_upgrade_path_unreachable(upgrade):
    """Test a target with no supported path is reported, not guessed."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.7", "windows": "A", "mssql": "S1"},
        {"windows": "D", "mssql": "S3"},
    )
    assert result["reachable"] is False
    assert result["steps"] == []


def test_find_upgrade_path_bigfix_min_from(upgrade):
    """Test BigFix is first patched to the minimum it can upgrade from."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "1.0.0", "windows": "A", "mssql": "S1"},
        {"windows": "C", "mssql": "S3"},
    )
    bigfix_steps = [s["to"] for s in result["steps"] if s["component"] == "bigfix"]
    assert result["reachable"] is True
    assert bigfix_steps == ["1.0.7", "2.0.3"]


@pytest.mark.parametrize(
    "server,expected",
    [
        ("BIGFIX", True),
        ("bigfix\\SQLEXPRESS", True),
        ("localhost", True),
        (".", True),
        ("(local)\\BES", True),
        ("SQLHOST01", False),
        ("sqlhost01.example.com,1433", False),
    ],
)
def test_is_local_sql_server(upgrade, server, expected):
    """Test the SQL server BigFix uses is classified as local or remote."""
    assert upgrade.is_local_sql_server(server, "BIGFIX") is expected


def test_find_upgrade_path_already_there(upgrade):
    """Test no steps are needed when the target is the current state."""
    result = upgrade.find_upgrade_path(
        FIXTURE_COMPAT,
        {"bigfix": "2.0.3", "windows": "C", "mssql": "S3"},
        {"windows": "C", "mssql": "S3"},
    )
    assert result["reachable"] is True
    assert result["steps"] == []


def test_default_target(upgrade):
    """Test the default target is the newest Windows and SQL BigFix supports."""
    assert upgrade.default_target(FIXTURE_COMPAT) == {"windows": "C", "mssql": "S3"}


def test_real_compat_path_for_2012r2_sql2008r2(upgrade, compat):
    """Test the path from BigFix 10.0.7, Server 2012 R2, SQL 2008 R2 to 2025/2025."""
    current = {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"}
    result = upgrade.find_upgrade_path(
        compat, current, {"windows": "2025", "mssql": "2025"}
    )

    assert upgrade.check_state(compat, current)["supported"] is False
    assert result["reachable"] is True
    assert [(s["component"], s["to"]) for s in result["steps"]] == [
        ("mssql", "2017"),
        ("windows", "2019"),
        ("bigfix", "11.0.6"),
        ("mssql", "2025"),
        ("windows", "2025"),
    ]
    assert any("SP3" in p for p in result["steps"][0]["prerequisites"])


# ---------------------------------------------------------------- REST (tier 1)


def rest_answers(upgrade):
    """Relevance answers from a real BigFix 10.0.7 root server on 2012 R2."""
    answers = {
        upgrade.MASTHEAD_RELEVANCE: [[152178487, "bigfix.example.com"]],
        upgrade.ROOT_COMPUTER_RELEVANCE: [
            [11333902, "BIGFIX", "Win2012R2 6.3.9600", "Sat, 26 Sep 2026 12:55:58"]
        ],
        upgrade.ROOT_PROPERTIES_RELEVANCE: [
            ["OS", "Win2012R2 6.3.9600"],
            ["RAM", "17984 MB"],
            ["Computer Type", "Server"],
            ["Computer Type", "Virtual"],
            ["IP Address", "192.168.5.40"],
        ],
    }
    for query in upgrade.REST_QUERIES:
        answers.setdefault(query["relevance"], [7])
    return answers


SERVERINFO = json.dumps(
    {
        "version": "10.0.7.52",
        "dbType": "SQL Server",
        "dbVersion": "10.50.2500.0",
        "dbSchemaVersion": "Enterprise 10.70",
    }
)


def test_collect_rest_info(upgrade):
    """Test serverinfo, the masthead, root server properties and counts are
    collected.
    """
    conn = FakeConnection(
        rest_answers(upgrade),
        {
            "serverinfo": SERVERINFO,
            "admin/masthead/parameters": PermissionError("403 master operator"),
        },
    )
    info = upgrade.collect_rest_info(conn)

    assert info["serverinfo"]["dbVersion"] == "10.50.2500.0"
    assert info["masthead"] == {"serial": 152178487, "fqdn": "bigfix.example.com"}
    assert info["root_server"]["name"] == "BIGFIX"
    assert info["root_server"]["properties"]["Computer Type"] == ["Server", "Virtual"]
    assert info["main_operator"] is False
    # a 403 on one probe doesn't stop the rest:
    assert "403" in info["masthead_parameters"]["error"]
    assert all(q["name"] in info["counts"] for q in upgrade.REST_QUERIES)


def test_collect_rest_info_relevance_error(upgrade):
    """Test a relevance error is recorded for that query only."""
    answers = rest_answers(upgrade)
    del answers[upgrade.REST_QUERIES[0]["relevance"]]
    conn = FakeConnection(answers, {"serverinfo": SERVERINFO})

    info = upgrade.collect_rest_info(conn)

    assert "error" in info["counts"][upgrade.REST_QUERIES[0]["name"]]
    assert info["counts"][upgrade.REST_QUERIES[1]["name"]] == 7


def test_collect_rest_info_no_connection(upgrade):
    """Test no connection gives a skipped section, not an exception."""
    assert "skipped" in upgrade.collect_rest_info(None)


# ---------------------------------------------------------------- local (tier 2)


BIGFIX_FOLDER = r"C:\Program Files (x86)\BigFix Enterprise"
SERVER_FOLDER = BIGFIX_FOLDER + r"\BES Server"


def service(name, display_name, state="Running", start_mode="Auto", path=None):
    """A Win32_Service record as PowerShell returns it."""
    return {
        "Name": name,
        "DisplayName": display_name,
        "State": state,
        "StartMode": start_mode,
        "StartName": "LocalSystem",
        "PathName": path or f'"{SERVER_FOLDER}\\{name}.exe"',
    }


def local_host(upgrade, **overrides):
    """A fake BigFix root server like the real one: 2012 R2, local SQL 2008 R2."""
    registry = {
        ES_KEY: {
            "Version": "10.0.7.52",
            "EnterpriseServerFolder": SERVER_FOLDER + "\\",
            "wwwRootFolder": SERVER_FOLDER + "\\wwwrootbes\\",
            "Port": "52311",
        },
        ES_KEY
        + r"\MFSConfig": {
            "RESTUsername": "api_user",
            "RESTPassword": "{obf}c2VjcmV0c2VjcmV0",
            "RESTURL": "https://localhost:52311/api",
            "RESTPasswordEncryption": "1",
        },
        ES_KEY
        + r"\BESReports": {
            "WRHTTP": "https://localhost:888/",
            "SOAPPassword": "hunter2",
            "SOAPPasswordIsEncrypted": "1",
        },
        ES_KEY + r"\BESReports\Preferences": {"Private Data Directory": r"C:\wr"},
        ES_KEY + r"\FillAggregateDB": {"LocalDBDSN": "LocalBESReportingServer"},
        upgrade.WINDOWS_CURRENT_VERSION_KEY: {
            "ProductName": "Windows Server 2012 R2 Datacenter",
            "EditionID": "ServerDatacenter",
            "CurrentBuild": "9600",
            "InstallationType": "Server",
        },
        upgrade.SQL_INSTANCE_NAMES_KEY: {"MSSQLSERVER": "MSSQL10_50.MSSQLSERVER"},
        r"SOFTWARE\Microsoft\Microsoft SQL Server\MSSQL10_50.MSSQLSERVER\Setup": {
            "Version": "10.51.2500.0",
            "Edition": "Developer Edition",
            "PatchLevel": "10.51.2500.0",
            "SQLDataRoot": r"C:\Program Files\Microsoft SQL Server\MSSQL10_50.MSSQLSERVER\MSSQL",
        },
        upgrade.ODBC_INI_KEY
        + r"\bes_bfenterprise": {
            "Driver": r"C:\Windows\system32\sqlncli11.dll",
            "Server": "(local)",
            "Database": "BFEnterprise",
        },
        upgrade.ODBC_INI_KEY
        + r"\LocalBESReportingServer": {
            "Driver": r"C:\Windows\system32\sqlncli11.dll",
            "Server": "(local)",
            "Database": "BESReporting",
        },
        upgrade.ODBC_INI_KEY + r"\SomeOtherApp": {"Server": "elsewhere"},
        upgrade.SESSION_MANAGER_KEY: {},
    }
    powershell = {
        upgrade.PS_COMPUTER_SYSTEM: {
            "Manufacturer": "Microsoft Corporation",
            "Model": "Virtual Machine",
            "PartOfDomain": False,
            "Domain": "WORKGROUP",
            "TotalPhysicalMemory": 18871259136,
            "NumberOfLogicalProcessors": 4,
        },
        upgrade.PS_OPERATING_SYSTEM: {
            "LastBootUpTime": "2025-07-07T12:45:04.7015660-04:00"
        },
        upgrade.PS_DISKS: [
            {"DeviceID": "C:", "Size": 536316211200, "FreeSpace": 171494592512}
        ],
        upgrade.PS_SERVICES: [
            service("BESClient", "BES Client"),
            service("BESRootServer", "BES Root Server"),
            service("FillDB", "BES FillDB"),
            service(
                "UnmanagedAssetImporter-NMAP",
                "BES NMAP Unmanaged Asset Importer",
                state="Stopped",
            ),
            service(
                "MSSQLSERVER",
                "SQL Server (MSSQLSERVER)",
                path='"C:\\sql\\sqlservr.exe" -sMSSQLSERVER',
            ),
            service(
                "SQLSERVERAGENT",
                "SQL Server Agent (MSSQLSERVER)",
                start_mode="Manual",
                state="Stopped",
                path='"C:\\sql\\SQLAGENT.EXE" -i MSSQLSERVER',
            ),
        ],
        upgrade.PS_FEATURES: ["Web-Server"],
    }
    sql = {
        upgrade.SQL_SERVER_PROPERTIES: [
            [
                "10.50.2500.0",
                "SP1",
                "Developer Edition (64-bit)",
                "SQL_Latin1_General_CP1_CI_AS",
                "1",
            ]
        ],
        upgrade.SQL_DATABASES: [
            ["BESReporting", "ONLINE", "SIMPLE", "100", "4", "NULL"],
            ["BFEnterprise", "ONLINE", "SIMPLE", "100", "77050", "NULL"],
            ["master", "ONLINE", "SIMPLE", "100", "5", "NULL"],
        ],
    }
    kwargs = {
        "registry": registry,
        "powershell": powershell,
        "sql": sql,
        "files": {
            SERVER_FOLDER + r"\BESRootServer.exe",
            SERVER_FOLDER + r"\actionsite.afxm",
            SERVER_FOLDER + r"\wwwrootbes\masthead\masthead.afxm",
            SERVER_FOLDER + r"\wwwrootbes\bfsites\actionsite.afxm",
            SERVER_FOLDER + r"\FillDBData\bufferdir\license.crt",
            BIGFIX_FOLDER + r"\BES Installers\license\license.crt",
            BIGFIX_FOLDER + r"\BES Installers\license\masthead.afxm",
            BIGFIX_FOLDER + r"\BES Installers\license\other.afxm",
        },
        "ports": {52311, 888},
    }
    kwargs.update(overrides)
    return FakeHost(**kwargs)


def test_is_local_root_server(upgrade):
    """Test the root server is detected from its registry key on Windows."""
    assert upgrade.is_local_root_server(local_host(upgrade)) is True
    assert upgrade.is_local_root_server(local_host(upgrade, registry={})) is False
    assert upgrade.is_local_root_server(local_host(upgrade, windows=False)) is False


def test_collect_local_info_not_root_server(upgrade):
    """Test tier 2 sections are skipped when not running on the root server."""
    info = upgrade.collect_local_info(local_host(upgrade, windows=False))
    assert "not running on" in info["skipped"]


def test_collect_local_info_requires_admin(upgrade):
    """Test tier 2 on the root server without admin rights says so."""
    info = upgrade.collect_local_info(local_host(upgrade, admin=False))
    assert "administrator" in info["skipped"]


def test_collect_local_info(upgrade):
    """Test Windows, SQL and BigFix details are discovered on the root server."""
    info = upgrade.collect_local_info(local_host(upgrade))

    assert info["windows"]["ProductName"] == "Windows Server 2012 R2 Datacenter"
    assert info["windows"]["pending_reboot"] == {
        "component_based_servicing": False,
        "windows_update": False,
        "pending_file_rename": False,
    }
    assert info["hardware"]["computer_system"]["Model"] == "Virtual Machine"
    assert info["sql"]["instances"]["MSSQLSERVER"]["Version"] == "10.51.2500.0"
    assert info["sql"]["bigfix_sql_server"] == "(local)"
    assert info["sql"]["server_properties"]["ProductLevel"] == "SP1"
    assert info["sql"]["databases"]["BFEnterprise"]["compatibility_level"] == 100
    assert info["bigfix"]["install_folder"] == SERVER_FOLDER
    assert info["bigfix"]["ports"]["52311"] is True
    assert [s["Name"] for s in info["bigfix"]["services"]] == [
        "BESClient",
        "BESRootServer",
        "FillDB",
        "UnmanagedAssetImporter-NMAP",
    ]


def test_collect_local_info_sql_agent(upgrade):
    """Test the SQL Server Agent service is collected, SQLSERVERAGENT by name."""
    info = upgrade.collect_local_info(local_host(upgrade))

    assert "'SQL*'" in upgrade.PS_SERVICES
    assert [s["Name"] for s in info["sql"]["services"]] == [
        "MSSQLSERVER",
        "SQLSERVERAGENT",
    ]
    assert [s["Name"] for s in info["sql"]["agents"]] == ["SQLSERVERAGENT"]


def test_collect_local_info_dsns_from_registry(upgrade):
    """Test the Web Reports DSN named in the registry is found, unrelated DSNs not."""
    dsns = upgrade.collect_local_info(local_host(upgrade))["sql"]["bigfix_dsns"]

    assert sorted(path.split("\\")[-1] for path in dsns) == [
        "LocalBESReportingServer",
        "bes_bfenterprise",
    ]


def test_collect_local_info_ports_from_registry(upgrade):
    """Test the Web Reports port comes from WRHTTP, not only the default list."""
    ports = upgrade.collect_local_info(local_host(upgrade))["bigfix"]["ports"]

    assert ports["888"] is True
    assert ports["52311"] is True


def test_key_files_searched(upgrade):
    """Test key files are found anywhere under BigFix Enterprise, skipping big data
    folders.
    """
    key_files = upgrade.collect_local_info(local_host(upgrade))["bigfix"]["key_files"]

    assert key_files["searched"] == [BIGFIX_FOLDER, "C:\\"]
    assert key_files["found"] == {
        "masthead.afxm": [
            BIGFIX_FOLDER + r"\BES Installers\license\masthead.afxm",
            SERVER_FOLDER + r"\wwwrootbes\masthead\masthead.afxm",
        ],
        # the copy under FillDBData\bufferdir is skipped:
        "license.crt": [BIGFIX_FOLDER + r"\BES Installers\license\license.crt"],
        "license.pvk": [],
        "actionsite.afxm": [SERVER_FOLDER + r"\actionsite.afxm"],
    }
    # bfsites is skipped too:
    assert key_files["other_afxm"] == [
        BIGFIX_FOLDER + r"\BES Installers\license\other.afxm"
    ]


def test_key_files_search_depth_limited(upgrade):
    """Test the search stops at a bounded depth."""
    deep = BIGFIX_FOLDER + r"\a\b\c\d\e\license.pvk"
    host = local_host(upgrade)
    host.files.add(deep)

    key_files = upgrade.collect_local_info(host)["bigfix"]["key_files"]

    assert deep not in key_files["found"]["license.pvk"]


def test_collect_local_info_probe_failure(upgrade):
    """Test one failing probe is recorded and the rest still run."""
    host = local_host(upgrade)
    host.sql[upgrade.SQL_DATABASES] = RuntimeError("sqlcmd not found")

    info = upgrade.collect_local_info(host)

    assert "sqlcmd not found" in info["sql"]["databases"]["error"]
    assert info["sql"]["server_properties"]["ProductLevel"] == "SP1"


def test_pending_reboot_detected(upgrade):
    """Test each pending reboot indicator is detected."""
    host = local_host(upgrade)
    host.registry[upgrade.CBS_REBOOT_PENDING_KEY] = {}
    host.registry[upgrade.SESSION_MANAGER_KEY] = {
        "PendingFileRenameOperations": ["\\??\\C:\\x"]
    }

    assert upgrade.pending_reboot(host) == {
        "component_based_servicing": True,
        "windows_update": False,
        "pending_file_rename": True,
    }


# ---------------------------------------------------------------- report


def test_redact(upgrade):
    """Test secrets are removed at any depth, other values kept."""
    data = {
        "RESTPassword": "{obf}abc",
        "nested": [{"Password": "x", "pwd": "y", "Server": "BIGFIX"}],
        "conn": "Driver=SQL;Server=BIGFIX;PWD=hunter2;UID=sa",
        "other": "{dpapi}abcd",
        "user": "api_user",
    }
    result = upgrade.redact(data)

    text = json.dumps(result)
    for secret in ("abc", "hunter2", '"x"', '"y"', "abcd"):
        assert secret not in text
    assert result["nested"][0]["Server"] == "BIGFIX"
    assert result["user"] == "api_user"
    assert "UID=sa" in result["conn"]


def test_redact_keeps_harmless_values(upgrade):
    """Test flags and folders with secret-like key names are kept, keys are not."""
    result = upgrade.redact(
        {
            "Private Data Directory": r"C:\wr",
            "SOAPPasswordIsEncrypted": "1",
            "RESTPasswordEncryption": "1",
            "SOAPPassword": "hunter2",
            "PrivateKey": "abc",
            # a list of where license.pvk files are, not the key itself:
            "license.pvk": [r"C:\\keys\\license.pvk"],
        }
    )
    assert result["Private Data Directory"] == r"C:\wr"
    assert result["SOAPPasswordIsEncrypted"] == "1"
    assert result["RESTPasswordEncryption"] == "1"
    assert result["SOAPPassword"] == upgrade.REDACTED
    assert result["PrivateKey"] == upgrade.REDACTED
    assert result["license.pvk"] == [r"C:\\keys\\license.pvk"]


def test_redact_hosts(upgrade):
    """Test host names can optionally be masked too."""
    result = upgrade.redact(
        {"fqdn": "bigfix.example.com", "ip": "192.168.5.40", "RAM": "1 MB"},
        hosts=["bigfix.example.com"],
    )
    assert "bigfix.example.com" not in json.dumps(result)
    assert "192.168.5.40" not in json.dumps(result)
    assert result["RAM"] == "1 MB"


def test_current_state_remote_only(upgrade):
    """Test the current state comes from serverinfo and the root's OS property remotely."""
    conn = FakeConnection(rest_answers(upgrade), {"serverinfo": SERVERINFO})
    rest = upgrade.collect_rest_info(conn)

    state = upgrade.current_state(rest, {"skipped": "not running on root server"})

    assert state == {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"}


def test_current_state_local_adds_detail(upgrade):
    """Test local details add the SQL servicing level and database compat level."""
    local = upgrade.collect_local_info(local_host(upgrade))

    state = upgrade.current_state({"skipped": "no connection"}, local)

    assert state == {
        "bigfix": "10.0.7.52",
        "windows": "2012 R2",
        "mssql": "2008 R2",
        "mssql_level": "SP1",
        "db_compat_level": 100,
    }


def test_build_report(upgrade, compat):
    """Test the report has every section, is JSON, and has no secrets in it."""
    conn = FakeConnection(rest_answers(upgrade), {"serverinfo": SERVERINFO})

    report = upgrade.build_report(conn, local_host(upgrade), compat)

    for section in ("meta", "rest", "local", "upgrade_assessment"):
        assert section in report
    assessment = report["upgrade_assessment"]
    assert assessment["current_state"]["mssql_level"] == "SP1"
    assert assessment["current_state_check"]["supported"] is False
    assert assessment["compatibility"]["reachable"] is True
    text = json.dumps(report)
    assert "c2VjcmV0c2VjcmV0" not in text
    assert "hunter2" not in text


# ---------------------------------------------------------------- walkthrough


def test_service_stop_order(upgrade):
    """Test HCL's stop order, with any other BES services stopped first."""
    services = [
        {"Name": "BESClient", "DisplayName": "BES Client"},
        {"Name": "BESRootServer", "DisplayName": "BES Root Server"},
        {"Name": "FillDB", "DisplayName": "BES FillDB"},
        {"Name": "BESWebReportsServer", "DisplayName": "BES Web Reports Server"},
        {"Name": "GatherDB", "DisplayName": "BES GatherDB"},
        {"Name": "BESWebUI", "DisplayName": "BES WebUI"},
        {"Name": "BESPluginService", "DisplayName": "BES Server Plugin Service"},
        {"Name": "BESProxyAgent", "DisplayName": "BES Proxy Agent"},
    ]
    assert upgrade.service_stop_order(services) == [
        "BESPluginService",
        "BESProxyAgent",
        "BESWebUI",
        "BESWebReportsServer",
        "BESClient",
        "GatherDB",
        "FillDB",
        "BESRootServer",
    ]


def test_build_steps_from_path(upgrade, compat):
    """Test each upgrade in the path gets stop, snapshot, upgrade and validate
    steps.
    """
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    steps = upgrade.build_steps(path, local_sql=True)
    ids = [step.id for step in steps]

    # BigFix is stopped before the backup, and stays stopped for the first snapshot:
    # with the service pack level unknown, it gets a step and a check too:
    assert ids[:6] == [
        "preflight",
        "stop_services_0",
        "backup",
        "snapshot_1",
        "service_pack_1",
        "upgrade_1_mssql_2017",
    ]
    assert ids[6:8] == ["start_services_1", "validate_1"]
    second = ids.index("upgrade_2_windows_2019")
    assert ids[second - 2 : second] == ["stop_services_2", "snapshot_2"]
    assert ids[-2:] == ["final_validation", "cleanup"]
    assert len(ids) == len(set(ids))
    backup = next(step for step in steps if step.id == "backup")
    assert backup.actions == [
        "registry_export",
        "key_files",
        "masthead",
        "client_data",
        "folder_backup",
        "db_info",
        "sql_backup",
        "server_keys",
        "restore_notes",
        "verify_backup",
    ]
    assert "remote_processes" in steps[1].actions


def test_build_steps_remote_sql(upgrade, compat):
    """Test SQL upgrade steps are replaced by a reminder when SQL is remote."""
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    steps = {step.id: step for step in upgrade.build_steps(path, local_sql=False)}

    assert "remote" in steps["upgrade_1_mssql_2017"].instructions.lower()
    assert "sql_backup" not in steps["backup"].actions


def test_state_file_resume(upgrade, tmp_path):
    """Test completed steps are saved, and the next run starts after them."""
    state_path = str(tmp_path / "state.json")
    steps = [
        upgrade.Step("a", "A", "do a"),
        upgrade.Step("b", "B", "do b"),
        upgrade.Step("c", "C", "do c"),
    ]
    state = upgrade.load_state(state_path)
    assert upgrade.next_step(steps, state).id == "a"

    upgrade.mark_step_done(state, "a")
    upgrade.save_state(state_path, state)

    reloaded = upgrade.load_state(state_path)
    assert upgrade.next_step(steps, reloaded).id == "b"
    upgrade.mark_step_done(reloaded, "b")
    upgrade.mark_step_done(reloaded, "c")
    assert upgrade.next_step(steps, reloaded) is None


def test_compare_baselines(upgrade):
    """Test meaningful differences between two local reports are found."""
    before = {
        "bigfix": {
            "services": [
                {"Name": "BESRootServer", "State": "Running"},
                {"Name": "FillDB", "State": "Running"},
            ],
            "ports": {"52311": True},
        },
        "sql": {
            "databases": {
                "BFEnterprise": {"state": "ONLINE"},
                "BESReporting": {"state": "ONLINE"},
            },
            "server_properties": {"ProductVersion": "10.50.2500.0"},
        },
        "windows": {"ProductName": "Windows Server 2012 R2 Standard"},
    }
    after = json.loads(json.dumps(before))
    after["bigfix"]["services"][1]["State"] = "Stopped"
    after["bigfix"]["ports"]["52311"] = False
    del after["sql"]["databases"]["BESReporting"]
    after["sql"]["server_properties"]["ProductVersion"] = "14.0.3456.2"
    after["windows"]["ProductName"] = "Windows Server 2019 Standard"

    differences = "\n".join(upgrade.compare_baselines(before, after))

    assert "FillDB" in differences and "Stopped" in differences
    assert "52311" in differences
    assert "BESReporting" in differences
    assert "14.0.3456.2" in differences
    assert "Windows Server 2019" in differences
    assert upgrade.compare_baselines(before, before) == []


def test_walkthrough_requires_local_admin(upgrade):
    """Test the walkthrough refuses to run remotely or without admin."""
    with pytest.raises(SystemExit):
        upgrade.require_walkthrough_host(local_host(upgrade, windows=False))
    with pytest.raises(SystemExit):
        upgrade.require_walkthrough_host(local_host(upgrade, admin=False))
    upgrade.require_walkthrough_host(local_host(upgrade))


def test_sql_backup_commands(upgrade):
    """Test backups are COPY_ONLY with CHECKSUM and verified afterwards."""
    queries = upgrade.sql_backup_queries("BFEnterprise", r"D:\backup\BFEnterprise.bak")

    assert "BACKUP DATABASE [BFEnterprise]" in queries[0]
    assert "COPY_ONLY" in queries[0] and "CHECKSUM" in queries[0]
    assert "RESTORE VERIFYONLY" in queries[1]


def test_sql_backup_rejects_bad_names(upgrade):
    """Test database names and paths can't inject T-SQL."""
    with pytest.raises(ValueError):
        upgrade.sql_backup_queries("BFEnterprise]; DROP DATABASE x;--", r"D:\b.bak")
    with pytest.raises(ValueError):
        upgrade.sql_backup_queries("BFEnterprise", "D:\\b.bak'; DROP DATABASE x;--")


# ---------------------------------------------------------------- report driven checks

REPORT_NOW = datetime.datetime(2026, 9, 26, 18, 0, tzinfo=datetime.timezone.utc)

MASTHEAD_PARAMETERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
	<MastheadParameters Resource="https://localhost:52311/api/admin/masthead/parameters">
		<PortNumber>52311</PortNumber>
		<GatherInterval>Half Day</GatherInterval>
		<RequireFIPSCompliantCrypto>false</RequireFIPSCompliantCrypto>
		<Enhanced91Security>true</Enhanced91Security>
		<UseSHA256FileChecksOnly>false</UseSHA256FileChecksOnly>
	</MastheadParameters>
</BESAPI>
"""


def test_parse_windows_datetime(upgrade):
    """Test PowerShell's 7 digit fractional seconds and offset are parsed."""
    parsed = upgrade.parse_windows_datetime("2025-07-07T12:45:04.7015660-04:00")

    assert parsed == datetime.datetime(
        2025,
        7,
        7,
        12,
        45,
        4,
        701566,
        tzinfo=datetime.timezone(datetime.timedelta(hours=-4)),
    )
    assert upgrade.parse_windows_datetime("not a date") is None


def test_report_warnings_from_real_report(upgrade):
    """Test the warnings the real root server report should have raised."""
    local = upgrade.collect_local_info(local_host(upgrade))

    text = "\n".join(upgrade.report_warnings(local, now=REPORT_NOW))

    assert "BFEnterprise has never had a full backup" in text
    assert "BESReporting has never had a full backup" in text
    assert "Developer" in text and "licensing" in text
    assert "up for 446 days" in text
    assert "sqlncli11.dll" in text and "ODBC Driver 17" in text
    assert "low free disk space" not in text
    assert "not enough free space" not in text


def test_report_warnings_backup_space(upgrade):
    """Test a warning when the BigFix databases would not fit on the data volume."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_DISKS] = [
        {"DeviceID": "C:", "Size": 536316211200, "FreeSpace": 60 * 1024**3}
    ]
    local = upgrade.collect_local_info(host)

    text = "\n".join(upgrade.report_warnings(local, now=REPORT_NOW))

    assert "not enough free space on C: for a full backup" in text


BIGFIX_PRODUCT_KEY = (
    r"SOFTWARE\Classes\Installer\Products\FA64E94D3EC6D4947A7678A17DC0A9B4"
)
MISSING_MST = (
    r"C:\Users\Administrator\AppData\Local\Temp\2"
    r"\{721436A9-54C7-48BB-A10C-40D3DBB46403}\1033.MST"
)


def test_collect_local_info_installer_transforms(upgrade):
    """Test the BigFix Server MSI's cached transforms are found and checked."""
    host = local_host(upgrade)
    host.registry[BIGFIX_PRODUCT_KEY] = {
        "ProductName": "BigFix Server",
        "Transforms": "|" + MISSING_MST + ";:embedded.mst",
    }
    host.registry[r"SOFTWARE\Classes\Installer\Products\0000OTHER"] = {
        "ProductName": "Something Else",
        "Transforms": r"C:\other.mst",
    }

    info = upgrade.collect_local_info(host)

    assert info["bigfix"]["installer_transforms"] == {
        "BigFix Server": {MISSING_MST: False}
    }


def test_report_warnings_missing_installer_transform(upgrade):
    """Test a missing cached 1033.MST is warned about, a present one is not."""
    host = local_host(upgrade)
    host.registry[BIGFIX_PRODUCT_KEY] = {
        "ProductName": "BigFix Server",
        "Transforms": "|" + MISSING_MST,
    }
    text = "\n".join(
        upgrade.report_warnings(upgrade.collect_local_info(host), now=REPORT_NOW)
    )
    assert MISSING_MST in text and "transform" in text

    host.files.add(MISSING_MST)
    text = "\n".join(
        upgrade.report_warnings(upgrade.collect_local_info(host), now=REPORT_NOW)
    )
    assert "1033.MST" not in text


SUPERSOCKET_KEY = (
    r"SOFTWARE\Microsoft\Microsoft SQL Server\MSSQL10_50.MSSQLSERVER"
    r"\MSSQLServer\SuperSocketNetLib"
)


@pytest.mark.parametrize(
    ("certificate", "bigfix_version", "warned"),
    [
        ("", "10.0.7.52", True),
        ("0123456789abcdef0123456789abcdef01234567", "10.0.7.52", False),
        ("", "11.0.7.61", False),
    ],
)
def test_report_warnings_sql_self_generated_certificate(
    upgrade, certificate, bigfix_version, warned
):
    """Test SQL Server without a configured certificate warns, ODBC Driver 18 rejects
    it.
    """
    host = local_host(upgrade)
    host.registry[SUPERSOCKET_KEY] = {"Certificate": certificate, "ForceEncryption": 0}
    host.registry[ES_KEY] = dict(host.registry[ES_KEY], Version=bigfix_version)

    local = upgrade.collect_local_info(host)
    text = "\n".join(upgrade.report_warnings(local, now=REPORT_NOW))

    assert local["sql"]["instances"]["MSSQLSERVER"]["Certificate"] == certificate
    assert ("ODBC Driver 18" in text) is warned


def test_report_warnings_no_certificate_info(upgrade):
    """Test no certificate warning when the SQL network settings weren't read."""
    local = upgrade.collect_local_info(local_host(upgrade))

    assert "ODBC Driver 18" not in "\n".join(
        upgrade.report_warnings(local, now=REPORT_NOW)
    )


def test_masthead_parameters_parsed(upgrade):
    """Test masthead parameters are parsed, booleans as booleans."""
    conn = FakeConnection(
        rest_answers(upgrade),
        {
            "serverinfo": SERVERINFO,
            "admin/masthead/parameters": MASTHEAD_PARAMETERS_XML,
        },
    )
    parsed = upgrade.collect_rest_info(conn)["masthead_parameters"]["parsed"]

    assert parsed["Enhanced91Security"] is True
    assert parsed["RequireFIPSCompliantCrypto"] is False
    assert parsed["PortNumber"] == "52311"


@pytest.mark.parametrize("enabled", [True, False])
def test_enhanced_security_prerequisite_met(upgrade, compat, enabled):
    """Test the Enhanced Security prerequisite is marked met from the masthead."""
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    rest = {"masthead_parameters": {"parsed": {"Enhanced91Security": enabled}}}

    upgrade.mark_met_prerequisites(path, rest)

    bigfix_step = next(s for s in path["steps"] if s["component"] == "bigfix")
    text = "\n".join(bigfix_step["prerequisites"])
    assert ("Enable Enhanced Security before upgrading." in text) is not enabled
    assert ("Enhanced Security is already enabled" in text) is enabled


def test_services_to_start_only_running(upgrade):
    """Test only services running at baseline are started, reverse of stop order."""
    services = upgrade.collect_local_info(local_host(upgrade))["bigfix"]["services"]

    assert upgrade.services_to_start(services) == [
        "BESRootServer",
        "FillDB",
        "BESClient",
    ]


@pytest.mark.parametrize(
    "major,edition,expected",
    [
        ("2008 R2", "Developer Edition (64-bit)", True),
        ("2008 R2", "Standard Edition", True),
        ("2008", "Standard Edition", False),
        ("2008", "Enterprise Edition", True),
        ("2017", "Express Edition", False),
        ("2019", "Web Edition", False),
        ("2022", "Standard Edition (64-bit)", True),
        (None, "Standard Edition", False),
    ],
)
def test_supports_backup_compression(upgrade, major, edition, expected):
    """Test backup compression by version and edition."""
    assert upgrade.supports_backup_compression(major, edition) is expected


def test_sql_backup_queries_compression(upgrade):
    """Test COMPRESSION is added only when asked for."""
    compressed = upgrade.sql_backup_queries(
        "BFEnterprise", r"D:\b.bak", compression=True
    )
    plain = upgrade.sql_backup_queries("BFEnterprise", r"D:\b.bak")

    assert "COMPRESSION" in compressed[0]
    assert "COMPRESSION" not in plain[0]


# ---------------------------------------------------------------- backups to a share


@pytest.mark.parametrize(
    "path,unc,root",
    [
        (r"\\fileserver\share\bigfix_upgrade", True, r"\\fileserver\share"),
        (r"\\fileserver\share", True, r"\\fileserver\share"),
        (r"D:\backup", False, None),
    ],
)
def test_unc_paths(upgrade, path, unc, root):
    """Test UNC paths and their share root are recognised."""
    assert upgrade.is_unc_path(path) is unc
    assert upgrade.unc_share_root(path) == root


def test_backup_run_folder(upgrade):
    """Test each run gets its own folder under the backup location."""
    folder = upgrade.backup_run_folder(
        r"\\fileserver\share\bigfix", "bigfix", datetime.datetime(2026, 9, 26, 14, 5, 9)
    )
    assert folder == r"\\fileserver\share\bigfix" + os.sep + "bigfix_20260926_140509Z"


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (r"C:\backup", r"C:\Program Files\Microsoft SQL Server\MSSQL", True),
        (r"c:\backup", r"C:\data", True),
        (r"D:\backup", r"C:\data", False),
        (r"\\fs\share\backup", r"C:\data", False),
    ],
)
def test_same_volume(upgrade, a, b, expected):
    """Test backups on the same volume as the database files are detected."""
    assert upgrade.same_volume(a, b) is expected


def test_default_staging_dir(upgrade):
    """Test staging defaults to the Backup folder of the instance BigFix uses."""
    local = upgrade.collect_local_info(local_host(upgrade))

    assert upgrade.default_staging_dir(local) == (
        r"C:\Program Files\Microsoft SQL Server\MSSQL10_50.MSSQLSERVER\MSSQL\Backup"
    )


def test_check_writable(upgrade, tmp_path):
    """Test the write probe leaves nothing behind, and fails on a missing folder."""
    upgrade.check_writable(str(tmp_path))
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(OSError):
        upgrade.check_writable(str(tmp_path / "missing"))


def backup_handler(fail_under=None):
    """A fake sqlcmd that writes backup files, failing under one folder."""
    path_pattern = re.compile(r"DISK = N'([^']+)'")

    def handler(_server, query):
        path = path_pattern.search(query).group(1)
        if fail_under and path.startswith(fail_under):
            raise RuntimeError("Operating system error 5(Access is denied.)")
        if query.startswith("BACKUP DATABASE"):
            database = re.search(r"\[(\w+)\]", query).group(1)
            with open(path, "w", encoding="utf-8") as backup:
                backup.write(f"backup of {database}")
        elif not os.path.isfile(path):
            raise RuntimeError(f"cannot open backup device {path}")
        return []

    return handler


def walkthrough_ctx(upgrade, tmp_path, host):
    """A walkthrough context with a baseline from the fake root server."""
    args = types.SimpleNamespace(
        backup_dir=str(tmp_path / "share"),
        staging_dir=str(tmp_path / "staging"),
        backup_share_user=None,
        dry_run=False,
        sql_instance=None,
    )
    os.makedirs(args.backup_dir)
    os.makedirs(args.staging_dir)
    baseline = upgrade.collect_local_info(host)
    # small enough for any test machine's free space:
    for database in baseline["sql"]["databases"].values():
        database["size_mb"] = 1
    state = {"done": [], "reports": {}, "baseline": baseline}
    return upgrade.WalkthroughContext(
        args,
        host,
        state,
        str(tmp_path / "state.json"),
        ask=lambda *a: "yes",
        input_fn=lambda prompt: "",
        getpass_fn=lambda prompt: "",
    )


def test_choose_backup_mode(upgrade, tmp_path):
    """Test direct mode when SQL Server can write to the target, staged when not."""
    target = str(tmp_path)
    direct = local_host(upgrade, sql_handler=backup_handler())
    staged = local_host(upgrade, sql_handler=backup_handler(fail_under=target))

    assert upgrade.choose_backup_mode(direct, "(local)", target) == "direct"
    assert list(tmp_path.iterdir()) == []  # the probe backup is removed
    assert upgrade.choose_backup_mode(staged, "(local)", target) == "staged"
    assert any("[model]" in query for query in staged.sql_ran)


def test_sql_backup_direct(upgrade, tmp_path):
    """Test databases are backed up straight to the share when SQL Server can."""
    ctx = walkthrough_ctx(
        upgrade, tmp_path, local_host(upgrade, sql_handler=backup_handler())
    )

    upgrade.ACTIONS["sql_backup"](ctx)

    backups = ctx.state["backups"]
    assert [b["database"] for b in backups] == ["BFEnterprise", "BESReporting"]
    assert {b["mode"] for b in backups} == {"direct"}
    for backup in backups:
        assert backup["file"].startswith(ctx.backup_dir())
        assert os.path.isfile(backup["file"])
    assert os.listdir(ctx.args.staging_dir) == []
    assert all("COMPRESSION" in q for q in ctx.host.sql_ran if q.startswith("BACKUP"))


def test_sql_backup_staged(upgrade, tmp_path):
    """Test staged backups are verified, copied, hash checked and then removed."""
    share = str(tmp_path / "share")
    ctx = walkthrough_ctx(
        upgrade, tmp_path, local_host(upgrade, sql_handler=backup_handler(share))
    )

    upgrade.ACTIONS["sql_backup"](ctx)

    backups = ctx.state["backups"]
    assert {b["mode"] for b in backups} == {"staged"}
    for backup in backups:
        assert backup["file"].startswith(ctx.backup_dir())
        assert backup["sha256"] == upgrade.sha256_file(backup["file"])
    assert os.listdir(ctx.args.staging_dir) == []
    verified = [q for q in ctx.host.sql_ran if q.startswith("RESTORE VERIFYONLY")]
    assert verified and all(ctx.args.staging_dir in q for q in verified)


def test_sql_backup_staged_hash_mismatch(upgrade, tmp_path, monkeypatch):
    """Test a copy whose hash doesn't match keeps the staging file and stops."""
    share = str(tmp_path / "share")
    ctx = walkthrough_ctx(
        upgrade, tmp_path, local_host(upgrade, sql_handler=backup_handler(share))
    )
    real_sha256 = upgrade.sha256_file
    monkeypatch.setattr(
        upgrade,
        "sha256_file",
        lambda path: real_sha256(path) + ("x" if path.startswith(share) else ""),
    )

    with pytest.raises(upgrade.BackupError):
        upgrade.ACTIONS["sql_backup"](ctx)

    assert os.listdir(ctx.args.staging_dir) != []


def test_backup_share_password_not_kept(upgrade, tmp_path, caplog):
    """Test the share password is used to connect, and appears nowhere else."""
    password = "S3cret-Share-Pw!"
    host = local_host(upgrade)
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.backup_dir = r"\\fileserver\share\bigfix"
    ctx.args.backup_share_user = r"fileserver\operator"
    caplog.set_level(logging.DEBUG)

    upgrade.connect_backup_share(ctx, getpass_fn=lambda prompt: password)

    assert host.shares == [(r"\\fileserver\share", r"fileserver\operator", password)]
    assert password not in json.dumps(ctx.state, default=str)
    assert password not in caplog.text
    assert password not in json.dumps(host.ran)
    assert password not in json.dumps(vars(ctx.args))


# ---------------------------------------------------------------- share: owner side

SHARE_FOLDER = r"D:\_tmp_backup"
SHARE_UNC = r"\\192.168.5.39\_tmp_backup"
PEERS = ["192.168.5.40"]


def share_status(upgrade, **changes):
    """What the share owner's probes return: a working share, changed as asked."""
    status = {
        "hostname": "HYPERV",
        "lanmanserver": {"Status": "Running"},
        "folder_exists": True,
        "share": {
            "Name": "_tmp_backup",
            "Path": SHARE_FOLDER,
            "Access": [
                {
                    "AccountName": r"HYPERV\bfupgrade_share",
                    "AccessControlType": "Allow",
                    "AccessRight": "Change",
                }
            ],
        },
        "folder_acl": [
            {
                "Identity": r"HYPERV\bfupgrade_share",
                "Rights": "Modify, Synchronize",
                "Type": "Allow",
            }
        ],
        "account": {"name": "bfupgrade_share"},
        "smb_server": {"EnableSMB2Protocol": True, "RejectUnencryptedAccess": False},
        "network_profiles": [
            {"InterfaceAlias": "vEthernet (External)", "NetworkCategory": "Private"}
        ],
        "host_ips": [{"IPAddress": "192.168.5.39", "PrefixLength": 24}],
        "firewall_rules": [
            {
                "DisplayName": "BigFix upgrade SMB (run1)",
                "Profile": "Any",
                "LocalPort": ["445"],
                "RemoteAddress": ["192.168.5.40"],
            },
            {
                "DisplayName": "BigFix upgrade coordinator (run1)",
                "Profile": "Any",
                "Protocol": "TCP",
                "LocalPort": ["52390"],
                "RemoteAddress": ["LocalSubnet"],
            },
            {
                "DisplayName": "BigFix upgrade discovery (run1)",
                "Profile": "Any",
                "Protocol": "UDP",
                "LocalPort": ["52390"],
                "RemoteAddress": ["LocalSubnet"],
            },
        ],
    }
    status.update(changes)
    return status


def share_spec(**changes):
    spec = {
        "share_name": "_tmp_backup",
        "folder": SHARE_FOLDER,
        "account": "bfupgrade_share",
        "peers": PEERS,
        "coordinator_port": 52390,
    }
    spec.update(changes)
    return spec


def failing(findings):
    return {f["check"]: f for f in findings if f["ok"] is False}


def test_analyze_share_host_all_good(upgrade):
    """Test a working share owner has no problems."""
    findings = upgrade.analyze_share_host(share_status(upgrade), share_spec())
    assert failing(findings) == {}


def test_analyze_share_host_problems(upgrade):
    """Test each missing piece is found, with the action that fixes it."""
    status = share_status(
        upgrade,
        folder_exists=False,
        share=None,
        folder_acl=[],
        account=None,
        firewall_rules=[],
    )
    problems = failing(upgrade.analyze_share_host(status, share_spec()))

    assert problems["folder"]["action"] == "create_folder"
    assert problems["share"]["action"] == "create_share"
    assert problems["account"]["action"] == "create_account"
    assert problems["firewall smb 192.168.5.40"]["action"] == "firewall_smb"
    assert problems["firewall coordinator"]["action"] == "firewall_coordinator"
    assert problems["firewall discovery"]["action"] == "firewall_discovery"


def test_analyze_share_host_permissions(upgrade):
    """Test missing share and folder permissions for the account are found."""
    share = share_status(upgrade)["share"]
    share["Access"] = [
        {"AccountName": "Everyone", "AccessControlType": "Allow", "AccessRight": "Read"}
    ]
    status = share_status(upgrade, share=share, folder_acl=[])
    problems = failing(upgrade.analyze_share_host(status, share_spec()))

    assert problems["share access"]["action"] == "grant_share_access"
    assert problems["folder access"]["action"] == "grant_folder_access"


def test_analyze_share_host_public_network(upgrade):
    """Test a Public network is flagged, and only fixed by hand."""
    status = share_status(
        upgrade,
        network_profiles=[
            {"InterfaceAlias": "vEthernet (External)", "NetworkCategory": "Public"}
        ],
        firewall_rules=[
            {
                "DisplayName": "File and Printer Sharing (SMB-In)",
                "Profile": "Private, Domain",
                "LocalPort": ["445"],
                "RemoteAddress": ["LocalSubnet"],
            }
        ],
        account=None,
    )
    findings = upgrade.analyze_share_host(status, share_spec(coordinator_port=None))
    problems = failing(findings)

    assert "Set-NetConnectionProfile" in problems["network profile"]["fix"]
    assert "action" not in problems["network profile"]
    # the SMB-In rule is for Private and Domain only, so on Public it doesn't help:
    assert "firewall smb 192.168.5.40" in problems


@pytest.mark.parametrize(
    "remote,expected",
    [
        (["Any"], True),
        (["LocalSubnet"], True),
        (["192.168.5.0/24"], True),
        (["192.168.5.0/255.255.255.0"], True),
        (["192.168.5.30-192.168.5.50"], True),
        (["10.0.0.0/8"], False),
        (["LocalSubnet"], True),
    ],
)
def test_firewall_address_matching(upgrade, remote, expected):
    """Test firewall RemoteAddress forms are matched against a peer."""
    networks = upgrade._host_networks(
        [{"IPAddress": "192.168.5.39", "PrefixLength": 24}]
    )
    assert upgrade._address_matches("192.168.5.40", remote, networks) is expected


def test_share_commands_are_validated(upgrade):
    """Test names, paths and addresses can't inject PowerShell."""
    with pytest.raises(ValueError):
        upgrade.firewall_rule_command(
            "run1", 445, ["192.168.5.40; Remove-Item x"], "SMB"
        )
    with pytest.raises(ValueError):
        upgrade.firewall_rule_command("run'1", 445, PEERS, "SMB")
    with pytest.raises(ValueError):
        upgrade.new_share_command("x'; Remove-Item y", SHARE_FOLDER, "HYPERV\\a")
    with pytest.raises(ValueError):
        upgrade.new_share_command("_tmp_backup", "D:\\a'b", "HYPERV\\a")

    command = upgrade.firewall_rule_command("run1", 445, PEERS, "SMB")
    assert command[:2] == ["powershell.exe", "-NoProfile"]
    assert "-RemoteAddress 192.168.5.40" in command[-1]
    assert "-Group 'BigFix upgrade run1'" in command[-1]


def test_share_account_password(upgrade):
    """Test temporary account passwords are long, random and complex."""
    first = upgrade.new_share_password()
    assert len(first) >= 24
    assert re.search(r"[a-z]", first) and re.search(r"[A-Z]", first)
    assert re.search(r"\d", first) and re.search(r"[^A-Za-z0-9]", first)
    assert first != upgrade.new_share_password()


def share_owner_host(upgrade, status):
    """A fake share owner whose PowerShell probes answer from `status`."""
    host = local_host(upgrade)
    host.powershell.update(
        upgrade.share_host_probe_scripts("_tmp_backup", SHARE_FOLDER)
    )
    scripts = upgrade.share_host_probe_scripts("_tmp_backup", SHARE_FOLDER)
    for key, script in scripts.items():
        host.powershell[script] = status[key]
    if status["folder_exists"]:
        host.dirs.add(SHARE_FOLDER)
    if status["account"]:
        host.users["bfupgrade_share"] = status["account"]
    return host


def test_setup_share_owner_creates_and_records(upgrade, tmp_path):
    """Test confirmed fixes run, and what was created is recorded for cleanup."""
    status = share_status(
        upgrade,
        folder_exists=False,
        share=None,
        folder_acl=[],
        account=None,
        firewall_rules=[],
    )
    host = share_owner_host(upgrade, status)
    state = {}

    result = upgrade.setup_share_owner(
        host,
        share_spec(),
        state,
        run_id="run1",
        confirm=lambda prompt: True,
        dry_run=False,
        hostname="HYPERV",
    )

    assert "bfupgrade_share" in host.users
    assert result["password"] == host.user_passwords["bfupgrade_share"]
    assert result["user"] == r"HYPERV\bfupgrade_share"
    created = state["share"]["created"]
    assert created == {
        "folder": SHARE_FOLDER,
        "share": "_tmp_backup",
        "account": "bfupgrade_share",
        "firewall_group": "BigFix upgrade run1",
    }
    commands = "\n".join(cmd[-1] for cmd in host.ran)
    assert "New-SmbShare -Name '_tmp_backup'" in commands
    assert "LocalPort 445" in commands and "-RemoteAddress 192.168.5.40" in commands
    assert "-Protocol TCP -LocalPort 52390 -RemoteAddress LocalSubnet" in commands
    assert "-Protocol UDP -LocalPort 52390 -RemoteAddress LocalSubnet" in commands
    assert any(cmd[0] == "icacls.exe" for cmd in host.ran)
    # the password is only returned, never stored:
    assert result["password"] not in json.dumps(state)
    assert result["password"] not in json.dumps(host.ran)


def test_setup_share_owner_declined(upgrade):
    """Test nothing changes when the operator declines each fix."""
    status = share_status(upgrade, share=None, account=None, firewall_rules=[])
    host = share_owner_host(upgrade, status)
    state = {}

    upgrade.setup_share_owner(
        host,
        share_spec(),
        state,
        run_id="run1",
        confirm=lambda prompt: False,
        dry_run=False,
        hostname="HYPERV",
    )

    assert host.ran == []
    assert host.users == {}
    assert state["share"]["created"] == {}


def test_setup_share_owner_resets_own_account_password(upgrade):
    """Test an account this tool created earlier gets a new password, others don't."""
    host = share_owner_host(upgrade, share_status(upgrade))
    ours = {"share": {"created": {"account": "bfupgrade_share"}}}

    result = upgrade.setup_share_owner(
        host,
        share_spec(),
        ours,
        run_id="run1",
        confirm=lambda prompt: True,
        dry_run=False,
        hostname="HYPERV",
    )
    assert result["password"] == host.user_passwords["bfupgrade_share"]

    with pytest.raises(upgrade.ShareError, match="not created by this tool"):
        upgrade.setup_share_owner(
            share_owner_host(upgrade, share_status(upgrade)),
            share_spec(),
            {},
            run_id="run1",
            confirm=lambda prompt: True,
            dry_run=False,
            hostname="HYPERV",
        )


def test_share_cleanup_removes_only_created(upgrade):
    """Test cleanup removes exactly what was recorded, never the folder."""
    host = local_host(upgrade)
    host.users["bfupgrade_share"] = {"name": "bfupgrade_share"}
    host.users["someone_else"] = {"name": "someone_else"}
    state = {
        "share": {
            "created": {
                "folder": SHARE_FOLDER,
                "share": "_tmp_backup",
                "account": "bfupgrade_share",
                "firewall_group": "BigFix upgrade run1",
            }
        }
    }

    upgrade.cleanup_share_owner(host, state, confirm=lambda prompt: True, dry_run=False)

    commands = "\n".join(cmd[-1] for cmd in host.ran)
    assert "Remove-NetFirewallRule -Group 'BigFix upgrade run1'" in commands
    assert "Remove-SmbShare -Name '_tmp_backup' -Force" in commands
    assert "Remove-Item" not in commands and "rmdir" not in commands
    assert set(host.users) == {"someone_else"}
    assert state["share"]["created"] == {}


# ---------------------------------------------------------------- share: client side


def client_host(upgrade, **changes):
    """A fake BigFix root server trying to use the share."""
    host = local_host(upgrade)
    host.reachable.add(("192.168.5.39", 445))
    host.powershell[upgrade.PS_SMB_CLIENT_CONFIG] = {
        "RequireSecuritySignature": False,
        "EnableInsecureGuestLogons": False,
    }
    for key, value in changes.items():
        setattr(host, key, value)
    return host


def win_error(code):
    err = OSError(f"error {code}")
    err.winerror = code
    return err


def test_diagnose_share_access_ok(upgrade):
    """Test a working share passes every check and is disconnected afterwards."""
    host = client_host(upgrade)
    findings = upgrade.diagnose_share_access(
        host, SHARE_UNC, r"HYPERV\bfupgrade_share", "pw"
    )

    assert failing(findings) == {}
    assert host.shares == [
        (r"\\192.168.5.39\_tmp_backup", r"HYPERV\bfupgrade_share", "pw")
    ]
    assert host.disconnected == [r"\\192.168.5.39\_tmp_backup"]


def test_diagnose_share_access_port_blocked(upgrade):
    """Test a blocked port 445 points at the owner's firewall and stops there."""
    host = client_host(upgrade, reachable=set())
    problems = failing(
        upgrade.diagnose_share_access(host, SHARE_UNC, r"HYPERV\u", "pw")
    )

    assert "firewall" in problems["tcp 445"]["fix"].lower()
    assert host.shares == []


@pytest.mark.parametrize(
    "code,words",
    [
        (53, "firewall"),
        (67, "share name"),
        (5, "permission"),
        (86, "password"),
        (1326, "HYPERV\\user"),
        (1219, "/delete"),
        (1272, "guest"),
        (1240, "signing"),
        (1331, "disabled"),
        (99999, "99999"),
    ],
)
def test_diagnose_share_access_error_codes(upgrade, code, words):
    """Test Windows error codes are explained with a fix."""
    host = client_host(upgrade)
    host.share_errors[r"\\192.168.5.39\_tmp_backup"] = win_error(code)

    problems = failing(
        upgrade.diagnose_share_access(host, SHARE_UNC, r"HYPERV\u", "pw")
    )

    text = problems["connect"]["detail"] + " " + problems["connect"]["fix"]
    assert words.lower() in text.lower()
    assert "write" not in problems  # stops after a failed connect


def test_diagnose_share_access_existing_connection(upgrade):
    """Test an existing connection to the same server is reported."""
    host = client_host(upgrade)
    host.run_handler = lambda cmd: (
        "New connections will be remembered.\n\n"
        "Status       Local     Remote                    Network\n"
        "-----------------------------------------------------------------\n"
        r"OK                     \\192.168.5.39\other      Microsoft Windows Network"
        "\n"
    )

    findings = upgrade.diagnose_share_access(host, SHARE_UNC, r"HYPERV\u", "pw")
    existing = next(f for f in findings if f["check"] == "existing connections")

    assert r"\\192.168.5.39\other" in existing["detail"]
    assert "1219" in existing["fix"]


def test_diagnose_share_access_write_and_sql(upgrade):
    """Test a read-only share, and SQL Server's own access, are reported."""
    host = client_host(upgrade, sql_handler=backup_handler(fail_under=SHARE_UNC))
    host.write_errors[SHARE_UNC] = PermissionError("Access is denied")

    findings = upgrade.diagnose_share_access(
        host, SHARE_UNC, r"HYPERV\u", "pw", sql_server="(local)"
    )
    problems = failing(findings)
    sql = next(f for f in findings if f["check"] == "sql server access")

    assert "NTFS" in problems["write"]["fix"]
    assert sql["ok"] is None  # information: staged backups will be used
    assert "staged" in sql["detail"]


# ---------------------------------------------------------------- serial


def test_resolve_masthead_serial(upgrade, tmp_path):
    """Test the serial comes from --masthead-serial, then REST, then the client."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_text("X-Fixlet-Site-Serial-Number: 111\r\n")
    rest = FakeConnection({upgrade.MASTHEAD_RELEVANCE: [[222, "bigfix.example.com"]]})

    assert upgrade.resolve_masthead_serial("333", rest, [str(masthead)]) == "333"
    assert upgrade.resolve_masthead_serial(None, rest, [str(masthead)]) == "222"
    assert upgrade.resolve_masthead_serial(None, None, [str(masthead)]) == "111"
    with pytest.raises(SystemExit):
        upgrade.resolve_masthead_serial(None, None, [str(tmp_path / "missing")])


def test_resolve_masthead_serial_mismatch_warns(upgrade, tmp_path, caplog):
    """Test a client of another deployment is warned about, and REST wins."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_text("X-Fixlet-Site-Serial-Number: 111\r\n")
    rest = FakeConnection({upgrade.MASTHEAD_RELEVANCE: [[222, "bigfix.example.com"]]})

    assert upgrade.resolve_masthead_serial(None, rest, [str(masthead)]) == "222"
    assert "different BigFix deployment" in caplog.text


# ---------------------------------------------------------------- share session


def session_options(upgrade, **changes):
    """Options for a ShareSessionNode."""
    options = dict(password=b"k" * 32, serial="123456789")
    options.update(changes)
    return options


def coordinator_options(upgrade, **changes):
    """Options for a ShareSessionCoordinator."""
    options = dict(session_options(upgrade), share_unc=SHARE_UNC, allow=[])
    options.update(changes)
    return options


async def run_session(
    upgrade,
    coordinator_commands,
    peer_hosts,
    console_commands=(),
    share=None,
    **options,
):
    """Run a coordinator, peers and a console over localhost with scripted input."""
    output = {"coordinator": [], "console": []}
    coordinator = upgrade.ShareSessionCoordinator(
        share=share
        or {
            "unc": SHARE_UNC,
            "user": r"HYPERV\bfupgrade_share",
            "password": "temp-Pw-123!",
        },
        output=output["coordinator"].append,
        **coordinator_options(upgrade, **options),
    )
    server = await coordinator.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    peers = []
    for name, host in peer_hosts.items():
        output[name] = []
        peers.append(
            upgrade.ShareSessionNode(
                name,
                ["root"],
                host,
                output=output[name].append,
                **session_options(upgrade, **options),
            )
        )
    tasks = [asyncio.create_task(p.run("127.0.0.1", port)) for p in peers]
    console = None
    if console_commands:
        console = upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=output["console"].append,
            commands=list(console_commands),
            **session_options(upgrade, **options),
        )
        tasks.append(asyncio.create_task(console.run("127.0.0.1", port)))

    await coordinator.wait_for_nodes(len(tasks), timeout=5)
    for command in coordinator_commands:
        await coordinator.handle_command(command, source="coordinator")
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    server.close()
    return coordinator, output


def test_share_session_end_to_end(upgrade):
    """Test peers get the share, diagnose it, and report back to the coordinator."""
    host = client_host(upgrade)

    async def scenario():
        return await run_session(upgrade, ["retry", "end"], {"bigfix": host})

    coordinator, output = asyncio.run(scenario())

    assert host.shares[0] == (
        r"\\192.168.5.39\_tmp_backup",
        r"HYPERV\bfupgrade_share",
        "temp-Pw-123!",
    )
    # the first run plus the retry:
    assert len(coordinator.results["bigfix"]) == 2
    assert coordinator.results["bigfix"][-1]["ok"] is True
    assert any("bigfix" in line and "OK" in line for line in output["coordinator"])
    # the password never shows in the coordinator's output or results:
    assert "temp-Pw-123!" not in "\n".join(output["coordinator"])
    assert "temp-Pw-123!" not in json.dumps(coordinator.results)


def test_share_session_console_drives(upgrade):
    """Test a console node sees results and its commands are accepted."""
    host = client_host(upgrade, reachable=set())

    async def scenario():
        return await run_session(
            upgrade, [], {"bigfix": host}, console_commands=["status", "retry", "end"]
        )

    coordinator, output = asyncio.run(scenario())

    assert len(coordinator.results["bigfix"]) == 2
    assert coordinator.results["bigfix"][-1]["ok"] is False
    console_text = "\n".join(output["console"])
    assert "bigfix" in console_text and "tcp 445" in console_text
    assert "firewall" in console_text.lower()
    # consoles don't receive share credentials:
    assert "temp-Pw-123!" not in console_text


def test_share_session_locks_after_wrong_codes(upgrade):
    """Test the coordinator stops accepting nodes after too many wrong codes."""
    channel = upgrade
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=output.append,
            max_failures=3,
            **coordinator_options(
                upgrade, password=channel.derive_password(None, "123456789", "111111")
            ),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        outcomes = []
        for code in ("222222", "333333", "444444", "111111"):
            node = upgrade.ShareSessionNode(
                "guesser",
                ["console"],
                None,
                output=lambda line: None,
                **session_options(
                    upgrade, password=channel.derive_password(None, "123456789", code)
                ),
            )
            try:
                await asyncio.wait_for(node.run("127.0.0.1", port), timeout=10)
                outcomes.append("connected")
            except Exception as err:  # pylint: disable=broad-exception-caught
                outcomes.append(type(err).__name__)
        server.close()
        return coordinator, outcomes

    coordinator, outcomes = asyncio.run(scenario())
    assert outcomes[:3] == ["WrongCode"] * 3
    # even the right code is refused once locked:
    assert outcomes[3] != "connected"
    assert coordinator.locked is True
    assert any("restart" in line for line in output)


def test_share_session_duplicate_names(upgrade):
    """Test two nodes with the same name, like a peer and console on one PC, both
    stay.
    """

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(upgrade),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        nodes = [
            upgrade.ShareSessionNode(
                "samepc",
                roles,
                host,
                output=lambda line: None,
                **session_options(upgrade),
            )
            for roles, host in ((["root"], client_host(upgrade)), (["console"], None))
        ]
        tasks = [asyncio.create_task(n.run("127.0.0.1", port)) for n in nodes]
        await coordinator.wait_for_nodes(2, timeout=5)
        names = sorted(coordinator.nodes)
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        server.close()
        return names

    assert asyncio.run(scenario()) == ["samepc", "samepc-2"]


def test_share_session_node_coordinator_gone(upgrade):
    """Test a node whose coordinator goes away stops with a message, not a
    traceback.
    """
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(upgrade),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = upgrade.ShareSessionNode(
            "console1",
            ["console"],
            None,
            output=output.append,
            **session_options(upgrade),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        for entry in coordinator.nodes.values():
            entry["channel"].close()
        await asyncio.wait_for(task, timeout=5)
        server.close()

    asyncio.run(scenario())
    assert any("coordinator disconnected" in line for line in output)


def test_share_session_results_saved_without_share_setup(upgrade):
    """Test results can be saved when this coordinator didn't set up the share."""
    state = {}
    upgrade.save_session_results(state, {"bigfix": [{"ok": True, "findings": []}]})
    assert state["share"]["results"]["bigfix"][0]["ok"] is True


# coverage of existing behaviour, checked by mutation rather than red first:
@pytest.mark.parametrize(
    "value", ["a'b", 'a"b', "a`b", "$(x)", "a;b", "a|b", "a&b", "a\nb"]
)
def test_ps_quote_rejects_breakouts(upgrade, value):
    """Test PowerShell quoting refuses anything that could end the string."""
    with pytest.raises(ValueError):
        upgrade._ps_quote(value)
    assert upgrade._ps_quote(r"BigFix upgrade run1") == "'BigFix upgrade run1'"


def test_firewall_address_matching_mixed_versions(upgrade):
    """Test an IPv6 peer against IPv4 ranges and networks doesn't crash."""
    assert upgrade._address_matches("::1", ["192.168.5.1-192.168.5.9"], []) is False
    assert upgrade._address_matches("::1", ["192.168.5.0/24"], []) is False


# ---------------------------------------------------------------- auto discovery


def answer_defaults(prompt, choices, default=None):
    """Like pressing Enter at every question."""
    return default


LOCAL_DISKS = [
    {"DeviceID": "C:", "FreeSpace": 50 * 1024**3},
    {"DeviceID": "D:", "FreeSpace": 900 * 1024**3},
]


def test_choose_backup_share_one_existing(upgrade):
    """Test the only existing share is offered, and Enter accepts it."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = [
        {"Name": "ADMIN$", "Path": r"C:\Windows", "Special": True},
        {"Name": "_tmp_backup", "Path": SHARE_FOLDER, "Special": False},
    ]
    host.powershell[upgrade.PS_DISKS] = LOCAL_DISKS

    assert upgrade.choose_backup_share(host, answer_defaults) == {
        "share_name": "_tmp_backup",
        "folder": SHARE_FOLDER,
    }


def test_choose_backup_share_new_on_biggest_drive(upgrade):
    """Test with no share, the default is a new one on the drive with most space."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = []
    host.powershell[upgrade.PS_DISKS] = LOCAL_DISKS

    assert upgrade.choose_backup_share(host, answer_defaults) == {
        "share_name": "bigfix_upgrade_backup",
        "folder": r"D:\bigfix_upgrade_backup",
    }


def test_choose_backup_share_pick_from_several(upgrade):
    """Test several shares are listed to pick from."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = [
        {"Name": "a", "Path": r"D:\a", "Special": False},
        {"Name": "b", "Path": r"E:\b", "Special": False},
    ]
    host.powershell[upgrade.PS_DISKS] = LOCAL_DISKS

    choice = upgrade.choose_backup_share(
        host, lambda prompt, choices, default=None: "2"
    )
    assert choice == {"share_name": "b", "folder": r"E:\b"}


@pytest.mark.parametrize(
    "target,expected",
    [
        ("192.168.5.40", "192.168.5.39"),
        ("10.9.9.9", "192.168.5.39"),
        (None, "192.168.5.39"),
    ],
)
def test_choose_host_ip(upgrade, target, expected):
    """Test the address peers reach: the one facing the root, never loopback or
    APIPA.
    """
    host_ips = [
        {"IPAddress": "127.0.0.1", "PrefixLength": 8},
        {"IPAddress": "169.254.1.5", "PrefixLength": 16},
        {"IPAddress": "172.16.0.1", "PrefixLength": 24},
        {"IPAddress": "192.168.5.39", "PrefixLength": 24},
    ]
    if target == "10.9.9.9":
        host_ips = [host_ips[0], host_ips[1], host_ips[3]]
    if target is None:
        host_ips = [host_ips[3]]
    assert upgrade.choose_host_ip(host_ips, target) == expected


def test_discover_root_ip(upgrade, tmp_path):
    """Test the root server address comes from the local masthead's gather URL."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_text(
        "X-Fixlet-Site-Gather-URL: http://bigfix.example.com:52311/cgi-bin/"
        "bfgather.exe/actionsite\r\n"
    )
    resolver = {"bigfix.example.com": "192.168.5.40"}.get

    assert upgrade.discover_root_ip([str(masthead)], resolver) == "192.168.5.40"
    assert upgrade.discover_root_ip([str(tmp_path / "missing")], resolver) is None


def test_plan_share_existing_unc_on_this_host(upgrade):
    """Test a --share-unc on this host finds its folder without --share-folder."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = [
        {"Name": "_tmp_backup", "Path": SHARE_FOLDER, "Special": False}
    ]
    host_ips = [{"IPAddress": "192.168.5.39", "PrefixLength": 24}]

    plan = upgrade.plan_share(
        host, SHARE_UNC, None, host_ips, "HYPERV", answer_defaults
    )

    assert plan == {
        "unc": SHARE_UNC,
        "share_name": "_tmp_backup",
        "folder": SHARE_FOLDER,
    }


def test_plan_share_all_defaults(upgrade):
    """Test with no share options, the share and its UNC path are worked out."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = [
        {"Name": "_tmp_backup", "Path": SHARE_FOLDER, "Special": False}
    ]
    host_ips = [{"IPAddress": "192.168.5.39", "PrefixLength": 24}]

    plan = upgrade.plan_share(
        host, None, "192.168.5.40", host_ips, "HYPERV", answer_defaults
    )

    assert plan == {
        "unc": SHARE_UNC,
        "share_name": "_tmp_backup",
        "folder": SHARE_FOLDER,
    }


def test_plan_share_remote_unc(upgrade):
    """Test a share on another server is only checked, not set up here."""
    host = local_host(upgrade)
    host.powershell[upgrade.PS_LOCAL_SHARES] = []
    host_ips = [{"IPAddress": "192.168.5.39", "PrefixLength": 24}]

    plan = upgrade.plan_share(
        host, r"\\nas01\backups", None, host_ips, "HYPERV", answer_defaults
    )

    assert plan == {"unc": r"\\nas01\backups", "share_name": "backups", "folder": None}


@pytest.mark.parametrize(
    "given,source,expected",
    [
        ("123456", "none", "123456"),
        ("123456", "env", "123456"),
        (None, "none", "generated"),
        (None, "env", None),
    ],
)
def test_decide_pairing_code(upgrade, given, source, expected, monkeypatch):
    """Test a code is generated exactly when there's no PSK to trust instead."""
    monkeypatch.setattr(upgrade, "generate_pairing_code", lambda: "generated")
    assert upgrade.decide_pairing_code(given, source) == expected


def test_require_session_packages(upgrade):
    """Test the share session packages check passes when they're installed."""
    upgrade.require_session_packages()


def test_service_commands_quoted(upgrade, tmp_path):
    """Test walkthrough service commands use the PowerShell helper, quoted."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))

    upgrade.ACTIONS["stop_services"](ctx)
    upgrade.ACTIONS["start_services"](ctx)
    upgrade.ACTIONS["restore_start_types"](ctx)

    scripts = [cmd[-1] for cmd in ctx.host.ran]
    assert all(cmd[:4] == upgrade._powershell("")[:4] for cmd in ctx.host.ran)
    assert "Stop-Service -Name 'FillDB' -Force" in scripts
    assert "Start-Service -Name 'FillDB'" in scripts
    assert "Set-Service -Name 'FillDB' -StartupType Automatic" in scripts
    with pytest.raises(ValueError):
        upgrade.service_command("Stop-Service", "Fill'DB")


def test_find_coordinator(upgrade):
    """Test an explicit address wins, otherwise discovery, otherwise a clear error."""

    async def found(serial):
        return ("192.168.5.39", 52390)

    async def nothing(serial):
        return None

    assert asyncio.run(upgrade.find_coordinator("hv:1234", "1", found)) == ("hv", 1234)
    assert asyncio.run(upgrade.find_coordinator(None, "1", found)) == (
        "192.168.5.39",
        52390,
    )
    with pytest.raises(SystemExit, match="--coordinator"):
        asyncio.run(upgrade.find_coordinator(None, "1", nothing))


# coverage of existing behaviour, checked by mutation rather than red first:
def test_share_session_credentials_only_to_peers(upgrade):
    """Test peers receive the share password, consoles never do."""

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={
                "unc": SHARE_UNC,
                "user": r"HYPERV\bfupgrade_share",
                "password": "temp-Pw-123!",
            },
            output=lambda line: None,
            **coordinator_options(upgrade),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        peer = upgrade.ShareSessionNode(
            "bigfix",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            **session_options(upgrade),
        )
        console = upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=lambda line: None,
            **session_options(upgrade),
        )
        tasks = [asyncio.create_task(n.run("127.0.0.1", port)) for n in (peer, console)]
        await coordinator.wait_for_nodes(2, timeout=5)
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        server.close()
        return peer.share, console.share

    peer_share, console_share = asyncio.run(scenario())
    assert peer_share["password"] == "temp-Pw-123!"
    assert "password" not in console_share


# ---------------------------------------------------------------- HCL backup


def make_server_folder(tmp_path):
    """A real BES Server folder tree with the HCL backup items, some missing."""
    server = tmp_path / "BES Server"
    files = {
        "BESReportsData/archive.db": b"reports",
        "BESReportsServer/wwwroot/ReportFiles/custom.html": b"<html/>",
        "Mirror Server/Inbox/action.fxf": b"fxf",
        "Mirror Server/Config/DownloadWhitelist.txt": b"http://example.com/.*",
        "UploadManagerData/BufferDir/sha1/1/upload.bin": b"x" * 100,
        "wwwrootbes/masthead/masthead.afxm": b"masthead",
        "actionsite.afxm": b"X-Fixlet-Site-Serial-Number: 123456789\r\n",
        "ServerKeyTool.exe": b"MZ",
    }
    for relative, content in files.items():
        path = server / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return server


def hcl_ctx(upgrade, tmp_path, **changes):
    """A walkthrough context whose baseline points at a real server folder."""
    server = make_server_folder(tmp_path)
    client = tmp_path / "BES Client"
    (client / "KeyStorage").mkdir(parents=True)
    (client / "KeyStorage" / "client.key").write_bytes(b"client key")
    host = local_host(upgrade)
    host.registry[upgrade.CLIENT_GLOBAL_OPTIONS_KEY] = {"ComputerID": 11333902}
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    baseline = ctx.state["baseline"]
    baseline["bigfix"]["registry"]["values"]["EnterpriseServerFolder"] = str(server)
    baseline["bigfix"]["key_files"]["found"]["actionsite.afxm"] = [
        str(server / "actionsite.afxm")
    ]
    for service in baseline["bigfix"]["services"]:
        if service["Name"] == "BESClient":
            service["PathName"] = f'"{client / "BESClient.exe"}"'
    for key, value in changes.items():
        setattr(ctx, key, value)
    return ctx, server


def test_server_backup_items_resolved(upgrade, tmp_path):
    """Test HCL's folders are found under the registry's server folder."""
    ctx, server = hcl_ctx(upgrade, tmp_path)

    items = dict(upgrade.server_backup_items(ctx.state["baseline"]))

    assert items["BESReportsData"] == str(server / "BESReportsData")
    assert items["Encryption Keys"] == str(server / "Encryption Keys")
    assert items["Mirror Server/Config/DownloadWhitelist.txt"] == str(
        server / "Mirror Server" / "Config" / "DownloadWhitelist.txt"
    )
    assert len(items) == 7


def test_folder_backup_copies_and_records(upgrade, tmp_path):
    """Test each HCL item is copied with its size, a missing one is recorded."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)

    upgrade.ACTIONS["folder_backup"](ctx)

    copied = ctx.state["server_files"]
    run_dir = ctx.backup_dir()
    assert copied["Encryption Keys"] == {"missing": True}
    assert copied["UploadManagerData"]["files"] == 1
    assert copied["UploadManagerData"]["bytes"] == 100
    assert os.path.isfile(
        os.path.join(
            run_dir, "server_files", "Mirror Server", "Config", "DownloadWhitelist.txt"
        )
    )
    assert os.path.isfile(
        os.path.join(run_dir, "server_files", "wwwrootbes", "masthead", "masthead.afxm")
    )


def test_folder_backup_large_folder_confirmed(upgrade, tmp_path, monkeypatch):
    """Test a large folder is only copied if the operator agrees."""
    monkeypatch.setattr(upgrade, "LARGE_BACKUP_BYTES", 50)
    prompts = []
    ctx, _server = hcl_ctx(
        upgrade,
        tmp_path,
        ask=lambda prompt, choices, default=None: prompts.append(prompt) or "no",
    )

    upgrade.ACTIONS["folder_backup"](ctx)

    assert ctx.state["server_files"]["UploadManagerData"] == {
        "skipped": True,
        "files": 1,
        "bytes": 100,
    }
    assert any("UploadManagerData" in prompt for prompt in prompts)
    assert "files" in ctx.state["server_files"]["BESReportsData"]


def test_folder_backup_space_warning(upgrade, tmp_path, monkeypatch):
    """Test not enough free space is asked about before copying."""
    ctx, _server = hcl_ctx(
        upgrade, tmp_path, ask=lambda prompt, choices, default=None: "no"
    )
    usage = types.SimpleNamespace(total=10, used=10, free=10)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    with pytest.raises(upgrade.BackupError, match="free space"):
        upgrade.ACTIONS["folder_backup"](ctx)


def test_masthead_copied_as_hcl_says(upgrade, tmp_path):
    """Test the server's actionsite.afxm is kept as masthead.afxm."""
    ctx, server = hcl_ctx(upgrade, tmp_path)

    upgrade.ACTIONS["masthead"](ctx)

    copy = os.path.join(ctx.backup_dir(), "key_files", "masthead.afxm")
    assert open(copy, "rb").read() == (server / "actionsite.afxm").read_bytes()


def test_db_info_exported(upgrade, tmp_path):
    """Test DBINFO and REPLICATION_SERVERS are saved with their column names."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    for table, columns, rows in (
        ("DBINFO", [["Name"], ["Value"]], [["DBVersion", "10.70"]]),
        ("REPLICATION_SERVERS", [["ServerID"], ["URL"]], [["0", "http://bigfix"]]),
    ):
        ctx.host.sql[upgrade.table_columns_query(table)] = columns
        ctx.host.sql[upgrade.table_rows_query(table)] = rows

    upgrade.ACTIONS["db_info"](ctx)

    with open(
        os.path.join(ctx.backup_dir(), "db_info.json"), encoding="utf-8"
    ) as saved:
        info = json.load(saved)
    assert info["DBINFO"] == {
        "columns": ["Name", "Value"],
        "rows": [["DBVersion", "10.70"]],
    }
    assert info["REPLICATION_SERVERS"]["rows"] == [["0", "http://bigfix"]]


def test_db_info_table_names_checked(upgrade):
    """Test only the known table names can be queried."""
    with pytest.raises(ValueError):
        upgrade.table_rows_query("DBINFO]; DROP TABLE x;--")


def test_client_data_backed_up(upgrade, tmp_path):
    """Test the root's client ComputerID and KeyStorage are saved, per HCL."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)

    upgrade.ACTIONS["client_data"](ctx)

    folder = os.path.join(ctx.backup_dir(), "client_data")
    assert open(os.path.join(folder, "ComputerID.txt"), encoding="utf-8").read() == (
        "11333902\n"
    )
    assert os.path.isfile(os.path.join(folder, "KeyStorage", "client.key"))
    assert ctx.state["client_data"]["computer_id"] == 11333902
    assert any(
        cmd[:2] == ["reg.exe", "export"] and "GlobalOptions" in cmd[2]
        for cmd in ctx.host.ran
    )


def test_server_keys_skipped_by_default(upgrade, tmp_path):
    """Test ServerKeyTool only runs when asked, Enter skips it."""
    ctx, _server = hcl_ctx(
        upgrade,
        tmp_path,
        ask=lambda prompt, choices, default=None: default,
        # even with a license.pvk and password at hand, Enter means no:
        input_fn=lambda prompt: r"E:\license.pvk",
        getpass_fn=lambda prompt: "Pvk-Passw0rd!",
    )

    upgrade.ACTIONS["server_keys"](ctx)

    assert ctx.host.secret_runs == []
    assert ctx.state["server_keys"] == {"skipped": True}


def test_server_keys_password_never_kept(upgrade, tmp_path, caplog):
    """Test the license.pvk password is only given to ServerKeyTool."""
    password = "Pvk-Passw0rd!"
    ctx, server = hcl_ctx(
        upgrade,
        tmp_path,
        input_fn=lambda prompt: r"E:\license.pvk",
        getpass_fn=lambda prompt: password,
    )
    caplog.set_level(logging.DEBUG)

    upgrade.ACTIONS["server_keys"](ctx)
    upgrade.ACTIONS["restore_notes"](ctx)

    (command,) = ctx.host.secret_runs
    assert command[0] == str(server / "ServerKeyTool.exe")
    assert command[1:] == [
        "/decrypt",
        f"/dirIn:{server}",
        f"/dirOut:{os.path.join(ctx.backup_dir(), 'server_keys')}",
        r"/sitePvkLocation:E:\license.pvk",
        f"/sitePvkPassword:{password}",
    ]
    notes = open(
        os.path.join(ctx.backup_dir(), "RESTORE_NOTES.txt"), encoding="utf-8"
    ).read()
    for text in (caplog.text, json.dumps(ctx.state), json.dumps(ctx.host.ran), notes):
        assert password not in text


def test_server_keys_tool_missing(upgrade, tmp_path):
    """Test a missing ServerKeyTool.exe is reported, not guessed."""
    ctx, server = hcl_ctx(upgrade, tmp_path)
    (server / "ServerKeyTool.exe").unlink()

    upgrade.ACTIONS["server_keys"](ctx)

    assert ctx.host.secret_runs == []
    assert "not found" in ctx.state["server_keys"]["error"]


def test_restore_notes(upgrade, tmp_path):
    """Test the restore notes list the backups and HCL's re-encrypt command."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    ctx.state["backups"] = [{"database": "BFEnterprise", "file": r"D:\b.bak"}]

    upgrade.ACTIONS["restore_notes"](ctx)

    notes = open(
        os.path.join(ctx.backup_dir(), "RESTORE_NOTES.txt"), encoding="utf-8"
    ).read()
    assert "BFEnterprise" in notes
    assert "ServerKeyTool.exe /encrypt" in notes
    assert "/sitePvkPassword:<password>" in notes
    assert "ClientIdentityMatch" in notes


def test_remote_processes_prompt(upgrade, tmp_path):
    """Test the operator is asked to stop remote WebUI and Web Reports first."""
    prompts = []
    ctx, _server = hcl_ctx(
        upgrade,
        tmp_path,
        ask=lambda prompt, choices, default=None: prompts.append(prompt) or default,
    )

    upgrade.ACTIONS["remote_processes"](ctx)

    assert prompts and "remote" in prompts[0].lower()


def test_masthead_warning_only_without_actionsite(upgrade):
    """Test no masthead warning while the server's actionsite.afxm can be copied."""
    local = upgrade.collect_local_info(local_host(upgrade))
    local["bigfix"]["key_files"]["found"]["masthead.afxm"] = []

    assert not any(
        "masthead" in w for w in upgrade.report_warnings(local, now=REPORT_NOW)
    )

    local["bigfix"]["key_files"]["found"]["actionsite.afxm"] = []
    assert any("masthead" in w for w in upgrade.report_warnings(local, now=REPORT_NOW))


def test_masthead_prefers_the_server_copy(upgrade, tmp_path):
    """Test the BES Server folder's ActionSite.afxm wins over the client's."""
    ctx, server = hcl_ctx(upgrade, tmp_path)
    client_copy = tmp_path / "BES Client" / "ActionSite.afxm"
    client_copy.write_bytes(b"client copy")
    ctx.state["baseline"]["bigfix"]["key_files"]["found"]["actionsite.afxm"] = [
        str(client_copy),
        str(server / "actionsite.afxm"),
    ]

    upgrade.ACTIONS["masthead"](ctx)

    assert ctx.state["masthead_copy"]["from"] == str(server / "actionsite.afxm")


def test_key_files_ignores_redacted_values(upgrade, tmp_path):
    """Test a redacted value from an older state file is never copied as paths."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    ctx.state["baseline"]["bigfix"]["key_files"]["found"]["license.pvk"] = "<redacted>"

    upgrade.ACTIONS["key_files"](ctx)

    assert not any(
        "license.pvk" in target for target in ctx.state.get("key_file_copies", {})
    )


def test_dry_run_keeps_no_progress(upgrade, tmp_path, monkeypatch):
    """Test a dry run walkthrough leaves the state file exactly as it was."""
    state_path = tmp_path / "state.json"
    host = local_host(upgrade)
    state = {
        "done": [],
        "reports": {},
        "plan": {"steps": []},
        "local_sql": True,
        "baseline": upgrade.collect_local_info(host),
    }
    state_path.write_text(json.dumps(state))
    before = state_path.read_text()
    args = types.SimpleNamespace(
        state_file=str(state_path),
        step=None,
        dry_run=True,
        backup_dir=str(tmp_path / "share"),
        backup_share_user=None,
        staging_dir=None,
        sql_instance=None,
        dry_run_file=str(tmp_path / "dryrun.txt"),
    )

    def no_prompts(*args, **kwargs):
        raise AssertionError("a dry run must not prompt")

    monkeypatch.setattr(upgrade, "_ask", no_prompts)
    monkeypatch.setattr("builtins.input", no_prompts)
    monkeypatch.setattr(upgrade.getpass, "getpass", no_prompts)
    monkeypatch.setattr(
        upgrade.besapi.plugin_utilities, "get_besapi_connection", lambda args: None
    )

    assert upgrade.run_walkthrough(args, None, host, {}) == 0

    assert state_path.read_text() == before
    assert not (tmp_path / "share").exists()
    # everything it printed is saved, to hand over in one file:
    saved = (tmp_path / "dryrun.txt").read_text(encoding="utf-8")
    for step_id in ("preflight", "stop_services_0", "backup", "final_validation"):
        assert f"===== {step_id}:" in saved
    assert "DRY RUN, would" in saved
    assert "All steps are complete." in saved


def test_dry_run_doesnt_connect_share(upgrade, tmp_path, monkeypatch):
    """Test a dry run never asks for the share password or connects."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.dry_run = True
    ctx.args.backup_dir = r"\\fileserver\share\bigfix"
    ctx.args.backup_share_user = r"fileserver\operator"
    monkeypatch.setattr(
        upgrade.getpass, "getpass", lambda prompt: pytest.fail("prompted")
    )

    ctx.backup_dir()

    assert ctx.host.shares == []


def test_client_data_computer_id_any_case(upgrade, tmp_path):
    """Test the client's ComputerId value is found, registry names ignore case."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    ctx.host.registry[upgrade.CLIENT_GLOBAL_OPTIONS_KEY] = {"ComputerId": 11333902}

    upgrade.ACTIONS["client_data"](ctx)

    assert ctx.state["client_data"]["computer_id"] == 11333902


@pytest.mark.parametrize(
    "path",
    [
        r"\192.168.5.39_tmp_backup",  # \\ and \_ eaten by a bash-like shell
        "192.168.5.39_tmp_backup",
    ],
)
def test_backup_dir_mangled_path_refused(upgrade, tmp_path, path):
    """Test a UNC path that lost its backslashes is refused with a hint."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.dry_run = True
    ctx.args.backup_dir = path

    with pytest.raises(SystemExit, match="backslash"):
        ctx.backup_dir()


def test_dry_run_folder_backup_reports_space(upgrade, tmp_path, monkeypatch, capsys):
    """Test a dry run still reports free space when the backup folder exists."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    ctx.args.dry_run = True
    ctx.args.backup_dir = str(tmp_path)
    usage = types.SimpleNamespace(total=10, used=10, free=10)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    upgrade.ACTIONS["folder_backup"](ctx)

    assert "WARNING: only" in capsys.readouterr().out


def _with_download_cache(server):
    cache = server / "wwwrootbes" / "bfmirror" / "downloads" / "sha1"
    cache.mkdir(parents=True)
    (cache / "0123abcd").write_bytes(b"d" * 1000)


def test_folder_backup_leaves_out_download_cache(upgrade, tmp_path):
    """Test wwwrootbes is backed up without bfmirror/downloads by default."""
    ctx, server = hcl_ctx(upgrade, tmp_path)
    _with_download_cache(server)

    upgrade.ACTIONS["folder_backup"](ctx)

    wwwroot = os.path.join(ctx.backup_dir(), "server_files", "wwwrootbes")
    assert os.path.isfile(os.path.join(wwwroot, "masthead", "masthead.afxm"))
    assert not os.path.exists(os.path.join(wwwroot, "bfmirror", "downloads"))
    assert ctx.state["server_files"]["wwwrootbes"]["bytes"] == len(b"masthead")
    assert ctx.state["server_files"]["wwwrootbes"]["excluded"] == ["bfmirror/downloads"]


def test_folder_backup_download_cache_option(upgrade, tmp_path):
    """Test --backup-download-cache keeps the download cache in the backup."""
    ctx, server = hcl_ctx(upgrade, tmp_path)
    _with_download_cache(server)
    ctx.args.backup_download_cache = True

    upgrade.ACTIONS["folder_backup"](ctx)

    cached = os.path.join(
        ctx.backup_dir(), "server_files", "wwwrootbes", "bfmirror", "downloads"
    )
    assert os.path.isfile(os.path.join(cached, "sha1", "0123abcd"))


def test_backup_download_cache_argument(upgrade):
    """Test the download cache is left out unless asked for."""
    parser = upgrade.build_parser()
    assert parser.parse_args([]).backup_download_cache is False
    assert parser.parse_args(["--backup-download-cache"]).backup_download_cache


def test_key_files_found_at_drive_root(upgrade):
    """Test license.pvk kept at the root of the server's drive is found, without
    searching the rest of the drive.
    """
    host = local_host(upgrade)
    host.files.add(r"C:\license.pvk")
    host.files.add(r"C:\Users\someone\license.pvk")

    key_files = upgrade.collect_local_info(host)["bigfix"]["key_files"]

    assert key_files["found"]["license.pvk"] == [r"C:\license.pvk"]
    assert "C:\\" in key_files["searched"]


def test_server_keys_found_pvk_is_default(upgrade, tmp_path):
    """Test Enter uses the license.pvk found on the server."""
    ctx, _server = hcl_ctx(
        upgrade,
        tmp_path,
        ask=lambda prompt, choices, default=None: "yes",
        input_fn=lambda prompt: "",
        getpass_fn=lambda prompt: "pw",
    )
    ctx.state["baseline"]["bigfix"]["key_files"]["found"]["license.pvk"] = [
        r"C:\license.pvk"
    ]

    upgrade.ACTIONS["server_keys"](ctx)

    (command,) = ctx.host.secret_runs
    assert r"/sitePvkLocation:C:\license.pvk" in command


def test_client_data_computer_id_binary(upgrade, tmp_path):
    """Test a REG_BINARY ComputerId, as the client writes it, is saved as a number."""
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    ctx.host.registry[upgrade.CLIENT_GLOBAL_OPTIONS_KEY] = {
        "ComputerId": b"\x0e\xf1\xac\x00\x00\x00\x00\x00"
    }

    upgrade.ACTIONS["client_data"](ctx)

    assert ctx.state["client_data"]["computer_id"] == 11333902


def test_build_steps_service_pack_step(upgrade, compat):
    """Test a needed service pack gets its own step, after the snapshot, and the
    upgrade checks it's applied first.
    """
    path = upgrade.find_upgrade_path(
        compat,
        {
            "bigfix": "10.0.7.52",
            "windows": "2012 R2",
            "mssql": "2008 R2",
            "mssql_level": "SP1",
        },
        {"windows": "2025", "mssql": "2025"},
    )
    steps = upgrade.build_steps(path, local_sql=True)
    ids = [step.id for step in steps]

    assert ids[3:6] == ["snapshot_1", "service_pack_1", "upgrade_1_mssql_2017"]
    service_pack = steps[4]
    assert "SP3" in service_pack.title and "2008 R2" in service_pack.title
    assert steps[5].actions == ["check_service_pack:SP3"]
    # the later SQL upgrade needs no service pack:
    assert not any(i.startswith("service_pack_") for i in ids[6:])


def test_build_steps_no_service_pack_step_when_applied(upgrade, compat):
    """Test no service pack step when the level is already enough."""
    path = upgrade.find_upgrade_path(
        compat,
        {
            "bigfix": "10.0.7.52",
            "windows": "2012 R2",
            "mssql": "2008 R2",
            "mssql_level": "SP3",
        },
        {"windows": "2025", "mssql": "2025"},
    )
    ids = [step.id for step in upgrade.build_steps(path, local_sql=True)]

    assert not any(i.startswith("service_pack_") for i in ids)


def _level_ctx(upgrade, tmp_path, level):
    host = local_host(
        upgrade,
        sql_handler=lambda server, query: (
            [[level]] if query == upgrade.SQL_PRODUCT_LEVEL else []
        ),
    )
    return walkthrough_ctx(upgrade, tmp_path, host)


def test_check_service_pack_stops_when_missing(upgrade, tmp_path):
    """Test the upgrade stops if the service pack isn't applied yet."""
    ctx = _level_ctx(upgrade, tmp_path, "SP1")

    with pytest.raises(SystemExit, match="SP3"):
        upgrade.ACTIONS["check_service_pack"](ctx, "SP3")


def test_check_service_pack_passes(upgrade, tmp_path, capsys):
    """Test the upgrade continues once the service pack is applied."""
    ctx = _level_ctx(upgrade, tmp_path, "SP3")

    upgrade.ACTIONS["check_service_pack"](ctx, "SP3")

    assert "OK" in capsys.readouterr().out


def test_check_service_pack_dry_run_warns(upgrade, tmp_path, capsys):
    """Test a dry run reports a missing service pack without stopping."""
    ctx = _level_ctx(upgrade, tmp_path, "SP1")
    ctx.args.dry_run = True

    upgrade.ACTIONS["check_service_pack"](ctx, "SP3")

    assert "WARNING" in capsys.readouterr().out


# ---------------------------------------------------------------- Hyper-V host

HYPERV_VMS = [
    {
        "Name": "bigfix-root",
        "Id": "0b6a7f1e-0000-4000-8000-000000000001",
        "State": "Running",
        "Generation": 2,
        "Version": "9.0",
        "CheckpointType": "Production",
        "Checkpoints": ["before sql"],
        "Heartbeat": "OkApplicationsHealthy",
        "Path": r"D:\VMs\bigfix-root",
        "IPAddresses": ["192.168.5.40", "fe80::1"],
        "SwitchNames": ["LAN"],
        "Disks": [{"Path": r"D:\VMs\bigfix-root\root.vhdx", "Bytes": 300 * 1024**3}],
    },
    {
        "Name": "other",
        "Id": "0b6a7f1e-0000-4000-8000-000000000002",
        "State": "Off",
        "Generation": 1,
        "Version": "5.0",
        "CheckpointType": "Standard",
        "Checkpoints": [],
        "Heartbeat": None,
        "Path": r"D:\VMs\other",
        "IPAddresses": [],
        "SwitchNames": ["LAN"],
        "Disks": [],
    },
]


def hyperv_host(upgrade, product="Windows Server 2019 Datacenter", **overrides):
    """A fake Hyper-V host with the root server's VM and one other."""
    options = {
        "registry": {
            upgrade.WINDOWS_CURRENT_VERSION_KEY: {
                "ProductName": product,
                "CurrentBuild": "17763",
            }
        },
        "powershell": {
            upgrade.PS_HYPERV_HOST: True,
            upgrade.PS_HYPERV_VMS: HYPERV_VMS,
            # a single switch comes back from ConvertTo-Json as an object:
            upgrade.PS_HYPERV_SWITCHES: {"Name": "LAN", "SwitchType": "External"},
            upgrade.PS_DISKS: [
                {"DeviceID": "D:", "Size": 2000 * 1024**3, "FreeSpace": 900 * 1024**3}
            ],
            upgrade.PS_FEATURES: ["Hyper-V"],
        },
    }
    options.update(overrides)
    return FakeHost(**options)


def test_collect_hyperv_info(upgrade):
    """Test the host, its VMs and switches are collected, and the BigFix VM is
    found by the root server's address.
    """
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), "192.168.5.40")

    assert info["windows_version"] == "2019"
    assert [vm["Name"] for vm in info["vms"]] == ["bigfix-root", "other"]
    assert info["switches"] == [{"Name": "LAN", "SwitchType": "External"}]
    assert info["bigfix_vms"] == ["bigfix-root"]
    assert info["disks"][0]["DeviceID"] == "D:"


def test_collect_hyperv_info_vm_name_override(upgrade):
    """Test --vm-name picks the BigFix VMs, even with no address match."""
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), None, ["other"])

    assert info["bigfix_vms"] == ["other"]


def test_collect_hyperv_info_unknown_vm_name(upgrade):
    """Test a --vm-name that isn't a VM here is reported, not used."""
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), None, ["nope"])

    assert info["bigfix_vms"] == []
    assert "nope" in info["errors"][0]


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"windows": False}, "Windows"),
        ({"admin": False}, "administrator"),
    ],
)
def test_collect_hyperv_info_skipped(upgrade, changes, reason):
    """Test collection is skipped off Windows, or without admin rights."""
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade, **changes), None)

    assert reason in info["skipped"]


def test_collect_hyperv_info_not_hyperv(upgrade):
    """Test a computer that isn't a Hyper-V host is skipped."""
    host = local_host(upgrade)

    assert "Hyper-V" in upgrade.collect_hyperv_info(host, None)["skipped"]


def test_compat_has_hyperv_guests(compat):
    """Test the guest to minimum host data is present, with its source."""
    section = compat["hyperv_guests"]
    assert section["versions"]["2025"] == "2022"
    assert section["versions"]["2022"] == "2019"
    assert any("learn.microsoft.com" in url for url in section["sources"])


def test_hyperv_check_host_new_enough(upgrade, compat):
    """Test no host upgrade is needed when the host supports every guest."""
    check = upgrade.hyperv_guest_check(compat, "2022", ["2012 R2", "2019", "2025"])

    assert check["supported"] is True
    assert check["host_upgrades"] == []


def test_hyperv_check_host_too_old(upgrade, compat):
    """Test a 2025 guest on a 2019 host plans a host upgrade to 2022 first."""
    check = upgrade.hyperv_guest_check(compat, "2019", ["2012 R2", "2019", "2025"])

    assert check["supported"] is False
    assert check["needed_host"] == "2022"
    assert check["host_upgrades"] == ["2022"]
    assert any("2025" in problem and "2022" in problem for problem in check["problems"])


def test_hyperv_check_host_path_multi_hop(upgrade, compat):
    """Test the host path follows Microsoft's in-place upgrade paths."""
    check = upgrade.hyperv_guest_check(compat, "2012", ["2025"])

    # 2012 can only go to 2012 R2 or 2016, then on to 2022 or later:
    assert check["host_upgrades"][-1] == "2022"
    assert check["host_upgrades"][0] in ("2012 R2", "2016")


def test_hyperv_check_unknown_host(upgrade, compat):
    """Test an unknown host version is reported, not guessed."""
    check = upgrade.hyperv_guest_check(compat, None, ["2025"])

    assert check["supported"] is None
    assert "host" in check["problems"][0]


def test_build_report_on_hyperv_host(upgrade, compat):
    """Test the report on the Hyper-V host checks the root server's planned
    Windows versions against the host, and finds its VM by the REST address.
    """
    conn = FakeConnection(rest_answers(upgrade), {"serverinfo": SERVERINFO})

    report = upgrade.build_report(conn, hyperv_host(upgrade), compat)

    assert report["hyperv"]["bigfix_vms"] == ["bigfix-root"]
    check = report["upgrade_assessment"]["hyperv"]
    # the root server's 2012 R2 goes to 2025 in the plan, so the 2019 host
    # must go to 2022 first:
    assert check["host"] == "2019"
    assert check["host_upgrades"] == ["2022"]
    assert "2025" in check["guests"]


def test_build_report_root_has_no_hyperv_check(upgrade, compat):
    """Test the root server's report has no Hyper-V assessment."""
    conn = FakeConnection(rest_answers(upgrade), {"serverinfo": SERVERINFO})

    report = upgrade.build_report(conn, local_host(upgrade), compat)

    assert "skipped" in report["hyperv"]
    assert "hyperv" not in report["upgrade_assessment"]


def test_vm_name_argument(upgrade):
    """Test --vm-name can be given more than once."""
    args = upgrade.build_parser().parse_args(["--vm-name", "a", "--vm-name", "b"])
    assert args.vm_name == ["a", "b"]


def test_build_report_hyperv_root_from_masthead(upgrade, compat, monkeypatch):
    """Test with no REST connection, the root's address comes from the local
    client's masthead.
    """
    seen = []
    monkeypatch.setattr(
        upgrade,
        "discover_root_ip",
        lambda paths: seen.append(paths) or "192.168.5.40",
    )

    report = upgrade.build_report(None, hyperv_host(upgrade), compat)

    assert seen == [upgrade.CLIENT_MASTHEAD_PATHS]
    assert report["hyperv"]["bigfix_vms"] == ["bigfix-root"]


def test_build_report_hyperv_without_root_versions(upgrade, compat, monkeypatch):
    """Test with nothing known about the root server, the target Windows is still
    checked against the host, so a 2012 R2 host isn't reported as fine.
    """
    monkeypatch.setattr(upgrade, "discover_root_ip", lambda paths: None)
    host = hyperv_host(upgrade, product="Windows Server 2012 R2 Datacenter")

    report = upgrade.build_report(None, host, compat)

    check = report["upgrade_assessment"]["hyperv"]
    assert check["guests"] == ["2025"]
    assert check["supported"] is False
    assert check["host_upgrades"]


def test_collect_hyperv_info_vm_by_name(upgrade):
    """Test with no root address, the one VM named like BigFix is picked, and
    says it was matched by name.
    """
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), None)

    assert info["bigfix_vms"] == ["bigfix-root"]
    assert info["bigfix_vms_matched_by"] == "name"
    assert info["root_ip"] is None


def test_collect_hyperv_info_vm_by_address(upgrade):
    """Test the address match is preferred and recorded."""
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), "192.168.5.40")

    assert info["bigfix_vms_matched_by"] == "address"
    assert info["root_ip"] == "192.168.5.40"


def test_vm_disk_bytes_counts_checkpoint_chain(upgrade):
    """Test a VM's disk size includes the parents of a checkpoint's .avhdx."""
    vm = {
        "Disks": [
            {
                "Path": r"C:\vhd\root_1.avhdx",
                "Bytes": 100,
                "Chain": [
                    {"Path": r"C:\vhd\root_1.avhdx", "Bytes": 100},
                    {"Path": r"C:\vhd\root.vhdx", "Bytes": 900},
                ],
            },
            # an older report without the chain:
            {"Path": r"D:\data.vhdx", "Bytes": 50},
        ]
    }

    assert upgrade.vm_disk_bytes(vm) == 1050


def test_collect_hyperv_info_vm_by_name_when_address_misses(upgrade):
    """Test an address no VM has, like a public DNS answer for the root server's
    name, still falls back to the one VM named like BigFix.
    """
    info = upgrade.collect_hyperv_info(hyperv_host(upgrade), "64.52.192.155")

    assert info["bigfix_vms"] == ["bigfix-root"]
    assert info["bigfix_vms_matched_by"] == "name"
    assert "64.52.192.155" in info["errors"][0]


# ---------------------------------------------------------------- resume tokens

CODE = "123456"


def code_password(upgrade, code=CODE):
    return upgrade.derive_password(None, "123456789", code)


class ResumeRig:
    """A coordinator on localhost, and helpers to connect nodes to it."""

    def __init__(self, upgrade, tokens=None, now=None, output=None):
        self.upgrade = upgrade
        self.output = output if output is not None else []
        self.coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=self.output.append,
            tokens=tokens if tokens is not None else {},
            **coordinator_options(
                upgrade,
                password=code_password(upgrade),
                **({"now": now} if now else {}),
            ),
        )
        self.server = None
        self.port = None

    async def start(self):
        self.server = await self.coordinator.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def node(self, password, resume=None, prompt_code=None, name="bigfix"):
        saved = {"token": resume}

        def on_resume(token):
            saved["token"] = token

        node = self.upgrade.ShareSessionNode(
            name,
            ["console"],
            None,
            output=lambda line: None,
            resume=resume,
            on_resume=on_resume,
            prompt_code=prompt_code,
            **session_options(self.upgrade, password=password),
        )
        return node, saved

    async def connect_until_token(self, node, saved):
        """Run a node until it has a resume token, then drop it, like a reboot."""
        task = asyncio.create_task(node.run("127.0.0.1", self.port))
        for _ in range(500):
            if saved["token"]:
                break
            await asyncio.sleep(0.01)
        channel = self.coordinator.nodes[node.name]["channel"]
        key = channel.send_key
        channel.close()
        await asyncio.wait_for(task, timeout=5)
        for _ in range(500):
            if node.name not in self.coordinator.nodes:
                break
            await asyncio.sleep(0.01)
        return key

    async def run_briefly(self, node):
        """Run a node until it's connected, return its coordinator side key."""
        task = asyncio.create_task(node.run("127.0.0.1", self.port))
        await self.coordinator.wait_for_nodes(1, timeout=5)
        key = self.coordinator.nodes[node.name]["channel"].send_key
        return task, key

    async def finish(self, *tasks):
        await self.coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.server.close()


def test_resume_reconnects_without_code(upgrade):
    """Test a node that rebooted reconnects with its token, no code, same name,
    and with fresh keys.
    """

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        node, saved = rig.node(code_password(upgrade))
        first_key = await rig.connect_until_token(node, saved)

        again, _saved = rig.node(None, resume=saved["token"])
        task, second_key = await rig.run_briefly(again)
        names = list(rig.coordinator.nodes)
        await rig.finish(task)
        return saved["token"], first_key, second_key, names, rig

    token, first_key, second_key, names, rig = asyncio.run(scenario())
    assert set(token) >= {"id", "secret", "expires"}
    assert len(bytes.fromhex(token["id"])) == 16
    assert len(bytes.fromhex(token["secret"])) == 32
    assert first_key != second_key
    assert names == ["bigfix"]
    # the secret is never shown:
    assert token["secret"] not in "\n".join(rig.output)


def test_resume_unknown_token_falls_back_to_code(upgrade):
    """Test a token the coordinator doesn't know makes the node ask for the code,
    and drop the old token.
    """
    asked = []

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        token = {"id": "ab" * 16, "secret": "cd" * 32, "expires": time.time() + 60}
        node, saved = rig.node(
            None,
            resume=token,
            prompt_code=lambda: asked.append(1) or code_password(upgrade),
        )
        task, _key = await rig.run_briefly(node)
        await rig.finish(task)
        return saved, rig

    saved, rig = asyncio.run(scenario())
    assert asked == [1]
    assert rig.coordinator.failures == 0
    # a new token was issued, then dropped when the session finished:
    assert saved["token"] is None


def test_resume_wrong_secret_counts_toward_lockout(upgrade):
    """Test a known id with the wrong secret is a failed guess."""

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        node, saved = rig.node(code_password(upgrade))
        await rig.connect_until_token(node, saved)
        forged = dict(saved["token"], secret="00" * 32)
        again, _saved = rig.node(None, resume=forged)
        with pytest.raises(upgrade.HandshakeError):
            await asyncio.wait_for(again.run("127.0.0.1", rig.port), timeout=10)
        rig.server.close()
        return rig

    rig = asyncio.run(scenario())
    assert rig.coordinator.failures == 1


def test_resume_expired_token_falls_back(upgrade):
    """Test a token older than its lifetime isn't accepted."""
    clock = {"now": 1000.0}
    asked = []

    async def scenario():
        rig = ResumeRig(upgrade, now=lambda: clock["now"])
        await rig.start()
        node, saved = rig.node(code_password(upgrade))
        await rig.connect_until_token(node, saved)
        clock["now"] += upgrade.RESUME_TOKEN_SECONDS + 1
        again, _saved = rig.node(
            None,
            resume=dict(saved["token"], expires=time.time() + 60),
            prompt_code=lambda: asked.append(1) or code_password(upgrade),
        )
        task, _key = await rig.run_briefly(again)
        await rig.finish(task)

    asyncio.run(scenario())
    assert asked == [1]


def test_resume_revoked_token_falls_back(upgrade):
    """Test `revoke <node>` removes that node's token."""
    asked = []

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        node, saved = rig.node(code_password(upgrade))
        await rig.connect_until_token(node, saved)
        await rig.coordinator.handle_command("revoke bigfix", source="coordinator")
        tokens_left = dict(rig.coordinator.tokens)
        again, _saved = rig.node(
            None,
            resume=saved["token"],
            prompt_code=lambda: asked.append(1) or code_password(upgrade),
        )
        task, _key = await rig.run_briefly(again)
        await rig.finish(task)
        return tokens_left, rig

    tokens_left, rig = asyncio.run(scenario())
    assert tokens_left == {}
    assert asked == [1]
    # done ends the session, so no token outlives it:
    assert rig.coordinator.tokens == {}


def test_resume_survives_coordinator_restart(upgrade):
    """Test a restarted coordinator with the saved tokens still accepts them."""
    tokens = {}

    async def scenario():
        rig = ResumeRig(upgrade, tokens=tokens)
        await rig.start()
        node, saved = rig.node(code_password(upgrade))
        await rig.connect_until_token(node, saved)
        rig.server.close()

        restarted = ResumeRig(upgrade, tokens=json.loads(json.dumps(tokens)))
        await restarted.start()
        again, _saved = restarted.node(None, resume=saved["token"])
        task, _key = await restarted.run_briefly(again)
        names = list(restarted.coordinator.nodes)
        await restarted.finish(task)
        return names

    assert asyncio.run(scenario()) == ["bigfix"]


def test_resume_no_code_no_token_asks(upgrade):
    """Test a node with neither a code nor a token asks for the code."""
    asked = []

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        node, _saved = rig.node(
            None, prompt_code=lambda: asked.append(1) or code_password(upgrade)
        )
        task, _key = await rig.run_briefly(node)
        await rig.finish(task)
        return rig

    rig = asyncio.run(scenario())
    assert asked == [1]
    # asked before connecting, so no failed attempt is logged:
    assert not any("refused" in line for line in rig.output)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_state_file_owner_only(upgrade, tmp_path):
    """Test the state file, which can hold resume tokens, is owner-only."""
    path = str(tmp_path / "state.json")

    upgrade.save_state(path, {"done": []})

    assert os.stat(path).st_mode & 0o777 == 0o600


def test_session_pairing_code_kept_across_restart(upgrade, monkeypatch):
    """Test a restarted coordinator shows the same code until the session ends."""
    codes = iter(["111111", "222222"])
    monkeypatch.setattr(upgrade, "generate_pairing_code", lambda: next(codes))
    state = {}

    first = upgrade.session_pairing_code(state, None, "none", now=1000.0)
    again = upgrade.session_pairing_code(state, None, "none", now=2000.0)
    later = upgrade.session_pairing_code(
        state, None, "none", now=1000.0 + upgrade.RESUME_TOKEN_SECONDS + 1
    )

    assert first == again == "111111"
    assert later == "222222"


def test_session_pairing_code_given_or_psk(upgrade):
    """Test a given code is used and not saved, and a PSK needs no code."""
    state = {}

    assert upgrade.session_pairing_code(state, "333333", "none", now=1.0) == "333333"
    assert upgrade.session_pairing_code({}, None, "env", now=1.0) is None
    assert "pairing_code" not in json.dumps(state)


def test_end_session_state_clears_secrets(upgrade):
    """Test finishing a session drops its tokens and code from the state."""
    state = {
        "session": {"pairing_code": "111111", "expires": 5.0},
        "session_tokens": {"ab": {"node": "x", "secret": "cd"}},
        "session_resume_peer": {"id": "ab", "secret": "cd"},
    }

    upgrade.end_session_state(state)

    assert state == {}


# ---------------------------------------------------------------- node logs


def log_entry(message, level="INFO"):
    return {"time": "2026-09-27T10:00:00", "level": level, "message": message}


def test_log_store_file_per_node(upgrade, tmp_path):
    """Test each node's log goes to its own file, numbered from 1."""
    store = upgrade.NodeLogStore(str(tmp_path))

    assert store.record("root", log_entry("stopping services")) == 1
    assert store.record("root", log_entry("backup started")) == 2
    assert store.record("sql", log_entry("sql ok")) == 1

    root_log = (tmp_path / "root.log").read_text(encoding="utf-8")
    assert root_log.splitlines() == [
        "000001 2026-09-27T10:00:00 INFO stopping services",
        "000002 2026-09-27T10:00:00 INFO backup started",
    ]
    assert (tmp_path / "sql.log").exists()


def test_log_store_continues_after_restart(upgrade, tmp_path):
    """Test a restarted store keeps numbering from its files."""
    upgrade.NodeLogStore(str(tmp_path)).record("root", log_entry("one"))

    store = upgrade.NodeLogStore(str(tmp_path))

    assert store.record("root", log_entry("two")) == 2
    assert [e["message"] for e in store.since("root", 0)] == ["one", "two"]
    assert [e["seq"] for e in store.since("root", 1)] == [2]
    assert [e["message"] for e in store.tail("root", 1)] == ["two"]


def test_log_store_keeps_given_numbers(upgrade, tmp_path):
    """Test a console keeps the coordinator's numbers, skipping ones it has."""
    store = upgrade.NodeLogStore(str(tmp_path))

    assert store.record("root", log_entry("a"), seq=5) == 5
    assert store.record("root", log_entry("a again"), seq=5) is None
    assert store.record("root", log_entry("b"), seq=6) == 6
    assert store.last_seq("root") == 6
    assert store.last_seqs() == {"root": 6}


@pytest.mark.parametrize(
    "name", ["../evil", r"..\evil", "a/b", "C:x", "con", "", "x" * 200]
)
def test_log_store_safe_file_names(upgrade, tmp_path, name):
    """Test a node name can't write outside the log folder."""
    store = upgrade.NodeLogStore(str(tmp_path))

    store.record(name, log_entry("x"))

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert len(files[0].name) <= 80


def test_log_text_cleaned(upgrade, tmp_path):
    """Test control characters, like terminal escapes and new lines, are removed,
    and long messages cut.
    """
    store = upgrade.NodeLogStore(str(tmp_path))

    store.record("root", log_entry("red \x1b[31mtext\nforged 000009 line\x07"))
    store.record("root", log_entry("y" * 10000, level="WARNING\x1b"))

    lines = (tmp_path / "root.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "\x1b" not in lines[0] and "\x07" not in lines[0]
    assert lines[0].endswith("red [31mtext forged 000009 line")
    assert len(lines[1]) < upgrade.LOG_MESSAGE_MAX + 50
    assert " WARNING " in lines[1]


class LogRig:
    """A coordinator with a log store, a peer that logs, and a console."""

    def __init__(self, upgrade, tmp_path):
        self.upgrade = upgrade
        self.tmp_path = tmp_path
        self.store = upgrade.NodeLogStore(str(tmp_path / "coordinator_logs"))
        self.coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            log_store=self.store,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        self.console_output = []

    async def start(self):
        self.server = await self.coordinator.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def peer(self, resume=None):
        saved = {"token": resume}
        node = self.upgrade.ShareSessionNode(
            "root",
            ["root"],
            client_host(self.upgrade),
            output=lambda line: None,
            resume=resume,
            on_resume=lambda token: saved.update(token=token),
            **session_options(self.upgrade, password=code_password(self.upgrade)),
        )
        return node, saved

    def console(self, commands=(), resume=None):
        saved = {"token": resume}
        node = self.upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=self.console_output.append,
            commands=list(commands),
            resume=resume,
            on_resume=lambda token: saved.update(token=token),
            log_store=self.upgrade.NodeLogStore(str(self.tmp_path / "console_logs")),
            **session_options(self.upgrade, password=code_password(self.upgrade)),
        )
        return node, saved

    async def until(self, condition, timeout=5):
        for _ in range(int(timeout * 100)):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting")

    def messages(self, folder, node="root"):
        store = self.upgrade.NodeLogStore(str(self.tmp_path / folder))
        return [entry["message"] for entry in store.since(node, 0)]

    async def drop(self, name, task):
        self.coordinator.nodes[name]["channel"].close()
        await asyncio.wait_for(task, timeout=5)
        await self.until(lambda: name not in self.coordinator.nodes)

    async def finish(self, *tasks):
        await self.coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.server.close()


def test_node_logs_reach_coordinator_and_console(upgrade, tmp_path):
    """Test a node's log lines are written per node by the coordinator and the
    console.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        console, _ = rig.console()
        peer, _ = rig.peer()
        tasks = [
            asyncio.create_task(n.run("127.0.0.1", rig.port)) for n in (console, peer)
        ]
        await rig.coordinator.wait_for_nodes(2, timeout=5)
        peer.log("INFO", "stopping BES services")
        await rig.until(lambda: "stopping BES services" in rig.messages("console_logs"))
        await rig.finish(*tasks)
        return rig

    rig = asyncio.run(scenario())
    assert "stopping BES services" in rig.messages("coordinator_logs")
    assert "stopping BES services" in rig.messages("console_logs")
    assert (tmp_path / "coordinator_logs" / "root.log").exists()
    assert (tmp_path / "console_logs" / "root.log").exists()
    assert any("stopping BES services" in line for line in rig.console_output)


def test_node_logs_buffered_while_disconnected(upgrade, tmp_path):
    """Test lines logged while a node is disconnected arrive once it reconnects,
    each only once.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, saved = rig.peer()
        peer.log("INFO", "before connecting")
        task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.until(lambda: saved["token"])
        await rig.until(lambda: "before connecting" in rig.messages("coordinator_logs"))
        await rig.drop("root", task)
        peer.log("INFO", "while rebooting")
        task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.until(lambda: "while rebooting" in rig.messages("coordinator_logs"))
        await rig.finish(task)
        return rig

    rig = asyncio.run(scenario())
    messages = rig.messages("coordinator_logs")
    assert messages.count("before connecting") == 1
    assert messages.count("while rebooting") == 1


def test_console_catches_up_on_missed_lines(upgrade, tmp_path):
    """Test a console that was away gets the lines it missed, only once."""

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, _ = rig.peer()
        console, console_saved = rig.console()
        peer_task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        console_task = asyncio.create_task(console.run("127.0.0.1", rig.port))
        await rig.coordinator.wait_for_nodes(2, timeout=5)
        peer.log("INFO", "seen live")
        await rig.until(lambda: "seen live" in rig.messages("console_logs"))
        await rig.until(lambda: console_saved["token"])
        await rig.drop("mac", console_task)

        peer.log("INFO", "missed one")
        peer.log("INFO", "missed two")
        await rig.until(lambda: "missed two" in rig.messages("coordinator_logs"))
        again, _ = rig.console(resume=console_saved["token"])
        console_task = asyncio.create_task(again.run("127.0.0.1", rig.port))
        await rig.until(lambda: "missed two" in rig.messages("console_logs"))
        await rig.finish(peer_task, console_task)
        return rig

    rig = asyncio.run(scenario())
    messages = rig.messages("console_logs")
    assert [m for m in messages if m in ("seen live", "missed one", "missed two")] == [
        "seen live",
        "missed one",
        "missed two",
    ]


def test_log_command_shows_recent_lines(upgrade, tmp_path):
    """Test `log <node> <lines>` shows that node's last lines to who asked."""

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, _ = rig.peer()
        peer_task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        # after the peer's share check, so its lines are the last ones:
        await rig.until(lambda: rig.coordinator.results.get("root"))
        for n in range(3):
            peer.log("INFO", f"line {n}")
        await rig.until(lambda: "line 2" in rig.messages("coordinator_logs"))
        console, _ = rig.console(commands=["log root 2"])
        console_task = asyncio.create_task(console.run("127.0.0.1", rig.port))
        await rig.until(
            lambda: any(
                re.match(r"root \d{6} .* line 2$", line) for line in rig.console_output
            )
        )
        await rig.finish(peer_task, console_task)
        return rig

    rig = asyncio.run(scenario())
    # the reply to `log root 2`, not the catch up's `[root] ...` lines:
    reply = [line for line in rig.console_output if re.match(r"root \d{6} ", line)]
    assert [line.rsplit(" ", 2)[-2:] for line in reply] == [
        ["line", "1"],
        ["line", "2"],
    ]


def test_coordinator_output_logged(upgrade, tmp_path):
    """Test the coordinator's own messages are kept in coordinator.log."""

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, _ = rig.peer()
        task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.finish(task)
        return rig

    rig = asyncio.run(scenario())
    assert any(
        "root" in m and "connected" in m
        for m in rig.messages("coordinator_logs", node="coordinator")
    )


def test_log_dir_argument(upgrade):
    """Test node logs go to a folder by default, --log-dir changes it."""
    parser = upgrade.build_parser()
    assert parser.parse_args([]).log_dir == upgrade.DEFAULT_LOG_DIR
    assert parser.parse_args(["--log-dir", "x"]).log_dir == "x"


def test_node_log_handler_forwards_records(upgrade):
    """Test a peer's logging records are sent on as its log lines."""
    node = upgrade.ShareSessionNode(
        "root", ["root"], None, output=lambda line: None, **session_options(upgrade)
    )
    handler = upgrade.NodeLogHandler(node)
    logger = logging.getLogger("test_node_log_handler")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("disk is %s full", "90%")
    finally:
        logger.removeHandler(handler)

    (entry,) = list(node.log_buffer)
    assert entry["level"] == "WARNING"
    assert entry["message"] == "disk is 90% full"


# ---------------------------------------------------------------- coordinated walkthrough


class WalkRig:
    """A coordinator, a root node running a scripted walkthrough, and consoles."""

    def __init__(self, upgrade, walkthrough, hyperv_host_=None):
        self.upgrade = upgrade
        self.walkthrough = walkthrough
        self.hyperv_host = hyperv_host_
        self.output = {"coordinator": []}
        self.coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=self.output["coordinator"].append,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        self.tasks = []
        self.nodes = {}

    async def start(self):
        self.server = await self.coordinator.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.add(
            "root", ["root"], client_host(self.upgrade), walkthrough=self.walkthrough
        )
        if self.hyperv_host:
            self.add("hyperv", ["hyperv"], self.hyperv_host)
        await self.coordinator.wait_for_nodes(len(self.tasks), timeout=5)

    def add(self, name, roles, host, commands=(), **options):
        self.output[name] = []
        node = self.upgrade.ShareSessionNode(
            name,
            roles,
            host,
            output=self.output[name].append,
            commands=list(commands),
            **session_options(self.upgrade, password=code_password(self.upgrade)),
            **options,
        )
        self.nodes[name] = node
        self.tasks.append(asyncio.create_task(node.run("127.0.0.1", self.port)))
        return node

    async def console(self, name, commands, oneshot=False):
        roles = ["console", "oneshot"] if oneshot else ["console"]
        count = len(self.coordinator.nodes)
        self.add(name, roles, None, commands=commands)
        await self.coordinator.wait_for_nodes(count + 1, timeout=5)

    async def until(self, condition, timeout=5):
        for _ in range(int(timeout * 100)):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"timed out, coordinator said: {self.output['coordinator']}"
        )

    def text(self, name):
        return "\n".join(self.output[name])

    async def finish(self):
        await self.coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=5)
        self.server.close()


def test_walkthrough_question_answered_from_console(upgrade):
    """Test the root's question reaches the coordinator and consoles, and a
    console's answer goes back to the walkthrough.
    """
    answers = []

    def walkthrough(bridge):
        answers.append(bridge.ask("Is this step complete?", ["done", "skip", "quit"]))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        await rig.console("mac", ["done"])
        await rig.until(lambda: answers)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert answers == ["done"]
    assert "Is this step complete?" in rig.text("coordinator")
    assert "Is this step complete?" in rig.text("mac")
    assert "answered done" in rig.text("coordinator")
    # shown on the root's own terminal too, where it can be answered:
    assert "QUESTION: Is this step complete? [done/skip/quit]" in rig.text("root")


def test_walkthrough_first_answer_wins(upgrade):
    """Test a second answer to the same question is ignored, not given to the
    next question.
    """
    answers = []

    def walkthrough(bridge):
        answers.append(bridge.ask("first?", ["yes", "no"]))
        answers.append(bridge.ask("second?", ["yes", "no"]))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        first_id = rig.coordinator.question["id"]
        await rig.coordinator.handle_command("answer no", source="coordinator")
        await rig.until(
            lambda: rig.coordinator.question
            and rig.coordinator.question["id"] != first_id
        )
        # a late answer to the first question, by id, is stale:
        await rig.coordinator.handle_command(
            f"answer {first_id} yes", source="coordinator"
        )
        await asyncio.sleep(0.2)
        assert len(answers) == 1
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.until(lambda: len(answers) == 2)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert answers == ["no", "yes"]
    assert "stale" in rig.text("coordinator")


def test_walkthrough_invalid_answer_refused(upgrade):
    """Test an answer that isn't one of the choices is refused."""
    answers = []

    def walkthrough(bridge):
        answers.append(bridge.ask("continue?", ["yes", "no"]))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        await rig.coordinator.handle_command("answer maybe", source="coordinator")
        await asyncio.sleep(0.2)
        assert answers == []
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.until(lambda: answers)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert "not one of" in rig.text("coordinator")


def test_halt_pauses_walkthrough_until_continue(upgrade):
    """Test halt stops the walkthrough at its next check, and continue from
    whoever halted resumes it.
    """
    progress = []

    def walkthrough(bridge):
        progress.append("started")
        bridge.ask("ready?", ["yes"])
        bridge.wait_if_halted()
        progress.append("after halt")

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        await rig.console("ai", ["halt disk filling up"], oneshot=True)
        await rig.until(lambda: rig.coordinator.halted)
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await asyncio.sleep(0.3)
        assert progress == ["started"]
        # another one-shot console can't continue someone else's halt:
        await rig.console("ai2", ["continue"], oneshot=True)
        await asyncio.sleep(0.3)
        assert progress == ["started"]
        await rig.nodes["ai"].command_queue.put("continue")
        await rig.until(lambda: progress == ["started", "after halt"])
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    text = rig.text("coordinator")
    assert "halted by ai: disk filling up" in text
    assert "only ai or a person" in text


def test_halt_continue_by_person(upgrade):
    """Test a person at the coordinator can continue any halt."""
    progress = []

    def walkthrough(bridge):
        bridge.ask("ready?", ["yes"])
        bridge.wait_if_halted()
        progress.append("continued")

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        await rig.console("ai", ["halt"], oneshot=True)
        await rig.until(lambda: rig.coordinator.halted)
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.coordinator.handle_command("continue", source="coordinator")
        await rig.until(lambda: progress)
        await rig.finish()

    asyncio.run(scenario())


def test_state_command_pulls_node_state(upgrade):
    """Test `state root` returns the root's walkthrough state to who asked,
    without secrets.
    """

    def walkthrough(bridge):
        bridge.set_state(
            {"step": "backup", "done": ["preflight"], "share_password": "hunter2"}
        )
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        await rig.console("mac", ["state root"])
        await rig.until(lambda: '"step": "backup"' in rig.text("mac"))
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert "preflight" in rig.text("mac")
    assert "hunter2" not in rig.text("mac") + rig.text("coordinator")


def test_diag_command_runs_checks_alongside(upgrade):
    """Test `diag root disk` runs a read-only check while a question waits."""

    def walkthrough(bridge):
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        # after the share check that runs when the root connects:
        await rig.until(lambda: rig.coordinator.results.get("root"))
        ran_before = list(rig.nodes["root"].host.ran)
        await rig.console("mac", ["diag root disk"])
        await rig.until(lambda: "FreeSpace" in rig.text("mac"))
        rig.ran_during_diag = rig.nodes["root"].host.ran[len(ran_before) :]
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    # read-only, so nothing ran on the root:
    assert rig.ran_during_diag == []


def test_node_diagnostics_checks(upgrade):
    """Test each diagnostic check, and an unknown one."""
    host = client_host(upgrade)
    host.ports.add(52311)

    result = upgrade.run_node_diagnostics(host, "all", sql_server="localhost")

    assert set(result) >= {"disk", "reboot", "services", "ports"}
    assert result["ports"] == {"52311": True}
    assert "unknown" in upgrade.run_node_diagnostics(host, "nope")["error"]


def test_checkpoint_requested_from_hyperv_node(upgrade):
    """Test the root asks the Hyper-V node for a checkpoint, and gets the result."""
    results = []

    def walkthrough(bridge):
        results.append(
            bridge.remote_action(
                "hyperv", "checkpoint", {"vm": "bigfix-root", "name": "before sql"}
            )
        )

    async def scenario():
        rig = WalkRig(upgrade, walkthrough, hyperv_host_=hyperv_host(upgrade))
        await rig.start()
        await rig.until(lambda: results)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert results[0]["ok"] is True
    (command,) = rig.nodes["hyperv"].host.ran
    assert command[-1] == (
        "Checkpoint-VM -Name 'bigfix-root' -SnapshotName 'bigfix-upgrade before sql'"
    )
    assert not any(
        "Remove-VMSnapshot" in " ".join(c) for c in rig.nodes["hyperv"].host.ran
    )


def test_checkpoint_unsafe_name_refused(upgrade):
    """Test a VM or checkpoint name that could break out of the quotes is refused."""
    results = []

    def walkthrough(bridge):
        results.append(
            bridge.remote_action(
                "hyperv", "checkpoint", {"vm": "x'; Remove-VM y", "name": "a"}
            )
        )

    async def scenario():
        rig = WalkRig(upgrade, walkthrough, hyperv_host_=hyperv_host(upgrade))
        await rig.start()
        await rig.until(lambda: results)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert results[0]["ok"] is False
    assert rig.nodes["hyperv"].host.ran == []


def test_remote_action_without_node(upgrade):
    """Test asking for a node role that isn't connected fails at once, so the
    walkthrough can fall back to asking the operator.
    """
    results = []

    def walkthrough(bridge):
        results.append(bridge.remote_action("hyperv", "checkpoint", {"vm": "a"}))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: results)
        await rig.finish()

    asyncio.run(scenario())
    assert results[0]["ok"] is False
    assert "no hyperv node" in results[0]["error"]


class FakeSession:
    """Stands in for the WalkthroughBridge, without a network."""

    def __init__(self, action_result=None):
        self.prompts = []
        self.states = []
        self.halt_checks = 0
        self.actions = []
        self.action_result = action_result or {"ok": True}

    def ask(self, prompt, choices, default=None):
        self.prompts.append(prompt)
        return "done" if "done" in choices else default or choices[0]

    def wait_if_halted(self):
        self.halt_checks += 1

    def set_state(self, state):
        self.states.append(json.loads(json.dumps(state)))

    def remote_action(self, role, action, params, timeout=600):
        self.actions.append((role, action, params))
        return self.action_result


def test_walkthrough_runs_through_session(upgrade, tmp_path, monkeypatch):
    """Test the walkthrough asks through the session, checks for a halt before
    each step, and publishes its state.
    """
    host = local_host(upgrade)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "done": [],
                "reports": {},
                "plan": {"steps": []},
                "local_sql": True,
                "baseline": upgrade.collect_local_info(host),
            }
        )
    )
    args = types.SimpleNamespace(
        state_file=str(state_path),
        step=None,
        dry_run=True,
        backup_dir=str(tmp_path / "share"),
        backup_share_user=None,
        staging_dir=None,
        sql_instance=None,
        dry_run_file=str(tmp_path / "dryrun.txt"),
    )
    monkeypatch.setattr(
        upgrade.besapi.plugin_utilities, "get_besapi_connection", lambda args: None
    )
    session = FakeSession()

    assert upgrade.run_walkthrough(args, None, host, {}, session=session) == 0

    steps = [s["step"] for s in session.states if s.get("step")]
    assert steps[0] == "preflight" and "cleanup" in steps
    assert session.states[-1]["finished"] is True
    assert session.halt_checks >= len(set(steps))
    # a dry run answers itself, even in a session, and says what it answered:
    assert session.prompts == []
    saved = (tmp_path / "dryrun.txt").read_text(encoding="utf-8")
    # steps the script does go on with next, steps people do with done:
    assert "Go on to the next step? [next/skip/quit]: next (dry run)" in saved
    assert "Have you finished this step? [done/skip/quit]: done (dry run)" in saved


def test_checkpoint_action_through_session(upgrade, tmp_path, capsys):
    """Test a snapshot step asks the Hyper-V node for a checkpoint of this VM,
    found by this computer's addresses, and records it.
    """
    host = local_host(upgrade)
    host.powershell[upgrade.PS_HOST_IPS] = [
        {"IPAddress": "192.168.5.40", "PrefixLength": 24}
    ]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.session = FakeSession({"ok": True, "vm": "BigFixRoot", "checkpoint": "x"})

    upgrade.ACTIONS["checkpoint"](ctx, "snapshot_1")

    ((role, action, params),) = ctx.session.actions
    assert (role, action) == ("hyperv", "checkpoint")
    assert params == {"name": "snapshot_1", "ips": ["192.168.5.40"]}
    assert ctx.state["checkpoints"][0]["vm"] == "BigFixRoot"
    assert "Remove-VMSnapshot" in capsys.readouterr().out


def test_checkpoint_action_falls_back(upgrade, tmp_path, capsys):
    """Test with no Hyper-V node, the operator is asked to take the snapshot."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.session = FakeSession({"ok": False, "error": "no hyperv node is connected"})

    upgrade.ACTIONS["checkpoint"](ctx, "snapshot_1")

    assert "checkpoints" not in ctx.state
    assert "take the VM snapshot yourself" in capsys.readouterr().out


def test_checkpoint_action_without_session(upgrade, tmp_path, capsys):
    """Test a walkthrough on its own keeps the manual snapshot step."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))

    upgrade.ACTIONS["checkpoint"](ctx, "snapshot_1")

    assert "take the VM snapshot yourself" in capsys.readouterr().out


def test_hyperv_checkpoint_finds_vm_by_address(upgrade):
    """Test the Hyper-V node picks the VM with the asking node's address."""
    host = hyperv_host(upgrade)

    result = upgrade.run_node_action(
        host, ["hyperv"], "checkpoint", {"name": "snapshot_1", "ips": ["192.168.5.40"]}
    )

    assert result["ok"] is True and result["vm"] == "bigfix-root"
    assert host.ran[-1][-1] == (
        "Checkpoint-VM -Name 'bigfix-root' -SnapshotName 'bigfix-upgrade snapshot_1'"
    )


def test_snapshot_steps_request_checkpoints(upgrade, compat):
    """Test each snapshot step has the checkpoint action, named by its step."""
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    steps = upgrade.build_steps(path, local_sql=True)

    for step in steps:
        if step.id.startswith("snapshot_"):
            assert step.actions == [f"checkpoint:{step.id}"]


@pytest.mark.parametrize(
    "state_file, expected",
    [
        (
            "bigfix_root_server_upgrade_win.state.json",
            "bigfix_root_server_upgrade_win.session.json",
        ),
        ("other.json", "other.session.json"),
    ],
)
def test_session_state_path(upgrade, state_file, expected):
    """Test a node keeps its session token apart from the walkthrough's state, so
    the two never overwrite each other.
    """
    assert upgrade.session_state_path(state_file) == expected


def test_session_roles(upgrade):
    """Test the node's roles: root, Hyper-V host, other peer, or console."""
    assert upgrade.session_roles(local_host(upgrade), "peer") == ["root"]
    assert upgrade.session_roles(hyperv_host(upgrade), "peer") == ["hyperv"]
    assert upgrade.session_roles(hyperv_host(upgrade), "share_owner") == [
        "hyperv",
        "share_owner",
    ]
    other = FakeHost(powershell={upgrade.PS_HYPERV_HOST: False})
    assert upgrade.session_roles(other, "peer") == ["peer"]
    assert upgrade.session_roles(local_host(upgrade), "console") == ["console"]
    assert upgrade.session_roles(None, "console", oneshot=True) == [
        "console",
        "oneshot",
    ]


def test_walkthrough_prints_forwarded(upgrade):
    """Test what the walkthrough thread prints is also sent as the node's log,
    and other threads' prints aren't.
    """
    node = upgrade.ShareSessionNode(
        "root", ["root"], None, output=lambda line: None, **session_options(upgrade)
    )
    sink = io.StringIO()
    tee = upgrade.ThreadLogTee(sink, node)

    def walkthrough_thread():
        tee.claim()
        tee.write("===== backup: Back up BigFix =====\nhalf ")
        tee.write("a line\n")

    thread = threading.Thread(target=walkthrough_thread)
    thread.start()
    thread.join()
    tee.write("from another thread\n")

    assert [e["message"] for e in node.log_buffer] == [
        "===== backup: Back up BigFix =====",
        "half a line",
    ]
    assert "from another thread" in sink.getvalue()


def test_progress_poller_prints_forwarded(upgrade, monkeypatch):
    """Test the 60 second progress lines, printed from the poller's own thread,
    are forwarded when the walkthrough thread started the poller, and not.

    otherwise.
    """
    node = upgrade.ShareSessionNode(
        "root", ["root"], None, output=lambda line: None, **session_options(upgrade)
    )
    tee = upgrade.ThreadLogTee(io.StringIO(), node)
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0.01)
    monkeypatch.setattr(sys, "stdout", tee)

    def poll(text):
        printed = threading.Event()

        def report():
            print(text)
            printed.set()

        with upgrade.ProgressPoller(report):
            printed.wait(5)

    def walkthrough_thread():
        tee.claim()
        poll("backup: 40% done, about 3 min left")

    # Unclaimed first: a finished thread's id can be reused by a later one.
    poll("unclaimed progress")
    thread = threading.Thread(target=walkthrough_thread)
    thread.start()
    thread.join()

    messages = [e["message"] for e in node.log_buffer]
    assert "backup: 40% done, about 3 min left" in messages
    assert "unclaimed progress" not in messages


def test_oneshot_console_state_as_json(upgrade):
    """Test a one-shot console runs one command, returns the reply as data, and
    leaves without ending the session.
    """

    def walkthrough(bridge):
        bridge.set_state({"step": "backup", "done": ["preflight"]})
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        result = await asyncio.wait_for(
            upgrade.run_oneshot_command(
                "127.0.0.1",
                rig.port,
                "state root",
                wait=5,
                **session_options(upgrade, password=code_password(upgrade)),
            ),
            timeout=10,
        )
        still_running = not rig.coordinator.done.is_set()
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.finish()
        return result, still_running

    result, still_running = asyncio.run(scenario())
    assert still_running
    assert result["command"] == "state root"
    (reply,) = (r for r in result["replies"] if r["type"] == "reply")
    assert reply["node"] == "root"
    assert reply["data"]["walkthrough"]["step"] == "backup"
    assert result["question"]["prompt"] == "ready?"
    json.dumps(result)


def test_oneshot_console_halts(upgrade):
    """Test a one-shot halt is recorded as sent by that one-shot console."""

    def walkthrough(bridge):
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        result = await upgrade.run_oneshot_command(
            "127.0.0.1",
            rig.port,
            "halt sql backup looks stuck",
            wait=1,
            name="claude",
            **session_options(upgrade, password=code_password(upgrade)),
        )
        halted = dict(rig.coordinator.halted)
        await rig.coordinator.handle_command("continue", source="coordinator")
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.finish()
        return result, halted

    result, halted = asyncio.run(scenario())
    assert halted == {"by": "claude", "reason": "sql backup looks stuck"}
    assert any("halted by claude" in line for line in result["lines"])


def test_command_and_json_arguments(upgrade):
    """Test --command and --json parse."""
    args = upgrade.build_parser().parse_args(["--command", "state root", "--json"])
    assert args.command == "state root" and args.json is True


# ---------------------------------------------------------------- share owner


def test_share_owner_offers_share_to_coordinator(upgrade):
    """Test a coordinator with no share yet, like one on a Mac, takes the share
    from the Hyper-V host and hands it to peers, never the password to consoles.
    """

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(
                upgrade, share_unc=None, password=code_password(upgrade)
            ),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        outputs = {"root": [], "mac": [], "hyperv": []}
        peer_host = client_host(upgrade)
        nodes = {
            "root": upgrade.ShareSessionNode(
                "root",
                ["root"],
                peer_host,
                output=outputs["root"].append,
                **session_options(upgrade, password=code_password(upgrade)),
            ),
            "mac": upgrade.ShareSessionNode(
                "mac",
                ["console"],
                None,
                output=outputs["mac"].append,
                **session_options(upgrade, password=code_password(upgrade)),
            ),
        }
        tasks = [asyncio.create_task(n.run("127.0.0.1", port)) for n in nodes.values()]
        await coordinator.wait_for_nodes(2, timeout=5)
        owner = upgrade.ShareSessionNode(
            "hyperv",
            ["hyperv", "share_owner"],
            hyperv_host(upgrade),
            output=outputs["hyperv"].append,
            share_offer={
                "unc": SHARE_UNC,
                "user": r"HYPERV\bfupgrade_share",
                "password": "temp-Pw-123!",
            },
            **session_options(upgrade, password=code_password(upgrade)),
        )
        tasks.append(asyncio.create_task(owner.run("127.0.0.1", port)))
        for _ in range(500):
            if len(coordinator.results.get("root", [])) >= 1 and peer_host.shares:
                break
            await asyncio.sleep(0.01)
        share_unc = coordinator.share_unc
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        server.close()
        return share_unc, peer_host, nodes, outputs

    share_unc, peer_host, nodes, outputs = asyncio.run(scenario())
    assert share_unc == SHARE_UNC
    assert (SHARE_UNC, r"HYPERV\bfupgrade_share", "temp-Pw-123!") in peer_host.shares
    assert nodes["mac"].share.get("password") is None
    assert "temp-Pw-123!" not in "\n".join(outputs["mac"])


def test_share_offer_only_from_share_owner(upgrade):
    """Test a node that isn't the share owner can't change the share."""

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(
                upgrade, share_unc=None, password=code_password(upgrade)
            ),
        )
        await coordinator._on_share_offer(
            "root", {"unc": r"\\evil\share", "user": "x", "password": "y"}
        )
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.share_unc is None


@pytest.mark.parametrize(
    "hyperv, windows, found, expected",
    [
        (True, True, ("192.168.5.20", 52390), "share_owner"),
        (True, True, None, "coordinator"),
        (False, True, None, "peer"),
        (False, False, None, "console"),
    ],
)
def test_choose_session_node(upgrade, hyperv, windows, found, expected):
    """Test the Hyper-V host joins a running coordinator, like one on a Mac, as
    the share owner, and only coordinates when none answers.
    """
    host = FakeHost(powershell={upgrade.PS_HYPERV_HOST: hyperv}, windows=windows)

    async def discover(serial):
        return found

    node, address = asyncio.run(upgrade.choose_session_node(host, "123", discover))

    assert node == expected
    assert address == (found if expected == "share_owner" else None)


@pytest.mark.parametrize(
    "output, expected",
    [
        ("Firewall is enabled. (State = 1)", True),
        ("Firewall is disabled. (State = 0)", False),
        ("", None),
    ],
)
def test_macos_firewall_enabled(upgrade, output, expected):
    """Test the macOS application firewall state is read, None if unknown."""
    assert upgrade.macos_firewall_enabled(lambda cmd: output) is expected


# ---------------------------------------------------------------- coordinator outages


def test_halt_saved_and_kept_across_restart(upgrade):
    """Test a halt is saved as it changes, and a restarted coordinator keeps it,
    so a restart never continues a halted run.
    """
    saved = []
    progress = []

    def walkthrough(bridge):
        bridge.wait_if_halted()
        progress.append("ran")

    async def scenario():
        first = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            on_halt=saved.append,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        await first.handle_command("halt checking disk", source="coordinator")
        await first.handle_command("continue", source="coordinator")
        await first.handle_command("halt again", source="coordinator")

        restarted = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            halted=saved[-1],
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await restarted.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = upgrade.ShareSessionNode(
            "root",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            walkthrough=walkthrough,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await restarted.wait_for_nodes(1, timeout=5)
        await asyncio.sleep(0.3)
        halted_while_restarted = list(progress)
        await restarted.handle_command("continue", source="coordinator")
        for _ in range(300):
            if progress:
                break
            await asyncio.sleep(0.01)
        await restarted.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return halted_while_restarted

    halted_while_restarted = asyncio.run(scenario())
    assert saved == [
        {"by": "coordinator", "reason": "checking disk"},
        None,
        {"by": "coordinator", "reason": "again"},
    ]
    assert halted_while_restarted == []
    assert progress == ["ran"]


def test_root_answers_locally_when_coordinator_gone(upgrade):
    """Test with the coordinator unreachable, the root's own terminal answers the
    waiting question, and a wrong answer there is refused.
    """
    answers = []
    printed = []

    def walkthrough(bridge):
        answers.append(bridge.ask("Is this step complete?", ["done", "skip", "quit"]))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        root = rig.nodes["root"]
        root._print = printed.append
        rig.server.close()
        rig.coordinator.nodes["root"]["channel"].close()
        await asyncio.wait_for(rig.tasks[0], timeout=5)
        root.on_typed("maybe")
        root.on_typed("done")
        for _ in range(300):
            if answers:
                break
            await asyncio.sleep(0.01)
        return root

    asyncio.run(scenario())
    assert answers == ["done"]
    text = "\n".join(printed)
    assert "not one of" in text
    assert "answered here, the coordinator isn't connected" in text


def test_typed_line_goes_to_coordinator_when_connected(upgrade):
    """Test while connected, what's typed on a node is a session command."""

    def walkthrough(bridge):
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        rig.nodes["root"].on_typed("yes")
        await rig.until(lambda: rig.coordinator.question is None)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert "root answered yes" in rig.text("coordinator")


def test_question_cleared_after_local_answer(upgrade):
    """Test a question answered while the coordinator was away is cleared there
    when the root reconnects, so it can't be answered twice.
    """
    answers = []

    def walkthrough(bridge):
        answers.append(bridge.ask("ready?", ["yes", "no"]))
        bridge.set_state({"step": "after"})

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        await rig.until(lambda: rig.coordinator.question)
        root = rig.nodes["root"]
        rig.coordinator.nodes["root"]["channel"].close()
        await asyncio.wait_for(rig.tasks[0], timeout=5)
        await rig.until(lambda: "root" not in rig.coordinator.nodes)
        root.on_typed("yes")
        await rig.until(lambda: answers)
        # the coordinator still thinks it's waiting, until the root is back:
        assert rig.coordinator.question is not None
        rig.tasks[0] = asyncio.create_task(root.run("127.0.0.1", rig.port))
        await rig.until(lambda: rig.coordinator.question is None)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert answers == ["yes"]
    assert "answered on root while the coordinator was away" in rig.text("coordinator")


def test_dry_run_file_keeps_only_its_thread(upgrade):
    """Test the dry run file gets the walkthrough thread's prints, not the
    session's, which still show on the screen.
    """
    screen, saved = io.StringIO(), io.StringIO()
    tee = upgrade._Tee(screen, saved)

    tee.write("step output\n")
    thread = threading.Thread(target=lambda: tee.write("session line\n"))
    thread.start()
    thread.join()

    assert saved.getvalue() == "step output\n"
    assert screen.getvalue() == "step output\nsession line\n"


def test_report_command_saves_node_report(upgrade, tmp_path):
    """Test `report root` has the root build its report, which the coordinator
    saves per node, secrets removed, and summarises to who asked.
    """
    store = upgrade.NodeLogStore(str(tmp_path))

    def build():
        return {
            "upgrade_assessment": {
                "warnings": ["a reboot is pending"],
                "compatibility": {"reachable": True},
            },
            "local": {"api_password": "hunter2"},
        }

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            log_store=store,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        root = upgrade.ShareSessionNode(
            "root",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            report_fn=build,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(root.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        result = await upgrade.run_oneshot_command(
            "127.0.0.1",
            port,
            "report root",
            wait=5,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return result

    result = asyncio.run(scenario())
    saved = json.loads((tmp_path / "root_report.json").read_text(encoding="utf-8"))
    assert saved["upgrade_assessment"]["warnings"] == ["a reboot is pending"]
    assert "hunter2" not in json.dumps(saved)
    (reply,) = (r for r in result["replies"] if r["type"] == "reply")
    assert reply["kind"] == "report"
    assert reply["data"]["warnings"] == ["a reboot is pending"]
    assert reply["data"]["saved"].endswith("root_report.json")


def test_report_command_without_report(upgrade, tmp_path):
    """Test a node that can't build a report, like a console, says so."""
    node = upgrade.ShareSessionNode(
        "mac", ["console"], None, output=lambda line: None, **session_options(upgrade)
    )
    assert "error" in node.build_node_report()


def test_choose_session_node_explicit_coordinator(upgrade):
    """Test the Hyper-V host given --coordinator joins it as the share owner,
    without needing broadcast discovery to find it.
    """
    host = FakeHost(powershell={upgrade.PS_HYPERV_HOST: True})

    async def discover(serial):
        raise AssertionError("no broadcast when --coordinator is given")

    node, address = asyncio.run(
        upgrade.choose_session_node(host, "123", discover, "10.0.0.5:52390")
    )

    assert (node, address) == ("share_owner", ("10.0.0.5", 52390))


class ShareSession(FakeSession):
    """A session whose coordinator handed out the backup share."""

    def share_credentials(self):
        return {
            "unc": SHARE_UNC,
            "user": r"HyperV\bfupgrade_share",
            "password": "temp-Pw-123!",
        }


def test_backup_uses_session_share_credentials(upgrade, tmp_path):
    """Test in a session the backup connects with the share account the
    coordinator handed out, without asking for a password.
    """
    host = local_host(upgrade)
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.backup_dir = SHARE_UNC + r"\bigfix"
    ctx.session = ShareSession()
    ctx.getpass_fn = lambda prompt: pytest.fail("no password prompt in a session")

    upgrade.connect_backup_share(ctx, getpass_fn=ctx.getpass_fn)

    assert host.shares == [(SHARE_UNC, r"HyperV\bfupgrade_share", "temp-Pw-123!")]


def test_backup_dir_defaults_to_session_share(upgrade, tmp_path):
    """Test with no --backup-dir, a session's share is used."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.backup_dir = None
    ctx.args.dry_run = True
    ctx.session = ShareSession()

    # joined with the local separator, a backslash on Windows:
    assert ctx.backup_dir().startswith(SHARE_UNC)


def test_dry_run_backup_names_session_account(upgrade, tmp_path, capsys):
    """Test a dry run in a session says which account it would connect as."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.backup_dir = SHARE_UNC
    ctx.args.dry_run = True
    ctx.session = ShareSession()

    ctx.backup_dir()

    out = capsys.readouterr().out
    assert r"would connect as HyperV\bfupgrade_share" in out
    assert "temp-Pw-123!" not in out


def test_dry_run_sql_backup_explains_staging(upgrade, tmp_path, capsys):
    """Test a dry run says the real run tests the target first, and where and
    with how much space it would stage instead.
    """
    host = local_host(upgrade)
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.dry_run = True
    ctx.args.staging_dir = str(tmp_path)

    upgrade.ACTIONS["sql_backup"](ctx)

    out = capsys.readouterr().out
    assert "tests whether SQL Server can write" in out
    assert f"stages in {tmp_path}" in out
    assert "GB free" in out


def test_log_times_have_time_zone(upgrade):
    """Test a node's log times carry their offset, so logs from computers in
    different time zones line up.
    """
    node = upgrade.ShareSessionNode(
        "root", ["root"], None, output=lambda line: None, **session_options(upgrade)
    )
    node.log("INFO", "x")

    when = datetime.datetime.fromisoformat(node.log_buffer[0]["time"])
    # UTC on every node, so time zones don't matter:
    assert when.utcoffset() == datetime.timedelta(0)


def test_dry_run_file_sent_to_coordinator_and_consoles(upgrade, tmp_path):
    """Test a node's file, like its dry run output, is saved by the coordinator
    and every console next to that node's log.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        console, _ = rig.console()
        peer, _ = rig.peer()
        tasks = [
            asyncio.create_task(n.run("127.0.0.1", rig.port)) for n in (console, peer)
        ]
        await rig.coordinator.wait_for_nodes(2, timeout=5)
        # kept until the peer's side of the connection is up, not dropped:
        peer.send_threadsafe(
            {"type": "artifact", "kind": "dryrun", "text": "===== preflight =====\n"},
            keep=True,
        )
        target = tmp_path / "console_logs" / "root_dryrun.txt"
        await rig.until(target.exists)
        await rig.finish(*tasks)

    asyncio.run(scenario())
    for folder in ("coordinator_logs", "console_logs"):
        saved = (tmp_path / folder / "root_dryrun.txt").read_text(encoding="utf-8")
        assert saved == "===== preflight =====\n"


@pytest.mark.parametrize("kind", ["../evil", "a b", "", "x" * 100])
def test_artifact_kind_is_safe(upgrade, tmp_path, kind):
    """Test a node can't pick where its file is written."""
    path = upgrade.save_artifact(str(tmp_path), "root", kind, "text")

    assert path is None or os.path.dirname(path) == str(tmp_path)
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_dry_run_in_session_sends_file(upgrade, tmp_path, monkeypatch):
    """Test a dry run in a session sends its saved output when it finishes."""
    host = local_host(upgrade)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "done": [],
                "reports": {},
                "plan": {"steps": []},
                "local_sql": True,
                "baseline": upgrade.collect_local_info(host),
            }
        )
    )
    args = types.SimpleNamespace(
        state_file=str(state_path),
        step=None,
        dry_run=True,
        backup_dir=str(tmp_path / "share"),
        backup_share_user=None,
        staging_dir=None,
        sql_instance=None,
        dry_run_file=str(tmp_path / "dryrun.txt"),
    )
    monkeypatch.setattr(
        upgrade.besapi.plugin_utilities, "get_besapi_connection", lambda args: None
    )
    sent = []
    session = FakeSession()
    session.send_artifact = lambda kind, text: sent.append((kind, text))

    upgrade.run_walkthrough(args, None, host, {}, session=session)

    ((kind, text),) = sent
    assert kind == "dryrun"
    assert text == (tmp_path / "dryrun.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- script versions


def test_script_fingerprint_ignores_line_endings(upgrade, tmp_path):
    """Test the same code copied with Windows line endings has the same
    fingerprint, and a changed copy doesn't.
    """
    # LF first: a Windows checkout may already have CRLF line endings
    source = open(upgrade.__file__, "rb").read().replace(b"\r\n", b"\n")
    windows = tmp_path / "windows.py"
    windows.write_bytes(source.replace(b"\n", b"\r\n"))
    changed = tmp_path / "changed.py"
    changed.write_bytes(source + b"\n# changed\n")

    own = upgrade.script_fingerprint()
    assert len(own) == 12
    assert upgrade.script_fingerprint(str(windows)) == own
    assert upgrade.script_fingerprint(str(changed)) != own


def test_hello_carries_script_version(upgrade):
    """Test each hello says which script version and fingerprint it runs."""
    hello = upgrade.make_hello("root", ["root"], "123")

    assert hello["script"] == {
        "version": upgrade.__version__,
        "fingerprint": upgrade.script_fingerprint(),
    }


def test_coordinator_warns_about_other_script(upgrade, monkeypatch):
    """Test a node running different code is flagged when it connects and in
    status, but still accepted.
    """
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=output.append,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original = upgrade.make_hello

        def old_hello(node_id, roles, serial, resume_id=None):
            hello = original(node_id, roles, serial, resume_id)
            if node_id == "root":
                hello["script"] = {"version": "0.0.9", "fingerprint": "0123456789ab"}
            return hello

        monkeypatch.setattr(upgrade, "make_hello", old_hello)
        node = upgrade.ShareSessionNode(
            "root",
            ["console"],
            None,
            output=lambda line: None,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        lines = coordinator.status_lines()
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return lines

    lines = asyncio.run(scenario())
    text = "\n".join(output)
    assert "root runs script 0.0.9 (0123456789ab)" in text
    assert "update it" in text
    assert any("0.0.9 0123456789ab, differs" in line for line in lines)


def test_node_warns_about_other_coordinator_script(upgrade, monkeypatch):
    """Test a node is told when the coordinator runs different code."""
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        original = coordinator._hello_for

        def newer(peer):
            hello = original(peer)
            hello["script"] = {"version": "9.9.9", "fingerprint": "ffffffffffff"}
            return hello

        coordinator._hello_for = newer
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=output.append,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()

    asyncio.run(scenario())
    assert any(
        "coordinator runs script 9.9.9 (ffffffffffff)" in line for line in output
    )


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--share-session"], "rejoin without the code"),
        (["--walkthrough"], "progress is saved"),
        ([], "stopped"),
    ],
)
def test_ctrl_c_is_friendly(upgrade, monkeypatch, capsys, tmp_path, argv, expected):
    """Test Ctrl+C prints a short note, not a traceback, and keeps the state."""
    state = tmp_path / "state.json"
    state.write_text('{"session_tokens": {"ab": {"node": "root"}}}')
    before = state.read_text()

    def interrupted(parser, args, host):
        raise KeyboardInterrupt

    monkeypatch.setattr(upgrade, "_main", interrupted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bigfix_root_server_upgrade_win.py", *argv, "--state-file", str(state)],
    )

    assert upgrade.main() == 130

    err = capsys.readouterr().err
    assert expected in err
    assert "Traceback" not in err
    assert state.read_text() == before


def test_console_catch_up_before_live_lines(upgrade, tmp_path):
    """Test a restarted console gets every line it missed, even though the
    coordinator logs a live line as it connects, before the catch up.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        console, saved = rig.console()
        task = asyncio.create_task(console.run("127.0.0.1", rig.port))
        await rig.until(lambda: saved["token"])
        await rig.drop("mac", task)
        for n in range(5):
            rig.coordinator.output(f"missed {n}")
        # a new console process, its store is read from the files:
        again, _ = rig.console(resume=saved["token"])
        task = asyncio.create_task(again.run("127.0.0.1", rig.port))
        await rig.until(
            lambda: "mac (console) resumed"
            in " ".join(rig.messages("console_logs", node="coordinator"))
        )
        await asyncio.sleep(0.3)
        await rig.finish(task)
        return rig

    rig = asyncio.run(scenario())
    kept = rig.messages("console_logs", node="coordinator")
    for n in range(5):
        assert f"missed {n}" in kept
    store = upgrade.NodeLogStore(str(tmp_path / "console_logs"))
    seqs = [e["seq"] for e in store.since("coordinator", 0)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_timestamps_are_utc(upgrade, tmp_path, monkeypatch):
    """Test the coordinator's own lines, backup folders and state times are UTC."""
    store = upgrade.NodeLogStore(str(tmp_path))
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=lambda line: None,
        log_store=store,
        **coordinator_options(upgrade),
    )
    coordinator.output("hello")
    (entry,) = store.since("coordinator", 0)
    assert entry["time"].endswith("+00:00")

    assert upgrade.utc_now().utcoffset() == datetime.timedelta(0)
    folder = upgrade.backup_run_folder(
        "D:/b",
        "root",
        datetime.datetime(2026, 9, 27, 17, 35, 29, tzinfo=datetime.timezone.utc),
    )
    assert folder.endswith("root_20260927_173529Z")


def test_commands_match_node_names_any_case(upgrade, tmp_path):
    """Test `report BIGFIX`, `state bigfix` and `log BIGFIX` find the node BIGFIX,
    whatever case is typed.
    """

    def build():
        return {"upgrade_assessment": {"warnings": []}}

    async def scenario():
        store = upgrade.NodeLogStore(str(tmp_path))
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            log_store=store,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        root = upgrade.ShareSessionNode(
            "BIGFIX",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            report_fn=build,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(root.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        # after its share check, so this is its last line:
        for _ in range(500):
            if coordinator.results.get("BIGFIX"):
                break
            await asyncio.sleep(0.01)
        root.log("INFO", "hello from BIGFIX")
        for _ in range(300):
            if "hello from BIGFIX" in json.dumps(store.since("BIGFIX", 0)):
                break
            await asyncio.sleep(0.01)
        results = {}
        for command in ("report BIGFIX", "state bigfix", "log BIGFIX 1"):
            results[command] = await upgrade.run_oneshot_command(
                "127.0.0.1",
                port,
                command,
                wait=5,
                **session_options(upgrade, password=code_password(upgrade)),
            )
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return results

    results = asyncio.run(scenario())
    for command, result in results.items():
        assert result["replies"], command
        assert not any("isn't connected" in line for line in result["lines"]), command
    assert (tmp_path / "BIGFIX_report.json").exists()
    assert "hello from BIGFIX" in json.dumps(results["log BIGFIX 1"]["replies"])


def test_status_names_share_owner(upgrade):
    """Test the Hyper-V share owner shows as that, not as waiting for a check."""
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=lambda line: None,
        **coordinator_options(upgrade),
    )
    coordinator.nodes["HYPERV"] = {
        "channel": None,
        "roles": ["hyperv", "share_owner"],
        "ip": "192.168.5.39",
        "script": {
            "version": upgrade.__version__,
            "fingerprint": upgrade.script_fingerprint(),
        },
    }

    assert "HYPERV (192.168.5.39): share owner" in "\n".join(coordinator.status_lines())


def test_version_mismatch_halts_until_a_person_continues(upgrade, monkeypatch):
    """Test a node with different code halts the session, and only a person, not
    a one-shot command, can continue.
    """
    saved = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            on_halt=saved.append,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original = upgrade.make_hello

        def old_hello(node_id, roles, serial, resume_id=None):
            hello = original(node_id, roles, serial, resume_id)
            if node_id == "root":
                hello["script"] = {"version": "0.0.9", "fingerprint": "0123456789ab"}
            return hello

        monkeypatch.setattr(upgrade, "make_hello", old_hello)
        node = upgrade.ShareSessionNode(
            "root",
            ["console"],
            None,
            output=lambda line: None,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        halted = dict(coordinator.halted or {})
        monkeypatch.setattr(upgrade, "make_hello", original)
        await upgrade.run_oneshot_command(
            "127.0.0.1",
            port,
            "continue",
            wait=1,
            name="claude",
            **session_options(upgrade, password=code_password(upgrade)),
        )
        still_halted = coordinator.halted is not None
        await coordinator.handle_command("continue", source="coordinator")
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return halted, still_halted, coordinator

    halted, still_halted, coordinator = asyncio.run(scenario())
    assert halted["by"] == "version check"
    assert "root runs 0.0.9 (0123456789ab)" in halted["reason"]
    assert saved[0] == halted
    assert still_halted
    assert coordinator.halted is None


def test_node_holds_walkthrough_for_other_coordinator_code(upgrade):
    """Test a node whose coordinator runs other code holds its walkthrough until
    a person continues, even if the coordinator doesn't halt it.
    """
    progress = []

    def walkthrough(bridge):
        bridge.wait_if_halted()
        progress.append("ran")

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        original = coordinator._hello_for

        def older(peer):
            hello = original(peer)
            hello["script"] = {"version": "0.0.1", "fingerprint": "aaaaaaaaaaaa"}
            return hello

        coordinator._hello_for = older
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = upgrade.ShareSessionNode(
            "root",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            walkthrough=walkthrough,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        # an older coordinator doesn't halt, so this node must hold by itself:
        coordinator.halted = None
        await asyncio.sleep(0.3)
        held = list(progress)
        node.on_typed_local("continue")
        for _ in range(300):
            if progress:
                break
            await asyncio.sleep(0.01)
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return held

    held = asyncio.run(scenario())
    assert held == []
    assert progress == ["ran"]


# ---------------------------------------------------------------- compat level and host checks


def real_path(upgrade, compat):
    return upgrade.find_upgrade_path(
        compat,
        {
            "bigfix": "10.0.7.52",
            "windows": "2012 R2",
            "mssql": "2008 R2",
            "mssql_level": "SP1",
            "db_compat_level": 100,
        },
        {"windows": "2025", "mssql": "2025"},
    )


def test_steps_raise_and_check_compat_level(upgrade, compat):
    """Test the level BigFix 11 needs is raised after the first SQL Server upgrade
    that supports it, and checked before the BigFix upgrade.
    """
    steps = {s.id: s for s in upgrade.build_steps(real_path(upgrade, compat))}

    assert steps["start_services_1"].actions == [
        "raise_compat:120",
        "udf_inlining_off",
        "start_services",
    ]
    assert steps["upgrade_3_bigfix_11_0_6"].actions == ["check_compat:120"]
    # already raised by then, so no more raising, but SQL Server 2025 is new:
    assert steps["start_services_4"].actions == ["udf_inlining_off", "start_services"]
    # no SQL Server upgrade before these:
    assert steps["start_services_2"].actions == ["start_services"]
    assert steps["start_services_3"].actions == ["start_services"]


def test_steps_check_hyperv_host_before_windows_2025(upgrade, compat):
    """Test only the Windows 2025 upgrade checks the Hyper-V host first."""
    steps = {s.id: s for s in upgrade.build_steps(real_path(upgrade, compat))}

    assert steps["upgrade_5_windows_2025"].actions == ["check_hyperv_host:2022"]
    assert steps["upgrade_2_windows_2019"].actions == []


def compat_ctx(upgrade, tmp_path, levels, answer="yes"):
    ran = []

    def handler(server, query):
        ran.append(query)
        if query == upgrade.SQL_COMPAT_LEVELS:
            return [[name, str(level)] for name, level in levels.items()]
        return []

    host = local_host(upgrade, sql_handler=handler)
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.ask = lambda prompt, choices, default=None: answer
    ctx.args.db_compat_level = None
    return ctx, ran


def test_raise_compat_level(upgrade, tmp_path, capsys):
    """Test both databases are raised after the operator agrees."""
    ctx, ran = compat_ctx(upgrade, tmp_path, {"BFEnterprise": 100, "BESReporting": 100})

    upgrade.ACTIONS["raise_compat"](ctx, "120")

    assert "ALTER DATABASE [BFEnterprise] SET COMPATIBILITY_LEVEL = 120" in ran
    assert "ALTER DATABASE [BESReporting] SET COMPATIBILITY_LEVEL = 120" in ran
    assert "BFEnterprise 100" in capsys.readouterr().out


def test_raise_compat_level_declined(upgrade, tmp_path, capsys):
    """Test nothing is changed when the operator says no."""
    ctx, ran = compat_ctx(
        upgrade, tmp_path, {"BFEnterprise": 100, "BESReporting": 100}, answer="no"
    )

    upgrade.ACTIONS["raise_compat"](ctx, "120")

    assert not any(q.startswith("ALTER") for q in ran)
    assert "not changed" in capsys.readouterr().out


def test_raise_compat_level_already_there(upgrade, tmp_path):
    """Test a database already at the level, or above, isn't changed."""
    ctx, ran = compat_ctx(upgrade, tmp_path, {"BFEnterprise": 130, "BESReporting": 120})

    upgrade.ACTIONS["raise_compat"](ctx, "120")

    assert not any(q.startswith("ALTER") for q in ran)


def test_raise_compat_level_override(upgrade, tmp_path):
    """Test --db-compat-level picks another level."""
    ctx, ran = compat_ctx(upgrade, tmp_path, {"BFEnterprise": 100, "BESReporting": 100})
    ctx.args.db_compat_level = 140

    upgrade.ACTIONS["raise_compat"](ctx, "120")

    assert "ALTER DATABASE [BFEnterprise] SET COMPATIBILITY_LEVEL = 140" in ran


def test_raise_compat_level_dry_run(upgrade, tmp_path, capsys):
    """Test a dry run only says what it would run."""
    ctx, ran = compat_ctx(upgrade, tmp_path, {"BFEnterprise": 100, "BESReporting": 100})
    ctx.args.dry_run = True

    upgrade.ACTIONS["raise_compat"](ctx, "120")

    assert not any(q.startswith("ALTER") for q in ran)
    assert (
        "DRY RUN, would run SQL: ALTER DATABASE [BFEnterprise]"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("level", ["abc", "95", "1000", "120; DROP"])
def test_compat_level_validated(upgrade, tmp_path, level):
    """Test only a real compatibility level reaches the SQL."""
    ctx, ran = compat_ctx(upgrade, tmp_path, {"BFEnterprise": 100, "BESReporting": 100})

    with pytest.raises((ValueError, SystemExit)):
        upgrade.ACTIONS["raise_compat"](ctx, level)
    assert not any(q.startswith("ALTER") for q in ran)


def test_check_compat_level(upgrade, tmp_path, capsys):
    """Test the BigFix upgrade stops while a database is below the level."""
    ctx, _ran = compat_ctx(
        upgrade, tmp_path, {"BFEnterprise": 120, "BESReporting": 100}
    )
    with pytest.raises(SystemExit, match="BESReporting"):
        upgrade.ACTIONS["check_compat"](ctx, "120")

    ctx.args.dry_run = True
    upgrade.ACTIONS["check_compat"](ctx, "120")
    assert "WARNING" in capsys.readouterr().out

    ok, _ = compat_ctx(
        upgrade, tmp_path / "ok", {"BFEnterprise": 120, "BESReporting": 140}
    )
    upgrade.ACTIONS["check_compat"](ok, "120")
    assert "OK" in capsys.readouterr().out


def test_check_hyperv_host(upgrade, tmp_path, capsys):
    """Test the Windows 2025 upgrade stops on a 2012 R2 host, goes on on 2022."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.session = FakeSession({"ok": True, "windows": "2012 R2"})
    with pytest.raises(SystemExit, match="2012 R2"):
        upgrade.ACTIONS["check_hyperv_host"](ctx, "2022")
    assert ctx.session.actions[0][:2] == ("hyperv", "host_version")

    ctx.session = FakeSession({"ok": True, "windows": "2025"})
    upgrade.ACTIONS["check_hyperv_host"](ctx, "2022")
    assert "OK" in capsys.readouterr().out


def test_check_hyperv_host_without_node(upgrade, tmp_path):
    """Test with no Hyper-V node the operator confirms, Enter meaning no."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.session = FakeSession({"ok": False, "error": "no hyperv node is connected"})
    ctx.ask = lambda prompt, choices, default=None: default
    with pytest.raises(SystemExit):
        upgrade.ACTIONS["check_hyperv_host"](ctx, "2022")

    ctx.ask = lambda prompt, choices, default=None: "yes"
    upgrade.ACTIONS["check_hyperv_host"](ctx, "2022")


def test_hyperv_node_reports_host_version(upgrade):
    """Test the Hyper-V node answers with its Windows Server version."""
    result = upgrade.run_node_action(
        hyperv_host(upgrade), ["hyperv"], "host_version", {}
    )

    assert result == {"ok": True, "windows": "2019"}


def test_diag_compat(upgrade):
    """Test `diag <node> compat` shows the databases' levels."""
    host = local_host(
        upgrade,
        sql_handler=lambda server, query: [
            ["BFEnterprise", "100"],
            ["BESReporting", "100"],
        ],
    )

    result = upgrade.run_node_diagnostics(host, "compat", sql_server="localhost")

    assert result == {"compat": {"BFEnterprise": 100, "BESReporting": 100}}


def test_db_compat_level_argument(upgrade):
    """Test --db-compat-level parses as a number."""
    assert (
        upgrade.build_parser().parse_args(["--db-compat-level", "140"]).db_compat_level
        == 140
    )


# ---------------------------------------------------------------- dry run checkpoints


def test_dry_run_checkpoint_asks_hyperv_without_running(upgrade, tmp_path, capsys):
    """Test a dry run still asks the Hyper-V node, which only says what it would
    run, so the whole path is tested without touching the VM.
    """
    host = local_host(upgrade)
    host.powershell[upgrade.PS_HOST_IPS] = [
        {"IPAddress": "192.168.5.40", "PrefixLength": 24}
    ]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.dry_run = True
    ctx.session = FakeSession(
        {"ok": True, "dry_run": True, "would_run": "Checkpoint-VM -Name 'BigFixRoot'"}
    )

    upgrade.ACTIONS["checkpoint"](ctx, "snapshot_1")

    ((role, action, params),) = ctx.session.actions
    assert params["dry_run"] is True
    assert "DRY RUN, the Hyper-V node would run: Checkpoint-VM -Name 'BigFixRoot'" in (
        capsys.readouterr().out
    )
    assert "checkpoints" not in ctx.state


def test_hyperv_dry_run_checkpoint_runs_nothing(upgrade):
    """Test the Hyper-V node finds the VM and builds the command, but runs none."""
    host = hyperv_host(upgrade)

    result = upgrade.run_node_action(
        host,
        ["hyperv"],
        "checkpoint",
        {"name": "snapshot_1", "ips": ["192.168.5.40"], "dry_run": True},
    )

    assert result["ok"] is True and result["dry_run"] is True
    assert result["would_run"] == (
        "Checkpoint-VM -Name 'bigfix-root' -SnapshotName 'bigfix-upgrade snapshot_1'"
    )
    assert host.ran == []


# ---------------------------------------------------------------- Hyper-V walkthrough


def hv_ctx(upgrade, tmp_path, host=None, answer=None):
    host = host or hyperv_host(upgrade)
    args = types.SimpleNamespace(
        backup_dir=str(tmp_path / "hv_backup"),
        backup_share_user=None,
        staging_dir=None,
        dry_run=False,
        sql_instance=None,
        vm_name=None,
        heartbeat_wait=0,
    )
    os.makedirs(args.backup_dir)
    info = upgrade.collect_hyperv_info(host, "192.168.5.40")
    state = {
        "done": [],
        "reports": {},
        "hyperv": info,
        "hyperv_plan": {"host_upgrades": ["2025"]},
    }
    return upgrade.WalkthroughContext(
        args,
        host,
        state,
        str(tmp_path / "hv_state.json"),
        ask=answer or (lambda prompt, choices, default=None: default or choices[0]),
        input_fn=lambda prompt: "",
        getpass_fn=lambda prompt: "",
    )


def test_hyperv_steps_with_host_upgrade(upgrade):
    """Test a host that must be upgraded gets backup, shutdown, upgrade and
    validation steps.
    """
    ids = [s.id for s in upgrade.build_hyperv_steps({"host_upgrades": ["2025"]})]

    assert ids == [
        "hv_preflight",
        "hv_config_backup",
        "hv_export",
        "hv_shutdown",
        "hv_upgrade_1_windows_2025",
        "hv_validate",
        "hv_cleanup",
    ]


def test_hyperv_export_step_warns_it_can_take_hours(upgrade):
    """Test the export step says up front that it can take many hours."""
    steps = upgrade.build_hyperv_steps({"host_upgrades": ["2025"]})
    export = next(s for s in steps if s.id == "hv_export")

    assert "many hours" in export.instructions


def test_hyperv_steps_without_host_upgrade(upgrade):
    """Test a host that's new enough only gets its backups."""
    ids = [s.id for s in upgrade.build_hyperv_steps({"host_upgrades": []})]

    assert ids == ["hv_preflight", "hv_config_backup", "hv_export", "hv_cleanup"]


def test_hyperv_config_backup(upgrade, tmp_path):
    """Test the VMs, switches and adapters are saved as JSON."""
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.host.powershell[upgrade.PS_HYPERV_ADAPTERS] = [
        {"VMName": "bigfix-root", "SwitchName": "LAN", "MacAddress": "00155D000001"}
    ]

    upgrade.ACTIONS["hv_config_backup"](ctx)

    (saved,) = list((tmp_path / "hv_backup").rglob("hyperv_config.json"))
    config = json.loads(saved.read_text(encoding="utf-8"))
    assert config["switches"][0]["Name"] == "LAN"
    assert [vm["Name"] for vm in config["vms"]] == ["bigfix-root", "other"]
    assert config["adapters"][0]["MacAddress"] == "00155D000001"


def test_hyperv_export_shuts_down_and_exports(upgrade, tmp_path, monkeypatch):
    """Test the BigFix VM is shut down for a consistent export, then exported,
    and started again when no host upgrade is next.
    """
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.state["hyperv_plan"] = {"host_upgrades": []}
    usage = types.SimpleNamespace(total=4000 * 1024**3, used=0, free=2000 * 1024**3)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    upgrade.ACTIONS["hv_export"](ctx)

    scripts = [cmd[-1] for cmd in ctx.host.ran]
    assert scripts[0] == "Stop-VM -Name 'bigfix-root'"
    assert scripts[1].startswith("Export-VM -Name 'bigfix-root' -Path '")
    assert scripts[2] == "Start-VM -Name 'bigfix-root'"
    assert ctx.state["hyperv_exports"][0]["vm"] == "bigfix-root"


def test_hyperv_export_space_check(upgrade, tmp_path, monkeypatch):
    """Test an export that won't fit is refused unless the operator insists."""
    ctx = hv_ctx(upgrade, tmp_path, answer=lambda prompt, choices, default=None: "no")
    usage = types.SimpleNamespace(total=10, used=0, free=10)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    with pytest.raises(upgrade.BackupError, match="free"):
        upgrade.ACTIONS["hv_export"](ctx)
    assert ctx.host.ran == []


def test_hyperv_export_dry_run(upgrade, tmp_path, capsys):
    """Test a dry run says what it would export, how big, and where."""
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.args.dry_run = True

    upgrade.ACTIONS["hv_export"](ctx)

    out = capsys.readouterr().out
    assert "DRY RUN, would run: powershell.exe" in out
    assert "Export-VM -Name 'bigfix-root'" in out
    assert "300.0 GB" in out
    assert ctx.host.ran == []


def test_hyperv_export_unsafe_path(upgrade, tmp_path, monkeypatch):
    """Test a backup path that could break out of the quotes is refused."""
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.args.backup_dir = str(tmp_path / "it's")
    os.makedirs(ctx.args.backup_dir)
    usage = types.SimpleNamespace(total=4000 * 1024**3, used=0, free=2000 * 1024**3)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    with pytest.raises(ValueError):
        upgrade.ACTIONS["hv_export"](ctx)
    assert not any("Export-VM" in cmd[-1] for cmd in ctx.host.ran)


def test_hyperv_shutdown_choices(upgrade, tmp_path):
    """Test each running VM is shut down or saved as the operator picks, and
    remembered for starting again.
    """
    answers = {"bigfix-root": "save"}
    ctx = hv_ctx(
        upgrade,
        tmp_path,
        answer=lambda prompt, choices, default=None: next(
            (a for vm, a in answers.items() if vm in prompt), default
        ),
    )

    upgrade.ACTIONS["hv_shutdown"](ctx)

    assert [cmd[-1] for cmd in ctx.host.ran] == ["Save-VM -Name 'bigfix-root'"]
    assert ctx.state["hyperv_was_running"] == ["bigfix-root"]


def test_hyperv_validate(upgrade, tmp_path, monkeypatch, capsys):
    """Test after the host upgrade: the role, the switches, the VMs start again
    with a heartbeat, and the root server answers on 52311.
    """
    host = hyperv_host(upgrade, product="Windows Server 2025 Datacenter")
    ctx = hv_ctx(upgrade, tmp_path, host=host)
    ctx.state["hyperv_was_running"] = ["bigfix-root"]
    monkeypatch.setattr(upgrade, "tcp_reachable", lambda ip, port, timeout=5: True)

    upgrade.ACTIONS["hv_validate"](ctx)

    out = capsys.readouterr().out
    assert [cmd[-1] for cmd in host.ran] == ["Start-VM -Name 'bigfix-root'"]
    assert "OK, the host is Windows Server 2025" in out
    assert "OK, switch LAN is back" in out
    assert "OK, bigfix-root heartbeat OkApplicationsHealthy" in out
    assert "OK, the root server 192.168.5.40 answers on 52311" in out
    assert "Update-VMVersion" in out  # only mentioned, never run


def test_hyperv_validate_missing_switch(upgrade, tmp_path, monkeypatch, capsys):
    """Test a switch that didn't come back is reported."""
    host = hyperv_host(upgrade, product="Windows Server 2025 Datacenter")
    ctx = hv_ctx(upgrade, tmp_path, host=host)
    ctx.state["hyperv"]["switches"] = [{"Name": "LAN"}, {"Name": "Backup"}]
    ctx.state["hyperv_was_running"] = []
    monkeypatch.setattr(upgrade, "tcp_reachable", lambda ip, port, timeout=5: False)

    upgrade.ACTIONS["hv_validate"](ctx)

    out = capsys.readouterr().out
    assert "WARNING: switch Backup is missing" in out
    assert "WARNING: the root server 192.168.5.40 doesn't answer on 52311" in out


def test_hyperv_walkthrough_never_updates_vm_version(upgrade, tmp_path, monkeypatch):
    """Test no Hyper-V action ever runs Update-VMVersion, which is one-way."""
    host = hyperv_host(upgrade, product="Windows Server 2025 Datacenter")
    ctx = hv_ctx(upgrade, tmp_path, host=host)
    ctx.state["hyperv_was_running"] = ["bigfix-root"]
    usage = types.SimpleNamespace(total=4000 * 1024**3, used=0, free=2000 * 1024**3)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)
    monkeypatch.setattr(upgrade, "tcp_reachable", lambda ip, port, timeout=5: True)

    for action in (
        "hv_collect",
        "hv_config_backup",
        "hv_export",
        "hv_shutdown",
        "hv_validate",
    ):
        upgrade.ACTIONS[action](ctx)

    assert not any("Update-VMVersion" in " ".join(cmd) for cmd in host.ran)


def test_hyperv_walkthrough_dry_run(upgrade, tmp_path, monkeypatch):
    """Test a dry run on the Hyper-V host walks every step, asks nothing, runs
    nothing, and saves what it printed.
    """
    host = hyperv_host(upgrade, product="Windows Server 2012 R2 Datacenter")
    args = types.SimpleNamespace(
        state_file=str(tmp_path / "hv_state.json"),
        step=None,
        dry_run=True,
        backup_dir=str(tmp_path / "hv_backup"),
        backup_share_user=None,
        staging_dir=None,
        sql_instance=None,
        vm_name=None,
        heartbeat_wait=0,
        target_os=None,
        target_sql=None,
        target_bigfix=None,
        compat_file=None,
        dry_run_file=str(tmp_path / "hv_dryrun.txt"),
    )

    def no_prompts(*args, **kwargs):
        raise AssertionError("a dry run must not prompt")

    monkeypatch.setattr(upgrade, "_ask", no_prompts)
    monkeypatch.setattr("builtins.input", no_prompts)
    monkeypatch.setattr(upgrade, "discover_root_ip", lambda paths: "192.168.5.40")
    monkeypatch.setattr(upgrade, "tcp_reachable", lambda ip, port, timeout=5: True)
    compat = upgrade.load_compat(COMPAT_PATH)

    assert upgrade.run_walkthrough(args, None, host, compat) == 0

    assert host.ran == []
    saved = (tmp_path / "hv_dryrun.txt").read_text(encoding="utf-8")
    for step_id in (
        "hv_preflight",
        "hv_export",
        "hv_upgrade_1_windows_2025",
        "hv_validate",
    ):
        assert f"===== {step_id}:" in saved
    assert "All steps are complete." in saved
    assert not (tmp_path / "hv_state.json").exists()


def test_hyperv_export_leaves_vm_off_before_host_upgrade(
    upgrade, tmp_path, monkeypatch
):
    """Test with a host upgrade next, the VM isn't started again by default."""
    ctx = hv_ctx(upgrade, tmp_path)
    usage = types.SimpleNamespace(total=4000 * 1024**3, used=0, free=2000 * 1024**3)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    upgrade.ACTIONS["hv_export"](ctx)

    assert not any("Start-VM" in cmd[-1] for cmd in ctx.host.ran)


def test_hyperv_default_backup_dir(upgrade):
    """Test the export goes to a local folder on the volume with most free space,
    not the host's own share over the network.
    """
    info = {
        "disks": [
            {"DeviceID": "C:", "FreeSpace": 400 * 1024**3},
            {"DeviceID": "D:", "FreeSpace": 1000 * 1024**3},
        ]
    }

    assert upgrade.hyperv_backup_dir(info) == r"D:\bigfix_hyperv_backup"


def test_hyperv_collect_volume_line(upgrade, tmp_path, capsys):
    """Test each volume is shown once with its free space."""
    ctx = hv_ctx(upgrade, tmp_path)

    upgrade.ACTIONS["hv_collect"](ctx)

    assert "volume D: 900 GB free" in capsys.readouterr().out


# ---------------------------------------------------------------- remote dry runs


def test_dryrun_command_starts_node_dry_run(upgrade):
    """Test `dryrun BIGFIX` from a console starts that node's dry run, and a
    second one while it runs is refused.
    """
    runs = []
    release = threading.Event()

    def dry_run(bridge):
        runs.append("started")
        bridge.set_state({"step": "preflight", "dry_run": True})
        release.wait(5)

    async def scenario():
        rig = WalkRig(upgrade, None)
        rig.server = await rig.coordinator.start("127.0.0.1", 0)
        rig.port = rig.server.sockets[0].getsockname()[1]
        rig.add("BIGFIX", ["root"], client_host(upgrade), dry_run_fn=dry_run)
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.console("mac", ["dryrun bigfix"])
        await rig.until(lambda: runs)
        await rig.nodes["mac"].command_queue.put("dryrun BIGFIX")
        await rig.until(lambda: "already running" in rig.text("mac"))
        release.set()
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert runs == ["started"]
    assert "dry run started on BIGFIX" in rig.text("mac")


def test_dryrun_command_on_node_without_walkthrough(upgrade):
    """Test a node that has no walkthrough, like a peer, says so."""

    async def scenario():
        rig = WalkRig(upgrade, None)
        rig.server = await rig.coordinator.start("127.0.0.1", 0)
        rig.port = rig.server.sockets[0].getsockname()[1]
        rig.add("peer1", ["peer"], client_host(upgrade))
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.console("mac", ["dryrun peer1"])
        await rig.until(lambda: "no walkthrough" in rig.text("mac"))
        await rig.finish()

    asyncio.run(scenario())


def test_oneshot_with_console_token_leaves_console_alone(upgrade):
    """Test a one-shot command using the live console's token joins under its
    own name, needs no code, and the console stays connected.
    """

    async def scenario():
        rig = ResumeRig(upgrade)
        await rig.start()
        console, saved = rig.node(code_password(upgrade), name="mac")
        task = asyncio.create_task(console.run("127.0.0.1", rig.port))
        for _ in range(500):
            if saved["token"]:
                break
            await asyncio.sleep(0.01)
        console_channel = rig.coordinator.nodes["mac"]["channel"]
        result = await upgrade.run_oneshot_command(
            "127.0.0.1",
            rig.port,
            "status",
            wait=5,
            name="mac",
            resume=saved["token"],
            **session_options(upgrade, password=None),
        )
        still_there = (
            rig.coordinator.nodes.get("mac", {}).get("channel") is console_channel
        )
        token_after = saved["token"]
        await rig.finish(task)
        return result, still_there, token_after

    result, still_there, token_after = asyncio.run(scenario())
    assert still_there
    status = [r for r in result["replies"] if r["type"] == "status"][0]["lines"]
    assert any("mac-oneshot" in line for line in status)
    # the console's own token wasn't touched by the one-shot:
    assert token_after is not None


def test_oneshot_token_saved_apart(upgrade):
    """Test a one-shot keeps its own token apart from the console's."""
    state = {"session_resume_console": {"id": "aa"}}

    token = upgrade.oneshot_resume(state)
    upgrade.save_oneshot_token(state, {"id": "bb"})
    upgrade.save_oneshot_token(state, None)

    assert token == {"id": "aa"}
    assert state["session_resume_console"] == {"id": "aa"}
    assert "session_resume_oneshot" not in state


# ---------------------------------------------------------------- local commands


def test_localcmd_runs_here_and_shares_output(upgrade, tmp_path):
    """Test `localcmd` typed at a node runs there, and its output and exit code
    reach the coordinator and consoles as a file.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        console, _ = rig.console()
        peer, _ = rig.peer()
        tasks = [
            asyncio.create_task(n.run("127.0.0.1", rig.port)) for n in (console, peer)
        ]
        await rig.coordinator.wait_for_nodes(2, timeout=5)
        await rig.until(lambda: peer._channel is not None)
        peer.on_typed("localcmd Get-Service BESClient")
        await rig.until(
            lambda: list((tmp_path / "console_logs").glob("root_localcmd_*.txt"))
        )
        await rig.finish(*tasks)
        return peer

    peer = asyncio.run(scenario())
    assert peer.host.captured == [upgrade._powershell("Get-Service BESClient")]
    (saved,) = list((tmp_path / "coordinator_logs").glob("root_localcmd_*.txt"))
    text = saved.read_text(encoding="utf-8")
    assert "command: Get-Service BESClient" in text
    assert "exit code: 0" in text
    assert "Running BESClient" in text
    assert "localcmd exit 0: Get-Service BESClient" in " ".join(
        rig_messages(upgrade, tmp_path / "coordinator_logs", "root")
    )


def rig_messages(upgrade, folder, node):
    return [e["message"] for e in upgrade.NodeLogStore(str(folder)).since(node, 0)]


def test_localcmd_never_runs_from_the_session(upgrade, tmp_path):
    """Test `localcmd` sent over the session, like from a one-shot, runs nothing
    on any node.
    """

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, _ = rig.peer()
        task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.coordinator.handle_command(
            "localcmd root whoami", source="coordinator"
        )
        result = await upgrade.run_oneshot_command(
            "127.0.0.1",
            rig.port,
            "localcmd whoami",
            wait=1,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        await rig.finish(task)
        return peer, result, rig

    peer, result, rig = asyncio.run(scenario())
    assert peer.host.captured == []
    assert any(
        "only works typed at the node" in m
        for m in rig_messages(upgrade, tmp_path / "coordinator_logs", "coordinator")
    )


def test_send_file_from_node(upgrade, tmp_path):
    """Test `send <file>` typed at a node shares that file."""
    source = tmp_path / "setup log.txt"
    source.write_text("SQL setup finished\n", encoding="utf-8")

    async def scenario():
        rig = LogRig(upgrade, tmp_path)
        await rig.start()
        peer, _ = rig.peer()
        task = asyncio.create_task(peer.run("127.0.0.1", rig.port))
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.until(lambda: peer._channel is not None)
        peer.on_typed(f'send "{source}"')
        await rig.until(
            lambda: list((tmp_path / "coordinator_logs").glob("root_sent_*.txt"))
        )
        await rig.finish(task)

    asyncio.run(scenario())
    (saved,) = list((tmp_path / "coordinator_logs").glob("root_sent_*.txt"))
    assert saved.read_text(encoding="utf-8") == "SQL setup finished\n"


def test_send_missing_file(upgrade, capsys):
    """Test sending a file that isn't there says so, and sends nothing."""
    printed = []
    node = upgrade.ShareSessionNode(
        "root", ["root"], None, output=printed.append, **session_options(upgrade)
    )
    node._print = printed.append

    node.on_typed("send C:/nope.txt")

    assert any("can't read" in line for line in printed)
    assert not node._outbox


def test_run_capture_exit_code_and_timeout(upgrade):
    """Test a local command's output and exit code are captured, and one that
    runs too long is stopped.
    """
    host = upgrade.LocalHost()

    code, output = host.run_capture(
        [sys.executable, "-c", "print('hi'); raise SystemExit(3)"]
    )
    assert (code, output.strip()) == (3, "hi")

    code, output = host.run_capture(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5
    )
    assert code is None and "timed out" in output


# ---------------------------------------------------------------- suggestions


def test_suggest_shows_command_on_node_and_runs_nothing(upgrade):
    """Test a one-shot's suggestion appears on the node as text to type, exactly
    as sent, and nothing runs until someone there types localcmd.
    """
    shown = []

    def walkthrough(bridge):
        bridge.ask("ready?", ["yes"])

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        rig.nodes["root"]._print = shown.append
        await upgrade.run_oneshot_command(
            "127.0.0.1",
            rig.port,
            "suggest ROOT Get-WinEvent -LogName Application -MaxEvents 50",
            wait=1,
            name="claude",
            **session_options(upgrade, password=code_password(upgrade)),
        )
        await rig.until(lambda: any("SUGGESTED" in line for line in shown))
        await rig.coordinator.handle_command("answer yes", source="coordinator")
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    text = "\n".join(shown)
    assert (
        "SUGGESTED by claude: Get-WinEvent -LogName Application -MaxEvents 50" in text
    )
    assert (
        "to run it, type: localcmd Get-WinEvent -LogName Application -MaxEvents 50"
        in text
    )
    assert rig.nodes["root"].host.captured == []
    assert rig.nodes["root"].host.ran == [] or all(
        "Get-WinEvent" not in " ".join(cmd) for cmd in rig.nodes["root"].host.ran
    )
    assert "claude suggested for root" in rig.text("coordinator")


def test_suggestion_cleaned_and_capped(upgrade):
    """Test a suggestion can be long, but has no control characters to hide
    anything in the terminal, and is cut at the limit.
    """
    long_command = "Get-Item C:\\\\ ; " * 1500
    assert len(long_command) < upgrade.SUGGEST_MAX
    assert upgrade.clean_suggestion(long_command) == long_command.strip()

    hidden = "Get-Date\x1b[8m; Remove-Item C:\\\\x\x1b[0m\nGet-Date"
    cleaned = upgrade.clean_suggestion(hidden)
    assert "\x1b" not in cleaned and "\n" not in cleaned
    assert "Remove-Item" in cleaned  # shown, not hidden

    assert len(upgrade.clean_suggestion("x" * (upgrade.SUGGEST_MAX + 10))) == (
        upgrade.SUGGEST_MAX
    )
    assert upgrade.SUGGEST_MAX >= 30000


def test_suggest_unknown_node(upgrade):
    """Test a suggestion for a node that isn't connected says so."""
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=output.append,
            **coordinator_options(upgrade),
        )
        await coordinator.handle_command("suggest nope whoami", source="coordinator")

    asyncio.run(scenario())
    assert any("nope isn't connected" in line for line in output)


def test_node_with_only_dry_run_starts_nothing_on_connect(upgrade):
    """Test a node started without --walkthrough, which can still do a remote
    dry run, doesn't try to start a walkthrough when it connects.
    """
    printed = []

    async def scenario():
        rig = WalkRig(upgrade, None)
        rig.server = await rig.coordinator.start("127.0.0.1", 0)
        rig.port = rig.server.sockets[0].getsockname()[1]
        node = rig.add(
            "BIGFIX", ["root"], client_host(upgrade), dry_run_fn=lambda bridge: None
        )
        node._print = printed.append
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await asyncio.sleep(0.3)
        await rig.finish()

    asyncio.run(scenario())
    assert not any("walkthrough failed" in line for line in printed)


def test_oneshot_wait_default_and_option(upgrade):
    """Test a one-shot waits 15 seconds for its reply by default, --wait changes
    it.
    """
    import inspect

    parser = upgrade.build_parser()
    assert parser.parse_args([]).wait == 15
    assert parser.parse_args(["--wait", "40"]).wait == 40
    default = inspect.signature(upgrade.run_oneshot_command).parameters["wait"].default
    assert default == upgrade.DEFAULT_ONESHOT_WAIT == 15


def test_oneshot_waits_for_slow_reply(upgrade, tmp_path):
    """Test a reply that takes longer than the old 5 seconds still arrives."""
    import time as clock

    def slow_report():
        clock.sleep(6)
        return {"upgrade_assessment": {"warnings": ["slow"]}}

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            log_store=upgrade.NodeLogStore(str(tmp_path)),
            **coordinator_options(upgrade, password=code_password(upgrade)),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        root = upgrade.ShareSessionNode(
            "BIGFIX",
            ["root"],
            client_host(upgrade),
            output=lambda line: None,
            report_fn=slow_report,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(root.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        result = await upgrade.run_oneshot_command(
            "127.0.0.1",
            port,
            "report BIGFIX",
            **session_options(upgrade, password=code_password(upgrade)),
        )
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return result

    result = asyncio.run(scenario())
    (reply,) = (r for r in result["replies"] if r["type"] == "reply")
    assert reply["data"]["warnings"] == ["slow"]


def test_hyperv_export_dry_run_free_space_from_parent(upgrade, tmp_path, capsys):
    """Test a dry run, which doesn't create the export folder, still shows free
    space, from the nearest folder that exists.
    """
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.args.dry_run = True
    ctx.args.backup_dir = str(tmp_path / "not" / "made" / "yet")

    upgrade.ACTIONS["hv_export"](ctx)

    out = capsys.readouterr().out
    assert "free space unknown" not in out
    assert "GB free" in out


def test_dry_run_local_backup_doesnt_mention_share(upgrade, tmp_path, capsys):
    """Test a local backup folder doesn't say it would connect to the session's
    share.
    """
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.dry_run = True
    ctx.args.backup_dir = str(tmp_path / "local")
    ctx.session = ShareSession()

    ctx.backup_dir()

    assert "would connect" not in capsys.readouterr().out


# ---------------------------------------------------------------- REST outages


def test_connection_errors_logged_without_traceback(upgrade):
    """Test besapi's connection failures become a one line warning, and other
    errors keep their traceback.
    """
    quiet = upgrade.QuietConnectionErrors()
    try:
        raise ConnectionError("root server down")
    except ConnectionError:
        import sys as _sys

        exc = _sys.exc_info()
    record = logging.LogRecord(
        "root",
        logging.ERROR,
        "/x/besapi/plugin_utilities.py",
        1,
        "----- ERROR: BigFix Connection Failed ------",
        None,
        exc,
    )
    other = logging.LogRecord(
        "root", logging.ERROR, "/x/other.py", 1, "boom", None, exc
    )

    assert quiet.filter(record) and quiet.filter(other)
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert other.levelno == logging.ERROR and other.exc_info is not None


def test_rest_connection_retries_until_root_is_back(upgrade):
    """Test with no connection, REST is tried again, no more than every so often,
    and kept once it works.
    """
    attempts = []
    clock = {"now": 0.0}
    answers = iter([None, None, "conn"])

    def connect(args):
        attempts.append(clock["now"])
        return next(answers)

    rest = upgrade.RestConnection(None, None, connect=connect, now=lambda: clock["now"])

    assert rest.get() is None  # first try
    clock["now"] = 10
    assert rest.get() is None  # too soon, not tried
    clock["now"] = 40
    assert rest.get() is None  # tried, still down
    clock["now"] = 80
    assert rest.get() == "conn"  # back up
    clock["now"] = 81
    assert rest.get() == "conn"
    assert attempts == [0.0, 40, 80]


def test_rest_unavailable_message(upgrade, capsys):
    """Test joining without REST says so plainly."""
    upgrade.explain_rest(None)
    assert "joining the session anyway" in capsys.readouterr().out
    upgrade.explain_rest(object())
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- share setup after connecting


def test_share_owner_asks_through_session_after_connecting(upgrade):
    """Test the Hyper-V host connects first, then its share setup questions can
    be answered from a console, then the share is offered.
    """
    asked = []

    def setup_share(ask):
        asked.append(
            ask("Use the share _tmp_backup (D:\\_tmp_backup)?", ["yes", "no"], "yes")
        )
        return {"unc": SHARE_UNC, "user": r"HyperV\bfupgrade_share", "password": "pw"}

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(
                upgrade, share_unc=None, password=code_password(upgrade)
            ),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        owner = upgrade.ShareSessionNode(
            "HYPERV",
            ["hyperv", "share_owner"],
            hyperv_host(upgrade),
            output=lambda line: None,
            share_setup_fn=setup_share,
            **session_options(upgrade, password=code_password(upgrade)),
        )
        task = asyncio.create_task(owner.run("127.0.0.1", port))
        for _ in range(500):
            if coordinator.question:
                break
            await asyncio.sleep(0.01)
        connected_before_answer = "HYPERV" in coordinator.nodes and not asked
        await coordinator.handle_command("yes", source="coordinator")
        for _ in range(500):
            if coordinator.share_unc:
                break
            await asyncio.sleep(0.01)
        share_unc = coordinator.share_unc
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return connected_before_answer, share_unc

    connected_before_answer, share_unc = asyncio.run(scenario())
    assert connected_before_answer
    assert asked == ["yes"]
    assert share_unc == SHARE_UNC


def test_prepare_share_uses_given_ask(upgrade, monkeypatch):
    """Test the share planning asks with the function it's given."""
    seen = []

    def fake_plan_share(
        host, share_unc, root_ip, host_ips, hostname, ask, share_folder=None
    ):
        seen.append(ask)
        return {"unc": SHARE_UNC, "share_name": "_tmp_backup", "folder": None}

    monkeypatch.setattr(upgrade, "plan_share", fake_plan_share)
    args = types.SimpleNamespace(
        share_unc=SHARE_UNC,
        share_folder=None,
        allow=[],
        backup_share_user=None,
        share_account=None,
        dry_run=False,
        state_file="x.json",
    )

    def marker(prompt, choices, default=None):
        return default

    upgrade._prepare_share(args, hyperv_host(upgrade), {}, None, None, ask=marker)

    assert seen == [marker]


# ---------------------------------------------------------------- showing a halt


def test_status_shows_halt(upgrade):
    """Test status says the session is halted, by whom, and how to go on."""

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            **coordinator_options(upgrade),
        )
        assert not any("HALTED" in line for line in coordinator.status_lines())
        await coordinator.handle_command("halt checking disk", source="coordinator")
        return coordinator.status_lines()

    lines = asyncio.run(scenario())
    assert lines[1] == "HALTED by coordinator: checking disk, type continue to resume"


def test_answer_while_halted_says_it_waits(upgrade):
    """Test an answer while halted says it goes on after continue, on the
    coordinator and on the node that asked.
    """
    answers = []
    shown = []

    def walkthrough(bridge):
        answers.append(bridge.ask("Which share?", ["1", "2", "new"], "1"))

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        rig.nodes["root"]._print = shown.append
        await rig.until(lambda: rig.coordinator.question)
        await rig.coordinator.handle_command("halt version check", source="coordinator")
        await rig.coordinator.handle_command("2", source="coordinator")
        await rig.until(lambda: any("waiting" in line for line in shown))
        waited = list(answers)
        await rig.coordinator.handle_command("continue", source="coordinator")
        await rig.until(lambda: answers)
        await rig.finish()
        return rig, waited

    rig, waited = asyncio.run(scenario())
    assert waited == []
    assert answers == ["2"]
    assert (
        "coordinator answered 2 to root, it goes on after continue, the session is"
        " halted by coordinator" in rig.text("coordinator")
    )
    assert any(
        "answered 2, waiting: the session is halted, it goes on after continue" in line
        for line in shown
    )


def test_unknown_command_lists_every_command(upgrade):
    """Test an unknown command shows the full, current list of commands."""
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=output.append,
            **coordinator_options(upgrade),
        )
        await coordinator.handle_command("frobnicate", source="coordinator")

    asyncio.run(scenario())
    assert any(upgrade.SESSION_COMMANDS in line for line in output)


def test_client_data_folder_exists_before_reg_export(upgrade, tmp_path):
    """Test the client_data folder is made before reg.exe exports into it, since
    reg.exe can't create folders.
    """
    ctx, _server = hcl_ctx(upgrade, tmp_path)
    seen = []

    def check_folder(cmd):
        if cmd[:2] == ["reg.exe", "export"]:
            seen.append(os.path.isdir(os.path.dirname(cmd[3])))
        return ""

    ctx.host.run_handler = check_folder

    upgrade.ACTIONS["client_data"](ctx)

    assert seen == [True]


def test_connection_that_raises_joins_anyway(upgrade, monkeypatch, caplog):
    """Test a connection attempt that raises, like besapi's config file path on a
    timeout, gives no connection and a one line warning, not a crash.
    """

    def raising(args=None):
        raise TimeoutError("Connection to 192.168.5.40 timed out")

    monkeypatch.setattr(
        upgrade.besapi.plugin_utilities, "get_besapi_connection", raising
    )
    upgrade.make_connection_safe()

    with caplog.at_level(logging.WARNING):
        assert upgrade.besapi.plugin_utilities.get_besapi_connection(None) is None

    (record,) = (r for r in caplog.records if "timed out" in r.getMessage())
    assert record.levelno == logging.WARNING and record.exc_info is None
    # safe to call twice, it doesn't wrap the wrapper:
    upgrade.make_connection_safe()
    assert upgrade.besapi.plugin_utilities.get_besapi_connection(None) is None


def test_masthead_serial_saved_for_rest_outages(upgrade, tmp_path):
    """Test the serial found once is saved, and used when REST and the local
    masthead are both unavailable, with --masthead-serial still first.
    """
    state = {}
    rest = FakeConnection(
        {upgrade.MASTHEAD_RELEVANCE: [[152178487, "bigfix.example.com"]]}
    )

    assert upgrade.session_masthead_serial(None, rest, [], state) == ("152178487", True)
    assert state["masthead_serial"] == "152178487"
    # REST down, no local client:
    assert upgrade.session_masthead_serial(None, None, [], state) == ("152178487", True)
    assert upgrade.session_masthead_serial("999", None, [], state) == ("999", True)
    with pytest.raises(SystemExit, match="masthead serial"):
        upgrade.session_masthead_serial(None, None, [], {})


def test_saved_serial_beats_local_masthead(upgrade, tmp_path):
    """Test with REST down, a serial saved from REST or the coordinator beats this
    computer's client masthead, which can be for another deployment, and the.

    local one alone is only a guess, not saved.
    """
    masthead = tmp_path / "actionsite.afxm"
    masthead.write_text("X-Fixlet-Site-Serial-Number: 152322981\r\n", encoding="utf-8")
    saved = {"masthead_serial": "152178487"}
    assert upgrade.session_masthead_serial(None, None, [str(masthead)], saved) == (
        "152178487",
        True,
    )

    fresh = {}
    assert upgrade.session_masthead_serial(None, None, [str(masthead)], fresh) == (
        "152322981",
        False,
    )
    assert "masthead_serial" not in fresh


def serial_password(upgrade, serial, code=CODE):
    return upgrade.derive_password(None, serial, code)


async def serial_rig(upgrade, coordinator_serial="152178487"):
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=lambda line: None,
        password=serial_password(upgrade, coordinator_serial),
        serial=coordinator_serial,
        share_unc=SHARE_UNC,
        allow=[],
    )
    server = await coordinator.start("127.0.0.1", 0)
    return coordinator, server, server.sockets[0].getsockname()[1]


def test_node_takes_coordinator_serial_when_only_guessing(upgrade):
    """Test a node whose serial came only from its own client masthead, for
    another deployment, takes the coordinator's serial and joins.
    """
    saved = []
    printed = []

    async def scenario():
        coordinator, server, port = await serial_rig(upgrade)
        node = upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=printed.append,
            password=serial_password(upgrade, "152322981"),
            serial="152322981",
            serial_confirmed=False,
            password_for=lambda serial: serial_password(upgrade, serial),
            on_serial=saved.append,
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        await coordinator.handle_command("end", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()
        return node

    node = asyncio.run(scenario())
    assert node.serial == "152178487"
    assert saved == ["152178487"]
    assert any(
        "using the coordinator's masthead serial 152178487" in line for line in printed
    )


def test_node_with_confirmed_serial_never_switches(upgrade):
    """Test a node whose serial is confirmed refuses another deployment's
    coordinator, instead of joining it.
    """

    async def scenario():
        coordinator, server, port = await serial_rig(upgrade)
        node = upgrade.ShareSessionNode(
            "mac",
            ["console"],
            None,
            output=lambda line: None,
            password=serial_password(upgrade, "152322981"),
            serial="152322981",
            password_for=lambda serial: serial_password(upgrade, serial),
        )
        try:
            with pytest.raises(
                upgrade.HandshakeError, match="different BigFix deployment"
            ):
                await asyncio.wait_for(node.run("127.0.0.1", port), timeout=10)
        finally:
            server.close()
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.nodes == {}


def test_pairing_code_prompt_without_input(upgrade, monkeypatch):
    """Test no input at the pairing code prompt stops with a message, not a
    traceback.
    """

    def no_input(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    with pytest.raises(SystemExit, match="pairing code"):
        upgrade.ask_pairing_code()


# ---------------------------------------------------------------- waiting for REST


def validate_ctx(upgrade, tmp_path, monkeypatch, answers, rest_wait=600):
    """A walkthrough whose REST answers as given, one per attempt."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.rest_wait = rest_wait
    attempts = iter(answers)
    sleeps = []
    monkeypatch.setattr(
        upgrade.besapi.plugin_utilities, "get_besapi_connection", lambda args: None
    )
    monkeypatch.setattr(upgrade, "collect_rest_info", lambda conn: next(attempts))
    monkeypatch.setattr(upgrade.time, "sleep", sleeps.append)
    return ctx, sleeps


def test_validate_waits_for_rest(upgrade, tmp_path, monkeypatch, capsys):
    """Test validate tries REST every 30 seconds until the root server answers."""
    down = {"skipped": "no BigFix REST connection"}
    # serverinfo answers while the root server still waits for its database:
    waiting = {
        "serverinfo": {"version": "11.0.6.1"},
        "masthead": {"error": "JSONDecodeError: Expecting value"},
        "root_server": {"error": "JSONDecodeError: Expecting value"},
    }
    up = {
        "serverinfo": {"version": "11.0.6.1"},
        "masthead": {"name": "x"},
        "root_server": {"properties": {}},
    }
    ctx, sleeps = validate_ctx(upgrade, tmp_path, monkeypatch, [down, waiting, up])

    upgrade.ACTIONS["validate"](ctx)

    out = capsys.readouterr().out
    assert sleeps == [30, 30]
    assert "waiting for BigFix REST, 30s so far" in out
    assert "OK, BigFix REST answers" in out
    assert "not answering" not in out


def test_validate_rest_wait_times_out(upgrade, tmp_path, monkeypatch, capsys):
    """Test validate gives up after --rest-wait, with a clear warning."""
    down = {"skipped": "no BigFix REST connection"}
    ctx, sleeps = validate_ctx(
        upgrade, tmp_path, monkeypatch, [down] * 10, rest_wait=60
    )

    upgrade.ACTIONS["validate"](ctx)

    assert sleeps == [30, 30]
    assert "WARNING: BigFix REST didn't answer within 60s" in capsys.readouterr().out


def test_validate_dry_run_doesnt_wait(upgrade, tmp_path, monkeypatch):
    """Test a dry run checks REST once, without waiting."""
    down = {"skipped": "no BigFix REST connection"}
    ctx, sleeps = validate_ctx(upgrade, tmp_path, monkeypatch, [down] * 3)
    ctx.args.dry_run = True

    upgrade.ACTIONS["validate"](ctx)

    assert sleeps == []


def test_rest_wait_argument(upgrade):
    """Test --rest-wait defaults to 10 minutes."""
    parser = upgrade.build_parser()
    assert parser.parse_args([]).rest_wait == 600
    assert parser.parse_args(["--rest-wait", "120"]).rest_wait == 120


# ---------------------------------------------------------------- stale share connections


def stale_connection_host(upgrade, existing):
    """A node with an old connection to the share, from its previous password."""
    host = client_host(upgrade)
    root = r"\\192.168.5.39\_tmp_backup"
    host.share_errors[root] = win_error(1219)
    host.run_handler = lambda cmd: (
        "\n".join(
            f"OK           {unc}    Microsoft Windows Network" for unc in existing
        )
        if cmd == ["net.exe", "use"]
        else ""
    )

    def disconnect(share_root):
        host.disconnected.append(share_root)
        host.share_errors.pop(share_root, None)

    host.disconnect_share = disconnect
    return host, root


def test_stale_session_share_connection_replaced(upgrade):
    """Test an old connection to the session's own share, made with its previous
    password, is replaced, so the new password connects.
    """
    host, root = stale_connection_host(upgrade, [r"\\192.168.5.39\_tmp_backup"])

    findings = upgrade.diagnose_share_access(
        host, SHARE_UNC, r"HyperV\bfupgrade_share", "new"
    )

    connect = next(f for f in findings if f["check"] == "connect")
    assert connect["ok"] is True
    # the old one first, the share check disconnects its own at the end too:
    assert host.disconnected[0] == root
    assert "replaced" in connect["detail"]


def test_other_connection_to_server_left_alone(upgrade):
    """Test a connection to another share on that server, which may be the
    person's own, isn't removed, and 1219 is still reported.
    """
    host, _root = stale_connection_host(upgrade, [r"\\192.168.5.39\other"])

    findings = upgrade.diagnose_share_access(
        host, SHARE_UNC, r"HyperV\bfupgrade_share", "new"
    )

    connect = next(f for f in findings if f["check"] == "connect")
    assert connect["ok"] is False and "1219" in connect["detail"]
    assert host.disconnected == []


def test_backup_replaces_stale_session_share_connection(upgrade, tmp_path):
    """Test the walkthrough's backup connection replaces a stale one too."""
    host, root = stale_connection_host(upgrade, [r"\\192.168.5.39\_tmp_backup"])
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.backup_dir = SHARE_UNC + r"\bigfix"
    ctx.session = ShareSession()

    upgrade.connect_backup_share(ctx)

    assert host.disconnected == [root]
    assert host.shares[-1] == (root, r"HyperV\bfupgrade_share", "temp-Pw-123!")


# ---------------------------------------------------------------- walkthrough command


def test_walkthrough_typed_at_node_starts_it(upgrade):
    """Test `walkthrough` typed at a node started without --walkthrough runs its
    real walkthrough, once at a time.
    """
    runs = []
    release = threading.Event()
    printed = []

    def real_walkthrough(bridge):
        runs.append("real")
        release.wait(5)

    async def scenario():
        rig = WalkRig(upgrade, None)
        rig.server = await rig.coordinator.start("127.0.0.1", 0)
        rig.port = rig.server.sockets[0].getsockname()[1]
        node = rig.add(
            "BIGFIX", ["root"], client_host(upgrade), walkthrough_fn=real_walkthrough
        )
        node._print = printed.append
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await asyncio.sleep(0.2)
        assert runs == []  # not on connect
        node.on_typed("walkthrough")
        await rig.until(lambda: runs)
        node.on_typed("walkthrough")
        release.set()
        await rig.finish()

    asyncio.run(scenario())
    assert runs == ["real"]
    assert any("already running" in line for line in printed)


def test_walkthrough_never_starts_from_the_session(upgrade):
    """Test `walkthrough <node>` sent over the session, like from a one-shot,
    starts nothing and says to type it at the node.
    """
    runs = []

    async def scenario():
        rig = WalkRig(upgrade, None)
        rig.server = await rig.coordinator.start("127.0.0.1", 0)
        rig.port = rig.server.sockets[0].getsockname()[1]
        rig.add("BIGFIX", ["root"], client_host(upgrade), walkthrough_fn=runs.append)
        await rig.coordinator.wait_for_nodes(1, timeout=5)
        await rig.coordinator.handle_command("walkthrough BIGFIX", source="coordinator")
        await asyncio.sleep(0.2)
        await rig.finish()
        return rig

    rig = asyncio.run(scenario())
    assert runs == []
    assert "walkthrough only starts typed at the node" in rig.text("coordinator")


# ---------------------------------------------------------------- several questions at once


def test_questions_from_two_nodes_both_kept(upgrade):
    """Test questions from two nodes at once are both kept, shown in status, a
    bare answer asks which, and `answer <node> <choice>` picks.
    """
    answers = {}

    def ask_as(name):
        def walkthrough(bridge):
            answers[name] = bridge.ask(f"{name} ready?", ["yes", "no"])

        return walkthrough

    async def scenario():
        rig = WalkRig(upgrade, ask_as("root"))
        await rig.start()
        rig.add(
            "HYPERV", ["hyperv"], hyperv_host(upgrade), walkthrough=ask_as("HYPERV")
        )
        await rig.until(lambda: len(rig.coordinator.questions) == 2)
        status = rig.coordinator.status_lines()
        await rig.coordinator.handle_command("yes", source="coordinator")
        await asyncio.sleep(0.2)
        unanswered = dict(answers)
        await rig.coordinator.handle_command("answer hyperv no", source="coordinator")
        await rig.until(lambda: "HYPERV" in answers)
        await rig.coordinator.handle_command("yes", source="coordinator")
        await rig.until(lambda: "root" in answers)
        await rig.finish()
        return rig, status, unanswered

    rig, status, unanswered = asyncio.run(scenario())
    assert unanswered == {}
    assert answers == {"HYPERV": "no", "root": "yes"}
    assert any("QUESTION from root: root ready?" in line for line in status)
    assert any("QUESTION from HYPERV: HYPERV ready?" in line for line in status)
    assert "answer <node> <choice>" in rig.text("coordinator")


def test_backup_waits_for_share_owner_credentials(upgrade, tmp_path):
    """Test a session share with no account yet stops the backup with a reason,
    instead of trying the share as this computer's own user.
    """

    class NoAccountYet(FakeSession):
        def share_credentials(self):
            return {"unc": SHARE_UNC, "user": None, "password": None}

    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.backup_dir = None
    ctx.session = NoAccountYet()

    with pytest.raises(SystemExit, match="hasn't handed out the share account"):
        ctx.backup_dir()
    assert ctx.host.shares == []


def test_dry_run_notes_missing_share_account(upgrade, tmp_path, capsys):
    """Test a dry run only notes the share owner hasn't handed out the account."""

    class NoAccountYet(FakeSession):
        def share_credentials(self):
            return {"unc": SHARE_UNC, "user": None, "password": None}

    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.args.backup_dir = None
    ctx.args.dry_run = True
    ctx.session = NoAccountYet()

    ctx.backup_dir()

    assert "hasn't handed out the share account" in capsys.readouterr().out


# ---------------------------------------------------------------- streamed output


def test_run_stream_gives_lines_as_they_come(upgrade):
    """Test each output line arrives while the command still runs."""
    host = upgrade.LocalHost()
    seen = []
    script = "import time; print('first', flush=True); time.sleep(1); print('second'); raise SystemExit(3)"
    started = time.monotonic()

    code, output = host.run_stream(
        [sys.executable, "-u", "-c", script],
        lambda line: seen.append((time.monotonic() - started, line)),
    )

    assert code == 3
    assert output.splitlines() == ["first", "second"]
    assert [line for _when, line in seen] == ["first", "second"]
    # the first line came about a second before the second:
    assert seen[1][0] - seen[0][0] > 0.5


def test_run_stream_timeout(upgrade):
    """Test a command that runs too long is stopped."""
    host = upgrade.LocalHost()

    code, output = host.run_stream(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        lambda line: None,
        timeout=0.5,
    )

    assert code is None and "timed out" in output


class StreamingHost(FakeHost):
    """A fake host that streams its commands' output."""

    def __init__(self, lines, code=0, **options):
        super().__init__(**options)
        self.stream_lines, self.stream_code = lines, code
        self.streamed = []

    def run_stream(self, cmd, on_line, timeout=None):
        self.streamed.append(cmd)
        for line in self.stream_lines:
            on_line(line)
        return self.stream_code, "\n".join(self.stream_lines) + "\n"


def test_walkthrough_command_output_streams(upgrade, tmp_path, capsys):
    """Test the walkthrough prints a command's lines as they come, and a failing
    command still fails the action.
    """
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.host = StreamingHost(["10 percent processed.", "100 percent processed."])

    ctx.execute(["powershell.exe", "Export-VM"])

    assert "10 percent processed." in capsys.readouterr().out
    ctx.host = StreamingHost(["Access is denied."], code=1)
    with pytest.raises(subprocess.CalledProcessError):
        ctx.execute(["reg.exe", "export"])


def test_localcmd_output_streams_to_session(upgrade):
    """Test localcmd sends each line to the session as it comes."""
    printed = []
    host = StreamingHost(["line one", "line two"])
    node = upgrade.ShareSessionNode(
        "root", ["root"], host, output=printed.append, **session_options(upgrade)
    )
    node._print = printed.append

    node._local_command("Get-Something")
    for _ in range(300):
        if any("exit code" in line for line in printed):
            break
        time.sleep(0.01)

    logged = [entry["message"] for entry in node.log_buffer]
    assert "[localcmd] line one" in logged and "[localcmd] line two" in logged
    assert logged.index("[localcmd] line one") < logged.index(
        "localcmd exit 0: Get-Something"
    )


# ---------------------------------------------------------------- progress


def test_progress_poller_reports_while_running(upgrade, monkeypatch):
    """Test the poller reports every interval while the body runs, then stops."""
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0.05)
    reports = []
    three = threading.Event()

    def report():
        reports.append(time.monotonic())
        if len(reports) >= 3:
            three.set()

    # waits for the reports, not a fixed time, so a slow CI runner still passes:
    with upgrade.ProgressPoller(report):
        three.wait(10)
    count = len(reports)
    time.sleep(0.2)

    assert count >= 3
    assert len(reports) == count  # stopped


def test_progress_interval_is_a_minute(upgrade):
    """Test progress is reported every 60 seconds."""
    assert upgrade.PROGRESS_INTERVAL == 60


def test_sql_backup_reports_progress(upgrade, tmp_path, monkeypatch, capsys):
    """Test a backup shows SQL Server's percent done while it runs."""
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0.05)
    host = local_host(upgrade)
    host.sql[upgrade.SQL_PROGRESS] = [["BACKUP DATABASE", "42.5", "6"]]

    def backing_up(server, query, on_line):
        # runs until progress was polled, however slow the runner:
        for _ in range(1000):
            if upgrade.SQL_PROGRESS in host.sql_ran:
                break
            time.sleep(0.01)
        time.sleep(0.05)

    host.sqlcmd_stream = backing_up
    ctx = walkthrough_ctx(upgrade, tmp_path, host)

    ctx.sql("BACKUP DATABASE [BFEnterprise] TO DISK = N'x.bak'")

    assert "BACKUP DATABASE: 42.5% done, about 6 min left" in capsys.readouterr().out


def test_other_sql_has_no_progress(upgrade, tmp_path, monkeypatch):
    """Test a quick statement doesn't poll for progress."""
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0.01)
    host = local_host(upgrade)
    host.sqlcmd_stream = lambda server, query, on_line: time.sleep(0.1)
    ctx = walkthrough_ctx(upgrade, tmp_path, host)

    ctx.sql("ALTER DATABASE [BFEnterprise] SET COMPATIBILITY_LEVEL = 120")

    assert upgrade.SQL_PROGRESS not in host.sql_ran


def test_folder_copy_reports_progress(upgrade, tmp_path, monkeypatch, capsys):
    """Test copying a folder shows files and GB copied so far."""
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0)
    source = tmp_path / "wwwrootbes"
    for n in range(3):
        (source / f"d{n}").mkdir(parents=True)
        (source / f"d{n}" / "f.txt").write_text("x" * 100)

    upgrade._copy_item(
        str(source), str(tmp_path / "copy"), label="wwwrootbes", total=(3, 300)
    )

    out = capsys.readouterr().out
    assert "wwwrootbes: 3 of 3 files" in out
    assert (tmp_path / "copy" / "d2" / "f.txt").exists()


def test_staged_copy_reports_progress(upgrade, tmp_path, monkeypatch, capsys):
    """Test copying a staged backup to the share shows GB copied so far."""
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(upgrade, "COPY_CHUNK", 1024)
    source = tmp_path / "BFEnterprise.bak"
    source.write_bytes(os.urandom(5000))
    dest = tmp_path / "share"
    dest.mkdir()

    record = upgrade.copy_verified(str(source), str(dest))

    assert "BFEnterprise.bak:" in capsys.readouterr().out
    assert os.path.getsize(record["file"]) == 5000
    assert not source.exists()


def test_hyperv_export_reports_progress(upgrade, tmp_path, monkeypatch, capsys):
    """Test the export shows how much of the VM's disks is written so far."""
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.host.powershell[upgrade.ps_export_status("bigfix-root")] = {"Jobs": []}
    _slow_export(upgrade, ctx, monkeypatch)

    upgrade.ACTIONS["hv_export"](ctx)

    assert "export of bigfix-root:" in capsys.readouterr().out


def _slow_export(upgrade, ctx, monkeypatch):
    monkeypatch.setattr(upgrade, "PROGRESS_INTERVAL", 0.05)
    ctx.state["hyperv_plan"] = {"host_upgrades": []}
    usage = types.SimpleNamespace(total=4000 * 1024**3, used=0, free=2000 * 1024**3)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda path: usage)

    checked = threading.Event()
    status_check = ctx.host.powershell_json

    def checking(script):
        try:
            return status_check(script)
        finally:
            checked.set()

    ctx.host.powershell_json = checking

    def exporting(cmd):
        if "Export-VM" in cmd[-1]:
            # Hyper-V sizes the exported disks in full before copying:
            folder = os.path.join(ctx.backup_dir(), "bigfix-root")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "disk.vhdx"), "wb") as disk:
                disk.write(b"x" * 1000)
            # runs until the progress was checked, however slow the runner:
            checked.wait(10)
            time.sleep(0.05)
        return ""

    ctx.host.run_handler = exporting


def test_hyperv_export_progress_from_hyperv_job(upgrade, tmp_path, monkeypatch, capsys):
    """Test the export's progress is Hyper-V's own job percentage, not the size
    of the exported files, which are full size from the start.
    """
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.host.powershell[upgrade.ps_export_status("bigfix-root")] = {
        "Jobs": [{"Description": "Exporting virtual machine", "PercentComplete": 37}],
        "Status": ["Operating normally"],
    }
    _slow_export(upgrade, ctx, monkeypatch)

    upgrade.ACTIONS["hv_export"](ctx)

    out = capsys.readouterr().out
    assert "export of bigfix-root: 37% done" in out
    assert "(100%)" not in out


def test_hyperv_export_progress_from_vm_status(upgrade, tmp_path, monkeypatch, capsys):
    """Test that when the job still says 0%, as on 2012 R2, the percentage is
    taken from the VM's status, what Hyper-V Manager shows as Exporting (2%).
    """
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.host.powershell[upgrade.ps_export_status("bigfix-root")] = {
        "Jobs": {"Description": "Exporting Virtual Machine", "PercentComplete": 0},
        "Status": ["Operating normally", "Exporting (2%)"],
    }
    _slow_export(upgrade, ctx, monkeypatch)

    upgrade.ACTIONS["hv_export"](ctx)

    out = capsys.readouterr().out
    assert "export of bigfix-root: 2% done" in out
    assert "0% done" not in out


def test_export_status_query_quotes_the_name(upgrade):
    """Test the VM name is quoted in the status query, and a name that could
    run code is refused.
    """
    assert "Where-Object ElementName -eq 'BigFixRoot'" in upgrade.ps_export_status(
        "BigFixRoot"
    )
    with pytest.raises(ValueError):
        upgrade.ps_export_status("x'; Remove-Item C:\\ -Recurse; '")


def test_hyperv_export_progress_without_job(upgrade, tmp_path, monkeypatch, capsys):
    """Test that without a Hyper-V job to read, the export only says it's still
    running and for how long, not a misleading size.
    """
    ctx = hv_ctx(upgrade, tmp_path)
    ctx.host.powershell[upgrade.ps_export_status("bigfix-root")] = OSError("no CIM")
    _slow_export(upgrade, ctx, monkeypatch)

    upgrade.ACTIONS["hv_export"](ctx)

    out = capsys.readouterr().out
    assert "export of bigfix-root: still running," in out
    assert "(100%)" not in out


def test_backup_dir_new_folder_when_share_changes(upgrade, tmp_path):
    """Test a saved backup folder on another share isn't reused: a walkthrough
    resumed with a new backup share starts a new folder there.
    """
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.state["backup_run_dir"] = "\\\\192.168.5.39\\_tmp_backup\\bigfix_old"

    run_dir = ctx.backup_dir()

    assert run_dir.startswith(str(tmp_path / "share"))
    assert ctx.state["backup_run_dir"] == run_dir


def test_backup_dir_kept_on_same_share(upgrade, tmp_path):
    """Test a resumed walkthrough keeps the folder it saved on the same share."""
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    saved = os.path.join(str(tmp_path / "share"), "bigfix_saved")
    ctx.state["backup_run_dir"] = saved

    assert ctx.backup_dir() == saved


def test_redoing_backup_step_starts_a_new_folder(upgrade):
    """Test --step at or before the backup forgets the saved backup folder, so
    the new backup doesn't mix with or overwrite the old one, and a later.

    step keeps it.
    """
    ids = ["preflight", "stop_services_0", "backup", "snapshot_1"]
    state = {"done": list(ids), "backup_run_dir": "\\\\h\\s\\bigfix_old"}

    upgrade.restart_from_step(state, ids, "backup")

    assert state["done"] == ["preflight", "stop_services_0"]
    assert "backup_run_dir" not in state

    state = {"done": list(ids), "backup_run_dir": "\\\\h\\s\\bigfix_old"}
    upgrade.restart_from_step(state, ids, "snapshot_1")

    assert state["backup_run_dir"] == "\\\\h\\s\\bigfix_old"


def _finished_backup(upgrade, tmp_path):
    """A backup folder and state as the backup step leaves them."""
    run_dir = tmp_path / "share" / "bigfix_20261001_033500Z"
    (run_dir / "key_files").mkdir(parents=True)
    (run_dir / "client_data").mkdir()
    files = run_dir / "server_files" / "BESReportsData"
    files.mkdir(parents=True)
    (files / "a.dat").write_bytes(b"a" * 10)
    (files / "b.dat").write_bytes(b"b" * 20)
    bak = run_dir / "BFEnterprise_20261001.bak"
    bak.write_bytes(b"backup" * 100)
    (run_dir / "BESReporting_20261001.bak").write_bytes(b"report" * 10)
    (run_dir / "key_files" / "masthead.afxm").write_bytes(b"masthead")
    (run_dir / "key_files" / "license.pvk").write_bytes(b"pvk")
    (run_dir / "bigfix_registry.reg").write_text("registry")
    (run_dir / "client_data" / "ComputerID.txt").write_text("12345")
    (run_dir / "db_info.json").write_text('{"DBINFO": []}')
    (run_dir / "RESTORE_NOTES.txt").write_text("notes")
    state = {
        "backup_run_dir": str(run_dir),
        "backups": [
            # from an earlier backup on another share, not this one's:
            {
                "file": "\\\\old\\share\\BFEnterprise_old.bak",
                "database": "BFEnterprise",
            },
            {
                "file": str(bak),
                "sha256": upgrade.sha256_file(str(bak)),
                "database": "BFEnterprise",
            },
            {
                "file": str(run_dir / "BESReporting_20261001.bak"),
                "database": "BESReporting",
            },
        ],
        "server_files": {
            "BESReportsData": {"files": 2, "bytes": 30, "copied_to": str(files)},
            "wwwrootbes": {"skipped": True, "files": 9, "bytes": 99},
        },
        "masthead_copy": {"to": str(run_dir / "key_files" / "masthead.afxm")},
        "key_file_copies": {
            str(run_dir / "key_files" / "license.pvk"): "C:\\license.pvk"
        },
    }
    return run_dir, state


def _check(upgrade, run_dir, state):
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    return upgrade.check_backup_folder(str(run_dir))


def test_backup_manifest_is_relative_and_this_runs_only(upgrade, tmp_path):
    """Test the manifest in the backup folder only names this run's files, by
    paths inside the folder, so the share's host can check it locally.
    """
    run_dir, state = _finished_backup(upgrade, tmp_path)

    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    text = (run_dir / upgrade.BACKUP_MANIFEST).read_text()
    manifest = json.loads(text)

    assert "BFEnterprise_old" not in text
    assert str(tmp_path) not in text
    assert manifest["databases"][0]["file"] == "BFEnterprise_20261001.bak"
    assert manifest["server_files"]["BESReportsData"]["path"] == (
        "server_files/BESReportsData"
    )


def test_backup_check_passes_a_complete_backup(upgrade, tmp_path):
    """Test a complete backup has no problems."""
    run_dir, state = _finished_backup(upgrade, tmp_path)

    results = _check(upgrade, run_dir, state)

    assert [r for r in results if r[0] == "FAIL"] == []
    text = "\n".join(message for _, message in results)
    assert "BFEnterprise" in text and "SHA-256 matches" in text
    assert "wwwrootbes" in text  # said to be left out, not a problem


def test_backup_check_finds_a_changed_bak(upgrade, tmp_path):
    """Test a .bak whose copy no longer matches its hash fails."""
    run_dir, state = _finished_backup(upgrade, tmp_path)
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    (run_dir / "BFEnterprise_20261001.bak").write_bytes(b"corrupt")

    results = upgrade.check_backup_folder(str(run_dir))

    assert any(
        level == "FAIL" and "BFEnterprise" in message for level, message in results
    )


def test_backup_check_finds_missing_pieces(upgrade, tmp_path):
    """Test a missing database backup, missing server files, a missing
    masthead and an unreadable db_info.json are each reported.
    """
    run_dir, state = _finished_backup(upgrade, tmp_path)
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    (run_dir / "BESReporting_20261001.bak").unlink()
    (run_dir / "server_files" / "BESReportsData" / "b.dat").unlink()
    (run_dir / "key_files" / "masthead.afxm").unlink()
    (run_dir / "db_info.json").write_text("not json")

    results = upgrade.check_backup_folder(str(run_dir))
    failed = "\n".join(message for level, message in results if level == "FAIL")

    assert "BESReporting" in failed
    assert "BESReportsData" in failed
    assert "masthead" in failed
    assert "db_info.json" in failed


@pytest.mark.parametrize(
    "bad", ["../outside.bak", "/etc/passwd", "C:\\x.bak", "a/../../b"]
)
def test_backup_check_stays_inside_the_folder(upgrade, tmp_path, bad):
    """Test a manifest path that leaves the backup folder is refused, not read."""
    run_dir, state = _finished_backup(upgrade, tmp_path)
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise"])
    manifest = json.loads((run_dir / upgrade.BACKUP_MANIFEST).read_text())
    manifest["databases"][0]["file"] = bad
    (run_dir / upgrade.BACKUP_MANIFEST).write_text(json.dumps(manifest))
    read = []

    results = upgrade.check_backup_folder(
        str(run_dir), hash_fn=lambda path: read.append(path) or ""
    )

    assert any(level == "FAIL" and "outside" in m for level, m in results)
    assert read == []


def test_backup_check_without_manifest(upgrade, tmp_path):
    """Test a folder without a manifest fails clearly."""
    results = upgrade.check_backup_folder(str(tmp_path))

    assert results[0][0] == "FAIL" and upgrade.BACKUP_MANIFEST in results[0][1]


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "", "C:", "x;y"])
def test_share_host_check_refuses_other_folders(upgrade, tmp_path, name):
    """Test the share's host only checks a plain folder name in its share."""
    result = upgrade.share_host_backup_check(str(tmp_path), {"folder": name})

    assert result["ok"] is False


def test_verify_backup_checked_by_coordinator_hosting_the_share(upgrade, tmp_path):
    """Test the root asks the share's host to check the backup, and the
    coordinator, with the share on its own disk, checks the local folder.
    """
    run_dir, state = _finished_backup(upgrade, tmp_path)
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    results = []

    def walkthrough(bridge):
        results.append(
            bridge.remote_action(
                "share_host", "verify_backup", {"folder": run_dir.name}
            )
        )

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        rig.coordinator.share_folder = str(tmp_path / "share")
        await rig.start()
        await rig.until(lambda: results)
        await rig.finish()

    asyncio.run(scenario())
    assert results[0]["ok"] is True
    assert not [r for r in results[0]["results"] if r[0] == "FAIL"]


def test_verify_backup_forwarded_to_share_owner(upgrade, tmp_path):
    """Test without the share on the coordinator's disk, the share owner node
    checks the backup in its own folder.
    """
    run_dir, state = _finished_backup(upgrade, tmp_path)
    upgrade.write_backup_manifest(state, str(run_dir), ["BFEnterprise", "BESReporting"])
    results = []

    def walkthrough(bridge):
        # the share owner joins after the root, so ask until it's there:
        for _ in range(100):
            reply = bridge.remote_action(
                "share_host", "verify_backup", {"folder": run_dir.name}
            )
            if "no share_owner node" not in str(reply.get("error")):
                break
            time.sleep(0.05)
        results.append(reply)

    async def scenario():
        rig = WalkRig(upgrade, walkthrough)
        await rig.start()
        rig.add(
            "hyperv",
            ["hyperv", "share_owner"],
            hyperv_host(upgrade),
            share_offer={
                "unc": SHARE_UNC,
                "user": None,
                "password": None,
                "folder": str(tmp_path / "share"),
            },
        )
        await rig.coordinator.wait_for_nodes(2, timeout=5)
        await rig.until(lambda: results)
        await rig.finish()

    asyncio.run(scenario())
    assert results[0]["ok"] is True, results


def test_backup_check_runs_last_in_the_backup_step(upgrade, compat):
    """Test the backup step checks its own backup after writing everything."""
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    steps = upgrade.build_steps(path, local_sql=True)
    backup = next(s for s in steps if s.id == "backup")

    assert backup.actions[-1] == "verify_backup"
    assert "verify_backup" in upgrade.ACTIONS


def test_verify_backup_uses_the_share_hosts_check(upgrade, tmp_path, capsys):
    """Test the root writes the manifest, shows the share host's results, and
    doesn't read the backup over the network itself.
    """
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.state["local_sql"] = True
    reply = {
        "ok": True,
        "checked_by": "GAMING-JAMES",
        "folder": "D:\\_bigfix_backup\\bigfix_x",
        "results": [["ok", "BFEnterprise: 9.0 GB, SHA-256 matches the copy taken"]],
    }
    ctx.session = FakeSession(action_result=reply)
    ctx.session.ask = lambda *a, **k: "yes"
    read = []
    original = upgrade.check_backup_folder
    upgrade.check_backup_folder = lambda *a, **k: read.append(a) or original(*a, **k)
    try:
        upgrade.ACTIONS["verify_backup"](ctx)
    finally:
        upgrade.check_backup_folder = original

    out = capsys.readouterr().out
    assert ctx.session.actions[0][:2] == ("share_host", "verify_backup")
    assert ctx.session.actions[0][2]["folder"] == os.path.basename(ctx.backup_dir())
    assert "on GAMING-JAMES" in out and "SHA-256 matches" in out
    assert read == []
    assert os.path.isfile(os.path.join(ctx.backup_dir(), upgrade.BACKUP_MANIFEST))


def test_verify_backup_checks_from_here_without_share_host(upgrade, tmp_path, capsys):
    """Test that when no share host answers, the root checks the folder itself
    and says why.
    """
    ctx = walkthrough_ctx(upgrade, tmp_path, local_host(upgrade))
    ctx.session = FakeSession(
        action_result={"ok": False, "error": "no share_owner node is connected"}
    )

    upgrade.ACTIONS["verify_backup"](ctx)

    out = capsys.readouterr().out
    assert "no share_owner node is connected, checking it from here" in out
    # checked from here: this empty backup is missing its restore notes
    assert "[FAIL] restore notes is missing" in out
    assert ctx.state["backup_check"]["checked_on"] == ctx.backup_dir()


class StepLoop:
    """Runs _walk_steps on given steps, with scripted answers."""

    def __init__(self, upgrade, steps, answers, host=None):
        self.upgrade = upgrade
        self.steps = steps
        self.answers = list(answers)
        self.asked = []
        self.host = host or local_host(upgrade)
        self.state = {"done": [], "reports": {}}
        self.ctx = types.SimpleNamespace(
            host=self.host,
            state=self.state,
            dry_run=False,
            session=None,
            sql_server=lambda: "localhost",
            manual_needed=False,
        )

    def ask(self, prompt, choices, default=None):
        self.asked.append((prompt, list(choices)))
        if not self.answers:
            return "quit"
        answer = self.answers.pop(0)
        assert answer in choices, (answer, choices)
        return answer

    def run(self):
        args = types.SimpleNamespace(dry_run=False)
        return self.upgrade._walk_steps(
            args, self.steps, self.state, self.ctx, self.ask, None, lambda: None
        )


def test_automatic_step_asks_for_next(upgrade, capsys):
    """Test a step the script did itself asks to go on with next, not done,
    and says there's nothing to change.
    """
    loop = StepLoop(upgrade, [upgrade.Step("stop", "Stop", "Stopped.")], ["next"])

    assert loop.run() == 0

    prompt, choices = loop.asked[0]
    assert choices == ["next", "skip", "quit"]
    assert "nothing for you to change" in capsys.readouterr().out.lower()
    assert loop.state["done"] == ["stop"]


def test_manual_step_asks_for_done_with_what_to_do(upgrade, monkeypatch, capsys):
    """Test a step a person has to carry out says what to do, and asks for
    done once it's finished.
    """
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 0)
    step = upgrade.Step(
        "sp", "Apply SP3", "Install SP3.", manual=True, todo="Install SP3 with setup."
    )
    loop = StepLoop(upgrade, [step], ["done"])

    assert loop.run() == 0

    prompt, choices = loop.asked[0]
    assert choices == ["done", "skip", "quit"]
    out = capsys.readouterr().out
    assert "YOUR TURN" in out and "Install SP3 with setup." in out
    assert "type done" in out


def test_build_steps_marks_the_steps_people_do(upgrade, compat):
    """Test service packs, upgrades and cleanup are steps people carry out,
    while backups, services and validation are done by the script.
    """
    path = upgrade.find_upgrade_path(
        compat,
        {"bigfix": "10.0.7.52", "windows": "2012 R2", "mssql": "2008 R2"},
        {"windows": "2025", "mssql": "2025"},
    )
    steps = {s.id: s for s in upgrade.build_steps(path, local_sql=True)}

    assert steps["service_pack_1"].manual
    assert steps["upgrade_1_mssql_2017"].manual
    assert steps["upgrade_2_windows_2019"].manual
    assert steps["cleanup"].manual
    for automatic in ("preflight", "backup", "stop_services_0", "start_services_1"):
        assert not steps[automatic].manual
    assert steps["upgrade_1_mssql_2017"].verify == ["verify_mssql:2017"]
    assert steps["service_pack_1"].verify == ["verify_service_pack:SP3"]
    assert steps["upgrade_2_windows_2019"].verify == ["verify_windows:2019"]


def test_done_is_checked_and_asked_again_when_not_upgraded(
    upgrade, monkeypatch, capsys
):
    """Test done on a SQL Server upgrade is checked: still 2008 R2 means it's
    asked again, and once 2017 reports, it's accepted.
    """
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 0)
    host = local_host(upgrade)
    versions = [[["10.50.6000.34", "SP3"]], [["14.0.1000.169", "RTM"]]]
    host.sql_handler = lambda server, query: versions.pop(0)
    step = upgrade.Step(
        "upgrade_1_mssql_2017",
        "Upgrade mssql to 2017",
        "Run setup.",
        manual=True,
        verify=["verify_mssql:2017"],
    )
    loop = StepLoop(upgrade, [step], ["done", "done"], host=host)

    assert loop.run() == 0

    out = capsys.readouterr().out
    assert "still reports 10.50.6000.34 (2008 R2), not 2017" in out
    assert len(loop.asked) == 2
    assert loop.state["done"] == ["upgrade_1_mssql_2017"]


def test_done_never_checks_out_stops_after_three_tries(upgrade, monkeypatch, capsys):
    """Test a step whose check keeps failing stops after three tries, not
    marked done, with what to do next.
    """
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 0)
    host = local_host(upgrade)
    host.sql_handler = lambda server, query: [["10.50.6000.34", "SP3"]]
    step = upgrade.Step(
        "upgrade_1_mssql_2017",
        "Upgrade mssql to 2017",
        "Run setup.",
        manual=True,
        verify=["verify_mssql:2017"],
    )
    loop = StepLoop(upgrade, [step], ["done", "done", "done"], host=host)

    assert loop.run() == 1

    assert loop.state["done"] == []
    assert "rerun" in capsys.readouterr().out.lower()


def test_done_accepted_with_a_warning_when_it_cant_be_checked(
    upgrade, monkeypatch, capsys
):
    """Test that when the version can't be read, done is taken with a warning."""
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 0)
    host = local_host(upgrade)

    def broken(server, query):
        raise OSError("sqlcmd not found")

    host.sql_handler = broken
    step = upgrade.Step(
        "x", "Upgrade", "Run setup.", manual=True, verify=["verify_mssql:2017"]
    )
    loop = StepLoop(upgrade, [step], ["done"], host=host)

    assert loop.run() == 0

    assert "couldn't check" in capsys.readouterr().out
    assert loop.state["done"] == ["x"]


def test_quick_done_on_a_manual_step_is_confirmed(upgrade, monkeypatch):
    """Test done right after a manual step starts asks if it's really finished,
    and no goes back to the step's question.
    """
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 60)
    step = upgrade.Step("sp", "Apply SP3", "Install SP3.", manual=True)
    loop = StepLoop(upgrade, [step], ["done", "no", "done", "yes"])

    assert loop.run() == 0

    prompts = [prompt for prompt, _ in loop.asked]
    assert "really finished" in prompts[1]
    assert loop.state["done"] == ["sp"]


def test_quick_next_on_an_automatic_step_isnt_questioned(upgrade, monkeypatch):
    """Test going on quickly from a step the script did isn't questioned."""
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 60)
    loop = StepLoop(upgrade, [upgrade.Step("stop", "Stop", "Stopped.")], ["next"])

    assert loop.run() == 0
    assert len(loop.asked) == 1


def test_snapshot_taken_by_hand_asks_for_done(upgrade, monkeypatch):
    """Test a snapshot step asks for done when the operator has to take it,
    and next when the Hyper-V node took it.
    """
    monkeypatch.setattr(upgrade, "FAST_DONE_SECONDS", 0)

    def by_hand(ctx):
        ctx.manual_needed = True

    monkeypatch.setitem(upgrade.ACTIONS, "fake_checkpoint", by_hand)
    monkeypatch.setitem(upgrade.ACTIONS, "fake_taken", lambda ctx: None)
    steps = [
        upgrade.Step("snapshot_1", "Snapshot", "Snap.", ["fake_checkpoint"]),
        upgrade.Step("snapshot_2", "Snapshot", "Snap.", ["fake_taken"]),
    ]
    loop = StepLoop(upgrade, steps, ["done", "next"])

    assert loop.run() == 0
    assert [choices[0] for _, choices in loop.asked] == ["done", "next"]


def test_compat_level_not_raised_past_what_sql_server_supports(upgrade, tmp_path):
    """Test raising to 120 on SQL Server 2008 R2 stops with what to do, without
    running ALTER DATABASE.
    """
    host = local_host(upgrade)
    host.sql[upgrade.SQL_COMPAT_LEVELS] = [
        ["BFEnterprise", "100"],
        ["BESReporting", "100"],
    ]
    host.sql[upgrade.SQL_PRODUCT_VERSION] = [["10.50.6000.34", "SP3"]]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.db_compat_level = None

    with pytest.raises(SystemExit) as stopped:
        upgrade.ACTIONS["raise_compat"](ctx, "120")

    message = str(stopped.value)
    assert "2008 R2" in message and "100" in message and "--step" in message
    assert not any("ALTER DATABASE" in query for query in host.sql_ran)


def test_prerequisite_says_when_the_level_was_read(upgrade, compat):
    """Test the service pack prerequisite says the level is from the plan,
    not the current one, since the step checks it live.
    """
    path = upgrade.find_upgrade_path(
        compat,
        {
            "bigfix": "10.0.7.52",
            "windows": "2012 R2",
            "mssql": "2008 R2",
            "mssql_level": "SP1",
        },
        {"windows": "2025", "mssql": "2025"},
    )
    text = "\n".join(path["steps"][0]["prerequisites"])

    assert "currently" not in text
    assert "when this plan was made" in text


def test_stray_done_doesnt_end_the_session(upgrade):
    """Test done typed when no question takes it explains, and leaves the
    session and its resume tokens alone: only end finishes it.
    """
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade),
    )
    coordinator.tokens["abc"] = {"node": "root", "secret": "x", "expires": 9e9}
    coordinator.questions["root"] = {
        "id": "q1",
        "node": "root",
        "prompt": "Go on to the next step?",
        "choices": ["next", "skip", "quit"],
    }

    async def scenario():
        await coordinator.handle_command("done", source="coordinator")
        stray_done = coordinator.done.is_set()
        await coordinator.handle_command("end", source="coordinator")
        return stray_done, coordinator.done.is_set()

    assert asyncio.run(scenario()) == (False, True)
    assert any("next" in line and "end" in line for line in output)


def _udf_host(upgrade, version, values):
    host = local_host(upgrade)
    host.sql[upgrade.SQL_PRODUCT_VERSION] = [[version, "RTM"]]
    host.sql_handler = lambda server, query: []  # the ALTER statements
    for name, value in values.items():
        host.sql[upgrade.sql_udf_inlining_query(name)] = (
            [[str(value)]] if value is not None else []
        )
    return host


def test_udf_inlining_turned_off_on_sql_2019_and_later(upgrade, tmp_path, capsys):
    """Test TSQL_SCALAR_UDF_INLINING is turned off in each BigFix database
    where it's on, after asking, in that database.
    """
    host = _udf_host(upgrade, "17.0.1000.7", {"BFEnterprise": 1, "BESReporting": 0})
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    asked = []
    ctx.ask = lambda prompt, choices, default=None: asked.append(prompt) or "yes"

    upgrade.ACTIONS["udf_inlining_off"](ctx)

    altered = [q for q in host.sql_ran if "SCOPED CONFIGURATION" in q]
    assert altered == [
        "USE [BFEnterprise]; ALTER DATABASE SCOPED CONFIGURATION SET"
        " TSQL_SCALAR_UDF_INLINING = OFF"
    ]
    assert len(asked) == 1 and "BFEnterprise" in asked[0]
    assert "BESReporting: already off" in capsys.readouterr().out


def test_udf_inlining_not_changed_when_declined(upgrade, tmp_path):
    """Test no is respected, and nothing is changed."""
    host = _udf_host(upgrade, "16.0.1000.6", {"BFEnterprise": 1, "BESReporting": 1})
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.ask = lambda prompt, choices, default=None: "no"

    upgrade.ACTIONS["udf_inlining_off"](ctx)

    assert not [q for q in host.sql_ran if "ALTER" in q]


def test_udf_inlining_skipped_before_sql_2019(upgrade, tmp_path, capsys):
    """Test on SQL Server 2017 and earlier, which don't have the setting,
    nothing is read or changed.
    """
    host = _udf_host(upgrade, "14.0.1000.169", {})
    ctx = walkthrough_ctx(upgrade, tmp_path, host)

    upgrade.ACTIONS["udf_inlining_off"](ctx)

    assert "2019" in capsys.readouterr().out
    assert not [q for q in host.sql_ran if "SCOPED" in q or "scoped" in q]


def test_udf_inlining_dry_run_changes_nothing(upgrade, tmp_path, capsys):
    """Test a dry run says what it would run, without running it."""
    host = _udf_host(upgrade, "17.0.1000.7", {"BFEnterprise": 1, "BESReporting": 1})
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.args.dry_run = True

    upgrade.ACTIONS["udf_inlining_off"](ctx)

    assert "DRY RUN" in capsys.readouterr().out
    assert not [q for q in host.sql_ran if "ALTER" in q]


# ---- checks around the BigFix upgrade, from the 11.0.7 upgrade incident


def test_bigfix_upgrade_gets_checks_before_and_after(upgrade, compat):
    """Test a BigFix upgrade is preceded by a fresh backup and SQL checks, is
    checked in the database after done, and Web Reports is checked after.
    """
    steps = upgrade.build_steps(real_path(upgrade, compat))
    ids = [s.id for s in steps]
    by_id = {s.id: s for s in steps}

    assert ids.index("pre_upgrade_3") == ids.index("upgrade_3_bigfix_11_0_6") - 1
    assert by_id["pre_upgrade_3"].actions == [
        "sql_backup",
        "check_sql_patch",
        "check_sql_health",
        "check_udf_inlining",
        "checkdb",
        "sql_memory_note",
    ]
    assert not by_id["pre_upgrade_3"].manual
    assert "verify_bigfix_db" in by_id["upgrade_3_bigfix_11_0_6"].verify
    assert "check_web_reports" in by_id["validate_3"].actions
    assert "check_web_reports" not in by_id["validate_1"].actions
    assert "pre_upgrade_1" not in ids  # only before BigFix upgrades
    assert "Cumulative Update" in by_id["upgrade_1_mssql_2017"].todo


def _db_ctx(upgrade, tmp_path, flag_rows, page="<html>ok</html>"):
    host = local_host(upgrade)
    host.sql[upgrade.SQL_UPGRADE_FLAG] = flag_rows
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    return ctx, page


@pytest.mark.parametrize(
    "rows, page, passed",
    [
        ([["1"]], "<html>BigFix</html>", True),
        ([], "<html>BigFix</html>", True),
        ([["0"]], "<html>BigFix</html>", False),
        (
            [["1"]],
            "Server is starting. Waiting for the database to become available",
            False,
        ),
    ],
)
def test_bigfix_db_upgrade_checked(upgrade, tmp_path, monkeypatch, rows, page, passed):
    """Test done on a BigFix upgrade checks the database finished upgrading:
    the PreviousUpgradeNotCompleted flag is cleared and the root server isn't.

    waiting for the database.
    """
    ctx, _ = _db_ctx(upgrade, tmp_path, rows)
    monkeypatch.setattr(upgrade, "fetch_root_page", lambda: page)

    ok, message = upgrade.VERIFIERS["verify_bigfix_db"](ctx, "")

    assert ok is passed
    if not passed:
        # what a person does to finish it, never run by the script:
        assert "start /wait" in message and "/silentupgrade" in message
        assert "CHECKDB" in message and "COPY_ONLY" in message


def test_bigfix_db_check_never_runs_besadmin(upgrade, tmp_path, monkeypatch):
    """Test a partially upgraded database is only reported, never fixed."""
    ctx, _ = _db_ctx(upgrade, tmp_path, [["0"]])
    monkeypatch.setattr(upgrade, "fetch_root_page", lambda: "ok")

    upgrade.VERIFIERS["verify_bigfix_db"](ctx, "")

    assert not [c for c in ctx.host.ran if "BESAdmin" in " ".join(c)]
    assert not [q for q in ctx.host.sql_ran if "UPDATE" in q.upper()]


def _sql_ctx(upgrade, tmp_path, version, level, update, answer="no"):
    host = local_host(upgrade)
    host.sql[upgrade.SQL_PATCH_LEVEL] = [[version, level, update]]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.ask = lambda prompt, choices, default=None: answer
    return ctx


def test_sql_rtm_without_cu_stops_unless_agreed(upgrade, tmp_path, capsys):
    """Test SQL Server 2016 and later on RTM with no Cumulative Update is
    warned about, and the BigFix upgrade only goes on if agreed.
    """
    ctx = _sql_ctx(upgrade, tmp_path / "declined", "15.0.2000.5", "RTM", "")
    with pytest.raises(SystemExit) as stopped:
        upgrade.ACTIONS["check_sql_patch"](ctx)
    assert "Cumulative Update" in str(stopped.value)

    ctx = _sql_ctx(upgrade, tmp_path / "agreed", "15.0.2000.5", "RTM", "", answer="yes")
    upgrade.ACTIONS["check_sql_patch"](ctx)
    assert "WARNING" in capsys.readouterr().out


def test_sql_with_cu_passes(upgrade, tmp_path, capsys):
    """Test a patched SQL Server passes, and the latest CU is still suggested."""
    ctx = _sql_ctx(upgrade, tmp_path, "14.0.3456.2", "RTM", "CU31")
    upgrade.ACTIONS["check_sql_patch"](ctx)
    out = capsys.readouterr().out
    assert "CU31" in out and "latest" in out


def test_sql_crashes_warned_before_bigfix_upgrade(upgrade, tmp_path, capsys):
    """Test recent SQL Server access violations and dumps are warned about,
    and stop the BigFix upgrade unless agreed.
    """
    host = local_host(upgrade)
    host.powershell[upgrade.PS_SQL_CRASH_EVENTS] = {"Count": 3}
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.ask = lambda prompt, choices, default=None: "no"
    with pytest.raises(SystemExit):
        upgrade.ACTIONS["check_sql_health"](ctx)

    host.powershell[upgrade.PS_SQL_CRASH_EVENTS] = {"Count": 0}
    upgrade.ACTIONS["check_sql_health"](ctx)
    assert "no SQL Server crashes" in capsys.readouterr().out


def test_checkdb_is_optional(upgrade, tmp_path):
    """Test CHECKDB only runs when asked for, on both databases."""
    host = local_host(upgrade)
    host.sql_handler = lambda server, query: []
    ctx = walkthrough_ctx(upgrade, tmp_path, host)
    ctx.ask = lambda prompt, choices, default=None: default or "no"
    upgrade.ACTIONS["checkdb"](ctx)
    assert not [q for q in host.sql_ran if "CHECKDB" in q]

    ctx.ask = lambda prompt, choices, default=None: "yes"
    upgrade.ACTIONS["checkdb"](ctx)
    ran = [q for q in host.sql_ran if "CHECKDB" in q]
    assert len(ran) == 2 and "[BFEnterprise]" in ran[0] and "[BESReporting]" in ran[1]


def test_low_sql_memory_noted(upgrade, tmp_path, capsys):
    """Test max server memory much below the computer's memory is noted,
    and nothing is changed.
    """
    host = local_host(upgrade)
    host.sql[upgrade.SQL_MEMORY] = [["4096", "20480"]]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)

    upgrade.ACTIONS["sql_memory_note"](ctx)

    assert "4096 MB" in capsys.readouterr().out
    assert not [q for q in host.sql_ran if "sp_configure" in q]


def test_web_reports_checked_after_bigfix_upgrade(upgrade, tmp_path, capsys):
    """Test validation warns when Web Reports isn't running, and asks for a
    login check either way.
    """
    host = local_host(upgrade)
    host.powershell[upgrade.PS_SERVICES] = [
        {"Name": "BESWebReportsServer", "State": "Stopped", "StartMode": "Manual"}
    ]
    ctx = walkthrough_ctx(upgrade, tmp_path, host)

    upgrade.ACTIONS["check_web_reports"](ctx)

    out = capsys.readouterr().out
    assert "WARNING" in out and "BESWebReportsServer" in out
    assert "log in to Web Reports" in out


def test_rest_isnt_ready_while_only_serverinfo_answers(upgrade):
    """Test REST counts as answering only when the masthead and root server
    queries answer too: while the root server waits for its database, they.

    fail although serverinfo answers.
    """
    ready = {
        "serverinfo": {"version": "10.0.7.52"},
        "masthead": {"name": "x"},
        "root_server": {"properties": {}},
    }
    assert upgrade._rest_answers(ready) is True
    for probe in ("masthead", "root_server", "serverinfo"):
        waiting = dict(ready)
        waiting[probe] = {"error": "JSONDecodeError: Expecting value: line 1 column 1"}
        assert upgrade._rest_answers(waiting) is False, probe
    assert upgrade._rest_answers({"skipped": "no BigFix REST connection"}) is False


@pytest.mark.parametrize(
    "error, text",
    [
        (asyncio.TimeoutError(), "timed out"),
        (ConnectionRefusedError(), "ConnectionRefusedError"),
        (OSError("No route to host"), "No route to host"),
    ],
)
def test_unreachable_coordinator_says_why(upgrade, error, text):
    """Test the reason is never blank, as with a timeout, whose text is empty."""
    assert upgrade.reach_error_text(error) == text


# ---- refused addresses, allow at run time, the share without prompts


def test_refused_node_is_told_why(upgrade):
    """Test a node whose address isn't allowed hears why, with the command
    to allow it, instead of a dropped connection.
    """
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade, allow=["10.9.9.9"]),
    )
    node = upgrade.ShareSessionNode(
        "hyperv", ["hyperv"], None, output=lambda line: None, **session_options(upgrade)
    )

    async def scenario():
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await node.connect("127.0.0.1", port)
        finally:
            server.close()

    with pytest.raises(upgrade.NotAllowed) as refused:
        asyncio.run(scenario())
    assert "allow 127.0.0.1" in str(refused.value)
    assert any("refused 127.0.0.1" in line for line in output)


def test_refused_node_keeps_retrying_until_allowed(upgrade):
    """Test a refused node retries, and joins once the coordinator's operator
    types allow with its address.
    """
    output, node_output = [], []
    saved = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        on_allow=lambda entries: saved.append(list(entries)),
        **coordinator_options(upgrade, allow=["10.9.9.9"]),
    )
    node = upgrade.ShareSessionNode(
        "hyperv",
        ["hyperv"],
        None,
        output=node_output.append,
        **session_options(upgrade),
    )

    async def scenario():
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        joining = asyncio.create_task(
            node.connect("127.0.0.1", port, attempts=50, delay=0.05)
        )
        await asyncio.sleep(0.2)
        await coordinator.handle_command("allow 127.0.0.1", source="coordinator")
        channel = await asyncio.wait_for(joining, 5)
        channel.close()
        server.close()

    asyncio.run(scenario())
    assert any("allow 127.0.0.1" in line for line in node_output)
    assert "127.0.0.1" in coordinator.allow
    assert saved == [["10.9.9.9", "127.0.0.1"]]


def test_allow_only_from_the_coordinators_own_terminal(upgrade):
    """Test a node can't widen who may connect: allow works only where the
    coordinator runs.
    """
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade, allow=["10.9.9.9"]),
    )

    asyncio.run(coordinator.handle_command("allow 0.0.0.0/0", source="BIGFIX"))

    assert coordinator.allow == ["10.9.9.9"]
    assert any("only" in line and "coordinator" in line for line in output)


@pytest.mark.parametrize("entry", ["not-an-ip", "300.1.1.1", "1.2.3.4; rm", ""])
def test_allow_rejects_bad_addresses(upgrade, entry):
    """Test allow only takes an address or a network."""
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade, allow=["10.9.9.9"]),
    )

    asyncio.run(coordinator.handle_command(f"allow {entry}", source="coordinator"))

    assert coordinator.allow == ["10.9.9.9"]


def test_firewall_allow_update_command(upgrade):
    """Test the firewall rule this tool made is updated to the new addresses,
    or made when missing.
    """
    command = upgrade.firewall_allow_update_command(
        "20260927120000Z", 445, ["192.168.5.40", "192.168.5.225"], "SMB"
    )
    script = command[-1]

    assert "Set-NetFirewallRule" in script and "New-NetFirewallRule" in script
    assert "192.168.5.40,192.168.5.225" in script
    assert "'BigFix upgrade 20260927120000Z SMB'" in script
    with pytest.raises(ValueError):
        upgrade.firewall_allow_update_command("x;y", 445, ["1.2.3.4"], "SMB")
    with pytest.raises(ValueError):
        upgrade.firewall_allow_update_command(
            "20260927120000Z", 445, ["1.2.3.4; Remove-Item x"], "SMB"
        )


def test_share_setup_doesnt_ask_by_default(upgrade, capsys):
    """Test setting up the share goes ahead without yes prompts, saying what
    it does, unless --share-confirm asks for them.
    """
    args = types.SimpleNamespace(share_confirm=False)
    confirm = upgrade.share_setup_confirm(args, ask=lambda *a, **k: "no")

    assert confirm("Set a new random password for the temporary account x?") is True
    assert "Set a new random password" in capsys.readouterr().out

    args.share_confirm = True
    confirm = upgrade.share_setup_confirm(args, ask=lambda *a, **k: "no")
    assert confirm("Fix the share?") is False


def test_default_allow_is_the_coordinators_subnets(upgrade):
    """Test that without --allow, nodes are allowed from the coordinator's own
    subnets, not loopback or link-local, since the pairing code still guards.

    joining.
    """
    host_ips = [
        {"IPAddress": "192.168.4.182", "PrefixLength": 24},
        {"IPAddress": "127.0.0.1", "PrefixLength": 8},
        {"IPAddress": "169.254.10.5", "PrefixLength": 16},
        {"IPAddress": "10.20.30.40", "PrefixLength": 22},
    ]

    assert upgrade.local_subnets(host_ips) == ["192.168.4.0/24", "10.20.28.0/22"]


@pytest.mark.parametrize(
    "given, saved, expected",
    [
        # default: own subnets, the root server, and what allow added
        ([], ["192.168.5.0/24"], ["192.168.4.0/24", "192.168.5.40", "192.168.5.0/24"]),
        # --allow narrows: only those, and what allow added
        (["192.168.4.189"], ["192.168.5.225"], ["192.168.4.189", "192.168.5.225"]),
    ],
)
def test_coordinator_allow_list(upgrade, given, saved, expected):
    """Test the coordinator's allow list with and without --allow."""
    host_ips = [{"IPAddress": "192.168.4.182", "PrefixLength": 24}]

    assert upgrade.coordinator_allow(given, host_ips, "192.168.5.40", saved) == expected


def test_firewall_peers_include_networks(upgrade):
    """Test the SMB firewall rule opens to allowed networks too, not only to
    single addresses.
    """
    assert upgrade.share_firewall_peers(
        "192.168.5.40", ["192.168.4.0/24", "192.168.5.225"]
    ) == [
        "192.168.5.40",
        "192.168.4.0/24",
        "192.168.5.225",
    ]


# ---- sliding resume expiry


def _coordinator_at(upgrade, clock, **changes):
    return upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=lambda line: None,
        now=lambda: clock[0],
        **coordinator_options(upgrade, **changes),
    )


def test_resume_token_slides_on_each_use(upgrade):
    """Test a node's token gets a fresh 24 hours each time it's used, so a
    session in use never expires under it.
    """
    clock = [1000.0]
    activity = []
    coordinator = _coordinator_at(
        upgrade, clock, on_activity=lambda: activity.append(1)
    )
    token = coordinator._issue_token("HYPERV")
    resume_id = token["id"]

    clock[0] += 20 * 3600
    coordinator._slide_token(coordinator.tokens[resume_id])

    assert coordinator.tokens[resume_id]["expires"] == clock[0] + 24 * 3600
    assert activity  # the pairing code's window slides too
    clock[0] += 23 * 3600
    assert coordinator._token(resume_id) is not None


def test_stopping_the_coordinator_slides_every_token(upgrade):
    """Test stopping the coordinator gives every token a fresh 24 hours, so
    the nodes rejoin without the code when it's back.
    """
    clock = [1000.0]
    coordinator = _coordinator_at(upgrade, clock)
    first = coordinator._issue_token("BIGFIX")["id"]
    second = coordinator._issue_token("HYPERV")["id"]

    clock[0] += 10 * 3600
    coordinator.slide_all()

    for resume_id in (first, second):
        assert coordinator.tokens[resume_id]["expires"] == clock[0] + 24 * 3600


def test_expired_token_isnt_slid_back_to_life(upgrade):
    """Test an expired token stays expired: sliding only extends live ones."""
    clock = [1000.0]
    coordinator = _coordinator_at(upgrade, clock)
    resume_id = coordinator._issue_token("HYPERV")["id"]

    clock[0] += 25 * 3600
    coordinator.slide_all()

    assert coordinator._token(resume_id) is None


def test_node_keeps_its_token_expiry_in_step(upgrade, monkeypatch):
    """Test a node takes the coordinator's new expiry, and slides its own copy
    when it disconnects.
    """
    saved = []
    node = upgrade.ShareSessionNode(
        "hyperv",
        ["hyperv"],
        None,
        output=lambda line: None,
        resume={"id": "r1", "secret": "s", "expires": 5.0},
        on_resume=saved.append,
        **session_options(upgrade),
    )

    node._on_resume_expires({"id": "r1", "expires": 99999.0})
    assert node.resume["expires"] == 99999.0
    node._on_resume_expires({"id": "other", "expires": 1.0})
    assert node.resume["expires"] == 99999.0

    monkeypatch.setattr(upgrade.time, "time", lambda: 50000.0)
    node._slide_resume()
    assert node.resume["expires"] == 50000.0 + 24 * 3600
    assert saved[-1]["expires"] == node.resume["expires"]


def test_session_pairing_code_window_slides(upgrade):
    """Test the saved pairing code's expiry gets a fresh 24 hours."""
    state = {"session": {"pairing_code": "123456", "expires": 10.0}}

    upgrade.slide_session_code(state, now=500.0)

    assert state["session"]["expires"] == 500.0 + 24 * 3600
    empty = {}
    upgrade.slide_session_code(empty, now=500.0)
    assert empty == {}


# ---- backup_bigfix on demand


class _SentTo:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


def test_backup_bigfix_goes_to_the_root_node(upgrade):
    """Test backup_bigfix from anywhere is sent to whichever node is the root
    server, by role, not by name.
    """
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade),
    )
    root, other = _SentTo(), _SentTo()
    coordinator.nodes = {
        "MYROOT": {"channel": root, "roles": ["root"], "ip": "x", "script": {}},
        "HV": {"channel": other, "roles": ["hyperv"], "ip": "y", "script": {}},
    }

    asyncio.run(coordinator.handle_command("backup_bigfix", source="HV"))

    assert [m["type"] for m in root.sent] == ["backup_request"]
    assert other.sent == []


def test_backup_bigfix_without_a_root_node(upgrade):
    """Test backup_bigfix says so when no root server node is connected."""
    output = []
    coordinator = upgrade.ShareSessionCoordinator(
        share={"unc": SHARE_UNC, "user": None, "password": None},
        output=output.append,
        **coordinator_options(upgrade),
    )

    asyncio.run(coordinator.handle_command("backup_bigfix", source="coordinator"))

    assert any("no root server node" in line for line in output)


def _backup_rig(upgrade, tmp_path, monkeypatch, states, answer):
    ran = []
    for name in upgrade.ON_DEMAND_BACKUP_ACTIONS + ["stop_services", "start_services"]:
        monkeypatch.setitem(
            upgrade.ACTIONS, name, lambda ctx, name=name: ran.append(name)
        )
    services = [
        {"Name": name, "State": state, "StartMode": "Manual"}
        for name, state in states.items()
    ]
    monkeypatch.setattr(upgrade, "_bigfix_services", lambda ctx: services)
    started = []
    monkeypatch.setattr(
        upgrade,
        "service_command",
        lambda verb, name, *rest: started.append((verb, name)) or ["x"],
    )
    asked = []
    session = FakeSession()
    session.ask = lambda prompt, choices, default=None: asked.append(prompt) or answer
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"done": [], "reports": {}, "baseline": {}, "backup_run_dir": "old"})
    )
    args = types.SimpleNamespace(
        state_file=str(state_path),
        dry_run=False,
        backup_dir=str(tmp_path / "share"),
        backup_share_user=None,
        staging_dir=None,
        sql_instance=None,
    )
    host = local_host(upgrade)
    host.run = lambda command: None
    return ran, started, asked, session, args, host, state_path


def test_backup_bigfix_with_services_stopped_doesnt_ask(upgrade, tmp_path, monkeypatch):
    """Test the on-demand backup runs at once when BigFix is stopped, into a
    new folder.
    """
    ran, started, asked, session, args, host, path = _backup_rig(
        upgrade, tmp_path, monkeypatch, {"BESRootServer": "Stopped"}, "no"
    )

    upgrade.run_backup_now(args, None, host, session)

    assert asked == []
    assert ran == upgrade.ON_DEMAND_BACKUP_ACTIONS
    assert (
        "backup_run_dir" not in json.loads(path.read_text())
        or json.loads(path.read_text())["backup_run_dir"] != "old"
    )


def test_backup_bigfix_asks_before_stopping_services(upgrade, tmp_path, monkeypatch):
    """Test no is respected: running services aren't stopped, nothing backed up."""
    ran, started, asked, session, args, host, path = _backup_rig(
        upgrade, tmp_path, monkeypatch, {"BESRootServer": "Running"}, "no"
    )

    upgrade.run_backup_now(args, None, host, session)

    assert len(asked) == 1 and "BESRootServer" in asked[0]
    assert ran == []


def test_backup_bigfix_restarts_only_what_was_running(upgrade, tmp_path, monkeypatch):
    """Test yes stops BigFix, backs up, then starts again only the services that
    were running, leaving stopped ones like WebUI stopped.
    """
    ran, started, asked, session, args, host, path = _backup_rig(
        upgrade,
        tmp_path,
        monkeypatch,
        {"BESRootServer": "Running", "FillDB": "Running", "BESWebUI": "Stopped"},
        "yes",
    )

    upgrade.run_backup_now(args, None, host, session)

    assert ran[0] == "stop_services"
    assert ran[1:] == upgrade.ON_DEMAND_BACKUP_ACTIONS
    started_names = [name for verb, name in started if verb == "Start-Service"]
    assert sorted(started_names) == ["BESRootServer", "FillDB"]
    assert "BESWebUI" not in started_names


def test_root_node_starts_backup_unless_walkthrough_runs(upgrade):
    """Test the root node starts the backup in the background, and refuses
    while its walkthrough is running.
    """
    started = threading.Event()
    node = upgrade.ShareSessionNode(
        "root",
        ["root"],
        local_host(upgrade),
        output=lambda line: None,
        backup_fn=lambda bridge: started.set(),
        **session_options(upgrade),
    )
    channel = _SentTo()

    async def ask():
        await node._handle_session_message(
            channel, {"type": "backup_request", "id": "1"}
        )

    node._walk_running.set()
    asyncio.run(ask())
    assert "walkthrough is running" in channel.sent[-1]["data"]["error"]

    node._walk_running.clear()
    asyncio.run(ask())
    assert channel.sent[-1]["data"] == {"started": True}
    assert started.wait(5)
