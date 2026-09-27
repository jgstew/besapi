"""Tests for examples/bigfix_root_server_upgrade_win.py, on any OS.

The BigFix REST API, the Windows registry, PowerShell and sqlcmd are all faked.
"""

import asyncio
import datetime
import importlib.util
import json
import logging
import os
import re
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
        stack = [top.rstrip("\\")]
        while stack:
            folder = stack.pop()
            prefix = (folder + "\\").lower()
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
            stack.extend(folder + "\\" + name for name in reversed(dirnames))

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

    assert key_files["searched"] == [BIGFIX_FOLDER]
    assert key_files["found"] == {
        "masthead.afxm": [
            BIGFIX_FOLDER + r"\BES Installers\license\masthead.afxm",
            SERVER_FOLDER + r"\wwwrootbes\masthead\masthead.afxm",
        ],
        # the copy under FillDBData\bufferdir is skipped:
        "license.crt": [BIGFIX_FOLDER + r"\BES Installers\license\license.crt"],
        "license.pvk": [],
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
            "license.pvk": "def",
        }
    )
    assert result["Private Data Directory"] == r"C:\wr"
    assert result["SOAPPasswordIsEncrypted"] == "1"
    assert result["RESTPasswordEncryption"] == "1"
    assert result["SOAPPassword"] == upgrade.REDACTED
    assert result["PrivateKey"] == upgrade.REDACTED
    assert result["license.pvk"] == upgrade.REDACTED


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
    """Test services are stopped front ends first, root server and client last."""
    services = [
        {"Name": "BESClient", "DisplayName": "BES Client"},
        {"Name": "BESRootServer", "DisplayName": "BES Root Server"},
        {"Name": "FillDB", "DisplayName": "BES FillDB"},
        {"Name": "BESWebReportsServer", "DisplayName": "BES Web Reports Server"},
        {"Name": "GatherDB", "DisplayName": "BES Gather Service"},
        {"Name": "BESWebUI", "DisplayName": "BES WebUI"},
    ]
    assert upgrade.service_stop_order(services) == [
        "BESWebUI",
        "BESWebReportsServer",
        "GatherDB",
        "FillDB",
        "BESRootServer",
        "BESClient",
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

    assert ids[:2] == ["preflight", "backup"]
    assert ids[-2:] == ["final_validation", "cleanup"]
    assert "upgrade_1_mssql_2017" in ids
    first = ids.index("upgrade_1_mssql_2017")
    assert ids[first - 2 : first + 3] == [
        "stop_services_1",
        "snapshot_1",
        "upgrade_1_mssql_2017",
        "start_services_1",
        "validate_1",
    ]
    assert len(ids) == len(set(ids))


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
        "BESClient",
        "BESRootServer",
        "FillDB",
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
    assert folder == r"\\fileserver\share\bigfix" + os.sep + "bigfix_20260926_140509"


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
        args, host, state, str(tmp_path / "state.json"), ask=lambda *a: "yes"
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
    options = dict(
        password=b"k" * 32,
        serial="123456789",
        share_unc=SHARE_UNC,
        allow=[],
    )
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
        **session_options(upgrade, **options),
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
        return await run_session(upgrade, ["retry", "done"], {"bigfix": host})

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
            upgrade, [], {"bigfix": host}, console_commands=["status", "retry", "done"]
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
    channel = upgrade.load_channel()
    output = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=output.append,
            max_failures=3,
            **session_options(
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
                await node.run("127.0.0.1", port)
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
            **session_options(upgrade),
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
        await coordinator.handle_command("done", source="coordinator")
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
            **session_options(upgrade),
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


def test_load_channel_once(upgrade):
    """Test the channel module is loaded once, so its exceptions can be caught."""
    assert upgrade.load_channel() is upgrade.load_channel()
    assert (
        upgrade.load_channel().HandshakeError is upgrade.load_channel().HandshakeError
    )


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


@pytest.mark.parametrize(
    "windows,hyperv,root,expected",
    [
        (True, True, False, "coordinator"),
        (True, False, True, "peer"),
        (True, False, False, "peer"),
        (False, False, False, "console"),
    ],
)
def test_detect_node_role(upgrade, windows, hyperv, root, expected):
    """Test the node role comes from what the computer is."""
    host = local_host(upgrade, windows=windows)
    host.powershell[upgrade.PS_HYPERV_HOST] = hyperv
    if not root:
        host.registry.pop(ES_KEY)
    assert upgrade.detect_node_role(host) == expected


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
        (None, "none", "generated"),
        (None, "env", None),
        ("new", "env", "generated"),
    ],
)
def test_decide_pairing_code(upgrade, given, source, expected):
    """Test a pairing code is made up whenever there's no PSK to trust instead."""
    assert upgrade.decide_pairing_code(given, source, lambda: "generated") == expected


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


def test_share_session_pairing_prompted(upgrade):
    """Test a node is asked for the pairing code only when the coordinator needs
    one.
    """
    channel = upgrade.load_channel()
    code = "123456"
    prompts = []

    async def scenario():
        coordinator = upgrade.ShareSessionCoordinator(
            share={"unc": SHARE_UNC, "user": None, "password": None},
            output=lambda line: None,
            pairing_required=True,
            **session_options(
                upgrade, password=channel.derive_password(b"psk", "123456789", code)
            ),
        )
        server = await coordinator.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = upgrade.ShareSessionNode(
            "console1",
            ["console"],
            None,
            output=lambda line: None,
            prompt_pairing=lambda: prompts.append("asked") or code,
            rekey=lambda pairing: channel.derive_password(b"psk", "123456789", pairing),
            **session_options(
                upgrade, password=channel.derive_password(b"psk", "123456789")
            ),
        )
        task = asyncio.create_task(node.run("127.0.0.1", port))
        await coordinator.wait_for_nodes(1, timeout=5)
        await coordinator.handle_command("done", source="coordinator")
        await asyncio.wait_for(task, timeout=5)
        server.close()

    asyncio.run(scenario())
    assert prompts == ["asked"]


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
            **session_options(upgrade),
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
        await coordinator.handle_command("done", source="coordinator")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        server.close()
        return peer.share, console.share

    peer_share, console_share = asyncio.run(scenario())
    assert peer_share["password"] == "temp-Pw-123!"
    assert "password" not in console_share
