# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "besapi[plugins]>=4.4.1",
# ]
#
# [tool.uv]
# # supply chain: skip releases under a week old, except besapi itself:
# exclude-newer = "7 days"
# exclude-newer-package = { besapi = false }
# ///
"""
Assess and walk through an in-place upgrade of a Windows BigFix root server.

Covers the BigFix server, a local Microsoft SQL Server and Windows Server, in
an order where every step along the way is a supported combination, based on
the HCL and Microsoft support matrices in
`bigfix_root_server_upgrade_win_compat.yaml` next to this script.

Two levels of detail are collected:
- from anywhere, over the REST API with besapi: serverinfo, the masthead,
  the root server's own reported properties and session relevance counts
- on the root server itself, run as administrator: also the registry, SQL
  Server instances and databases, services, ports, disks and pending reboots

Report mode (the default) is read-only and prints a JSON document of all of
it, including the upgrade assessment, with secrets removed.

Walkthrough mode (`--walkthrough`, on the root server only) guides the
upgrade one step at a time: backups, stopping services, snapshots, each
upgrade, and validation against the starting baseline. Progress is saved to a
state file so it resumes after reboots. The upgrades themselves and VM
snapshots are done by the operator when prompted.

requires `besapi[plugins]`, install with command:
`pip install besapi[plugins]`

or run it with its PEP 723 dependencies installed automatically:
`uv run bigfix_root_server_upgrade_win.py`

Example Usage, report using the besapi config file or local root server creds:
python bigfix_root_server_upgrade_win.py --report-file root_server_report.json

Example Usage, report with an explicit connection:
python bigfix_root_server_upgrade_win.py -r https://bigfix:52311/api -u API_USER

Example Usage, the upgrade walkthrough on the root server as administrator:
python bigfix_root_server_upgrade_win.py --walkthrough --backup-dir D:\\bigfix_backup

References:
- https://support.hcl-software.com/csm?id=kb_article&sysparm_article=KB0104120
- https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/c_before_upgrading.html
- https://learn.microsoft.com/en-us/troubleshoot/sql/general/use-sql-server-in-windows
- https://learn.microsoft.com/en-us/windows-server/get-started/install-upgrade-migrate
"""

import collections
import contextlib
import dataclasses
import datetime
import json
import logging
import ntpath
import os
import platform
import re
import shutil
import socket
import sys
from typing import Any, Dict, List, Optional

import besapi
import besapi.plugin_utilities

__version__ = "0.1.0"

COMPAT_FILE_NAME = "bigfix_root_server_upgrade_win_compat.yaml"

# ---------------------------------------------------------------- REST queries

# masthead serial number and the FQDN from the masthead gather url:
MASTHEAD_RELEVANCE = (
    '(site number of it, preceding text of first ":" of following text of '
    'first "://" of gather url of it) of bes license'
)

ROOT_COMPUTER_RELEVANCE = (
    "(id of it, name of it, operating system of it, last report time of it"
    " as string) of bes computers whose (root server flag of it)"
)

# properties the root server's own client reports:
ROOT_PROPERTY_NAMES = [
    "OS",
    "CPU",
    "RAM",
    "Computer Type",
    "BES Client Version",
    "Free Space on System Drive",
    "Total Size of System Drive",
    "DNS Name",
    "IP Address",
    "Relay",
]
ROOT_PROPERTIES_RELEVANCE = (
    "(name of property of it, values of it) of property results whose"
    " (name of property of it is contained by set of ("
    + ";".join(f'"{name}"' for name in ROOT_PROPERTY_NAMES)
    + ")) of bes computers whose (root server flag of it)"
)

# each is run on its own, one failing does not stop the others:
REST_QUERIES = [
    {"name": "computers", "relevance": "number of bes computers"},
    {
        "name": "computers_reporting_45_days",
        "relevance": (
            "number of bes computers whose (last report time of it >= now - 45 * day)"
        ),
    },
    {
        "name": "relays",
        "relevance": "number of bes computers whose (relay server flag of it)",
    },
    {"name": "operators", "relevance": "number of bes users"},
    {
        "name": "open_actions",
        "relevance": 'number of bes actions whose (state of it = "Open")',
    },
    {
        "name": "sites",
        "relevance": '(name of it, (version of it as string | "none")) of bes sites',
    },
]

# ---------------------------------------------------------------- local probes

BIGFIX_SERVER_KEY = r"SOFTWARE\Wow6432Node\BigFix\Enterprise Server"
WINDOWS_CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
WINDOWS_VERSION_VALUES = [
    "ProductName",
    "EditionID",
    "DisplayVersion",
    "ReleaseId",
    "CurrentBuild",
    "UBR",
    "InstallationType",
]
SQL_INSTANCE_NAMES_KEY = r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL"
SQL_SETUP_VALUES = ["Version", "Edition", "PatchLevel", "SQLDataRoot", "SqlProgramDir"]
ODBC_INI_KEY = r"SOFTWARE\ODBC\ODBC.INI"
ODBC_INI_WOW_KEY = r"SOFTWARE\Wow6432Node\ODBC\ODBC.INI"
SESSION_MANAGER_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager"
CBS_REBOOT_PENDING_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing"
    r"\RebootPending"
)
WU_REBOOT_REQUIRED_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update"
    r"\RebootRequired"
)

# PowerShell 4 (Windows Server 2012 R2) has Get-CimInstance and ConvertTo-Json:
PS_COMPUTER_SYSTEM = (
    "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model,"
    " Domain, PartOfDomain, TotalPhysicalMemory, NumberOfLogicalProcessors,"
    " HypervisorPresent | ConvertTo-Json -Compress"
)
PS_OPERATING_SYSTEM = (
    "Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version,"
    " OSLanguage, @{n='LastBootUpTime';e={$_.LastBootUpTime.ToString('o')}}"
    " | ConvertTo-Json -Compress"
)
PS_DISKS = (
    "@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object"
    " DeviceID, Size, FreeSpace, FileSystem) | ConvertTo-Json -Compress"
)
PS_SERVICES = (
    "@(Get-CimInstance Win32_Service | Where-Object { $_.DisplayName -like 'BES *'"
    " -or $_.DisplayName -like '*BigFix*' -or $_.Name -like 'MSSQL*'"
    " -or $_.Name -like 'SQLAgent*' -or $_.Name -eq 'SQLBrowser' }"
    " | Select-Object Name, DisplayName, State, StartMode, StartName, PathName)"
    " | ConvertTo-Json -Compress"
)
PS_FEATURES = (
    "@(Get-WindowsFeature | Where-Object { $_.Installed } | ForEach-Object"
    " { $_.Name }) | ConvertTo-Json -Compress"
)

