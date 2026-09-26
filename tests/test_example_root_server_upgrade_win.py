"""Tests for examples/bigfix_root_server_upgrade_win.py, on any OS.

The BigFix REST API, the Windows registry, PowerShell and sqlcmd are all faked.
"""

import importlib.util
import json
import os
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
    ):
        # registry: {key path: {value name: data}}, subkeys derived from paths
        self.registry = registry or {}
        self.powershell = powershell or {}
        self.sql = sql or {}
        self.files = set(files)
        self.ports = set(ports)
        self.admin = admin
        self.windows = windows
        self.ran = []

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
        result = self.sql[query]
        if isinstance(result, Exception):
            raise result
        return result

    def port_open(self, port):
        return port in self.ports

    def file_exists(self, path):
        return path in self.files

    def run(self, cmd):
        self.ran.append(cmd)
        return ""


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "version,expected",
    [
        ("10.50.2500.0", "2008 R2"),
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


def local_host(upgrade, **overrides):
    """A fake BigFix root server on 2012 R2 with a local SQL 2008 R2 instance."""
    registry = {
        ES_KEY: {"Version": "10.0.7.52"},
        ES_KEY
        + r"\MFSConfig": {
            "RESTUsername": "api_user",
            "RESTPassword": "{obf}c2VjcmV0c2VjcmV0",
            "RESTURL": "https://bigfix.example.com:52311/api",
        },
        ES_KEY + r"\Database": {"Server": "BIGFIX", "Password": "hunter2"},
        upgrade.WINDOWS_CURRENT_VERSION_KEY: {
            "ProductName": "Windows Server 2012 R2 Standard",
            "EditionID": "ServerStandard",
            "CurrentBuild": "9600",
            "InstallationType": "Server",
        },
        upgrade.SQL_INSTANCE_NAMES_KEY: {"MSSQLSERVER": "MSSQL10_50.MSSQLSERVER"},
        r"SOFTWARE\Microsoft\Microsoft SQL Server\MSSQL10_50.MSSQLSERVER\Setup": {
            "Version": "10.50.2500.0",
            "Edition": "Standard Edition",
            "PatchLevel": "10.50.2500.0",
        },
        upgrade.ODBC_INI_KEY
        + r"\bes_bfenterprise": {
            "Server": "BIGFIX",
            "Database": "BFEnterprise",
        },
        upgrade.SESSION_MANAGER_KEY: {},
    }
    powershell = {
        upgrade.PS_COMPUTER_SYSTEM: {
            "Manufacturer": "Microsoft Corporation",
            "Model": "Virtual Machine",
            "PartOfDomain": False,
            "Domain": "WORKGROUP",
            "TotalPhysicalMemory": 18857758720,
            "NumberOfLogicalProcessors": 4,
        },
        upgrade.PS_OPERATING_SYSTEM: {"LastBootUpTime": "2026-09-01T00:00:00"},
        upgrade.PS_DISKS: [{"DeviceID": "C:", "Size": 536000000000, "FreeSpace": 1}],
        upgrade.PS_SERVICES: [
            {
                "Name": "BESRootServer",
                "DisplayName": "BES Root Server",
                "State": "Running",
                "StartMode": "Auto",
                "StartName": "LocalSystem",
                "PathName": r"C:\BES Server\BESRootServer.exe",
            },
            {
                "Name": "FillDB",
                "DisplayName": "BES FillDB",
                "State": "Running",
                "StartMode": "Auto",
                "StartName": "LocalSystem",
                "PathName": r"C:\BES Server\FillDB.exe",
            },
        ],
        upgrade.PS_FEATURES: ["Web-Server"],
    }
    sql = {
        upgrade.SQL_SERVER_PROPERTIES: [
            [
                "10.50.2500.0",
                "SP1",
                "Standard Edition (64-bit)",
                "SQL_Latin1_General_CP1_CI_AS",
                "1",
            ]
        ],
        upgrade.SQL_DATABASES: [
            ["BFEnterprise", "ONLINE", "SIMPLE", "100", "2048", "2026-09-20 01:00:00"],
            ["BESReporting", "ONLINE", "SIMPLE", "100", "64", "NULL"],
            ["master", "ONLINE", "SIMPLE", "100", "5", "NULL"],
        ],
    }
    kwargs = {
        "registry": registry,
        "powershell": powershell,
        "sql": sql,
        "files": {r"C:\BES Server\masthead.afxm", r"C:\BES Server\license.crt"},
        "ports": {52311},
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

    assert info["windows"]["ProductName"] == "Windows Server 2012 R2 Standard"
    assert info["windows"]["pending_reboot"] == {
        "component_based_servicing": False,
        "windows_update": False,
        "pending_file_rename": False,
    }
    assert info["hardware"]["computer_system"]["Model"] == "Virtual Machine"
    assert info["sql"]["instances"]["MSSQLSERVER"]["Version"] == "10.50.2500.0"
    assert info["sql"]["bigfix_sql_server"] == "BIGFIX"
    assert info["sql"]["server_properties"]["ProductLevel"] == "SP1"
    assert info["sql"]["databases"]["BFEnterprise"]["compatibility_level"] == 100
    assert info["bigfix"]["install_folder"] == r"C:\BES Server"
    assert info["bigfix"]["key_files"] == {
        "masthead.afxm": True,
        "license.crt": True,
        "license.pvk": False,
    }
    assert info["bigfix"]["ports"]["52311"] is True
    assert [s["Name"] for s in info["bigfix"]["services"]] == [
        "BESRootServer",
        "FillDB",
    ]


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