SQL_SERVER_PROPERTIES = (
    "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)),"
    " CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)),"
    " CAST(SERVERPROPERTY('Edition') AS nvarchar(128)),"
    " CAST(SERVERPROPERTY('Collation') AS nvarchar(128)),"
    " IS_SRVROLEMEMBER('sysadmin')"
)
SQL_DATABASES = (
    "SET NOCOUNT ON; SELECT d.name, d.state_desc, d.recovery_model_desc,"
    " d.compatibility_level,"
    " (SELECT SUM(CAST(f.size AS bigint)) * 8 / 1024 FROM sys.master_files f"
    " WHERE f.database_id = d.database_id),"
    " (SELECT CONVERT(varchar(19), MAX(b.backup_finish_date), 120)"
    " FROM msdb.dbo.backupset b WHERE b.database_name = d.name AND b.type = 'D')"
    " FROM sys.databases d ORDER BY d.name"
)

BIGFIX_DATABASES = ["BFEnterprise", "BESReporting"]
KEY_FILES = ["masthead.afxm", "license.crt", "license.pvk"]
# BigFix server, Web Reports, http and https:
PORTS = [52311, 8083, 80, 443]
LOW_DISK_BYTES = 20 * 1024**3

# ---------------------------------------------------------------- versions

SQL_MAJOR_VERSIONS = {
    11: "2012",
    12: "2014",
    13: "2016",
    14: "2017",
    15: "2019",
    16: "2022",
    17: "2025",
}
SERVICING_LEVELS = ["RTM", "SP1", "SP2", "SP3", "SP4"]


def version_tuple(version: str) -> tuple:
    """Get a version string as a tuple of ints, so 10.0.10 sorts after 10.0.7."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version)))


def bigfix_line(version: str) -> str:
    """Get the BigFix version line, like `10.0`, from a full version."""
    return ".".join(str(part) for part in version_tuple(version)[:2])


def sql_major_from_version(version: Optional[str]) -> Optional[str]:
    """Get the SQL Server product version, like `2008 R2`, from a build number."""
    parts = version_tuple(version) if version else ()
    if len(parts) < 2:
        return None
    if parts[0] == 10:
        return "2008 R2" if parts[1] == 50 else "2008"
    return SQL_MAJOR_VERSIONS.get(parts[0])


def windows_server_version(os_name: Optional[str]) -> Optional[str]:
    """Get the Windows Server version, like `2012 R2`.

    Works with the BigFix OS property (`Win2012R2 6.3.9600`) and the registry
    product name (`Windows Server 2012 R2 Standard`).
    """
    match = re.search(r"(?:Win|Windows Server )(20\d\d)\s?(R2)?", os_name or "")
    if not match:
        return None
    return match.group(1) + (" R2" if match.group(2) else "")


def servicing_level_at_least(level: Optional[str], minimum: str) -> Optional[bool]:
    """Compare SQL servicing levels, RTM < SP1 < ... < SP4. None if unknown."""
    level = (level or "").upper()
    if level not in SERVICING_LEVELS:
        return None
    return SERVICING_LEVELS.index(level) >= SERVICING_LEVELS.index(minimum.upper())


def product_sort_key(version: str) -> tuple:
    """Sort key for product versions like `2012 R2`, newest last."""
    match = re.match(r"(\d{4})( R2)?$", version)
    if match:
        return (1, int(match.group(1)), 1 if match.group(2) else 0, version)
    return (0, 0, 0, version)


# ---------------------------------------------------------------- compat


def load_compat(path: Optional[str] = None) -> dict:
    """Load the compatibility data, by default from next to this script."""
    # NOTE: imported here, only report and walkthrough runs need it:
    from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel

    path = path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), COMPAT_FILE_NAME
    )
    with open(path, encoding="utf-8") as compat_file:
        return YAML(typ="safe").load(compat_file)


def _mssql_on_windows(compat: dict) -> dict:
    return compat["mssql_on_windows"]["versions"]


def _bigfix_entry(compat: dict, version: str) -> Optional[dict]:
    return compat["bigfix_server"].get(bigfix_line(version))


def check_state(compat: dict, state: dict) -> dict:
    """Check a combination of BigFix, Windows Server and SQL Server versions.

    Arguments:
        compat: from load_compat()
        state: `bigfix`, `windows` and `mssql` versions, optionally the
            `mssql_level` servicing level and lowest `db_compat_level` of the
            BigFix databases.
    Returns:
        `supported`, the `problems` that make it unsupported, and
        `prerequisites` that could not be checked, like an unknown SQL
        servicing level.
    """
    problems = []
    prerequisites = []
    bigfix, windows, mssql = state["bigfix"], state["windows"], state["mssql"]

    min_level = _mssql_on_windows(compat).get(mssql, {}).get(windows)
    if min_level is None:
        problems.append(
            f"SQL Server {mssql} is not supported on Windows Server {windows}"
        )
    elif min_level != "RTM":
        level_ok = servicing_level_at_least(state.get("mssql_level"), min_level)
        requirement = f"needs {min_level} or later on Windows Server {windows}"
        if level_ok is None:
            prerequisites.append(f"SQL Server {mssql} {requirement}")
        elif not level_ok:
            problems.append(f"SQL Server {mssql} {state['mssql_level']} {requirement}")

    entry = _bigfix_entry(compat, bigfix)
    if entry is None:
        problems.append(
            f"BigFix {bigfix_line(bigfix)} is not in the compatibility data"
        )
    else:
        for component, name, version in (
            ("windows", "Windows Server", windows),
            ("mssql", "SQL Server", mssql),
        ):
            minimum = entry[component].get(version)
            if minimum is None:
                problems.append(f"BigFix {bigfix} does not support {name} {version}")
            elif version_tuple(bigfix) < version_tuple(minimum):
                problems.append(
                    f"BigFix {bigfix} does not support {name} {version}"
                    f" (needs {minimum} or later)"
                )

        db_compat_level = state.get("db_compat_level")
        min_compat = entry.get("min_db_compat_level")
        if db_compat_level is not None and min_compat and db_compat_level < min_compat:
            problems.append(
                f"database compatibility level {db_compat_level} is below the"
                f" {min_compat} BigFix {bigfix_line(bigfix)} needs"
            )

    return {
        "supported": not problems,
        "problems": problems,
        "prerequisites": prerequisites,
    }


def default_target(compat: dict) -> dict:
    """Get the newest Windows Server and SQL Server the newest BigFix supports."""
    newest = max(compat["bigfix_server"], key=version_tuple)
    entry = compat["bigfix_server"][newest]
    return {
        "windows": max(entry["windows"], key=product_sort_key),
        "mssql": max(entry["mssql"], key=product_sort_key),
    }


def _bigfix_candidates(compat: dict, current: str) -> List[str]:
    """BigFix versions worth upgrading to: every minimum in the data, newest first."""
    versions = set()
    for entry in compat["bigfix_server"].values():
        versions.update(entry["windows"].values())
        versions.update(entry["mssql"].values())
    for path in compat.get("bigfix_upgrade_paths", {}).values():
        versions.add(path["min_from"])

    candidates = []
    for version in versions:
        if version_tuple(version) <= version_tuple(current):
            continue
        line = bigfix_line(version)
        if line != bigfix_line(current):
            path = compat.get("bigfix_upgrade_paths", {}).get(line)
            # an upgrade into a new line is only possible if it is listed:
            if not path or version_tuple(current) < version_tuple(path["min_from"]):
                continue
        candidates.append(version)
    return sorted(candidates, key=version_tuple, reverse=True)


def _upgrade_edges(compat: dict, state: dict) -> List[tuple]:
    """Every single component upgrade from a state: (component, to, new state)."""
    edges = []
    for version in _bigfix_candidates(compat, state["bigfix"]):
        edges.append(("bigfix", version, dict(state, bigfix=version)))

    mssql_targets = [
        target
        for target, sources in compat["mssql_upgrade_paths"]["versions"].items()
        if state["mssql"] in sources
    ]
    for target in sorted(mssql_targets, key=product_sort_key, reverse=True):
        edges.append(("mssql", target, dict(state, mssql=target)))

    windows_targets = compat["windows_upgrade_paths"]["versions"].get(
        state["windows"], []
    )
    for target in sorted(windows_targets, key=product_sort_key, reverse=True):
        edges.append(("windows", target, dict(state, windows=target)))
    return edges


def _is_goal(state: dict, target: dict) -> bool:
    if target.get("windows") and state["windows"] != target["windows"]:
        return False
    if target.get("mssql") and state["mssql"] != target["mssql"]:
        return False
    if target.get("bigfix"):
        return version_tuple(state["bigfix"]) >= version_tuple(target["bigfix"])
    return True


def _state_key(state: dict) -> tuple:
    return (state["bigfix"], state["windows"], state["mssql"])


def _core_state(state: dict) -> dict:
    return {key: state[key] for key in ("bigfix", "windows", "mssql")}


def _step_details(compat: dict, current: dict, path: List[tuple]) -> List[dict]:
    """Add the prerequisites and notes for each upgrade step of a path.

    The database compatibility level is tracked along the path, since an
    in-place SQL Server upgrade does not raise it.
    """
    max_compat = compat["mssql_max_compat_level"]["versions"]
    db_compat = current.get("db_compat_level") or max_compat.get(current["mssql"])
    mssql_level = current.get("mssql_level")
    steps = []

    for before, component, target, after in path:
        prerequisites = []
        notes = []
        if component == "mssql":
            min_level = compat["mssql_upgrade_paths"]["versions"][target][
                before["mssql"]
            ]
            if min_level != "RTM":
                level_ok = servicing_level_at_least(mssql_level, min_level)
                if level_ok is None:
                    prerequisites.append(
                        f"SQL Server {before['mssql']} must be at {min_level} or later"
                        f" to upgrade to SQL Server {target}"
                    )
                elif not level_ok:
                    prerequisites.append(
                        f"Apply SQL Server {before['mssql']} {min_level} or later first"
                        f" (currently {mssql_level})"
                    )
            mssql_level = None
            notes.extend(compat["mssql_upgrade_paths"].get("notes", []))
        elif component == "windows":
            notes.extend(compat["windows_upgrade_paths"].get("notes", []))
        elif bigfix_line(target) != bigfix_line(before["bigfix"]):
            path_info = compat["bigfix_upgrade_paths"][bigfix_line(target)]
            prerequisites.extend(path_info.get("prerequisites", []))

        # a newer SQL Server can need a service pack on this Windows version:
        os_level = (
            _mssql_on_windows(compat).get(after["mssql"], {}).get(after["windows"])
        )
        if os_level and os_level != "RTM":
            prerequisites.append(
                f"SQL Server {after['mssql']} needs {os_level} or later on"
                f" Windows Server {after['windows']}"
            )

        min_compat = (_bigfix_entry(compat, after["bigfix"]) or {}).get(
            "min_db_compat_level"
        )
        if min_compat and db_compat is not None and db_compat < min_compat:
            when = "first" if component == "bigfix" else "after this upgrade"
            prerequisites.append(
                f"Raise the compatibility level of {' and '.join(BIGFIX_DATABASES)}"
                f" to at least {min_compat} {when} (SQL Server {after['mssql']}"
                f" supports up to {max_compat.get(after['mssql'])})"
            )
            db_compat = min_compat

        steps.append(
            {
                "component": component,
                "from": before[component],
                "to": target,
                "prerequisites": prerequisites,
                "notes": notes,
            }
        )
    return steps


def find_upgrade_path(compat: dict, current: dict, target: dict) -> dict:
    """Find the shortest order of single upgrades that stays supported throughout.

    A breadth first search over (BigFix, Windows Server, SQL Server) versions,
    where every state after the current one must be a supported combination.
    The current state itself can be unsupported, it is what is being fixed.

    Arguments:
        compat: from load_compat()
        current: from current_state()
        target: `windows` and `mssql` versions to reach, and optionally the
            minimum `bigfix` version.
    Returns:
        `reachable`, the `steps` with their prerequisites, the `states` the
        path passes through, and the first steps that were `rejected` and why.
    """
    start = _core_state(current)
    result: Dict[str, Any] = {
        "current": start,
        "target": target,
        "reachable": False,
        "steps": [],
        "states": [start],
        "rejected": [],
    }
    if _is_goal(start, target):
        result["reachable"] = True
        return result

    for component, version, state in _upgrade_edges(compat, start):
        check = check_state(compat, state)
        if not check["supported"]:
            result["rejected"].append(
                {
                    "component": component,
                    "from": start[component],
                    "to": version,
                    "reasons": check["problems"],
                }
            )

    parents: Dict[tuple, Optional[tuple]] = {_state_key(start): None}
    queue = collections.deque([start])
    while queue:
        state = queue.popleft()
        for component, version, new_state in _upgrade_edges(compat, state):
            key = _state_key(new_state)
            if key in parents or not check_state(compat, new_state)["supported"]:
                continue
            parents[key] = (state, component, version)
            if _is_goal(new_state, target):
                path = []
                node = new_state
                while (parent := parents[_state_key(node)]) is not None:
                    before, step_component, step_version = parent
                    path.append((before, step_component, step_version, node))
                    node = before
                path.reverse()
                result["reachable"] = True
                result["states"] = [start] + [step[3] for step in path]
                result["steps"] = _step_details(compat, current, path)
                return result
            queue.append(new_state)

    return result


# ---------------------------------------------------------------- REST collection


def _probe(func, *args) -> Any:
    """Run one collection probe, recording an error instead of raising."""
    try:
        return func(*args)
    except Exception as err:  # pylint: disable=broad-exception-caught
        logging.warning("probe %s failed: %s", getattr(func, "__name__", func), err)
        return {"error": f"{type(err).__name__}: {err}"}


def _relevance(bes_conn, relevance: str) -> list:
    """Run session relevance, raising on a relevance error."""
    response = bes_conn.session_relevance_json(relevance)
    if response.get("error"):
        raise RuntimeError(response["error"])
    return response.get("result", [])


def _serverinfo(bes_conn) -> dict:
    return json.loads(bes_conn.get("serverinfo").text)


def _masthead(bes_conn) -> dict:
    serial, fqdn = _relevance(bes_conn, MASTHEAD_RELEVANCE)[0]
    return {"serial": serial, "fqdn": fqdn}


def _masthead_parameters(bes_conn) -> dict:
    # NOTE: needs a master operator, the error is kept if not:
    return {"xml": bes_conn.get("admin/masthead/parameters").text}


def _root_server(bes_conn) -> dict:
    rows = _relevance(bes_conn, ROOT_COMPUTER_RELEVANCE)
    if not rows:
        return {"error": "no computer has the root server flag"}
    computer_id, name, os_name, last_report = rows[0]
    properties: Dict[str, list] = {}
    for prop_name, value in _probe(_relevance, bes_conn, ROOT_PROPERTIES_RELEVANCE):
        properties.setdefault(prop_name, []).append(value)
    return {
        "id": computer_id,
        "name": name,
        "os": os_name,
        "last_report_time": last_report,
        "properties": properties,
    }


def _query_value(bes_conn, relevance: str) -> Any:
    result = _relevance(bes_conn, relevance)
    return result[0] if len(result) == 1 else result


def collect_rest_info(bes_conn) -> dict:
    """Collect what the REST API can tell about the root server, from anywhere."""
    if bes_conn is None:
        return {"skipped": "no BigFix REST connection"}

    return {
        "serverinfo": _probe(_serverinfo, bes_conn),
        "masthead": _probe(_masthead, bes_conn),
        "masthead_parameters": _probe(_masthead_parameters, bes_conn),
        "main_operator": _probe(bes_conn.am_i_main_operator),
        "root_server": _probe(_root_server, bes_conn),
        "counts": {
            query["name"]: _probe(_query_value, bes_conn, query["relevance"])
            for query in REST_QUERIES
        },
    }


# ---------------------------------------------------------------- local collection


class LocalHost:
    """The Windows host this runs on: registry, PowerShell, sqlcmd and ports.

    Tests use a fake with the same methods.
    """

    def is_windows(self) -> bool:
        """Check if running on Windows."""
        return platform.system() == "Windows"

    def is_admin(self) -> bool:
        """Check if running elevated, as an administrator."""
        try:
            import ctypes  # pylint: disable=import-outside-toplevel

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False

    def _open_key(self, path: str):
        import winreg  # pylint: disable=import-outside-toplevel,import-error

        # NOTE: the 64 bit view, so `Wow6432Node` paths mean the same thing
        # whether this python is 32 or 64 bit:
        return winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
            path,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,  # type: ignore[attr-defined]
        )

    def reg_values(self, path: str) -> Optional[dict]:
        """Get all values of an HKLM key, or None if the key doesn't exist."""
        import winreg  # pylint: disable=import-outside-toplevel,import-error

        try:
            key = self._open_key(path)
        except FileNotFoundError:
            return None
        values = {}
        with key:
            index = 0
            while True:
                try:
                    name, data, _type = winreg.EnumValue(  # type: ignore[attr-defined]
                        key, index
                    )
                except OSError:
                    break
                values[name] = data
                index += 1
        return values

    def reg_subkeys(self, path: str) -> Optional[List[str]]:
        """Get the subkey names of an HKLM key, or None if it doesn't exist."""
        import winreg  # pylint: disable=import-outside-toplevel,import-error

        try:
            key = self._open_key(path)
        except FileNotFoundError:
            return None
        names = []
        with key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))  # type: ignore[attr-defined]
                except OSError:
                    break
                index += 1
        return names

    def powershell_json(self, script: str) -> Any:
        """Run a PowerShell script that outputs JSON, and parse it."""
        result = besapi.plugin_utilities.run_logged(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        return json.loads(result.stdout) if result.stdout.strip() else None

    def sqlcmd(self, server: str, query: str) -> List[List[str]]:
        """Run a query with Windows authentication, returning rows of columns."""
        extra_paths = [
            rf"C:\Program Files\Microsoft SQL Server\{folder}\Tools\Binn\SQLCMD.EXE"
            for folder in ("170", "160", "150", "140", "130", "120", "110", "100")
        ]
        sqlcmd = besapi.plugin_utilities.find_executable("sqlcmd", extra_paths)
        if not sqlcmd:
            raise FileNotFoundError("sqlcmd was not found")
        result = besapi.plugin_utilities.run_logged(
            [sqlcmd, "-S", server, "-E", "-b", "-h", "-1", "-W", "-s", "|", "-Q", query]
        )
        return [line.split("|") for line in result.stdout.splitlines() if line.strip()]

    def port_open(self, port: int) -> bool:
        """Check if something is listening on a local TCP port."""
        try:
            with socket.create_connection(("localhost", port), timeout=3):
                return True
        except OSError:
            return False

    def file_exists(self, path: str) -> bool:
        """Check if a file exists."""
        return os.path.isfile(path)

    def run(self, cmd: List[str]) -> str:
        """Run a command, raising if it fails."""
        return besapi.plugin_utilities.run_logged(cmd).stdout


def is_local_root_server(host) -> bool:
    """Check if this is the BigFix root server: Windows, with its registry key."""
    return host.is_windows() and host.reg_values(BIGFIX_SERVER_KEY) is not None


def is_local_sql_server(server: str, hostname: str) -> bool:
    """Check if a SQL server name, maybe with an instance or port, is this host."""
    name = re.split(r"[\\,]", server.strip())[0].lower()
    return name in {".", "localhost", "(local)", "127.0.0.1", hostname.lower()}


def pending_reboot(host) -> dict:
    """Check the usual indicators of a pending reboot, which block upgrades."""
    session_manager = host.reg_values(SESSION_MANAGER_KEY) or {}
    return {
        "component_based_servicing": host.reg_values(CBS_REBOOT_PENDING_KEY)
        is not None,
        "windows_update": host.reg_values(WU_REBOOT_REQUIRED_KEY) is not None,
        "pending_file_rename": bool(session_manager.get("PendingFileRenameOperations")),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _reg_tree(host, path: str, depth: int = 2) -> Optional[dict]:
    """Dump a registry key's values and subkeys, a few levels deep."""
    values = host.reg_values(path)
    if values is None:
        return None
    tree: Dict[str, Any] = {
        "values": {name: _json_safe(data) for name, data in values.items()}
    }
    if depth > 0:
        tree["subkeys"] = {
            name: _reg_tree(host, path + "\\" + name, depth - 1)
            for name in host.reg_subkeys(path) or []
        }
    return tree


def _as_list(value: Any) -> list:
    """ConvertTo-Json gives an object for one item, a list for more."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _windows_info(host) -> dict:
    values = host.reg_values(WINDOWS_CURRENT_VERSION_KEY) or {}
    info: Dict[str, Any] = {
        name: values[name] for name in WINDOWS_VERSION_VALUES if name in values
    }
    info["pending_reboot"] = _probe(pending_reboot, host)
    info["features"] = _probe(host.powershell_json, PS_FEATURES)
    return info


def _sql_instances(host) -> dict:
    instances = {}
    for name, instance_id in (host.reg_values(SQL_INSTANCE_NAMES_KEY) or {}).items():
        setup = (
            host.reg_values(
                rf"SOFTWARE\Microsoft\Microsoft SQL Server\{instance_id}\Setup"
            )
            or {}
        )
        instances[name] = {"id": instance_id}
        instances[name].update({k: setup[k] for k in SQL_SETUP_VALUES if k in setup})
    return instances


def _bigfix_dsns(host) -> dict:
    """The ODBC data sources BigFix uses, named `bes_...`."""
    dsns = {}
    for base in (ODBC_INI_KEY, ODBC_INI_WOW_KEY):
        for name in host.reg_subkeys(base) or []:
            if name.lower().startswith("bes_"):
                dsns[f"{base}\\{name}"] = host.reg_values(base + "\\" + name) or {}
    return dsns


def _bigfix_sql_server(dsns: dict) -> Optional[str]:
    """The SQL server BigFix uses, from its bes_bfenterprise data source."""
    by_name = sorted(
        dsns.items(), key=lambda item: "bfenterprise" not in item[0].lower()
    )
    for _path, values in by_name:
        if values.get("Server"):
            return values["Server"]
    return None


def _sql_server_properties(host, server: str) -> dict:
    version, level, edition, collation, sysadmin = host.sqlcmd(
        server, SQL_SERVER_PROPERTIES
    )[0]
    return {
        "ProductVersion": version,
        "ProductLevel": level,
        "Edition": edition,
        "Collation": collation,
        "IsSysAdmin": sysadmin.strip() == "1",
    }


def _int_or_none(value: str) -> Optional[int]:
    return int(value) if value and value.strip().isdigit() else None


def _sql_databases(host, server: str) -> dict:
    databases = {}
    for name, state, recovery, compat_level, size_mb, last_backup in host.sqlcmd(
        server, SQL_DATABASES
    ):
        databases[name] = {
            "state": state,
            "recovery_model": recovery,
            "compatibility_level": _int_or_none(compat_level),
            "size_mb": _int_or_none(size_mb),
            "last_full_backup": None if last_backup == "NULL" else last_backup,
        }
    return databases


def _sql_info(host, sql_server: Optional[str], services: list) -> dict:
    dsns = _probe(_bigfix_dsns, host)
    server = (
        sql_server
        or (_bigfix_sql_server(dsns) if "error" not in dsns else None)
        or "localhost"
    )
    return {
        "instances": _probe(_sql_instances, host),
        "bigfix_dsns": dsns,
        "bigfix_sql_server": server,
        "bigfix_sql_is_local": is_local_sql_server(server, socket.gethostname()),
        "services": [s for s in services if not _is_bigfix_service(s)],
        "server_properties": _probe(_sql_server_properties, host, server),
        "databases": _probe(_sql_databases, host, server),
    }


def _is_bigfix_service(service: dict) -> bool:
    display_name = service.get("DisplayName") or ""
    return display_name.startswith("BES ") or "bigfix" in display_name.lower()


def _bigfix_info(host, services: list) -> dict:
    bigfix_services = [s for s in services if _is_bigfix_service(s)]
    install_folder = None
    for service in bigfix_services:
        if service.get("Name") == "BESRootServer" and service.get("PathName"):
            install_folder = ntpath.dirname(service["PathName"].strip().strip('"'))

    values = host.reg_values(BIGFIX_SERVER_KEY) or {}
    info: Dict[str, Any] = {
        "version": values.get("Version"),
        "install_folder": install_folder,
        "services": bigfix_services,
        "ports": {str(port): host.port_open(port) for port in PORTS},
        "registry": _probe(_reg_tree, host, BIGFIX_SERVER_KEY),
    }
    if install_folder:
        # presence only, never contents:
        info["key_files"] = {
            name: host.file_exists(ntpath.join(install_folder, name))
            for name in KEY_FILES
        }
    return info


def collect_local_info(host, sql_server: Optional[str] = None) -> dict:
    """Collect details only available on the root server itself, as admin."""
    if not is_local_root_server(host):
        return {"skipped": "not running on the BigFix root server"}
    if not host.is_admin():
        return {"skipped": "on the root server, but requires administrator rights"}

    services = _probe(host.powershell_json, PS_SERVICES)
    services = [] if isinstance(services, dict) else _as_list(services)
    return {
        "windows": _probe(_windows_info, host),
        "hardware": {
            "computer_system": _probe(host.powershell_json, PS_COMPUTER_SYSTEM),
            "operating_system": _probe(host.powershell_json, PS_OPERATING_SYSTEM),
            "disks": _probe(lambda: _as_list(host.powershell_json(PS_DISKS))),
        },
        "sql": _sql_info(host, sql_server, services),
        "bigfix": _bigfix_info(host, services),
    }


# ---------------------------------------------------------------- report

REDACTED = "<redacted>"
SECRET_KEY_PATTERN = re.compile(r"pass|pwd|secret|token|credential|private", re.I)
SECRET_IN_STRING_PATTERN = re.compile(r"(?i)\b(pwd|password)=[^;]*")
IPV4_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def redact(data: Any, hosts: Optional[List[str]] = None) -> Any:
    """Remove secrets at any depth, and optionally host names and IP addresses.

    Arguments:
        data: JSON like data.
        hosts: host names to mask. If given, IPv4 addresses are masked too.
    """
    if isinstance(data, dict):
        return {
            key: (
                REDACTED
                if SECRET_KEY_PATTERN.search(str(key))
                else redact(value, hosts)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item, hosts) for item in data]
    if isinstance(data, str):
        if data.startswith(("{obf}", "{dpapi}")):
            return REDACTED
        data = SECRET_IN_STRING_PATTERN.sub(lambda m: f"{m.group(1)}={REDACTED}", data)
        if hosts is not None:
            for host in sorted({h for h in hosts if h}, key=len, reverse=True):
                data = re.sub(rf"\b{re.escape(host)}\b", "<host>", data)
            data = IPV4_PATTERN.sub("<ip>", data)
    return data


def _get(data: Any, *keys) -> Any:
    """Get a nested value, None if any level is missing or an error."""
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def current_state(rest: dict, local: dict) -> dict:
    """Work out the current BigFix, Windows Server and SQL Server versions.

    Local details win when available, otherwise the REST API is used.
    """
    state = {
        "bigfix": _get(rest, "serverinfo", "version")
        or _get(local, "bigfix", "version"),
        "windows": windows_server_version(_get(local, "windows", "ProductName"))
        or windows_server_version(_get(rest, "root_server", "os")),
        "mssql": sql_major_from_version(
            _get(local, "sql", "server_properties", "ProductVersion")
            or _get(rest, "serverinfo", "dbVersion")
        ),
        "mssql_level": _get(local, "sql", "server_properties", "ProductLevel"),
    }
    compat_levels = [
        _get(local, "sql", "databases", name, "compatibility_level")
        for name in BIGFIX_DATABASES
    ]
    compat_levels = [level for level in compat_levels if level is not None]
    if compat_levels:
        state["db_compat_level"] = min(compat_levels)
    return {key: value for key, value in state.items() if value is not None}


def report_warnings(local: dict) -> List[str]:
    """Things to fix or know about before starting, from the local details."""
    warnings = []
    for indicator, pending in (_get(local, "windows", "pending_reboot") or {}).items():
        if pending:
            warnings.append(f"a reboot is pending ({indicator}), it blocks upgrades")
    if _get(local, "bigfix", "key_files", "license.pvk"):
        warnings.append("license.pvk is on the server, keep it offline instead")
    if _get(local, "sql", "server_properties", "IsSysAdmin") is False:
        warnings.append("the current user is not a SQL Server sysadmin")
    if _get(local, "sql", "bigfix_sql_is_local") is False:
        warnings.append("the BigFix database is on a remote SQL server")
    disks = _get(local, "hardware", "disks")
    for disk in disks if isinstance(disks, list) else []:
        if disk.get("FreeSpace") is not None and disk["FreeSpace"] < LOW_DISK_BYTES:
            warnings.append(f"low free disk space on {disk.get('DeviceID')}")
    return warnings


def build_report(
    bes_conn,
    host,
    compat: dict,
    target: Optional[dict] = None,
    sql_server: Optional[str] = None,
    redact_hosts: bool = False,
) -> dict:
    """Build the full JSON report, with the upgrade assessment, secrets removed."""
    rest = collect_rest_info(bes_conn)
    local = collect_local_info(host, sql_server)
    target = dict(
        default_target(compat), **{k: v for k, v in (target or {}).items() if v}
    )

    assessment: Dict[str, Any] = {"target": target, "warnings": report_warnings(local)}
    state = current_state(rest, local)
    assessment["current_state"] = state
    if all(state.get(key) for key in ("bigfix", "windows", "mssql")):
        assessment["current_state_check"] = check_state(compat, state)
        path = find_upgrade_path(compat, state, target)
        assessment["compatibility"] = path
        local_sql = _get(local, "sql", "bigfix_sql_is_local") is not False
        assessment["planned_steps"] = [
            {"id": step.id, "title": step.title}
            for step in build_steps(path, local_sql=local_sql)
        ]
    else:
        assessment["compatibility"] = {
            "error": "could not work out the current versions, see current_state"
        }
    assessment["sources"] = sorted(
        {url for section in compat.values() for url in _sources(section)}
    )

    report = {
        "meta": {
            "script_version": __version__,
            "besapi_version": besapi.besapi.__version__,
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "running_on_root_server": is_local_root_server(host),
            "is_admin": host.is_admin() if host.is_windows() else None,
        },
        "rest": rest,
        "local": local,
        "upgrade_assessment": assessment,
    }

    hosts = None
    if redact_hosts:
        hosts = [
            socket.gethostname(),
            _get(rest, "masthead", "fqdn"),
            _get(rest, "root_server", "name"),
            _get(local, "sql", "bigfix_sql_server"),
        ]
    return redact(report, hosts)


def _sources(section: Any) -> List[str]:
    """Source URLs anywhere in a compat data section."""
    if not isinstance(section, dict):
        return []
    urls = list(section.get("sources", []))
    for value in section.values():
        if isinstance(value, dict):
            urls.extend(_sources(value))
    return urls


# ---------------------------------------------------------------- walkthrough


@dataclasses.dataclass
class Step:
    """One step of the walkthrough.

    `actions` are done by the script, by name from ACTIONS, the instructions
    are for the operator, who confirms each step is complete.
    """

    id: str
    title: str
    instructions: str
    actions: List[str] = dataclasses.field(default_factory=list)


def _id_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _upgrade_instructions(step: dict, local_sql: bool) -> str:
    component, before, target = step["component"], step["from"], step["to"]
    lines = []
    if component == "mssql":
        if not local_sql:
            lines.append(
                "The BigFix database is on a remote SQL server: upgrade SQL Server"
                f" {before} to {target} on that host, with its own backups first."
            )
        else:
            lines.append(
                f"Upgrade SQL Server {before} to {target} in place: run the SQL"
                f" Server {target} setup.exe, choose Upgrade from a previous version,"
                " and select the instance BigFix uses."
            )
    elif component == "windows":
        lines.append(
            f"Upgrade Windows Server {before} to {target} in place: mount the"
            f" Windows Server {target} media, run setup.exe, keep files and apps,"
            " and keep the same edition and Desktop Experience. Rerun this script"
            " after the reboots."
        )
    else:
        lines.append(
            f"Upgrade the BigFix server from {before} to {target} or later, with the"
            " BigFix server installer or the upgrade Fixlet in BES Support."
        )
    lines.extend(f"Prerequisite: {item}" for item in step["prerequisites"])
    lines.extend(f"Note: {item}" for item in step["notes"])
    return "\n".join(lines)


def build_steps(path: dict, local_sql: bool = True) -> List[Step]:
    """Build the walkthrough steps for an upgrade path from find_upgrade_path()."""
    backup_actions = ["registry_export", "key_files"]
    if local_sql:
        backup_actions.append("sql_backup")

    steps = [
        Step(
            "preflight",
            "Preflight checks and baseline",
            "Review the checks. Fix any pending reboot, low disk space or failed"
            " service before continuing.",
            ["collect_baseline"],
        ),
        Step(
            "backup",
            "Back up BigFix",
            "Keys, registry and COPY_ONLY database backups go to the backup folder."
            " Copy them off this server, and keep license.pvk offline.",
            backup_actions,
        ),
    ]
    for number, step in enumerate(path["steps"], start=1):
        upgrade_id = f"upgrade_{number}_{step['component']}_{_id_part(step['to'])}"
        steps.extend(
            [
                Step(
                    f"stop_services_{number}",
                    "Stop BigFix services",
                    "BigFix services are stopped and set to Manual so they stay"
                    " stopped across reboots. Close all consoles.",
                    ["stop_services"],
                ),
                Step(
                    f"snapshot_{number}",
                    "Snapshot the VM",
                    "Take a VM snapshot now, ideally with the VM shut down, or at"
                    " least with services stopped, so SQL Server is consistent."
                    f" Name it like `before {step['component']} {step['to']}`.",
                ),
                Step(
                    upgrade_id,
                    f"Upgrade {step['component']} to {step['to']}",
                    _upgrade_instructions(step, local_sql),
                ),
                Step(
                    f"start_services_{number}",
                    "Start BigFix services",
                    "BigFix services are started again.",
                    ["start_services"],
                ),
                Step(
                    f"validate_{number}",
                    "Validate",
                    "The server is checked against the baseline. Check the console"
                    " connects and clients report before continuing.",
                    ["validate"],
                ),
            ]
        )
    steps.extend(
        [
            Step(
                "final_validation",
                "Final validation",
                "The server is checked against the baseline, and the services'"
                " original start types are restored.",
                ["validate", "restore_start_types"],
            ),
            Step(
                "cleanup",
                "Clean up",
                "After an agreed soak period, delete the VM snapshots, and securely"
                " remove old backups that are no longer needed.",
            ),
        ]
    )
    return steps


def load_state(path: str) -> dict:
    """Load walkthrough progress, or start fresh."""
    try:
        with open(path, encoding="utf-8") as state_file:
            return json.load(state_file)
    except FileNotFoundError:
        return {"done": [], "reports": {}}


def save_state(path: str, state: dict) -> None:
    """Save walkthrough progress, atomically so a crash can't corrupt it."""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2)
    os.replace(temp_path, path)


def mark_step_done(state: dict, step_id: str) -> None:
    """Record a step as complete."""
    if step_id not in state["done"]:
        state["done"].append(step_id)


def next_step(steps: List[Step], state: dict) -> Optional[Step]:
    """Get the first step not done yet, None when finished."""
    return next((step for step in steps if step.id not in state["done"]), None)


# lower stops first: front ends, then the server, the client last.
SERVICE_STOP_RANKS = [
    ("webui", 0),
    ("web reports", 1),
    ("webreports", 1),
    ("plugin portal", 2),
    ("pluginportal", 2),
    ("gather", 4),
    ("filldb", 5),
    ("root server", 6),
    ("rootserver", 6),
    ("client", 7),
]


def service_stop_order(services: List[dict]) -> List[str]:
    """Get BigFix service names in the order to stop them, reverse to start."""

    def rank(service: dict) -> int:
        names = f"{service.get('DisplayName', '')} {service.get('Name', '')}".lower()
        return next((r for keyword, r in SERVICE_STOP_RANKS if keyword in names), 3)

    return [service["Name"] for service in sorted(services, key=rank)]


def compare_baselines(before: dict, after: dict) -> List[str]:
    """Find meaningful differences between two local reports."""
    differences = []

    def services(report):
        found = _get(report, "bigfix", "services")
        return {s["Name"]: s for s in found} if isinstance(found, list) else {}

    after_services = services(after)
    for name, service in services(before).items():
        if name not in after_services:
            differences.append(f"service {name} is missing")
        elif after_services[name].get("State") != service.get("State"):
            differences.append(
                f"service {name} was {service.get('State')},"
                f" now {after_services[name].get('State')}"
            )

    after_ports = _get(after, "bigfix", "ports") or {}
    for port, listening in (_get(before, "bigfix", "ports") or {}).items():
        if after_ports.get(port) != listening:
            differences.append(
                f"port {port} was {'open' if listening else 'closed'},"
                f" now {'open' if after_ports.get(port) else 'closed'}"
            )

    after_databases = _get(after, "sql", "databases") or {}
    for name, database in (_get(before, "sql", "databases") or {}).items():
        if not isinstance(database, dict):
            continue
        if name not in after_databases:
            differences.append(f"database {name} is missing")
        elif after_databases[name].get("state") != database.get("state"):
            differences.append(
                f"database {name} was {database.get('state')},"
                f" now {after_databases[name].get('state')}"
            )

    for label, keys in (
        ("SQL Server version", ("sql", "server_properties", "ProductVersion")),
        ("Windows", ("windows", "ProductName")),
        ("Windows build", ("windows", "CurrentBuild")),
        ("BigFix version", ("bigfix", "version")),
    ):
        old, new = _get(before, *keys), _get(after, *keys)
        if old != new:
            differences.append(f"{label} changed: {old} -> {new}")
    return differences


def require_walkthrough_host(host) -> None:
    """Exit unless running on the root server as an administrator."""
    if not is_local_root_server(host):
        raise SystemExit("The walkthrough must run on the BigFix root server itself.")
    if not host.is_admin():
        raise SystemExit("The walkthrough requires an elevated administrator prompt.")


SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
UNSAFE_PATH_PATTERN = re.compile(r"['\";\[\]]")


def sql_backup_queries(database: str, backup_path: str) -> List[str]:
    """T-SQL to back up a database COPY_ONLY, so the backup chain is unchanged,
    then verify it.
    """
    if not SAFE_NAME_PATTERN.match(database):
        raise ValueError(f"unexpected database name: {database!r}")
    if UNSAFE_PATH_PATTERN.search(backup_path):
        raise ValueError(f"unexpected characters in backup path: {backup_path!r}")
    return [
        f"BACKUP DATABASE [{database}] TO DISK = N'{backup_path}'"
        " WITH COPY_ONLY, CHECKSUM, INIT, STATS = 10",
        f"RESTORE VERIFYONLY FROM DISK = N'{backup_path}' WITH CHECKSUM",
    ]


@dataclasses.dataclass
class WalkthroughContext:
    """What the walkthrough actions need."""

    args: Any
    host: Any
    state: dict
    state_path: str

    @property
    def dry_run(self) -> bool:
        """Only print what would be done."""
        return bool(self.args.dry_run)

    def execute(self, cmd: List[str]) -> None:
        """Run a command, or print it in a dry run."""
        if self.dry_run:
            print("DRY RUN, would run:", " ".join(cmd))
            return
        self.host.run(cmd)

    def sql(self, query: str) -> None:
        """Run T-SQL on the BigFix SQL server, or print it in a dry run."""
        if self.dry_run:
            print("DRY RUN, would run SQL:", query)
            return
        for row in self.host.sqlcmd(self.sql_server(), query):
            print("  ", "|".join(row))

    def sql_server(self) -> str:
        """The SQL server BigFix uses, from the baseline."""
        return _get(self.state, "baseline", "sql", "bigfix_sql_server") or "localhost"

    def backup_dir(self) -> str:
        """The backup folder, created if needed."""
        path = self.args.backup_dir
        if not path:
            raise SystemExit("--backup-dir is required for the backup step")
        os.makedirs(path, exist_ok=True)
        return path


def _action_collect_baseline(ctx: WalkthroughContext) -> None:
    local = redact(collect_local_info(ctx.host, ctx.args.sql_instance))
    ctx.state["baseline"] = local
    for warning in report_warnings(local):
        print("WARNING:", warning)


def _action_registry_export(ctx: WalkthroughContext) -> None:
    # NOTE: contains the encrypted REST password, protect the backup folder:
    out = os.path.join(ctx.backup_dir(), "bigfix_registry.reg")
    ctx.execute(["reg.exe", "export", r"HKLM\SOFTWARE\Wow6432Node\BigFix", out, "/y"])


def _action_key_files(ctx: WalkthroughContext) -> None:
    folder = _get(ctx.state, "baseline", "bigfix", "install_folder")
    if not folder:
        print("WARNING: BigFix install folder unknown, copy key files manually")
        return
    for name in KEY_FILES:
        source = ntpath.join(folder, name)
        if not ctx.host.file_exists(source):
            continue
        if name == "license.pvk":
            print("WARNING: license.pvk is on the server, keep it offline instead")
        if ctx.dry_run:
            print(f"DRY RUN, would copy {source}")
        else:
            shutil.copy2(source, ctx.backup_dir())


def _action_sql_backup(ctx: WalkthroughContext) -> None:
    databases = _get(ctx.state, "baseline", "sql", "databases") or {}
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    print(
        "NOTE: the SQL Server service account needs write access to the backup folder"
    )
    for name in BIGFIX_DATABASES:
        if name not in databases:
            continue
        path = os.path.join(ctx.backup_dir(), f"{name}_{stamp}.bak")
        for query in sql_backup_queries(name, path):
            ctx.sql(query)


def _bigfix_services(ctx: WalkthroughContext) -> List[dict]:
    services = _get(ctx.state, "baseline", "bigfix", "services")
    return services if isinstance(services, list) else []


def _action_stop_services(ctx: WalkthroughContext) -> None:
    services = _bigfix_services(ctx)
    # remember the original start types once, to restore at the end:
    ctx.state.setdefault(
        "start_modes", {s["Name"]: s.get("StartMode") for s in services}
    )
    for name in service_stop_order(services):
        ctx.execute(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Set-Service -Name '{name}' -StartupType Manual",
            ]
        )
        ctx.execute(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Stop-Service -Name '{name}' -Force",
            ]
        )


def _action_start_services(ctx: WalkthroughContext) -> None:
    for name in reversed(service_stop_order(_bigfix_services(ctx))):
        ctx.execute(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Start-Service -Name '{name}'",
            ]
        )


def _action_restore_start_types(ctx: WalkthroughContext) -> None:
    # Win32_Service StartMode names, to Set-Service StartupType names:
    startup_types = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled"}
    for name, mode in (ctx.state.get("start_modes") or {}).items():
        if mode in startup_types:
            ctx.execute(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    f"Set-Service -Name '{name}' -StartupType {startup_types[mode]}",
                ]
            )


def _action_validate(ctx: WalkthroughContext) -> None:
    local = redact(collect_local_info(ctx.host, ctx.args.sql_instance))
    ctx.state["reports"][datetime.datetime.now().isoformat()] = local
    bes_conn = besapi.plugin_utilities.get_besapi_connection(ctx.args)
    rest = collect_rest_info(bes_conn)
    if "error" in rest.get("serverinfo", {}) or "skipped" in rest:
        print("WARNING: the REST API is not answering:", rest)
    differences = compare_baselines(ctx.state.get("baseline") or {}, local)
    for difference in differences:
        print("DIFFERENCE:", difference)
    state = current_state(rest, local)
    print("current state:", json.dumps(state))


ACTIONS = {
    "collect_baseline": _action_collect_baseline,
    "registry_export": _action_registry_export,
    "key_files": _action_key_files,
    "sql_backup": _action_sql_backup,
    "stop_services": _action_stop_services,
    "start_services": _action_start_services,
    "restore_start_types": _action_restore_start_types,
    "validate": _action_validate,
}


def _ask(prompt: str, choices: List[str]) -> str:
    while True:
        answer = input(f"{prompt} [{'/'.join(choices)}]: ").strip().lower()
        if answer in choices:
            return answer


def run_walkthrough(args, bes_conn, host, compat: dict) -> int:
    """Guide the upgrade one step at a time, resuming from the state file."""
    require_walkthrough_host(host)
    state = load_state(args.state_file)

    if "plan" not in state:
        report = build_report(
            bes_conn, host, compat, _target_from_args(args), args.sql_instance
        )
        assessment = report["upgrade_assessment"]
        print(json.dumps(assessment, indent=2))
        if not _get(assessment, "compatibility", "reachable"):
            print("No supported upgrade path was found, see the assessment above.")
            return 1
        if _ask("Use this upgrade plan?", ["yes", "no"]) != "yes":
            return 1
        state["plan"] = assessment["compatibility"]
        state["local_sql"] = (
            _get(report, "local", "sql", "bigfix_sql_is_local") is not False
        )
        save_state(args.state_file, state)

    steps = build_steps(state["plan"], local_sql=state.get("local_sql", True))
    if args.step:
        ids = [step.id for step in steps]
        if args.step not in ids:
            raise SystemExit(f"unknown step {args.step}, one of: {', '.join(ids)}")
        state["done"] = ids[: ids.index(args.step)]

    ctx = WalkthroughContext(args, host, state, args.state_file)
    while True:
        step = next_step(steps, state)
        if step is None:
            print("All steps are complete.")
            return 0
        print(f"\n===== {step.id}: {step.title} =====\n{step.instructions}\n")
        for action in step.actions:
            logging.info("running action %s for step %s", action, step.id)
            ACTIONS[action](ctx)
        save_state(args.state_file, state)
        answer = _ask("Is this step complete?", ["done", "skip", "quit"])
        if answer == "quit":
            return 0
        mark_step_done(state, step.id)
        if answer == "skip":
            state.setdefault("skipped", []).append(step.id)
        save_state(args.state_file, state)


# ---------------------------------------------------------------- main


def _target_from_args(args) -> dict:
    return {
        "windows": args.target_os,
        "mssql": args.target_sql,
        "bigfix": args.target_bigfix,
    }


def build_parser():
    """The plugin args plus this script's own."""
    parser = besapi.plugin_utilities.setup_plugin_argparse(
        description="Assess or walk through a BigFix root server in-place upgrade."
    )
    parser.add_argument(
        "--walkthrough",
        action="store_true",
        help="run the upgrade walkthrough, on the root server as admin",
    )
    parser.add_argument(
        "--report-file", help="write the JSON report here instead of stdout"
    )
    parser.add_argument(
        "--redact-hosts",
        action="store_true",
        help="also mask host names and IP addresses in the report",
    )
    parser.add_argument("--target-os", help="target Windows Server version, like 2022")
    parser.add_argument("--target-sql", help="target SQL Server version, like 2022")
    parser.add_argument(
        "--target-bigfix", help="minimum target BigFix version, like 11.0.6"
    )
    parser.add_argument(
        "--compat-file",
        help=f"compatibility data, default {COMPAT_FILE_NAME} next to this script",
    )
    parser.add_argument(
        "--sql-instance", help="SQL server BigFix uses, only if discovery gets it wrong"
    )
    parser.add_argument(
        "--state-file",
        default="bigfix_root_server_upgrade_win.state.json",
        help="walkthrough progress file",
    )
    parser.add_argument("--backup-dir", help="folder for backups in the walkthrough")
    parser.add_argument(
        "--step", help="walkthrough step id to start from, redoing it and later steps"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="walkthrough prints commands instead of running them",
    )
    return parser


def main():
    """Execution starts here."""
    parser = build_parser()
    args, _unknown = parser.parse_known_args()
    host = LocalHost()

    if args.walkthrough:
        with besapi.plugin_utilities.init_plugin(
            __version__, parser, require_connection=False
        ) as (args, bes_conn):
            compat = load_compat(args.compat_file)
            return run_walkthrough(args, bes_conn, host, compat)

    # keep stdout for the JSON report only, besapi prints connection messages:
    with contextlib.redirect_stdout(sys.stderr):
        with besapi.plugin_utilities.init_plugin(
            __version__, parser, require_connection=False
        ) as (args, bes_conn):
            compat = load_compat(args.compat_file)
            report = build_report(
                bes_conn,
                host,
                compat,
                _target_from_args(args),
                args.sql_instance,
                args.redact_hosts,
            )

    output = json.dumps(report, indent=2, default=str)
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as report_file:
            report_file.write(output + "\n")
        print(f"report written to {args.report_file}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
