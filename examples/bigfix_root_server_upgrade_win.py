# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "besapi[plugins]>=4.4.1",
#     "cryptography",
#     "spake2>=0.9",
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

Share session mode (`--share-session`) checks the backup share from every
node at once, over encrypted connections (see the node channel section). Everything is
discovered where possible, and every question has a default for Enter:
- the role: the Hyper-V host coordinates, Windows servers are peers, other
  computers are consoles that watch and send commands (status, retry, done)
- the masthead serial, from REST or the local BigFix client
- the backup share: an existing one on the coordinator, or a new one on the
  drive with the most free space, reached at its address facing the root
- the root server's address, from REST or the masthead, for the firewall
- the coordinator, found by broadcast on the local subnet
- trust: a 6 digit pairing code the coordinator shows, the other nodes ask
  for it once (not needed when BIGFIX_UPGRADE_PSK is set on every node). It
  only authenticates a SPAKE2 key exchange, so every connection gets its own
  fresh key, and the coordinator stops after 5 wrong codes
The coordinator can create the folder, share, a temporary share account and
firewall rules (each confirmed first). Remove them afterwards with
`--share-cleanup` on the coordinator.

Example Usage, share session, the same on every node (coordinator as admin):
python bigfix_root_server_upgrade_win.py --share-session

Example Usage, with explicit choices instead of discovery:
python bigfix_root_server_upgrade_win.py --share-session --node coordinator
  --share-unc \\\\hyperv\\_tmp_backup --allow 192.168.5.40 --pairing-code new
python bigfix_root_server_upgrade_win.py --share-session --coordinator hyperv:52390

NOTE: On Windows Server 2012 R2 use Python 3.11, later versions don't support it.

References:
- https://support.hcl-software.com/csm?id=kb_article&sysparm_article=KB0104120
- https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/c_before_upgrading.html
- https://learn.microsoft.com/en-us/troubleshoot/sql/general/use-sql-server-in-windows
- https://learn.microsoft.com/en-us/windows-server/get-started/install-upgrade-migrate
"""

import asyncio
import collections
import contextlib
import dataclasses
import datetime
import functools
import getpass
import hashlib
import hmac
import importlib.util
import io
import ipaddress
import json
import logging
import ntpath
import os
import platform
import re
import secrets
import shutil
import socket
import string
import struct
import subprocess
import sys
import threading
import types
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, TextIO, Tuple, cast

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
    # SQL Server Agent is SQLSERVERAGENT for the default instance:
    " -or $_.Name -like 'SQL*' }"
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
# HCL's backup keeps a copy of the server's actionsite.afxm as masthead.afxm:
MASTHEAD_SOURCE = "actionsite.afxm"
SEARCHED_KEY_FILES = KEY_FILES + [MASTHEAD_SOURCE]
DEFAULT_SERVER_FOLDER = r"C:\Program Files (x86)\BigFix Enterprise\BES Server"
DEFAULT_CLIENT_FOLDER = r"C:\Program Files (x86)\BigFix Enterprise\BES Client"
CLIENT_GLOBAL_OPTIONS_KEY = (
    r"SOFTWARE\Wow6432Node\BigFix\EnterpriseClient\GlobalOptions"
)
# HCL's server backup: files and folders under the BES Server folder
# https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/c_backup_procedure_windows.html
SERVER_BACKUP_ITEMS = [
    ("BESReportsData",),
    ("BESReportsServer", "wwwroot", "ReportFiles"),
    ("Encryption Keys",),
    ("Mirror Server", "Inbox"),
    ("Mirror Server", "Config", "DownloadWhitelist.txt"),
    ("UploadManagerData",),
    ("wwwrootbes",),
]
# asked about before copying, UploadManagerData can hold years of uploads:
LARGE_BACKUP_BYTES = 5 * 1024**3
DB_INFO_TABLES = ["DBINFO", "REPLICATION_SERVERS"]
# searched below the `BigFix Enterprise` folder, skipping folders of site and
# client data that can hold many thousands of files:
KEY_FILE_SEARCH_DEPTH = 4
KEY_FILE_SKIP_FOLDERS = {
    "archivedata",
    "bfmirror",
    "bfsites",
    "bufferdir",
    "fastbufferdir",
    "sitedata",
    "uploadmanagerdata",
}
# defaults for the BigFix server, Web Reports, http and https, the real BigFix
# server and Web Reports ports are added from the registry:
PORTS = [52311, 8083, 80, 443]
LOW_DISK_BYTES = 20 * 1024**3
# allowance on top of the database sizes, for a backup with no compression:
BACKUP_SPACE_FACTOR = 1.1
LONG_UPTIME_DAYS = 30

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
        # NOTE: the registry can say 10.51.x for 2008 R2, SERVERPROPERTY 10.50.x
        return "2008 R2" if parts[1] >= 50 else "2008"
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


def parse_masthead_parameters(xml: str) -> dict:
    """Get the simple `<Name>value</Name>` masthead parameters, booleans as bool.

    A regex, not an XML parser: the document is flat, and this reads nothing
    from it but these elements.
    """
    parsed: Dict[str, Any] = {}
    for name, value in re.findall(r"<(\w+)>([^<]*)</\1>", xml):
        parsed[name] = {"true": True, "false": False}.get(value.lower(), value)
    return parsed


def _masthead_parameters(bes_conn) -> dict:
    # NOTE: needs a master operator, the error is kept if not:
    xml = bes_conn.get("admin/masthead/parameters").text
    return {"xml": xml, "parsed": parse_masthead_parameters(xml)}


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

    def walk(self, top: str):
        """Walk a folder tree like os.walk, top down so folders can be pruned."""
        return os.walk(top)

    def connect_share(self, share_root: str, user: str, password: str) -> None:
        """Connect to an SMB share with credentials, without a drive letter.

        Uses the Windows API rather than `net use`, so the password is never on
        a command line where other processes could see it.
        """
        # pylint: disable=import-outside-toplevel,import-error
        import win32netcon  # type: ignore[import-not-found]
        import win32wnet  # type: ignore[import-not-found]

        resource = win32wnet.NETRESOURCE()
        resource.dwType = win32netcon.RESOURCETYPE_DISK
        resource.lpRemoteName = share_root
        win32wnet.WNetAddConnection2(resource, password, user, 0)

    def disconnect_share(self, share_root: str) -> None:
        """Remove a connection made by connect_share()."""
        # pylint: disable=import-outside-toplevel,import-error
        import win32wnet  # type: ignore[import-not-found]

        win32wnet.WNetCancelConnection2(share_root, 0, True)

    def tcp_connect(self, server: str, port: int, timeout: float = 5) -> Optional[str]:
        """Try a TCP connection, None if it worked, otherwise the error."""
        try:
            with socket.create_connection((server, port), timeout=timeout):
                return None
        except OSError as err:
            return str(err)

    def dir_exists(self, path: str) -> bool:
        """Check if a folder exists."""
        return os.path.isdir(path)

    def make_dirs(self, path: str) -> None:
        """Create a folder and its parents."""
        os.makedirs(path, exist_ok=True)

    def write_probe(self, folder: str) -> None:
        """Check a folder can be written to."""
        check_writable(folder)

    def disk_free(self, path: str) -> int:
        """Free bytes where a path is, a local folder or a share."""
        return shutil.disk_usage(path).free

    def local_user(self, name: str) -> Optional[dict]:
        """A local account's details, None if it doesn't exist."""
        # pylint: disable=import-outside-toplevel,import-error
        import pywintypes  # type: ignore[import-not-found]
        import win32net  # type: ignore[import-not-found]

        try:
            info = win32net.NetUserGetInfo(None, name, 1)
        except pywintypes.error as err:
            if err.winerror == 2221:  # NERR_UserNotFound
                return None
            raise
        return {
            "name": info["name"],
            "comment": info.get("comment"),
            "flags": info["flags"],
        }

    def create_local_user(self, name: str, password: str, comment: str) -> None:
        """Create a local account with the Windows API, not a command line."""
        # pylint: disable=import-outside-toplevel,import-error
        import win32net  # type: ignore[import-not-found]
        import win32netcon  # type: ignore[import-not-found]

        win32net.NetUserAdd(
            None,
            1,
            {
                "name": name,
                "password": password,
                "priv": win32netcon.USER_PRIV_USER,
                "comment": comment,
                "flags": win32netcon.UF_SCRIPT
                | win32netcon.UF_DONT_EXPIRE_PASSWD
                | win32netcon.UF_PASSWD_CANT_CHANGE,
            },
        )

    def set_local_user_password(self, name: str, password: str) -> None:
        """Set a local account's password with the Windows API."""
        # pylint: disable=import-outside-toplevel,import-error
        import win32net  # type: ignore[import-not-found]

        win32net.NetUserSetInfo(None, name, 1003, {"password": password})

    def delete_local_user(self, name: str) -> None:
        """Remove a local account."""
        # pylint: disable=import-outside-toplevel,import-error
        import win32net  # type: ignore[import-not-found]

        win32net.NetUserDel(None, name)

    def run(self, cmd: List[str]) -> str:
        """Run a command, raising if it fails."""
        return besapi.plugin_utilities.run_logged(cmd).stdout

    def run_secret(self, cmd: List[str]) -> None:
        """Run a command with a secret in its arguments, logging none of it."""
        result = subprocess.run(  # nosec B603
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode:
            # NOTE: the arguments and output could hold the secret, never shown:
            raise RuntimeError(
                f"{os.path.basename(cmd[0])} failed with exit code {result.returncode}"
            )


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
    """The ODBC data sources BigFix uses: named `bes_...`, or named in its registry.

    Web Reports uses one named by `FillAggregateDB\\LocalDBDSN`, for example
    `LocalBESReportingServer`.
    """
    aggregate = host.reg_values(BIGFIX_SERVER_KEY + r"\FillAggregateDB") or {}
    named = (
        {str(aggregate["LocalDBDSN"]).lower()} if aggregate.get("LocalDBDSN") else set()
    )
    dsns = {}
    for base in (ODBC_INI_KEY, ODBC_INI_WOW_KEY):
        for name in host.reg_subkeys(base) or []:
            if name.lower().startswith("bes_") or name.lower() in named:
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
    sql_services = [s for s in services if _is_sql_service(s)]
    return {
        "instances": _probe(_sql_instances, host),
        "bigfix_dsns": dsns,
        "bigfix_sql_server": server,
        "bigfix_sql_is_local": is_local_sql_server(server, socket.gethostname()),
        "services": sql_services,
        "agents": [s for s in sql_services if _is_sql_agent(s)],
        "server_properties": _probe(_sql_server_properties, host, server),
        "databases": _probe(_sql_databases, host, server),
    }


def _is_bigfix_service(service: dict) -> bool:
    display_name = service.get("DisplayName") or ""
    return display_name.startswith("BES ") or "bigfix" in display_name.lower()


def _is_sql_service(service: dict) -> bool:
    name = (service.get("Name") or "").upper()
    return name.startswith(("MSSQL", "SQL")) and not _is_bigfix_service(service)


def _is_sql_agent(service: dict) -> bool:
    """SQL Server Agent: SQLSERVERAGENT, or SQLAgent$<instance> for named ones."""
    name = (service.get("Name") or "").upper()
    return name == "SQLSERVERAGENT" or name.startswith("SQLAGENT$")


def _bigfix_ports(host) -> dict:
    """Check the default ports plus the BigFix and Web Reports ports it is set to."""
    ports = list(PORTS)
    server_port = (host.reg_values(BIGFIX_SERVER_KEY) or {}).get("Port")
    web_reports_url = (host.reg_values(BIGFIX_SERVER_KEY + r"\BESReports") or {}).get(
        "WRHTTP"
    )
    try:
        if server_port:
            ports.append(int(server_port))
        if web_reports_url and urllib.parse.urlsplit(web_reports_url).port:
            ports.append(urllib.parse.urlsplit(web_reports_url).port)
    except ValueError:
        logging.warning("unexpected port in the BigFix registry, using defaults")
    # unique, in order:
    return {str(port): host.port_open(port) for port in dict.fromkeys(ports)}


def _key_file_folders(values: dict, install_folder: Optional[str]) -> List[str]:
    """Where to look for key files: all of `BigFix Enterprise` if that's the parent."""
    server_folder = str(values.get("EnterpriseServerFolder") or install_folder or "")
    server_folder = server_folder.rstrip("\\")
    if not server_folder:
        return []
    parent = ntpath.dirname(server_folder)
    if ntpath.basename(parent).lower() == "bigfix enterprise":
        return [parent]
    folders = [server_folder]
    www_folder = str(values.get("wwwRootFolder") or "").rstrip("\\")
    if www_folder and not www_folder.lower().startswith(server_folder.lower() + "\\"):
        folders.append(www_folder)
    return folders


def find_key_files(
    host, folders: List[str], max_depth: int = KEY_FILE_SEARCH_DEPTH
) -> dict:
    """Find the masthead and license files by name, reporting paths only."""
    found: Dict[str, List[str]] = {name: [] for name in SEARCHED_KEY_FILES}
    other_afxm = []
    for root in folders:
        for dirpath, dirnames, filenames in host.walk(root):
            relative = dirpath[len(root) :].strip("\\")
            depth = len(relative.split("\\")) if relative else 0
            # prune in place, so the walk doesn't descend into these:
            dirnames[:] = (
                []
                if depth >= max_depth
                else [d for d in dirnames if d.lower() not in KEY_FILE_SKIP_FOLDERS]
            )
            for filename in filenames:
                path = ntpath.join(dirpath, filename)
                if filename.lower() in found:
                    found[filename.lower()].append(path)
                elif filename.lower().endswith(".afxm"):
                    other_afxm.append(path)
    return {
        "searched": folders,
        "found": {name: sorted(paths) for name, paths in found.items()},
        "other_afxm": sorted(other_afxm)[:20],
    }


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
        "ports": _probe(_bigfix_ports, host),
        "registry": _probe(_reg_tree, host, BIGFIX_SERVER_KEY),
        # paths only, never contents:
        "key_files": _probe(
            find_key_files, host, _key_file_folders(values, install_folder)
        ),
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
SECRET_KEY_PATTERN = re.compile(r"pass|pwd|secret|token|credential|private.?key", re.I)
# flags about a secret, like `SOAPPasswordIsEncrypted`, not the secret itself:
SECRET_FLAG_KEY_PATTERN = re.compile(r"(IsEncrypted|Encryption)$", re.I)
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
                and not SECRET_FLAG_KEY_PATTERN.search(str(key))
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


def parse_windows_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    """Parse a PowerShell `ToString('o')` time, which has 7 fractional digits.

    A time with no offset is taken as UTC.
    """
    match = re.match(
        r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$",
        str(value or ""),
    )
    if not match:
        return None
    base, fraction, offset = match.groups()
    fraction = ((fraction or "") + "000000")[:6]
    offset = "+00:00" if offset in (None, "Z") else offset
    return datetime.datetime.fromisoformat(f"{base}.{fraction}{offset}")


def _bigfix_sql_instance(local: dict) -> Optional[dict]:
    """The registry details of the SQL instance BigFix uses, if it is local."""
    server = _get(local, "sql", "bigfix_sql_server") or ""
    name = server.split("\\", 1)[1] if "\\" in server else "MSSQLSERVER"
    instances = _get(local, "sql", "instances") or {}
    for instance_name, instance in instances.items():
        if instance_name.lower() == name.lower() and isinstance(instance, dict):
            return instance
    return None


def _bigfix_database_bytes(local: dict) -> int:
    databases = _get(local, "sql", "databases") or {}
    return (
        sum(
            (databases.get(name) or {}).get("size_mb") or 0
            for name in BIGFIX_DATABASES
            if isinstance(databases.get(name), dict)
        )
        * 1024**2
    )


def report_warnings(local: dict, now: Optional[datetime.datetime] = None) -> List[str]:
    """Things to fix or know about before starting, from the local details."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    warnings = []
    for indicator, pending in (_get(local, "windows", "pending_reboot") or {}).items():
        if pending:
            warnings.append(f"a reboot is pending ({indicator}), it blocks upgrades")

    boot = parse_windows_datetime(
        _get(local, "hardware", "operating_system", "LastBootUpTime")
    )
    if boot and (now - boot).days > LONG_UPTIME_DAYS:
        warnings.append(
            f"the server has been up for {(now - boot).days} days: reboot it and"
            " check it comes back cleanly before starting"
        )

    pvk_paths = _get(local, "bigfix", "key_files", "found", "license.pvk")
    if pvk_paths:
        warnings.append(
            f"license.pvk is on the server ({', '.join(pvk_paths)}), keep it offline"
        )
    found = _get(local, "bigfix", "key_files", "found") or {}
    # the backup copies the server's actionsite.afxm as masthead.afxm:
    if found.get("masthead.afxm") == [] and found.get(MASTHEAD_SOURCE) == []:
        warnings.append(
            "no masthead.afxm or actionsite.afxm was found on the server,"
            " have a masthead copy before the backup"
        )

    if _get(local, "sql", "server_properties", "IsSysAdmin") is False:
        warnings.append("the current user is not a SQL Server sysadmin")
    if _get(local, "sql", "bigfix_sql_is_local") is False:
        warnings.append("the BigFix database is on a remote SQL server")
    edition = _get(local, "sql", "server_properties", "Edition") or ""
    if "developer" in edition.lower() or "evaluation" in edition.lower():
        warnings.append(
            f"SQL Server is {edition}: an in-place upgrade keeps the edition, check"
            " licensing with your license owner before relying on it in production"
        )

    databases = _get(local, "sql", "databases") or {}
    for name in BIGFIX_DATABASES:
        database = databases.get(name)
        if isinstance(database, dict) and not database.get("last_full_backup"):
            warnings.append(f"{name} has never had a full backup, according to msdb")

    dsns = _get(local, "sql", "bigfix_dsns") or {}
    drivers = sorted(
        {
            ntpath.basename(str(values.get("Driver")))
            for values in dsns.values()
            if isinstance(values, dict)
            and "sqlncli" in str(values.get("Driver")).lower()
        }
    )
    if drivers:
        warnings.append(
            f"BigFix connects with SQL Server Native Client ({', '.join(drivers)}),"
            " per HCL BigFix 11 Patch 8 and later need Microsoft ODBC Driver 17"
        )

    disks = _get(local, "hardware", "disks")
    disks = disks if isinstance(disks, list) else []
    for disk in disks:
        if disk.get("FreeSpace") is not None and disk["FreeSpace"] < LOW_DISK_BYTES:
            warnings.append(f"low free disk space on {disk.get('DeviceID')}")

    # a full backup next to the databases needs about their size again:
    data_root = (_bigfix_sql_instance(local) or {}).get("SQLDataRoot") or ""
    data_drive = ntpath.splitdrive(data_root)[0].upper()
    database_bytes = _bigfix_database_bytes(local)
    for disk in disks:
        free = disk.get("FreeSpace")
        if str(disk.get("DeviceID")).upper() != data_drive or free is None:
            continue
        needed = database_bytes * BACKUP_SPACE_FACTOR + LOW_DISK_BYTES
        if free < needed:
            warnings.append(
                f"not enough free space on {data_drive} for a full backup of the"
                f" BigFix databases (about {needed / 1024**3:.0f} GB needed,"
                f" {free / 1024**3:.0f} GB free): back up to another volume or a share"
            )
    return warnings


def mark_met_prerequisites(path: dict, rest: dict) -> None:
    """Mark upgrade prerequisites the current server already meets, in place."""
    parameters = _get(rest, "masthead_parameters", "parsed") or {}
    if parameters.get("Enhanced91Security") is not True:
        return
    for step in path.get("steps", []):
        step["prerequisites"] = [
            (
                "Enhanced Security is already enabled (masthead Enhanced91Security)"
                if prerequisite.startswith("Enable Enhanced Security")
                else prerequisite
            )
            for prerequisite in step["prerequisites"]
        ]


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
        mark_met_prerequisites(path, rest)
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
    """Build the walkthrough steps for an upgrade path from find_upgrade_path().

    BigFix is stopped before the backup, as HCL's backup procedure says, and
    stays stopped for the first snapshot.
    """
    backup_actions = ["registry_export", "key_files", "masthead", "client_data"]
    backup_actions += ["folder_backup", "db_info"]
    if local_sql:
        backup_actions.append("sql_backup")
    backup_actions += ["server_keys", "restore_notes"]

    steps = [
        Step(
            "preflight",
            "Preflight checks and baseline",
            "Review the checks. Fix any pending reboot, low disk space or failed"
            " service before continuing.",
            ["collect_baseline"],
        ),
        Step(
            "stop_services_0",
            "Stop BigFix for the backup",
            "BigFix services are stopped in HCL's order and set to Manual. Close"
            " all consoles, and stop remote WebUI or Web Reports servers.",
            ["remote_processes", "stop_services"],
        ),
        Step(
            "backup",
            "Back up BigFix",
            "Following HCL's server backup: registry, keys, masthead, the server's"
            " own client identity, server folders, DB info, COPY_ONLY database"
            " backups, and optionally the decrypted server keys. RESTORE_NOTES.txt"
            " in the backup folder says how to restore. Copy the backups off this"
            " server, and keep license.pvk offline.",
            backup_actions,
        ),
    ]
    for number, step in enumerate(path["steps"], start=1):
        upgrade_id = f"upgrade_{number}_{step['component']}_{_id_part(step['to'])}"
        if number > 1:
            steps.append(
                Step(
                    f"stop_services_{number}",
                    "Stop BigFix services",
                    "BigFix services are stopped and set to Manual so they stay"
                    " stopped across reboots. Close all consoles.",
                    ["stop_services"],
                )
            )
        steps.extend(
            [
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
                " remove old backups that are no longer needed, especially the"
                " decrypted server keys and client KeyStorage.",
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


# HCL's backup procedure stops WebUI, Web Reports, Client, GatherDB, FillDB,
# then the Root Server. Lower stops first, any other BES service before them:
SERVICE_STOP_RANKS = [
    ("webui", 0),
    ("web reports", 1),
    ("webreports", 1),
    ("client", 2),
    ("gather", 3),
    ("filldb", 4),
    ("root server", 5),
    ("rootserver", 5),
]


def service_stop_order(services: List[dict]) -> List[str]:
    """Get BigFix service names in the order to stop them, reverse to start."""

    def rank(service: dict) -> int:
        names = f"{service.get('DisplayName', '')} {service.get('Name', '')}".lower()
        return next((r for keyword, r in SERVICE_STOP_RANKS if keyword in names), -1)

    return [service["Name"] for service in sorted(services, key=rank)]


def services_to_start(services: List[dict]) -> List[str]:
    """Services to start again: only those running at baseline, reverse stop order."""
    running = [service for service in services if service.get("State") == "Running"]
    return list(reversed(service_stop_order(running)))


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


def supports_backup_compression(mssql: Optional[str], edition: Optional[str]) -> bool:
    """Check if this SQL Server version and edition can create compressed backups.

    SQL Server 2008 only in Enterprise and Developer, 2008 R2 and later also in
    Standard. Never in Express, Web or Workgroup.
    """
    if not mssql or not edition:
        return False
    edition = edition.lower()
    editions = ["enterprise", "developer"]
    if mssql != "2008":
        editions += ["standard", "datacenter"]
    return any(name in edition for name in editions)


def sql_backup_queries(
    database: str, backup_path: str, compression: bool = False
) -> List[str]:
    """T-SQL to back up a database COPY_ONLY, so the backup chain is unchanged,
    then verify it.
    """
    if not SAFE_NAME_PATTERN.match(database):
        raise ValueError(f"unexpected database name: {database!r}")
    if UNSAFE_PATH_PATTERN.search(backup_path):
        raise ValueError(f"unexpected characters in backup path: {backup_path!r}")
    options = "COPY_ONLY, CHECKSUM, INIT, STATS = 10"
    if compression:
        options += ", COMPRESSION"
    return [
        f"BACKUP DATABASE [{database}] TO DISK = N'{backup_path}' WITH {options}",
        f"RESTORE VERIFYONLY FROM DISK = N'{backup_path}' WITH CHECKSUM",
    ]


class BackupError(Exception):
    """A backup could not be made or verified."""


def is_unc_path(path: str) -> bool:
    """Check if a path is on an SMB share, like `\\\\server\\share\\folder`."""
    return path.startswith("\\\\")


def unc_share_root(path: str) -> Optional[str]:
    """Get the `\\\\server\\share` part of a UNC path, None if not UNC."""
    if not is_unc_path(path):
        return None
    server, share = path[2:].split("\\")[:2]
    return f"\\\\{server}\\{share}"


def backup_run_folder(base: str, hostname: str, now: datetime.datetime) -> str:
    """A folder per run under the backup location, so runs never overwrite."""
    return os.path.join(base, f"{hostname}_{now:%Y%m%d_%H%M%S}")


def same_volume(path_a: str, path_b: str) -> bool:
    """Check if two Windows paths are on the same local drive."""
    drive_a = ntpath.splitdrive(path_a)[0].upper()
    return (
        bool(drive_a)
        and not is_unc_path(path_a)
        and (drive_a == ntpath.splitdrive(path_b)[0].upper())
    )


def default_staging_dir(local: dict) -> Optional[str]:
    """The Backup folder of the SQL instance BigFix uses, which it can write to."""
    data_root = (_bigfix_sql_instance(local) or {}).get("SQLDataRoot")
    return ntpath.join(data_root, "Backup") if data_root else None


def check_writable(folder: str) -> None:
    """Check a folder can be written to, by writing and removing a test file."""
    probe = os.path.join(folder, f"bigfix_upgrade_write_test_{os.getpid()}.tmp")
    with open(probe, "w", encoding="utf-8") as probe_file:
        probe_file.write("write test\n")
    os.remove(probe)


def sha256_file(path: str) -> str:
    """The SHA-256 of a file, read in chunks since backups are large."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source: str, dest_folder: str) -> dict:
    """Copy a file, compare SHA-256 hashes, and only then remove the source."""
    dest = os.path.join(dest_folder, os.path.basename(source))
    shutil.copyfile(source, dest)
    source_hash, dest_hash = sha256_file(source), sha256_file(dest)
    if source_hash != dest_hash:
        raise BackupError(
            f"copy of {source} to {dest} does not match, the staging copy is kept"
        )
    os.remove(source)
    return {"file": dest, "sha256": dest_hash}


def choose_backup_mode(
    host, server: str, folder: str, compression: bool = False
) -> str:
    """Check if SQL Server itself can write backups to a folder.

    A tiny backup of `model` is tried there: `direct` if it works, otherwise
    `staged`, backing up locally and copying as the operator. The SQL Server
    service account often can't reach a share, like NETWORKSERVICE on a
    workgroup server.
    """
    probe = os.path.join(folder, f"bigfix_upgrade_probe_{os.getpid()}.bak")
    query = sql_backup_queries("model", probe, compression)[0]
    try:
        host.sqlcmd(server, query)
        return "direct"
    except Exception as err:  # pylint: disable=broad-exception-caught
        logging.info("SQL Server can't back up to %s, staging instead: %s", folder, err)
        return "staged"
    finally:
        with contextlib.suppress(OSError):
            os.remove(probe)


def connect_backup_share(ctx, getpass_fn=getpass.getpass) -> None:
    """Connect to the backup share with --backup-share-user, if one was given.

    The password is only passed to the connection, never kept or logged.
    """
    root = unc_share_root(ctx.args.backup_dir or "")
    user = ctx.args.backup_share_user
    if not root or not user:
        return
    ctx.host.connect_share(root, user, getpass_fn(f"Password for {user} on {root}: "))
    logging.info("connected to %s as %s", root, user)


@dataclasses.dataclass
class WalkthroughContext:
    """What the walkthrough actions need."""

    args: Any
    host: Any
    state: dict
    state_path: str
    # ask the operator, replaceable in tests:
    ask: Any = None
    input_fn: Any = None
    getpass_fn: Any = None
    share_connected: bool = False

    def confirm(self, prompt: str, default: str = "yes") -> bool:
        """Ask the operator to confirm, yes or no, Enter takes the default."""
        return (self.ask or _ask)(prompt, ["yes", "no"], default) == "yes"

    def read(self, prompt: str) -> str:
        """Ask the operator for a value."""
        return (self.input_fn or input)(prompt).strip()

    def read_secret(self, prompt: str) -> str:
        """Ask the operator for a secret, not echoed."""
        return (self.getpass_fn or getpass.getpass)(prompt)

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
        """This run's folder under --backup-dir, a local folder or UNC share path.

        Connects to the share first if --backup-share-user was given, and checks
        the folder can be written to. The folder is kept in the state file, so a
        resumed walkthrough keeps using it.
        """
        if not self.args.backup_dir:
            raise SystemExit("--backup-dir is required for the backup step")
        if not self.share_connected:
            if self.dry_run:
                if self.args.backup_share_user:
                    print(f"DRY RUN, would connect as {self.args.backup_share_user}")
            else:
                connect_backup_share(self)
            self.share_connected = True
        run_dir = self.state.get("backup_run_dir")
        if not run_dir:
            run_dir = backup_run_folder(
                self.args.backup_dir, socket.gethostname(), datetime.datetime.now()
            )
            self.state["backup_run_dir"] = run_dir
        if not self.dry_run and not os.path.isdir(run_dir):
            os.makedirs(run_dir)
            check_writable(run_dir)
        return run_dir


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
    found = _get(ctx.state, "baseline", "bigfix", "key_files", "found") or {}
    dest = os.path.join(ctx.backup_dir(), "key_files")
    for name in KEY_FILES:
        paths = found.get(name)
        paths = paths if isinstance(paths, list) else []
        if not paths:
            print(f"NOTE: {name} was not found on this server, add your copy to {dest}")
        if name == "license.pvk" and paths:
            print("WARNING: license.pvk is on the server, keep it offline instead")
        for index, source in enumerate(paths, start=1):
            # several copies can exist, keep them all apart, and the masthead
            # copy HCL asks for (from actionsite.afxm) is masthead.afxm:
            if name == "masthead.afxm":
                target = os.path.join(dest, f"found_{index}_{name}")
            else:
                target = os.path.join(
                    dest, name if len(paths) == 1 else f"{index}_{name}"
                )
            if ctx.dry_run:
                print(f"DRY RUN, would copy {source} to {target}")
                continue
            if not os.path.isfile(source):
                print(f"NOTE: {source} is gone since the baseline, not copied")
                continue
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(source, target)
            ctx.state.setdefault("key_file_copies", {})[target] = source


def _action_sql_backup(ctx: WalkthroughContext) -> None:
    baseline = ctx.state.get("baseline") or {}
    databases = _get(baseline, "sql", "databases") or {}
    names = [name for name in BIGFIX_DATABASES if name in databases]
    properties = _get(baseline, "sql", "server_properties") or {}
    compression = supports_backup_compression(
        sql_major_from_version(properties.get("ProductVersion")),
        properties.get("Edition"),
    )
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ctx.backup_dir()
    server = ctx.sql_server()

    if ctx.dry_run:
        for name in names:
            path = os.path.join(run_dir, f"{name}_{stamp}.bak")
            for query in sql_backup_queries(name, path, compression):
                print("DRY RUN, would run SQL:", query)
        return

    mode = choose_backup_mode(ctx.host, server, run_dir, compression)
    print(f"SQL Server backups: {mode}, compression {'on' if compression else 'off'}")
    folders = [run_dir]
    staging = None
    if mode == "staged":
        staging = ctx.args.staging_dir or default_staging_dir(baseline)
        if not staging:
            raise BackupError(
                "SQL Server can't write to the backup folder, give --staging-dir"
            )
        print(f"staging in {staging}, then copying as {getpass.getuser()}")
        folders.append(staging)

    # worst case, no compression:
    needed = _bigfix_database_bytes(baseline) * BACKUP_SPACE_FACTOR
    for folder in folders:
        free = shutil.disk_usage(folder).free
        if free < needed:
            print(
                f"WARNING: {folder} has {free / 1024**3:.0f} GB free, the databases"
                f" are about {needed / 1024**3:.0f} GB uncompressed"
            )
            if not ctx.confirm("Continue with the backup anyway?"):
                raise BackupError(f"not enough free space in {folder}")
    data_root = (_bigfix_sql_instance(baseline) or {}).get("SQLDataRoot") or ""
    if same_volume(run_dir, data_root):
        print("WARNING: the backups are on the same volume as the databases")

    backups = ctx.state.setdefault("backups", [])
    for name in names:
        file_name = f"{name}_{stamp}.bak"
        path = os.path.join(staging if staging else run_dir, file_name)
        for query in sql_backup_queries(name, path, compression):
            ctx.sql(query)
        record = copy_verified(path, run_dir) if staging else {"file": path}
        record.update(database=name, mode=mode, compression=compression)
        backups.append(record)
        save_state(ctx.state_path, ctx.state)


def _server_folder(baseline: dict) -> str:
    """The BES Server folder: from the registry, the root server service, or HCL's
    default.
    """
    folder = (
        _get(baseline, "bigfix", "registry", "values", "EnterpriseServerFolder")
        or _get(baseline, "bigfix", "install_folder")
        or DEFAULT_SERVER_FOLDER
    )
    return str(folder).rstrip("\\/")


def _client_folder(baseline: dict) -> str:
    """The BES Client folder, from the BESClient service, or the default."""
    services = _get(baseline, "bigfix", "services")
    for service in services if isinstance(services, list) else []:
        if service.get("Name") == "BESClient" and service.get("PathName"):
            return os.path.dirname(str(service["PathName"]).strip().strip('"'))
    return DEFAULT_CLIENT_FOLDER


def server_backup_items(baseline: dict) -> List[tuple]:
    """HCL's server backup items as (label, full path), in the server folder."""
    server = _server_folder(baseline)
    return [
        ("/".join(parts), os.path.join(server, *parts)) for parts in SERVER_BACKUP_ITEMS
    ]


def _tree_size(path: str) -> tuple:
    """(files, bytes) of a file or a folder tree."""
    if os.path.isfile(path):
        return 1, os.path.getsize(path)
    files = size = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            with contextlib.suppress(OSError):
                size += os.path.getsize(os.path.join(dirpath, filename))
                files += 1
    return files, size


def _copy_item(source: str, dest: str) -> None:
    if os.path.isdir(source):
        shutil.copytree(source, dest, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(source, dest)


def _action_remote_processes(ctx: WalkthroughContext) -> None:
    if not ctx.confirm(
        "Are remote WebUI and Web Reports servers that use the BigFix databases"
        " stopped? (Enter if there are none)"
    ):
        print("Stop them before continuing, HCL's backup procedure needs them stopped.")


def _action_folder_backup(ctx: WalkthroughContext) -> None:
    run_dir = ctx.backup_dir()
    dest_root = os.path.join(run_dir, "server_files")
    record = ctx.state.setdefault("server_files", {})
    chosen = []
    for label, path in server_backup_items(ctx.state.get("baseline") or {}):
        if not os.path.exists(path):
            record[label] = {"missing": True}
            continue
        files, size = _tree_size(path)
        if size > LARGE_BACKUP_BYTES and not ctx.confirm(
            f"{label} is {size / 1024**3:.1f} GB in {files} files, back it up too?"
        ):
            record[label] = {"skipped": True, "files": files, "bytes": size}
            continue
        chosen.append((label, path, files, size))

    needed = sum(size for *_rest, size in chosen) * BACKUP_SPACE_FACTOR
    if not ctx.dry_run:
        free = shutil.disk_usage(run_dir).free
        if free < needed and not ctx.confirm(
            f"only {free / 1024**3:.1f} GB free for about {needed / 1024**3:.1f} GB"
            " of server files, continue anyway?",
            default="no",
        ):
            raise BackupError(
                f"not enough free space in {run_dir} for the server files"
            )

    for label, path, files, size in chosen:
        dest = os.path.join(dest_root, *label.split("/"))
        if ctx.dry_run:
            print(f"DRY RUN, would copy {path} ({files} files) to {dest}")
            continue
        _copy_item(path, dest)
        record[label] = {"files": files, "bytes": size, "copied_to": dest}
        save_state(ctx.state_path, ctx.state)


def _action_masthead(ctx: WalkthroughContext) -> None:
    sources = _get(
        ctx.state, "baseline", "bigfix", "key_files", "found", MASTHEAD_SOURCE
    )
    if not sources:
        print(f"NOTE: {MASTHEAD_SOURCE} was not found, keep your own masthead copy")
        return
    # the server's own copy, nearest the top of the BES Server folder:
    server = _server_folder(ctx.state.get("baseline") or {}).lower()

    def preference(path: str) -> tuple:
        in_server = path.lower().startswith(server + "\\") or path.lower().startswith(
            server + "/"
        )
        return (not in_server, path.count("\\") + path.count("/"), path)

    source = min(sources, key=preference)
    dest = os.path.join(ctx.backup_dir(), "key_files", "masthead.afxm")
    if ctx.dry_run:
        print(f"DRY RUN, would copy {source} to {dest}")
        return
    _copy_item(source, dest)
    ctx.state["masthead_copy"] = {"from": source, "to": dest}


def _action_client_data(ctx: WalkthroughContext) -> None:
    """The root server's own client identity, so its restore makes no duplicate.

    See https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/t_preserving_bundling_when_clientreinstalled.html
    """
    folder = os.path.join(ctx.backup_dir(), "client_data")
    values = ctx.host.reg_values(CLIENT_GLOBAL_OPTIONS_KEY) or {}
    computer_id = values.get("ComputerID")
    key_storage = os.path.join(
        _client_folder(ctx.state.get("baseline") or {}), "KeyStorage"
    )
    ctx.execute(
        [
            "reg.exe",
            "export",
            "HKLM\\" + CLIENT_GLOBAL_OPTIONS_KEY,
            os.path.join(folder, "GlobalOptions.reg"),
            "/y",
        ]
    )
    if ctx.dry_run:
        print(f"DRY RUN, would save ComputerID {computer_id} and copy {key_storage}")
        return
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "ComputerID.txt"), "w", encoding="utf-8") as saved:
        saved.write(f"{computer_id}\n")
    copied = os.path.isdir(key_storage)
    if copied:
        _copy_item(key_storage, os.path.join(folder, "KeyStorage"))
    else:
        print(f"WARNING: {key_storage} was not found")
    ctx.state["client_data"] = {"computer_id": computer_id, "key_storage": copied}
    print(
        "NOTE: before restoring this client, set ClientIdentityMatch to 100 in the"
        " BigFix Administrative Tool, Advanced Options"
    )


def table_columns_query(table: str) -> str:
    """T-SQL for the column names of a BFEnterprise table from DB_INFO_TABLES."""
    if table not in DB_INFO_TABLES:
        raise ValueError(f"unexpected table: {table!r}")
    return (
        "SET NOCOUNT ON; SELECT name FROM BFEnterprise.sys.columns WHERE object_id ="
        f" OBJECT_ID('BFEnterprise.dbo.{table}') ORDER BY column_id"
    )


def table_rows_query(table: str) -> str:
    """T-SQL for all rows of a BFEnterprise table from DB_INFO_TABLES."""
    if table not in DB_INFO_TABLES:
        raise ValueError(f"unexpected table: {table!r}")
    return f"SET NOCOUNT ON; SELECT * FROM [BFEnterprise].[dbo].[{table}]"


def _action_db_info(ctx: WalkthroughContext) -> None:
    """Record DBINFO and REPLICATION_SERVERS, as HCL's backup says, to check a
    restore.
    """
    path = os.path.join(ctx.backup_dir(), "db_info.json")
    if ctx.dry_run:
        for table in DB_INFO_TABLES:
            print("DRY RUN, would run SQL:", table_rows_query(table))
        return
    info: Dict[str, Any] = {}
    for table in DB_INFO_TABLES:
        try:
            columns = ctx.host.sqlcmd(ctx.sql_server(), table_columns_query(table))
            rows = ctx.host.sqlcmd(ctx.sql_server(), table_rows_query(table))
            info[table] = {"columns": [row[0] for row in columns], "rows": rows}
        except Exception as err:  # pylint: disable=broad-exception-caught
            info[table] = {"error": str(err)}
    with open(path, "w", encoding="utf-8") as saved:
        json.dump(info, saved, indent=2)


def _action_server_keys(ctx: WalkthroughContext) -> None:
    """Optionally decrypt the server's Encrypted* key files with ServerKeyTool.

    It needs license.pvk and its password, which ServerKeyTool only takes on its
    command line: it's run without logging, and the password is never kept.
    """
    server = _server_folder(ctx.state.get("baseline") or {})
    tool = os.path.join(server, "ServerKeyTool.exe")
    if not os.path.isfile(tool):
        ctx.state["server_keys"] = {"error": f"ServerKeyTool.exe not found in {server}"}
        print(f"NOTE: ServerKeyTool.exe not found in {server}, key files not decrypted")
        return
    if not ctx.confirm(
        "Decrypt the server key files with ServerKeyTool, as HCL's backup does?"
        " It needs license.pvk and its password",
        default="no",
    ):
        ctx.state["server_keys"] = {"skipped": True}
        return
    pvk = ctx.read("Path to license.pvk, like a USB drive: ").strip('"')
    if not pvk:
        ctx.state["server_keys"] = {"skipped": True}
        return
    out = os.path.join(ctx.backup_dir(), "server_keys")
    arguments = [
        "/decrypt",
        f"/dirIn:{server}",
        f"/dirOut:{out}",
        f"/sitePvkLocation:{pvk}",
    ]
    if ctx.dry_run:
        print("DRY RUN, would run:", tool, *arguments, "/sitePvkPassword:<password>")
        return
    command = [
        tool,
        *arguments,
        "/sitePvkPassword:" + ctx.read_secret("license.pvk password: "),
    ]
    os.makedirs(out, exist_ok=True)
    try:
        ctx.host.run_secret(command)
    except Exception as err:  # pylint: disable=broad-exception-caught
        ctx.state["server_keys"] = {"error": str(err)}
        print(f"WARNING: ServerKeyTool failed: {err}")
        return
    finally:
        del command
    ctx.state["server_keys"] = {"decrypted_to": out}
    print(
        f"WARNING: {out} now holds decrypted server keys, as sensitive as"
        " license.pvk: keep the backup secure, and remove them when done"
    )


def _action_restore_notes(ctx: WalkthroughContext) -> None:
    """Write RESTORE_NOTES.txt: what was backed up, and HCL's recovery steps.

    Only from the state file, which never holds secrets.
    """
    state = ctx.state
    run_dir = ctx.backup_dir()
    lines = [
        f"BigFix server backup in {run_dir}",
        f"made {datetime.datetime.now().isoformat(timespec='seconds')} by"
        f" {os.path.basename(__file__)} {__version__}",
        "",
        "Contents:",
    ]
    for backup in state.get("backups") or []:
        lines.append(f"- database {backup.get('database')}: {backup.get('file')}")
    lines.append(f"- database info: {os.path.join(run_dir, 'db_info.json')}")
    for label, item in (state.get("server_files") or {}).items():
        status = item.get("copied_to") or (
            "missing" if item.get("missing") else "skipped"
        )
        lines.append(f"- server file {label}: {status}")
    if state.get("masthead_copy"):
        lines.append(f"- masthead.afxm: {state['masthead_copy']['to']}")
    for target in state.get("key_file_copies") or {}:
        lines.append(f"- key file: {target}")
    lines.append(f"- registry: {os.path.join(run_dir, 'bigfix_registry.reg')}")
    client = state.get("client_data") or {}
    lines.append(
        f"- the root server's client: ComputerID {client.get('computer_id')},"
        " KeyStorage (holds its private key, protect it)"
    )
    keys = state.get("server_keys") or {}
    lines.append(
        f"- server keys: decrypted to {keys['decrypted_to']}, as sensitive as license.pvk"
        if keys.get("decrypted_to")
        else "- server keys: not decrypted, keep license.pvk and its password"
    )
    lines += [
        "",
        "Restore, following HCL's Server Recovery in order:",
        "https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/c_recovery_procedure.html",
        "1. check the masthead URL reaches the new server",
        "2. on the same computer, remove the existing BigFix components",
        "3. reinstall SQL Server if needed",
        "4. restore the BFEnterprise and BESReporting databases",
        "5. restore the backed up server files",
        "6. encrypt the server keys again:",
        '   ServerKeyTool.exe /encrypt /dirIn:"<backup>\\server_keys"'
        ' /dirOut:"<BigFix Server folder>" /sitePvkLocation:"<path to license.pvk>"'
        " /sitePvkPassword:<password>",
        "7. continue with the installer steps on HCL's Server Recovery page",
        "",
        "Restore the root server's own client without a duplicate computer:",
        "https://help.hcl-software.com/bigfix/11.0/platform/Platform/Installation/t_preserving_bundling_when_clientreinstalled.html",
        "- first set ClientIdentityMatch to 100 (BigFix Administrative Tool, Advanced Options)",
        "- install the client, stop it, remove RegCount, ComputerID and"
        " ReportSequenceNumber from GlobalOptions, delete __BESData and KeyStorage",
        "- put back ComputerID and KeyStorage from client_data, start the client",
    ]
    if ctx.dry_run:
        print("DRY RUN, would write RESTORE_NOTES.txt:\n" + "\n".join(lines))
        return
    with open(
        os.path.join(run_dir, "RESTORE_NOTES.txt"), "w", encoding="utf-8"
    ) as notes:
        notes.write("\n".join(lines) + "\n")


def _bigfix_services(ctx: WalkthroughContext) -> List[dict]:
    services = _get(ctx.state, "baseline", "bigfix", "services")
    return services if isinstance(services, list) else []


def service_command(cmdlet: str, name: str, *options: str) -> List[str]:
    """A PowerShell service command, like Stop-Service, for one service by name."""
    return _powershell(" ".join([cmdlet, "-Name", _ps_quote(name), *options]))


def _action_stop_services(ctx: WalkthroughContext) -> None:
    services = _bigfix_services(ctx)
    # remember the original start types once, to restore at the end:
    ctx.state.setdefault(
        "start_modes", {s["Name"]: s.get("StartMode") for s in services}
    )
    for name in service_stop_order(services):
        ctx.execute(service_command("Set-Service", name, "-StartupType", "Manual"))
        ctx.execute(service_command("Stop-Service", name, "-Force"))


def _action_start_services(ctx: WalkthroughContext) -> None:
    for name in services_to_start(_bigfix_services(ctx)):
        ctx.execute(service_command("Start-Service", name))


def _action_restore_start_types(ctx: WalkthroughContext) -> None:
    # Win32_Service StartMode names, to Set-Service StartupType names:
    startup_types = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled"}
    for name, mode in (ctx.state.get("start_modes") or {}).items():
        if mode in startup_types:
            ctx.execute(
                service_command(
                    "Set-Service", name, "-StartupType", startup_types[mode]
                )
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
    "masthead": _action_masthead,
    "client_data": _action_client_data,
    "folder_backup": _action_folder_backup,
    "db_info": _action_db_info,
    "sql_backup": _action_sql_backup,
    "server_keys": _action_server_keys,
    "restore_notes": _action_restore_notes,
    "remote_processes": _action_remote_processes,
    "stop_services": _action_stop_services,
    "start_services": _action_start_services,
    "restore_start_types": _action_restore_start_types,
    "validate": _action_validate,
}


def _ask(prompt: str, choices: List[str], default: Optional[str] = None) -> str:
    """Ask until one of the choices is typed, Enter picks the default."""
    hint = "/".join(choices) + (f", Enter = {default}" if default else "")
    while True:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
        if not answer and default:
            return default
        if answer in choices:
            return answer


DEFAULT_DRY_RUN_FILE = "bigfix_root_server_upgrade_win.dryrun.txt"


def _auto_answer(prompt: str, choices: List[str], default: Optional[str] = None) -> str:
    """Answer a question without asking, for dry runs: the default, or go ahead."""
    answer = next((c for c in (default, "done", "yes") if c in choices), choices[0])
    print(f"{prompt} [{'/'.join(choices)}]: {answer} (dry run)")
    return answer


class _Tee(io.TextIOBase):
    """Writes to several streams, to keep a copy of what's printed."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def run_walkthrough(args, bes_conn, host, compat: dict) -> int:
    """Guide the upgrade one step at a time, resuming from the state file.

    A dry run asks nothing, changes nothing, and saves everything it prints to
    --dry-run-file, to hand over in one go.
    """
    if not args.dry_run:
        return _run_walkthrough(args, bes_conn, host, compat, _ask)
    path = getattr(args, "dry_run_file", None) or DEFAULT_DRY_RUN_FILE
    with open(path, "w", encoding="utf-8") as saved:
        with contextlib.redirect_stdout(cast(TextIO, _Tee(sys.stdout, saved))):
            result = _run_walkthrough(args, bes_conn, host, compat, _auto_answer)
    print(f"dry run saved to {os.path.abspath(path)}")
    return result


def _run_walkthrough(args, bes_conn, host, compat: dict, ask) -> int:
    require_walkthrough_host(host)
    state = load_state(args.state_file)

    # NOTE: a dry run walks the steps in memory, and never saves the state:
    persist = (
        (lambda: None) if args.dry_run else (lambda: save_state(args.state_file, state))
    )
    if "plan" not in state:
        report = build_report(
            bes_conn, host, compat, _target_from_args(args), args.sql_instance
        )
        assessment = report["upgrade_assessment"]
        print(json.dumps(assessment, indent=2))
        if not _get(assessment, "compatibility", "reachable"):
            print("No supported upgrade path was found, see the assessment above.")
            return 1
        if ask("Use this upgrade plan?", ["yes", "no"], "yes") != "yes":
            return 1
        state["plan"] = assessment["compatibility"]
        state["local_sql"] = (
            _get(report, "local", "sql", "bigfix_sql_is_local") is not False
        )
        persist()

    steps = build_steps(state["plan"], local_sql=state.get("local_sql", True))
    if args.step:
        ids = [step.id for step in steps]
        if args.step not in ids:
            raise SystemExit(f"unknown step {args.step}, one of: {', '.join(ids)}")
        state["done"] = ids[: ids.index(args.step)]

    ctx = WalkthroughContext(
        args,
        host,
        state,
        args.state_file,
        ask=ask,
        # a dry run never asks for a license.pvk or its password:
        input_fn=(lambda prompt: "") if args.dry_run else None,
        getpass_fn=(lambda prompt: "") if args.dry_run else None,
    )
    while True:
        step = next_step(steps, state)
        if step is None:
            print("All steps are complete.")
            return 0
        print(f"\n===== {step.id}: {step.title} =====\n{step.instructions}\n")
        for action in step.actions:
            logging.info("running action %s for step %s", action, step.id)
            ACTIONS[action](ctx)
        persist()
        answer = ask("Is this step complete?", ["done", "skip", "quit"])
        if answer == "quit":
            return 0
        mark_step_done(state, step.id)
        if answer == "skip":
            state.setdefault("skipped", []).append(step.id)
        persist()


# ---------------------------------------------------------------- shares

SHARE_ACCOUNT = "bfupgrade_share"
SHARE_ACCOUNT_COMMENT = (
    "Temporary BigFix upgrade share account, removed by --share-cleanup"
)
SAFE_SHARE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_$.-][A-Za-z0-9_$. -]{0,79}$")
SAFE_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,20}$")
SAFE_QUALIFIED_ACCOUNT_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]{1,63}\\[A-Za-z0-9_.-]{1,20}$"
)
SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SHARE_FIX_ORDER = [
    "create_folder",
    "create_account",
    "create_share",
    "grant_share_access",
    "grant_folder_access",
    "firewall_smb",
    "firewall_coordinator",
    "firewall_discovery",
]

PS_SMB_CLIENT_CONFIG = (
    "Get-SmbClientConfiguration | Select-Object RequireSecuritySignature,"
    " EnableSecuritySignature, EnableInsecureGuestLogons | ConvertTo-Json -Compress"
)

# Windows error codes from connecting to a share, the likely cause and a fix.
# `{server}`, `{share}`, `{domain}` are filled in from the attempt:
NET_ERRORS = {
    5: (
        "access denied: the account signed in, but has no permission on the share",
        "on the share owner give the account Change on the share"
        " (Grant-SmbShareAccess) and Modify on the folder (icacls)",
    ),
    53: (
        "the network path was not found: the server did not answer SMB",
        "check the share owner's firewall allows TCP 445 from this computer, and"
        " its network profile is not Public",
    ),
    64: (
        "the connection was dropped by the server",
        "check the share owner's SMB server settings, like EncryptData and"
        " RejectUnencryptedAccess, and its event logs",
    ),
    67: (
        "the share name was not found on {server}",
        "check the share name with Get-SmbShare on the share owner: {share}",
    ),
    86: (
        "the password is wrong",
        "check the password, it is for the account on the share owner",
    ),
    1219: (
        "this computer already has a connection to {server} with other credentials",
        "remove it first: net use \\\\{server}\\{share} /delete  (or net use * /delete)",
    ),
    1225: ("the connection was refused", "check SMB is running on the share owner"),
    1231: ("the network is unreachable", "check routing between the two computers"),
    1232: ("the host is unreachable", "check the address and routing"),
    1240: (
        "the account is not allowed to sign in from this computer: often the"
        " share owner requires SMB signing, or blocks NTLM",
        "check RequireSecuritySignature on both sides (Get-SmbServerConfiguration,"
        " Get-SmbClientConfiguration) and any NTLM restriction policy",
    ),
    1272: (
        "guest access is blocked: the connection fell back to guest",
        "connect with a real account on the share owner, like {domain}\\user,"
        " guest access is blocked by default on newer Windows",
    ),
    1326: (
        "the user name or password is wrong",
        "in a workgroup the account must exist on the share owner, given as"
        " COMPUTERNAME\\user, like {domain}\\user, and have a password",
    ),
    1327: (
        "an account restriction stopped the sign in, often a blank password",
        "give the account a password, blank passwords can't be used over the network",
    ),
    1330: ("the account's password has expired", "set a new password for the account"),
    1331: ("the account is disabled", "enable the account on the share owner"),
    1385: (
        "the account is not granted network logon on the share owner",
        "check 'Access this computer from the network' in the share owner's"
        " local security policy",
    ),
    1909: ("the account is locked out", "unlock the account or wait for the lockout"),
}


class ShareError(Exception):
    """The backup share could not be set up."""


def _ps_quote(value: str) -> str:
    """Quote a value for PowerShell, refusing anything that could break out."""
    if any(char in str(value) for char in "'\"`$\r\n;|&"):
        raise ValueError(f"unsafe characters in {value!r}")
    return f"'{value}'"


def _powershell(script: str) -> List[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def _check_share_name(name: str) -> str:
    if not SAFE_SHARE_NAME_PATTERN.match(name or ""):
        raise ValueError(f"unexpected share name: {name!r}")
    return name


def _check_folder(folder: str) -> str:
    if not folder or UNSAFE_PATH_PATTERN.search(folder) or "$" in folder:
        raise ValueError(f"unexpected characters in folder: {folder!r}")
    return folder


def _check_qualified_account(account: str) -> str:
    if not SAFE_QUALIFIED_ACCOUNT_PATTERN.match(account or ""):
        raise ValueError(f"unexpected account name: {account!r}")
    return account


FIREWALL_KEYWORDS = {"any": "Any", "localsubnet": "LocalSubnet"}


def _check_addresses(peers: List[str]) -> List[str]:
    """Validate addresses, networks, or LocalSubnet/Any for a firewall rule."""
    checked = []
    for peer in peers:
        if str(peer).lower() in FIREWALL_KEYWORDS:
            checked.append(FIREWALL_KEYWORDS[str(peer).lower()])
        elif "/" in str(peer):
            checked.append(str(ipaddress.ip_network(peer, strict=False)))
        else:
            checked.append(str(ipaddress.ip_address(peer)))
    if not checked:
        raise ValueError("no peer addresses for the firewall rule")
    return checked


def firewall_group(run_id: str) -> str:
    """The firewall rule group of one run, removed together at cleanup."""
    if not SAFE_RUN_ID_PATTERN.match(run_id or ""):
        raise ValueError(f"unexpected run id: {run_id!r}")
    return f"BigFix upgrade {run_id}"


def firewall_rule_command(
    run_id: str, port: int, peers: List[str], label: str, protocol: str = "TCP"
) -> List[str]:
    """A firewall rule allowing one port, only from the given peers."""
    group = firewall_group(run_id)
    if not 0 < int(port) < 65536:
        raise ValueError(f"unexpected port: {port!r}")
    if protocol not in ("TCP", "UDP"):
        raise ValueError(f"unexpected protocol: {protocol!r}")
    addresses = ",".join(_check_addresses(peers))
    return _powershell(
        f"New-NetFirewallRule -DisplayName {_ps_quote(f'{group} {label}')}"
        f" -Group {_ps_quote(group)} -Direction Inbound -Protocol {protocol}"
        f" -LocalPort {int(port)} -RemoteAddress {addresses} -Action Allow"
        " -Profile Any | Out-Null"
    )


def new_share_command(name: str, folder: str, account: str) -> List[str]:
    """Create an SMB share: Change for the account, Full for Administrators."""
    return _powershell(
        f"New-SmbShare -Name {_ps_quote(_check_share_name(name))}"
        f" -Path {_ps_quote(_check_folder(folder))}"
        f" -ChangeAccess {_ps_quote(_check_qualified_account(account))}"
        " -FullAccess 'BUILTIN\\Administrators' | Out-Null"
    )


def grant_share_access_command(name: str, account: str) -> List[str]:
    """Give an account Change on an existing share."""
    return _powershell(
        f"Grant-SmbShareAccess -Name {_ps_quote(_check_share_name(name))}"
        f" -AccountName {_ps_quote(_check_qualified_account(account))}"
        " -AccessRight Change -Force | Out-Null"
    )


def grant_folder_access_command(folder: str, account: str) -> List[str]:
    """Give an account Modify on a folder and everything below it."""
    return [
        "icacls.exe",
        _check_folder(folder),
        "/grant",
        f"{_check_qualified_account(account)}:(OI)(CI)M",
    ]


def new_share_password() -> str:
    """A random password that meets Windows complexity rules."""
    return (
        secrets.token_urlsafe(24)
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + "!"
    )


def share_host_probe_scripts(share_name: str, folder: str) -> Dict[str, str]:
    """The read-only PowerShell probes of the share owner, by status key."""
    name = _ps_quote(_check_share_name(share_name))
    path = _ps_quote(_check_folder(folder))
    return {
        "lanmanserver": (
            "Get-Service LanmanServer | Select-Object"
            " @{n='Status';e={$_.Status.ToString()}} | ConvertTo-Json -Compress"
        ),
        "share": (
            f"$s = Get-SmbShare -Name {name} -ErrorAction SilentlyContinue;"
            " if ($s) { [pscustomobject]@{Name=$s.Name; Path=$s.Path;"
            " EncryptData=$s.EncryptData; Access=@(Get-SmbShareAccess -Name"
            f" {name} | Select-Object AccountName,"
            " @{n='AccessControlType';e={$_.AccessControlType.ToString()}},"
            " @{n='AccessRight';e={$_.AccessRight.ToString()}})}"
            " | ConvertTo-Json -Depth 4 -Compress }"
        ),
        "folder_acl": (
            f"if (Test-Path -LiteralPath {path}) {{ @((Get-Acl -LiteralPath {path})"
            ".Access | Select-Object @{n='Identity';e={$_.IdentityReference.Value}},"
            " @{n='Rights';e={$_.FileSystemRights.ToString()}},"
            " @{n='Type';e={$_.AccessControlType.ToString()}})"
            " | ConvertTo-Json -Compress }"
        ),
        "smb_server": (
            "Get-SmbServerConfiguration | Select-Object EnableSMB1Protocol,"
            " EnableSMB2Protocol, RequireSecuritySignature, EncryptData,"
            " RejectUnencryptedAccess | ConvertTo-Json -Compress"
        ),
        "network_profiles": (
            "@(Get-NetConnectionProfile | Select-Object InterfaceAlias,"
            " @{n='NetworkCategory';e={$_.NetworkCategory.ToString()}})"
            " | ConvertTo-Json -Compress"
        ),
        "host_ips": PS_HOST_IPS,
        # enabled inbound allow rules, only those for TCP and UDP ports:
        "firewall_rules": (
            "@(Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow"
            " | ForEach-Object { $p = $_ | Get-NetFirewallPortFilter;"
            " if ($p.Protocol -eq 'TCP' -or $p.Protocol -eq 'UDP') {"
            " $a = $_ | Get-NetFirewallAddressFilter;"
            " [pscustomobject]@{DisplayName=$_.DisplayName; Protocol=$p.Protocol;"
            " Profile=$_.Profile.ToString(); LocalPort=@($p.LocalPort);"
            " RemoteAddress=@($a.RemoteAddress)} } }) | ConvertTo-Json -Depth 3"
            " -Compress"
        ),
    }


def collect_share_host_status(host, spec: dict, hostname: str) -> dict:
    """Collect the share owner's state for analyze_share_host()."""
    status: Dict[str, Any] = {"hostname": hostname}
    for key, script in share_host_probe_scripts(
        spec["share_name"], spec["folder"]
    ).items():
        status[key] = _probe(host.powershell_json, script)
    status["folder_exists"] = _probe(host.dir_exists, spec["folder"])
    if spec.get("account"):
        status["account"] = _probe(host.local_user, spec["account"])
    return status


def _host_networks(host_ips: Any) -> list:
    networks = []
    for entry in _as_list(host_ips) if not isinstance(host_ips, dict) else []:
        with contextlib.suppress(ValueError, KeyError, TypeError):
            networks.append(
                ipaddress.ip_network(
                    f"{entry['IPAddress']}/{entry['PrefixLength']}", strict=False
                )
            )
    return networks


def _address_matches(peer: str, remote: List[str], host_networks: list) -> bool:
    """Check if a firewall rule's RemoteAddress entries include a peer."""
    address = ipaddress.ip_address(peer)
    for entry in remote:
        entry = str(entry)
        if entry.lower() == "any":
            return True
        if entry.lower() == "localsubnet":
            if any(address in network for network in host_networks):
                return True
            continue
        with contextlib.suppress(ValueError):
            if "-" in entry:
                low, high = (ipaddress.ip_address(p) for p in entry.split("-", 1))
                # NOTE: IPv4 and IPv6 addresses can't be compared:
                if low.version == high.version == address.version and (
                    low <= address <= high  # type: ignore[operator]
                ):
                    return True
            elif address in ipaddress.ip_network(entry, strict=False):
                return True
    return False


def _rule_allows(
    rule: dict,
    port: int,
    peer: str,
    categories: set,
    networks: list,
    protocol: str = "TCP",
) -> bool:
    """Check if one firewall rule lets a peer reach a port."""
    if str(rule.get("Protocol") or "TCP").upper() != protocol:
        return False
    ports = [str(p).lower() for p in _as_list(rule.get("LocalPort"))]
    if str(port) not in ports and "any" not in ports:
        return False
    profile = str(rule.get("Profile") or "Any").lower()
    if categories and "any" not in profile:
        if not all(category.lower() in profile for category in categories):
            return False
    remote = [str(entry) for entry in _as_list(rule.get("RemoteAddress"))]
    if peer.lower() in FIREWALL_KEYWORDS:
        # a rule for the whole subnet, or anyone:
        return any(entry.lower() in (peer.lower(), "any") for entry in remote)
    return _address_matches(peer, remote, networks)


def _finding(check, ok, detail, fix=None, action=None) -> dict:
    finding = {"check": check, "ok": ok, "detail": detail, "fix": fix}
    if action:
        finding["action"] = action
    return finding


def analyze_share_host(status: dict, spec: dict) -> List[dict]:
    """Check the share owner can serve the backup share to the peers.

    Arguments:
        status: from collect_share_host_status()
        spec: `share_name`, `folder`, `account` (or None), `peers` (IPs), and
            `coordinator_port` (or None).
    Returns:
        findings, `ok` False for problems, with the `action` that fixes them
        where one can be done automatically.
    """
    findings = []
    hostname = status.get("hostname") or ""
    account = spec.get("account")
    qualified = f"{hostname}\\{account}".lower() if account else None

    service = _get(status, "lanmanserver", "Status")
    findings.append(
        _finding(
            "smb server service",
            service == "Running",
            f"LanmanServer is {service}",
            "Start-Service LanmanServer",
        )
    )
    findings.append(
        _finding(
            "folder",
            bool(status.get("folder_exists")),
            f"{spec['folder']} {'exists' if status.get('folder_exists') else 'is missing'}",
            f"create {spec['folder']}",
            None if status.get("folder_exists") else "create_folder",
        )
    )
    if account:
        findings.append(
            _finding(
                "account",
                bool(status.get("account")),
                f"local account {account}"
                f" {'exists' if status.get('account') else 'is missing'}",
                "create a temporary local account with a random password",
                None if status.get("account") else "create_account",
            )
        )

    share = status.get("share") if isinstance(status.get("share"), dict) else None
    if not share or "error" in share:
        findings.append(
            _finding(
                "share",
                False,
                f"share {spec['share_name']} does not exist",
                f"New-SmbShare -Name '{spec['share_name']}' -Path '{spec['folder']}'",
                "create_share",
            )
        )
    else:
        same_path = str(share.get("Path", "")).rstrip("\\").lower() == (
            spec["folder"].rstrip("\\").lower()
        )
        findings.append(
            _finding(
                "share",
                same_path,
                f"share {share.get('Name')} is {share.get('Path')}",
                None if same_path else f"use --share-folder {share.get('Path')}",
            )
        )
        if account:
            granted = any(
                str(entry.get("AccountName", "")).lower() in (qualified, "everyone")
                and entry.get("AccessControlType") == "Allow"
                and entry.get("AccessRight") in ("Change", "Full")
                for entry in _as_list(share.get("Access"))
            )
            findings.append(
                _finding(
                    "share access",
                    granted,
                    f"{hostname}\\{account} {'has' if granted else 'lacks'} Change"
                    " on the share",
                    "Grant-SmbShareAccess ... -AccessRight Change",
                    None if granted else "grant_share_access",
                )
            )

    if account:
        acl = status.get("folder_acl")
        allowed = any(
            str(entry.get("Identity", "")).lower() == qualified
            and entry.get("Type") == "Allow"
            and (
                "modify" in str(entry.get("Rights", "")).lower()
                or "fullcontrol" in str(entry.get("Rights", "")).lower()
            )
            for entry in (_as_list(acl) if not isinstance(acl, dict) else [])
        )
        findings.append(
            _finding(
                "folder access",
                allowed,
                f"{hostname}\\{account} {'has' if allowed else 'lacks'} Modify on"
                " the folder",
                "icacls <folder> /grant <account>:(OI)(CI)M",
                None if allowed else "grant_folder_access",
            )
        )

    profiles = status.get("network_profiles")
    profiles = _as_list(profiles) if not isinstance(profiles, dict) else []
    public = [p for p in profiles if p.get("NetworkCategory") == "Public"]
    findings.append(
        _finding(
            "network profile",
            not public,
            ", ".join(
                f"{p.get('InterfaceAlias')}: {p.get('NetworkCategory')}"
                for p in profiles
            )
            or "unknown",
            " ; ".join(
                f"Set-NetConnectionProfile -InterfaceAlias '{p.get('InterfaceAlias')}'"
                " -NetworkCategory Private"
                for p in public
            )
            or None,
        )
    )

    smb2 = _get(status, "smb_server", "EnableSMB2Protocol")
    if smb2 is False:
        findings.append(
            _finding(
                "smb2",
                False,
                "SMB2/3 is disabled on the server",
                "Set-SmbServerConfiguration -EnableSMB2Protocol $true",
            )
        )

    rules = status.get("firewall_rules")
    rules = _as_list(rules) if not isinstance(rules, dict) else []
    categories = {
        str(p.get("NetworkCategory")) for p in profiles if p.get("NetworkCategory")
    }
    networks = _host_networks(status.get("host_ips"))
    for peer in spec.get("peers") or []:
        if "/" in str(peer):
            continue
        allowed = any(_rule_allows(r, 445, peer, categories, networks) for r in rules)
        findings.append(
            _finding(
                f"firewall smb {peer}",
                allowed,
                f"TCP 445 from {peer} is {'allowed' if allowed else 'not allowed'}",
                f"allow TCP 445 from {peer} only",
                None if allowed else "firewall_smb",
            )
        )
    # the coordinator port: authenticated, so open to the local subnet by default:
    if spec.get("coordinator_port"):
        port = int(spec["coordinator_port"])
        remote = spec.get("coordinator_remote") or ["LocalSubnet"]
        for label, protocol, action in (
            ("coordinator", "TCP", "firewall_coordinator"),
            ("discovery", "UDP", "firewall_discovery"),
        ):
            allowed = all(
                any(
                    _rule_allows(r, port, str(entry), categories, networks, protocol)
                    for r in rules
                )
                for entry in remote
                if "/" not in str(entry)
            )
            findings.append(
                _finding(
                    f"firewall {label}",
                    allowed,
                    f"{protocol} {port} from {', '.join(remote)} is"
                    f" {'allowed' if allowed else 'not allowed'}",
                    f"allow {protocol} {port} from {', '.join(remote)}",
                    None if allowed else action,
                )
            )
    return findings


def setup_share_owner(
    host,
    spec: dict,
    state: dict,
    run_id: str,
    confirm,
    dry_run: bool,
    hostname: str,
    output=print,
) -> dict:
    """Check the share owner and fix what's missing, each fix confirmed first.

    Everything created is recorded in `state["share"]["created"]` for
    cleanup_share_owner(). The temporary account's password is only returned,
    never stored or logged.

    Returns:
        `user` and `password` for the peers, the password None if no
        temporary account password was set.
    """
    created = state.setdefault("share", {}).setdefault("created", {})
    account = spec.get("account")
    qualified = f"{hostname}\\{account}" if account else None
    # the share and its permissions are for this account:
    grantee = qualified or ""
    password = None

    status = collect_share_host_status(host, spec, hostname)
    findings = analyze_share_host(status, spec)
    for finding in findings:
        mark = {True: "OK  ", False: "FAIL", None: "INFO"}[finding["ok"]]
        output(f"{mark} {finding['check']}: {finding['detail']}")
        if finding["ok"] is False and finding.get("fix") and not finding.get("action"):
            output(f"     fix by hand: {finding['fix']}")

    if account and status.get("account"):
        if created.get("account") != account:
            raise ShareError(
                f"local account {account} exists but was not created by this tool:"
                " choose another with --share-account, or remove it"
            )
        if confirm(f"Set a new random password for the temporary account {account}?"):
            password = new_share_password()
            if not dry_run:
                host.set_local_user_password(account, password)

    actions = {f["action"] for f in findings if f.get("action")}
    peers = spec.get("peers") or []
    for action in SHARE_FIX_ORDER:
        if action not in actions:
            continue
        if action == "firewall_smb" and not peers:
            output(f"skipping {action}: give --allow with the peer addresses")
            continue
        detail = next(f for f in findings if f.get("action") == action)
        if not confirm(f"Fix {detail['check']}: {detail['fix']}?"):
            continue
        command = None
        if action == "create_folder":
            if not dry_run:
                host.make_dirs(spec["folder"])
            created["folder"] = spec["folder"]
        elif action == "create_account":
            password = new_share_password()
            if not dry_run:
                host.create_local_user(account, password, SHARE_ACCOUNT_COMMENT)
            created["account"] = account
        elif action == "create_share":
            command = new_share_command(spec["share_name"], spec["folder"], grantee)
            created["share"] = spec["share_name"]
        elif action == "grant_share_access":
            command = grant_share_access_command(spec["share_name"], grantee)
        elif action == "grant_folder_access":
            command = grant_folder_access_command(spec["folder"], grantee)
        elif action == "firewall_smb":
            command = firewall_rule_command(run_id, 445, peers, "SMB")
            created["firewall_group"] = firewall_group(run_id)
        elif action in ("firewall_coordinator", "firewall_discovery"):
            command = firewall_rule_command(
                run_id,
                spec["coordinator_port"],
                spec.get("coordinator_remote") or ["LocalSubnet"],
                "coordinator" if action == "firewall_coordinator" else "discovery",
                "TCP" if action == "firewall_coordinator" else "UDP",
            )
            created["firewall_group"] = firewall_group(run_id)
        if command:
            if dry_run:
                output("DRY RUN, would run: " + " ".join(command))
            else:
                host.run(command)
    return {"user": qualified, "password": password}


def cleanup_share_owner(
    host, state: dict, confirm, dry_run: bool, output=print
) -> None:
    """Remove what setup_share_owner() created: firewall rules, share, account.

    The folder and the backups in it are never removed.
    """
    created = state.setdefault("share", {}).setdefault("created", {})
    commands = []
    if created.get("firewall_group"):
        commands.append(
            (
                "firewall_group",
                _powershell(
                    f"Remove-NetFirewallRule -Group {_ps_quote(created['firewall_group'])}"
                ),
            )
        )
    if created.get("share") and confirm(
        f"Remove the share {created['share']}? The folder and backups are kept."
    ):
        commands.append(
            (
                "share",
                _powershell(
                    f"Remove-SmbShare -Name {_ps_quote(_check_share_name(created['share']))}"
                    " -Force"
                ),
            )
        )
    for key, command in commands:
        if dry_run:
            output("DRY RUN, would run: " + " ".join(command))
        else:
            host.run(command)
            created.pop(key)
    account = created.get("account")
    if account and SAFE_ACCOUNT_PATTERN.match(account):
        if dry_run:
            output(f"DRY RUN, would remove the local account {account}")
        else:
            host.delete_local_user(account)
            created.pop("account")
    if created.get("folder"):
        output(f"kept {created['folder']} and the backups in it")
        if not dry_run:
            created.pop("folder")


def _win_error_code(err: Exception) -> Optional[int]:
    code = getattr(err, "winerror", None)
    if isinstance(code, int):
        return code
    if err.args and isinstance(err.args[0], int):
        return err.args[0]
    return None


def _existing_connections(host, server: str) -> List[str]:
    output = host.run(["net.exe", "use"]) or ""
    prefix = f"\\\\{server}\\".lower()
    return [
        token
        for line in output.splitlines()
        for token in line.split()
        if token.lower().startswith(prefix)
    ]


def diagnose_share_access(
    host, unc: str, user: Optional[str], password: Optional[str], sql_server=None
) -> List[dict]:
    """Troubleshoot using a share from this computer, step by step.

    Stops at the first check the rest depend on. A connection made here is
    removed again afterwards.
    """
    findings: List[dict] = []
    root = unc_share_root(unc)
    if not root:
        return [
            _finding("unc", False, f"{unc} is not a UNC path", r"use \\server\share")
        ]
    server, share = root[2:].split("\\", 1)
    domain = (user or "COMPUTERNAME\\user").split("\\")[0]

    error = host.tcp_connect(server, 445)
    findings.append(
        _finding(
            "tcp 445",
            error is None,
            f"{server} port 445 is {'reachable' if error is None else 'not reachable: ' + str(error)}",
            (
                None
                if error is None
                else "check the share owner's firewall allows TCP 445 from this computer"
                " (New-NetFirewallRule ... -LocalPort 445 -RemoteAddress <this IP>),"
                " and its network profile is not Public"
            ),
        )
    )
    if error is not None:
        return findings

    existing = _probe(_existing_connections, host, server)
    if isinstance(existing, list):
        findings.append(
            _finding(
                "existing connections",
                None,
                ", ".join(existing) if existing else f"none to {server}",
                (
                    (
                        f"connecting with other credentials fails with error 1219, remove"
                        f" them first: net use \\\\{server}\\{share} /delete"
                    )
                    if existing
                    else None
                ),
            )
        )

    connected = False
    if user:
        try:
            host.connect_share(root, user, password)
            connected = True
            findings.append(_finding("connect", True, f"connected as {user}"))
        except Exception as err:  # pylint: disable=broad-exception-caught
            code = _win_error_code(err)
            cause, fix = NET_ERRORS.get(
                code if code is not None else -1,
                (
                    f"Windows error {code}: {err}",
                    "search for the error code, and"
                    " check the share owner's Security event log (event 4625)",
                ),
            )
            names = {"server": server, "share": share, "domain": domain}
            findings.append(
                _finding(
                    "connect",
                    False,
                    f"error {code}: {cause.format(**names)}",
                    fix.format(**names),
                )
            )
            return findings
    else:
        findings.append(
            _finding("connect", None, "no user given, using this session's access")
        )

    try:
        try:
            host.write_probe(unc)
            findings.append(_finding("write", True, f"{unc} is writable"))
        except Exception as err:  # pylint: disable=broad-exception-caught
            # NOTE: carry on, SQL Server writes with its own account, not this one:
            findings.append(
                _finding(
                    "write",
                    False,
                    f"can't write to {unc}: {err}",
                    "on the share owner give the account Change on the share and"
                    " Modify in the folder's NTFS permissions",
                )
            )

        free = _probe(host.disk_free, unc)
        if isinstance(free, int):
            findings.append(
                _finding(
                    "free space", None, f"{free / 1024**3:.0f} GB free on the share"
                )
            )
        client = _probe(host.powershell_json, PS_SMB_CLIENT_CONFIG)
        if isinstance(client, dict) and "error" not in client:
            findings.append(
                _finding(
                    "smb client",
                    None,
                    ", ".join(f"{k} {v}" for k, v in client.items()),
                )
            )
        if sql_server:
            mode = choose_backup_mode(host, sql_server, unc)
            findings.append(
                _finding(
                    "sql server access",
                    True if mode == "direct" else None,
                    (
                        "SQL Server can write backups to the share directly"
                        if mode == "direct"
                        else "SQL Server's service account can't write to the share,"
                        " backups will be staged locally and copied (expected with"
                        " NETWORKSERVICE on a workgroup server)"
                    ),
                )
            )
    finally:
        if connected:
            with contextlib.suppress(Exception):
                host.disconnect_share(root)
    return findings


def summarize_findings(findings: List[dict]) -> dict:
    """The overall result of a list of findings."""
    return {
        "ok": all(finding["ok"] is not False for finding in findings),
        "findings": findings,
    }


def format_findings(name: str, result: dict) -> List[str]:
    """Lines to show for one node's result."""
    problems = [f for f in result["findings"] if f["ok"] is False]
    lines = [f"{name}: {'OK' if result['ok'] else f'{len(problems)} problem(s)'}"]
    for finding in result["findings"]:
        mark = {True: "ok", False: "FAIL", None: "info"}[finding["ok"]]
        lines.append(f"  [{mark}] {finding['check']}: {finding['detail']}")
        if finding["ok"] is False and finding.get("fix"):
            lines.append(f"         fix: {finding['fix']}")
    return lines


# ---------------------------------------------------------------- node channel
#
# Encrypted, mutually authenticated connections between share session nodes.
#
# Trust comes from a shared secret: a 6 digit pairing code the coordinator
# shows and the other nodes ask for, or BIGFIX_UPGRADE_PSK set on every node,
# or both. The masthead serial is mixed in, so nodes of different deployments
# never connect.
#
# That secret only authenticates a SPAKE2 exchange (a password authenticated
# Diffie-Hellman), which negotiates a fresh key for each connection:
# - an eavesdropper learns nothing, and can't test guesses of the code offline
# - an attacker gets one guess per connection, the coordinator stops after a
#   few wrong codes
# - recorded traffic stays safe even if the code or PSK leaks later
# Both sides then confirm the key with an HMAC over the whole handshake.
#
# Messages are JSON, encrypted with AES-GCM, with a key per direction and
# strictly increasing counters: no tampering, replay, reordering or reflection.
#
# NOTE: `cryptography` and `spake2` are imported only when used, so reports
# and the walkthrough work without them. spake2 does its group arithmetic in
# pure Python, so it isn't constant time, acceptable for a short LAN session.

PROTOCOL = "bigfix-upgrade-v2"
PSK_ENV_VAR = "BIGFIX_UPGRADE_PSK"
DEFAULT_PORT = 52390
PAIRING_CODE_DIGITS = 6

MASTHEAD_SERIAL_PATTERN = re.compile(
    rb"^X-Fixlet-Site-Serial-Number:\s*(\d+)\s*$", re.MULTILINE | re.IGNORECASE
)
MASTHEAD_GATHER_URL_PATTERN = re.compile(
    rb"^X-Fixlet-Site-Gather-URL:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)
# the headers are at the start, the rest is certificates and a signature:
MASTHEAD_HEADER_BYTES = 64 * 1024

MAX_FRAME_BYTES = 16 * 1024 * 1024
HANDSHAKE_TIMEOUT = 15
FRAME_HEADER = struct.Struct(">I")
COUNTER = struct.Struct(">Q")


def _hkdf(material: bytes, salt: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 to 32 bytes."""
    # NOTE: imported here, only share sessions need `cryptography`:
    # pylint: disable=import-outside-toplevel
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(
        material
    )


class HandshakeError(Exception):
    """The other node could not be authenticated."""


class WrongCode(HandshakeError):
    """The other node has a different pairing code or PSK."""


class ChannelError(Exception):
    """A frame was invalid: tampered with, replayed, or too large."""


# ---------------------------------------------------------------- secrets


def _masthead_headers(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as masthead:
            return masthead.read(MASTHEAD_HEADER_BYTES)
    except OSError as err:
        logging.debug("can't read masthead %s: %s", path, err)
        return None


def read_masthead_serial(path: str) -> Optional[str]:
    """Read the site serial number from a masthead's headers, None if not found.

    Clients keep theirs in `<BES Client folder>\\ActionSite.afxm`.
    """
    match = MASTHEAD_SERIAL_PATTERN.search(_masthead_headers(path) or b"")
    return match.group(1).decode() if match else None


def read_masthead_gather_host(path: str) -> Optional[str]:
    """The root server's host name, from the masthead's gather URL."""
    match = MASTHEAD_GATHER_URL_PATTERN.search(_masthead_headers(path) or b"")
    if not match:
        return None
    return urllib.parse.urlsplit(match.group(1).decode()).hostname


def load_psk(
    environ: Mapping[str, str], psk_file: Optional[str] = None
) -> Tuple[Optional[bytes], str]:
    """Get the preshared key and where it came from: env, file, or none."""
    if environ.get(PSK_ENV_VAR):
        return environ[PSK_ENV_VAR].encode(), "env"
    if psk_file:
        with open(psk_file, encoding="utf-8") as key_file:
            return key_file.read().strip().encode(), "file"
    return None, "none"


def code_required(psk_source: str) -> bool:
    """With no PSK, the nodes trust each other through a pairing code."""
    return psk_source == "none"


def _normalise_pairing_code(code: str) -> bytes:
    return re.sub(r"[\s-]", "", code).upper().encode()


def derive_password(
    psk: Optional[bytes], serial: str, pairing_code: Optional[str] = None
) -> bytes:
    """The SPAKE2 password, from the PSK and/or pairing code, and the serial.

    Never used as a key: SPAKE2 negotiates the keys, this only authenticates it.
    """
    material = b""
    if psk:
        material += b"psk:" + psk
    if pairing_code:
        material += b"\x00code:" + _normalise_pairing_code(pairing_code)
    if not material:
        raise ValueError(f"a pairing code or {PSK_ENV_VAR} is needed")
    return _hkdf(
        material,
        salt=f"masthead-serial:{serial}".encode(),
        info=f"{PROTOCOL} password".encode(),
    )


def generate_pairing_code() -> str:
    """A random 6 digit code for one session, like `042917`."""
    return f"{secrets.randbelow(10**PAIRING_CODE_DIGITS):0{PAIRING_CODE_DIGITS}d}"


def peer_allowed(ip: str, allow: List[str]) -> bool:
    """Check a peer address against --allow addresses and networks, empty allows
    all.
    """
    if not allow:
        return True
    address = ipaddress.ip_address(ip)
    # NOTE: raises ValueError on a bad entry, a typo must not allow everyone:
    networks = [ipaddress.ip_network(entry, strict=False) for entry in allow]
    return any(address in network for network in networks)


# ---------------------------------------------------------------- framing


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    (length,) = FRAME_HEADER.unpack(await reader.readexactly(FRAME_HEADER.size))
    if length > MAX_FRAME_BYTES:
        raise ChannelError(f"frame of {length} bytes is too large")
    return await reader.readexactly(length)


def _frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise ChannelError(f"frame of {len(payload)} bytes is too large")
    return FRAME_HEADER.pack(len(payload)) + payload


class SecureChannel:
    """An authenticated, encrypted connection to one other node.

    `seal` and `open` do the cryptography on frames without the length header,
    `send` and `recv` add the socket.
    """

    def __init__(self, reader, writer, send_key, recv_key, send_label, recv_label):
        self.reader = reader
        self.writer = writer
        self.send_key = send_key
        self.recv_key = recv_key
        # pylint: disable=import-outside-toplevel
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._send_aead = AESGCM(send_key)
        self._recv_aead = AESGCM(recv_key)
        self.send_label = send_label.encode()
        self.recv_label = recv_label.encode()
        self._send_counter = 0
        self._recv_counter = 0
        self.peer_hello: dict = {}

    def seal(self, message: Any) -> bytes:
        """Encrypt one message: counter, then AES-GCM ciphertext."""
        counter = self._send_counter
        self._send_counter += 1
        nonce = b"\x00" * 4 + COUNTER.pack(counter)
        plaintext = json.dumps(message).encode()
        return COUNTER.pack(counter) + self._send_aead.encrypt(
            nonce, plaintext, self.send_label
        )

    def open(self, frame: bytes) -> Any:
        """Decrypt one message, rejecting anything out of sequence or altered."""
        if len(frame) < COUNTER.size:
            raise ChannelError("frame is too short")
        (counter,) = COUNTER.unpack(frame[: COUNTER.size])
        if counter != self._recv_counter:
            raise ChannelError(
                f"frame {counter} is replayed or out of order,"
                f" expected {self._recv_counter}"
            )
        nonce = b"\x00" * 4 + COUNTER.pack(counter)
        # pylint: disable=import-outside-toplevel
        from cryptography.exceptions import InvalidTag

        try:
            plaintext = self._recv_aead.decrypt(
                nonce, frame[COUNTER.size :], self.recv_label
            )
        except InvalidTag as err:
            raise ChannelError("frame was tampered with, or has the wrong key") from err
        self._recv_counter += 1
        return json.loads(plaintext)

    async def send(self, message: Any) -> None:
        """Send one message."""
        self.writer.write(_frame(self.seal(message)))
        await self.writer.drain()

    async def recv(self) -> Any:
        """Receive one message, raising asyncio.IncompleteReadError on disconnect."""
        return self.open(await _read_frame(self.reader))

    def close(self) -> None:
        """Close the connection."""
        if self.writer is not None:
            self.writer.close()


# ---------------------------------------------------------------- handshake


def make_hello(node_id: str, roles: List[str], serial: str) -> dict:
    """The first, unencrypted message from each side.

    The whole hello is bound into the key exchange, so it can't be altered.
    """
    return {
        "protocol": PROTOCOL,
        "node_id": node_id,
        "roles": roles,
        "serial": serial,
        "nonce": secrets.token_hex(32),
    }


def _confirmation(key: bytes, side: str, id_a: bytes, id_b: bytes) -> bytes:
    """Proves this side negotiated the same key, over the whole handshake."""
    return hmac.new(
        key, b"confirm|" + side.encode() + id_a + id_b, hashlib.sha256
    ).digest()


def _session_key(key: bytes, client_nonce: str, server_nonce: str, label: str) -> bytes:
    return _hkdf(
        key,
        salt=bytes.fromhex(client_nonce) + bytes.fromhex(server_nonce),
        info=f"{PROTOCOL} {label}".encode(),
    )


def _check_peer_hello(peer: dict, own: dict) -> None:
    if not isinstance(peer, dict) or peer.get("protocol") != PROTOCOL:
        protocol = peer.get("protocol") if isinstance(peer, dict) else peer
        raise HandshakeError(
            f"the other node speaks {protocol!r}, not {PROTOCOL}:"
            " use the same version of the script on both"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(peer.get("nonce"))):
        raise HandshakeError("the other node sent an invalid hello")
    if str(peer.get("serial")) != str(own["serial"]):
        raise HandshakeError(
            "the other node is for a different BigFix deployment: masthead serial"
            f" {peer.get('serial')}, this node has {own['serial']}"
        )


async def _read(reader) -> bytes:
    return await asyncio.wait_for(_read_frame(reader), HANDSHAKE_TIMEOUT)


async def _handshake(
    reader, writer, password: bytes, hello: dict, side: str
) -> SecureChannel:
    own_bytes = json.dumps(hello).encode()
    writer.write(_frame(own_bytes))
    await writer.drain()
    try:
        peer_bytes = await _read(reader)
        peer = json.loads(peer_bytes)
        _check_peer_hello(peer, hello)

        # both hellos are the SPAKE2 identities, so neither can be altered:
        client_bytes, server_bytes = (
            (own_bytes, peer_bytes) if side == "client" else (peer_bytes, own_bytes)
        )
        id_a = b"client:" + hashlib.sha256(client_bytes).digest()
        id_b = b"server:" + hashlib.sha256(server_bytes).digest()
        # pylint: disable=import-outside-toplevel
        import spake2  # type: ignore[import-untyped]

        pake_class = spake2.SPAKE2_A if side == "client" else spake2.SPAKE2_B
        pake = pake_class(password, idA=id_a, idB=id_b)
        writer.write(_frame(pake.start()))
        await writer.drain()
        peer_message = await _read(reader)
        try:
            shared = pake.finish(peer_message)
        except Exception as err:  # pylint: disable=broad-exception-caught
            raise HandshakeError(f"invalid key exchange message: {err}") from err

        other = "server" if side == "client" else "client"
        writer.write(_frame(_confirmation(shared, side, id_a, id_b)))
        await writer.drain()
        peer_confirmation = await _read(reader)
    except (asyncio.IncompleteReadError, ConnectionError) as err:
        raise HandshakeError(
            "the other node closed the connection during the handshake,"
            " it probably rejected this node's hello or code"
        ) from err
    except asyncio.TimeoutError as err:
        raise HandshakeError("the other node did not answer the handshake") from err
    except (ValueError, UnicodeDecodeError) as err:
        raise HandshakeError(
            f"the other node sent an invalid handshake: {err}"
        ) from err

    if not hmac.compare_digest(
        peer_confirmation, _confirmation(shared, other, id_a, id_b)
    ):
        raise WrongCode(
            "wrong pairing code: the other node has a different code (or"
            f" {PSK_ENV_VAR}), check they match"
        )

    client_hello = json.loads(client_bytes)
    server_hello = json.loads(server_bytes)
    nonces = (client_hello["nonce"], server_hello["nonce"])
    to_server = _session_key(shared, *nonces, "c2s")
    to_client = _session_key(shared, *nonces, "s2c")
    if side == "client":
        channel = SecureChannel(reader, writer, to_server, to_client, "c2s", "s2c")
    else:
        channel = SecureChannel(reader, writer, to_client, to_server, "s2c", "c2s")
    channel.peer_hello = peer
    return channel


async def open_channel(
    host: str, port: int, password: bytes, hello: dict
) -> Tuple[SecureChannel, dict]:
    """Connect to the coordinator, and negotiate an authenticated key."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), HANDSHAKE_TIMEOUT
    )
    try:
        channel = await _handshake(reader, writer, password, hello, "client")
    except Exception:
        writer.close()
        raise
    return channel, channel.peer_hello


async def accept_channel(
    reader, writer, password: bytes, hello: dict
) -> Tuple[SecureChannel, dict]:
    """Negotiate an authenticated key with a node that connected."""
    try:
        channel = await _handshake(reader, writer, password, hello, "server")
    except Exception:
        writer.close()
        raise
    return channel, channel.peer_hello


# ---------------------------------------------------------------- discovery


class _DiscoveryResponder(asyncio.DatagramProtocol):
    """Answers nodes looking for the coordinator of this deployment."""

    def __init__(self, serial: str, tcp_port: int):
        self.serial = serial
        self.tcp_port = tcp_port
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        message = _parse_discovery(data)
        if message.get("type") != "discover" or message.get("serial") != self.serial:
            return
        reply = {
            "protocol": PROTOCOL,
            "type": "coordinator",
            "serial": self.serial,
            "port": self.tcp_port,
        }
        self.transport.sendto(json.dumps(reply).encode(), addr)


def _parse_discovery(data: bytes) -> dict:
    """A discovery datagram, or {} if it isn't one.

    Never trusted beyond an address.
    """
    if len(data) > 4096:
        return {}
    try:
        message = json.loads(data)
    except ValueError:
        return {}
    if not isinstance(message, dict) or message.get("protocol") != PROTOCOL:
        return {}
    message["serial"] = str(message.get("serial"))
    return message


async def serve_discovery(
    serial: str, tcp_port: int, host: str = "0.0.0.0", port: int = DEFAULT_PORT
):
    """Answer UDP discovery broadcasts, returning the transport to close."""
    transport, _protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _DiscoveryResponder(serial, tcp_port), local_addr=(host, port)
    )
    return transport


async def discover_coordinator(
    serial: str,
    port: int = DEFAULT_PORT,
    targets: Tuple[str, ...] = ("255.255.255.255",),
    timeout: float = 3,
) -> Optional[Tuple[str, int]]:
    """Find the coordinator for this masthead serial by UDP broadcast.

    The answer only gives an address: connecting still authenticates it.
    """
    loop = asyncio.get_running_loop()
    found: asyncio.Future = loop.create_future()

    class Listener(asyncio.DatagramProtocol):
        """Takes the first valid answer."""

        def datagram_received(self, data, addr):
            message = _parse_discovery(data)
            if (
                message.get("type") == "coordinator"
                and message.get("serial") == serial
                and not found.done()
            ):
                with contextlib.suppress(TypeError, ValueError):
                    found.set_result((addr[0], int(message["port"])))

    transport, _protocol = await loop.create_datagram_endpoint(
        Listener, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    payload = json.dumps({"protocol": PROTOCOL, "type": "discover", "serial": serial})
    try:
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for target in targets:
                with contextlib.suppress(OSError):
                    transport.sendto(payload.encode(), (target, port))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(found), min(0.5, max(0.01, deadline - loop.time()))
                )
            except asyncio.TimeoutError:
                continue
        return None
    finally:
        transport.close()


def default_node_id() -> str:
    """This node's name in messages: its hostname."""
    return os.environ.get("COMPUTERNAME") or os.uname().nodename


# ---------------------------------------------------------------- share session: discovery

PS_HYPERV_HOST = (
    "[bool](Get-Service vmms -ErrorAction SilentlyContinue) | ConvertTo-Json -Compress"
)
PS_LOCAL_SHARES = (
    "@(Get-SmbShare | Select-Object Name, Path, Special) | ConvertTo-Json -Compress"
)
PS_HOST_IPS = (
    "@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,"
    " PrefixLength, InterfaceAlias) | ConvertTo-Json -Compress"
)
DEFAULT_SHARE_NAME = "bigfix_upgrade_backup"
# wrong pairing codes before the coordinator stops accepting nodes, each is
# one guess out of a million:
MAX_WRONG_CODES = 5


def detect_node_role(host) -> str:
    """The share session role for this computer, from what it is.

    The Hyper-V host coordinates, since it stays up while the others reboot.
    Other Windows computers are peers, anything else can only be a console.
    """
    if not host.is_windows():
        return "console"
    if _probe(host.powershell_json, PS_HYPERV_HOST) is True:
        return "coordinator"
    return "peer"


def _local_shares(host) -> List[dict]:
    """This computer's own shares, not the special ones like C$ or IPC$."""
    shares = _probe(host.powershell_json, PS_LOCAL_SHARES)
    return [
        share
        for share in (_as_list(shares) if not isinstance(shares, dict) else [])
        if isinstance(share, dict)
        and not share.get("Special")
        and not str(share.get("Name", "")).endswith("$")
    ]


def _default_share_folder(host, name: str) -> str:
    """A folder for a new share, on the fixed drive with the most free space."""
    disks = _probe(lambda: _as_list(host.powershell_json(PS_DISKS)))
    disks = [d for d in disks if isinstance(d, dict)] if isinstance(disks, list) else []
    drive = (
        max(disks, key=lambda d: d.get("FreeSpace") or 0)["DeviceID"] if disks else "C:"
    )
    return f"{drive}\\{name}"


def choose_backup_share(host, ask, output=print) -> dict:
    """Pick the backup share on this computer: an existing one, or a new one.

    Enter at each question takes the default.
    """
    shares = _local_shares(host)
    if len(shares) == 1:
        share = shares[0]
        if (
            ask(
                f"Use the share {share['Name']} ({share['Path']}) for the backups?",
                ["yes", "no"],
                "yes",
            )
            == "yes"
        ):
            return {"share_name": share["Name"], "folder": share["Path"]}
    elif shares:
        for number, share in enumerate(shares, start=1):
            output(f"  {number}) {share['Name']} ({share['Path']})")
        answer = ask(
            "Which share for the backups? A number, or new",
            [str(n) for n in range(1, len(shares) + 1)] + ["new"],
            "1",
        )
        if answer != "new":
            share = shares[int(answer) - 1]
            return {"share_name": share["Name"], "folder": share["Path"]}
    folder = _default_share_folder(host, DEFAULT_SHARE_NAME)
    if (
        ask(f"Create the share {DEFAULT_SHARE_NAME} at {folder}?", ["yes", "no"], "yes")
        != "yes"
    ):
        raise SystemExit("no backup share: give --share-unc, or --share-folder")
    return {"share_name": DEFAULT_SHARE_NAME, "folder": folder}


def choose_host_ip(host_ips: Any, target: Optional[str] = None) -> Optional[str]:
    """This computer's IPv4 address that others reach: the one facing `target`."""
    candidates = []
    for entry in _as_list(host_ips) if not isinstance(host_ips, dict) else []:
        with contextlib.suppress(ValueError, KeyError, TypeError):
            interface = ipaddress.ip_interface(
                f"{entry['IPAddress']}/{entry['PrefixLength']}"
            )
            if not (interface.ip.is_loopback or interface.ip.is_link_local):
                candidates.append(interface)
    if target:
        with contextlib.suppress(ValueError):
            address = ipaddress.ip_address(target)
            for interface in candidates:
                if address in interface.network:
                    return str(interface.ip)
    return str(candidates[0].ip) if candidates else None


def _resolve(name: str) -> Optional[str]:
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def discover_root_ip(masthead_paths: List[str], resolver=_resolve) -> Optional[str]:
    """The root server's address, from the host in the local masthead gather URL."""
    for path in masthead_paths:
        name = read_masthead_gather_host(path)
        if name:
            with contextlib.suppress(ValueError):
                return str(ipaddress.ip_address(name))
            return resolver(name)
    return None


def plan_share(
    host,
    share_unc: Optional[str],
    root_ip: Optional[str],
    host_ips: Any,
    hostname: str,
    ask,
    share_folder: Optional[str] = None,
) -> dict:
    """Work out the backup share: its UNC path, and its folder if it is local.

    With no --share-unc, a share on this computer is picked or created, and
    its UNC path uses this computer's address facing the root server.
    """
    if share_unc:
        root = unc_share_root(share_unc)
        if not root:
            raise SystemExit(f"--share-unc {share_unc} is not a UNC path")
        server, name = root[2:].split("\\", 1)
        own = {hostname.lower(), "localhost", "."} | {
            str(e.get("IPAddress")) for e in _as_list(host_ips) if isinstance(e, dict)
        }
        if server.lower() not in own and server not in own:
            # on another server, only checked from here:
            return {"unc": share_unc, "share_name": name, "folder": None}
        existing = {s["Name"].lower(): s["Path"] for s in _local_shares(host)}
        folder = (
            share_folder
            or existing.get(name.lower())
            or _default_share_folder(host, name)
        )
        return {"unc": share_unc, "share_name": name, "folder": folder}

    if share_folder:
        choice = {"share_name": DEFAULT_SHARE_NAME, "folder": share_folder}
    else:
        choice = choose_backup_share(host, ask)
    address = choose_host_ip(host_ips, root_ip) or hostname
    return {
        "unc": f"\\\\{address}\\{choice['share_name']}",
        "share_name": choice["share_name"],
        "folder": choice["folder"],
    }


def decide_pairing_code(given: Optional[str], psk_source: str) -> Optional[str]:
    """The pairing code: the one given, or a new one when there's no PSK.

    Without BIGFIX_UPGRADE_PSK, the code is what the nodes trust each other by.
    """
    if given:
        return given
    return generate_pairing_code() if code_required(psk_source) else None


async def find_coordinator(explicit, serial: str, discover) -> tuple:
    """The coordinator's address: --coordinator, or found by broadcast."""
    if explicit:
        return _parse_address(explicit, DEFAULT_PORT)
    found = await discover(serial)
    if found:
        return found
    raise SystemExit(
        "could not find the coordinator on this subnet, give --coordinator"
        " host:port (check it's running, and its firewall allows UDP discovery)"
    )


# ---------------------------------------------------------------- share session: nodes


def require_session_packages() -> None:
    """Exit with an install hint unless `cryptography` and `spake2` are installed.

    Share sessions need them, reports and the walkthrough don't.
    """
    missing = [
        name
        for name in ("cryptography", "spake2")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise SystemExit(
            f"share sessions need {' and '.join(missing)}:"
            f" pip install {' '.join(missing)}"
        )


CLIENT_MASTHEAD_PATHS = [
    r"C:\Program Files (x86)\BigFix Enterprise\BES Client\ActionSite.afxm",
    "/Library/Application Support/BigFix/BES Agent/actionsite.afxm",
    "/etc/opt/BESClient/actionsite.afxm",
]


def resolve_masthead_serial(override, bes_conn, masthead_paths: List[str]) -> str:
    """The masthead serial of the deployment being upgraded.

    From --masthead-serial, then the REST connection, then the local client's
    masthead. REST wins over the client, since this computer's client might
    belong to another deployment.
    """
    if override:
        return str(override)
    rest_serial = None
    if bes_conn is not None:
        masthead = _probe(_masthead, bes_conn)
        if "serial" in masthead:
            rest_serial = str(masthead["serial"])
    local_serial = next(
        (s for s in (read_masthead_serial(p) for p in masthead_paths) if s),
        None,
    )
    if rest_serial and local_serial and rest_serial != local_serial:
        logging.warning(
            "this computer's BigFix client is for a different BigFix deployment"
            " (masthead serial %s), using %s from the REST connection",
            local_serial,
            rest_serial,
        )
    serial = rest_serial or local_serial
    if not serial:
        raise SystemExit(
            "no masthead serial found: give --masthead-serial, or a REST connection"
        )
    return serial


class ShareSessionCoordinator:
    """The share owner's side: hands out the share and collects each node's checks.

    Commands (`status`, `retry`, `done`) can come from its own console or from
    any connected node.
    """

    def __init__(
        self,
        share,
        output,
        password,
        serial,
        share_unc,
        allow,
        max_failures=MAX_WRONG_CODES,
    ):
        self.share = share
        self.output = output
        # authenticates the key exchange with each node, never used as a key:
        self.password = password
        self.serial = serial
        self.max_failures = max_failures
        self.failures = 0
        self.locked = False
        self.share_unc = share_unc
        self.allow = allow
        self.nodes: Dict[str, dict] = {}
        self.results: Dict[str, List[dict]] = {}
        self.pending: Dict[str, int] = {}
        self.done = asyncio.Event()
        self._changed = asyncio.Event()

    async def start(self, host: str, port: int):
        """Listen for nodes, returning the asyncio server."""
        return await asyncio.start_server(self._on_connect, host, port)

    def _share_for(self, roles: List[str]) -> dict:
        share = {"unc": self.share_unc, "user": self.share.get("user")}
        # only over the negotiated key, and never to consoles:
        if "console" not in roles:
            share["password"] = self.share.get("password")
        return share

    async def _send(self, name: str, message: dict) -> None:
        node = self.nodes.get(name)
        if node:
            with contextlib.suppress(ConnectionError):
                await node["channel"].send(message)

    async def _broadcast_consoles(self, message: dict) -> None:
        for name, node in list(self.nodes.items()):
            if "console" in node["roles"]:
                await self._send(name, message)

    async def _diagnose(self, name: str) -> None:
        self.pending[name] = self.pending.get(name, 0) + 1
        await self._send(name, {"type": "diagnose"})

    async def _on_connect(self, reader, writer) -> None:
        peer_ip = str(writer.get_extra_info("peername")[0])
        if self.locked:
            self.output(f"refused {peer_ip}: locked after too many wrong codes")
            writer.close()
            return
        try:
            if not peer_allowed(peer_ip, self.allow):
                self.output(f"refused {peer_ip}: not in --allow")
                writer.close()
                return
            hello = make_hello("coordinator", ["coordinator"], self.serial)
            channel, peer = await accept_channel(reader, writer, self.password, hello)
        except WrongCode as err:
            # each wrong code is one online guess, so only allow a few:
            self.failures += 1
            self.output(
                f"refused {peer_ip}: {err} ({self.failures}/{self.max_failures})"
            )
            if self.failures >= self.max_failures:
                self.locked = True
                self.output(
                    "too many wrong pairing codes, no more nodes are accepted:"
                    " restart the coordinator for a new code"
                )
            return
        except Exception as err:  # pylint: disable=broad-exception-caught
            self.output(f"refused {peer_ip}: {err}")
            return
        # names must be unique, a peer and a console can run on the same computer:
        base_name = name = str(peer.get("node_id"))
        suffix = 2
        while name in self.nodes:
            name = f"{base_name}-{suffix}"
            suffix += 1
        roles = [str(role) for role in peer.get("roles") or []]
        self.nodes[name] = {"channel": channel, "roles": roles, "ip": peer_ip}
        self.output(f"{name} ({', '.join(roles)}) connected from {peer_ip}")
        await channel.send(
            {"type": "welcome", "name": name, "share": self._share_for(roles)}
        )
        self._changed.set()
        if "console" not in roles:
            await self._diagnose(name)
        try:
            while True:
                message = await channel.recv()
                await self._on_message(name, message)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as err:  # pylint: disable=broad-exception-caught
            self.output(f"{name}: dropped, {err}")
        finally:
            self.nodes.pop(name, None)
            self.pending.pop(name, None)
            self._changed.set()
            channel.close()
            self.output(f"{name} disconnected")

    async def _on_message(self, name: str, message: dict) -> None:
        if message.get("type") == "share_result":
            result = {
                "ok": bool(message.get("ok")),
                "findings": message.get("findings", []),
            }
            self.results.setdefault(name, []).append(result)
            for line in format_findings(name, result):
                self.output(line)
            await self._broadcast_consoles(
                {"type": "result", "node": name, "result": result}
            )
            self.pending[name] = max(0, self.pending.get(name, 1) - 1)
            self._changed.set()
        elif message.get("type") == "command":
            await self.handle_command(str(message.get("command")), source=name)

    def status_lines(self) -> List[str]:
        """The current state of every node."""
        lines = [f"share {self.share_unc}, {len(self.nodes)} node(s) connected"]
        for name, node in self.nodes.items():
            history = self.results.get(name)
            last = history[-1] if history else None
            state = (
                "console"
                if "console" in node["roles"]
                else (
                    "checking"
                    if self.pending.get(name)
                    else (
                        "OK"
                        if last and last["ok"]
                        else "problems" if last else "waiting"
                    )
                )
            )
            lines.append(f"  {name} ({node['ip']}): {state}")
        return lines

    async def handle_command(self, command: str, source: str) -> None:
        """Run one command, from this console or a node."""
        command = command.strip().lower()
        self.output(f"{source}: {command}")
        if command == "status":
            lines = self.status_lines()
            for line in lines:
                self.output(line)
            if source in self.nodes:
                await self._send(source, {"type": "status", "lines": lines})
        elif command == "retry":
            for name, node in list(self.nodes.items()):
                if "console" not in node["roles"]:
                    await self._diagnose(name)
        elif command == "done":
            await self._wait(lambda: not any(self.pending.values()), timeout=120)
            for name in list(self.nodes):
                await self._send(name, {"type": "bye"})
            self.done.set()
        else:
            self.output(f"unknown command {command!r}, use status, retry or done")

    async def _wait(self, condition, timeout: float) -> None:
        async def waiter():
            while not condition():
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(waiter(), timeout)

    async def wait_for_nodes(self, count: int, timeout: float) -> None:
        """Wait until this many nodes are connected."""
        await self._wait(lambda: len(self.nodes) >= count, timeout)


class ShareSessionNode:
    """A node connecting to the coordinator: checks the share and reports back.

    A `console` node only shows results and sends commands.
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        name,
        roles,
        host,
        output,
        password,
        serial,
        commands=None,
        prompt_password=None,
        sql_server=None,
        read_stdin=False,
    ):
        self.name = name
        self.read_stdin = read_stdin
        self.roles = roles
        self.host = host
        self.output = output
        self.password = password
        self.serial = serial
        self.commands = commands
        self.prompt_password = prompt_password
        self.sql_server = sql_server
        self.share: dict = {}
        self.command_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self, host: str, port: int, attempts: int = 1, delay: float = 5):
        """Connect and authenticate, retrying while the coordinator isn't up yet."""
        for attempt in range(1, attempts + 1):
            hello = make_hello(self.name, self.roles, self.serial)
            try:
                channel, _peer = await open_channel(host, port, self.password, hello)
                return channel
            except (OSError, asyncio.TimeoutError) as err:
                if attempt == attempts:
                    raise
                self.output(f"coordinator not reachable ({err}), retrying in {delay}s")
                await asyncio.sleep(delay)
        raise ConnectionError(f"coordinator {host}:{port} not reachable")

    async def run(self, host: str, port: int, attempts: int = 1) -> None:
        """Serve the coordinator until it says bye."""
        channel = await self.connect(host, port, attempts)
        sender = asyncio.create_task(self._send_commands(channel))
        try:
            while True:
                message = await channel.recv()
                kind = message.get("type")
                if kind == "welcome":
                    self.share = message.get("share") or {}
                    self.name = message.get("name") or self.name
                    self.output(
                        f"connected as {self.name}, share {self.share.get('unc')}"
                    )
                    self._ask_missing_password()
                    if self.read_stdin:
                        # NOTE: only after any prompts, so they don't compete for input:
                        threading.Thread(
                            target=_read_stdin_commands,
                            args=(self.command_queue, asyncio.get_running_loop()),
                            daemon=True,
                        ).start()
                        self.output("commands: status, retry, done")
                    for command in self.commands or []:
                        await self.command_queue.put(command)
                elif kind == "diagnose" and "console" not in self.roles:
                    await self._diagnose(channel)
                elif kind == "result":
                    for line in format_findings(message["node"], message["result"]):
                        self.output(line)
                elif kind == "status":
                    for line in message.get("lines", []):
                        self.output(line)
                elif kind == "bye":
                    self.output("coordinator finished the session")
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            self.output("coordinator disconnected")
        finally:
            sender.cancel()
            channel.close()

    async def _send_commands(self, channel) -> None:
        while True:
            command = await self.command_queue.get()
            await channel.send({"type": "command", "command": command})

    def _ask_missing_password(self) -> None:
        """Ask for the share password once, if the coordinator didn't send one."""
        user = self.share.get("user")
        if (
            "console" not in self.roles
            and user
            and self.share.get("password") is None
            and self.prompt_password
        ):
            self.share["password"] = self.prompt_password(user)

    async def _diagnose(self, channel) -> None:
        user = self.share.get("user")
        password = self.share.get("password")
        findings = await asyncio.get_running_loop().run_in_executor(
            None,
            diagnose_share_access,
            self.host,
            str(self.share.get("unc") or ""),
            user,
            password,
            self.sql_server,
        )
        result = summarize_findings(findings)
        for line in format_findings(self.name, result):
            self.output(line)
        await channel.send({"type": "share_result", **result})


def save_session_results(state: dict, results: dict) -> None:
    """Keep each node's share check results in the state file."""
    state.setdefault("share", {})["results"] = results


def _read_stdin_commands(queue: asyncio.Queue, loop) -> None:
    """Feed typed commands into the event loop, from a thread."""
    while True:
        try:
            line = input()
        except EOFError:
            return
        if line.strip():
            loop.call_soon_threadsafe(queue.put_nowait, line.strip())


def _parse_address(value: str, default_port: int) -> tuple:
    host, _, port = value.rpartition(":")
    if not host:
        return value, default_port
    return host, int(port)


def run_share_session(args, bes_conn, host) -> int:
    """The --share-session mode: as coordinator, peer or console, auto detected.

    Everything has a default: the role, the share, the addresses and the
    pairing code, so on most nodes `--share-session` is all that's needed.
    """
    require_session_packages()
    serial = resolve_masthead_serial(
        args.masthead_serial, bes_conn, CLIENT_MASTHEAD_PATHS
    )
    psk, psk_source = load_psk(os.environ, args.psk_file)
    given_code = args.pairing_code
    node = args.node or ("coordinator" if args.listen else detect_node_role(host))
    print(f"share session as {node}, masthead serial {serial}")
    if node == "coordinator":
        return _run_coordinator(
            args, bes_conn, host, serial, psk, psk_source, given_code
        )
    return _run_node(args, host, node, serial, psk, given_code)


def _root_ip_from_rest(bes_conn) -> Optional[str]:
    if bes_conn is None:
        return None
    addresses = _get(_probe(_root_server, bes_conn), "properties", "IP Address")
    return str(addresses[0]) if addresses else None


def _run_coordinator(args, bes_conn, host, serial, psk, psk_source, given_code) -> int:
    # pylint: disable=too-many-arguments,too-many-locals
    state = load_state(args.state_file)
    state.setdefault("run_id", datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    listen_host, listen_port = _parse_address(
        args.listen or f"0.0.0.0:{DEFAULT_PORT}", DEFAULT_PORT
    )
    host_ips = _probe(host.powershell_json, PS_HOST_IPS) if host.is_windows() else []
    root_ip = _root_ip_from_rest(bes_conn) or discover_root_ip(CLIENT_MASTHEAD_PATHS)
    print(f"root server: {root_ip or 'not found, give --allow with its address'}")
    if not host.is_windows() and not args.share_unc:
        raise SystemExit("give --share-unc, a share can only be set up on Windows")
    plan = plan_share(
        host,
        args.share_unc,
        root_ip,
        host_ips,
        socket.gethostname(),
        _ask,
        args.share_folder,
    )
    print(
        f"backup share: {plan['unc']}"
        + (f" ({plan['folder']})" if plan["folder"] else "")
    )

    peers = list(
        dict.fromkeys(
            [ip for ip in [root_ip] if ip]
            + [entry for entry in args.allow or [] if "/" not in entry]
        )
    )
    share: Dict[str, Optional[str]] = {"user": None, "password": None}
    if plan["folder"]:
        if not host.is_admin():
            raise SystemExit("setting up the share needs an elevated prompt")
        spec = {
            "share_name": plan["share_name"],
            "folder": plan["folder"],
            "account": args.share_account,
            "peers": peers,
            "coordinator_port": listen_port,
            "coordinator_remote": ["LocalSubnet"] + list(args.allow or []),
        }
        share = setup_share_owner(
            host,
            spec,
            state,
            state["run_id"],
            lambda prompt: _ask(prompt, ["yes", "no"], "yes") == "yes",
            args.dry_run,
            socket.gethostname(),
        )
        save_state(args.state_file, state)
    elif args.backup_share_user:
        share = {
            "user": args.backup_share_user,
            "password": getpass.getpass(
                f"Password for {args.backup_share_user}, to give to the nodes: "
            ),
        }

    pairing_code = decide_pairing_code(given_code, psk_source)
    if pairing_code and pairing_code != given_code:
        print(
            f"\n    pairing code: {pairing_code}\n    the other nodes ask for it once\n"
        )
    password = derive_password(psk, serial, pairing_code)

    async def serve():
        coordinator = ShareSessionCoordinator(
            share=share,
            output=print,
            password=password,
            serial=serial,
            share_unc=plan["unc"],
            allow=args.allow or [],
        )
        server = await coordinator.start(listen_host, listen_port)
        discovery = None
        try:
            discovery = await serve_discovery(
                serial, listen_port, listen_host, listen_port
            )
        except OSError as err:
            print(f"discovery is off ({err}), nodes need --coordinator")
        print(
            f"listening on {listen_host}:{listen_port}. On the other computers run:"
            f" {os.path.basename(__file__)} --share-session"
        )
        print("commands: status, retry, done")
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(
            target=_read_stdin_commands, args=(queue, loop), daemon=True
        ).start()
        while not coordinator.done.is_set():
            getter = asyncio.create_task(queue.get())
            finished = asyncio.create_task(coordinator.done.wait())
            done, _ = await asyncio.wait(
                {getter, finished}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                await coordinator.handle_command(getter.result(), "coordinator")
            else:
                getter.cancel()
        server.close()
        if discovery:
            discovery.close()
        save_session_results(state, coordinator.results)
        save_state(args.state_file, state)

    asyncio.run(serve())
    print("after the upgrade, remove what was set up with --share-cleanup here")
    return 0


def _run_node(args, host, node, serial, psk, given_code) -> int:
    # pylint: disable=too-many-arguments
    if node == "peer" and not host.is_windows():
        raise SystemExit(
            "a peer checks the share with Windows SMB, on this computer use"
            " --node console to watch and send commands"
        )
    roles = (
        ["console"]
        if node == "console"
        else (["root"] if is_local_root_server(host) else ["peer"])
    )
    sql_server = None
    if "root" in roles and host.is_admin():
        dsns = _probe(_bigfix_dsns, host)
        sql_server = _bigfix_sql_server(dsns) if "error" not in dsns else None
    if not psk and not given_code:
        # nothing to trust the coordinator by without the code, ask for it now:
        given_code = input("Pairing code shown on the coordinator: ").strip()

    async def serve_node():
        coord_host, coord_port = await find_coordinator(
            args.coordinator, serial, discover_coordinator
        )
        print(f"coordinator: {coord_host}:{coord_port}")
        node_obj = ShareSessionNode(
            default_node_id(),
            roles,
            None if node == "console" else host,
            output=print,
            prompt_password=lambda user: getpass.getpass(
                f"Password for {user} on the share: "
            ),
            sql_server=sql_server,
            read_stdin=True,
            password=derive_password(psk, serial, given_code),
            serial=serial,
        )
        await node_obj.run(coord_host, coord_port, attempts=120)

    try:
        asyncio.run(serve_node())
    except HandshakeError as err:
        raise SystemExit(f"could not join the session: {err}") from err
    except OSError as err:
        raise SystemExit(f"could not reach the coordinator: {err}") from err
    return 0


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
    parser.add_argument(
        "--backup-dir",
        help="walkthrough backups: a local folder or a UNC share path",
    )
    parser.add_argument(
        "--backup-share-user",
        help="user to connect to a --backup-dir share as, the password is prompted",
    )
    parser.add_argument(
        "--staging-dir",
        help="local folder for SQL backups when SQL Server can't write to"
        " --backup-dir, default the instance's Backup folder",
    )
    parser.add_argument(
        "--step", help="walkthrough step id to start from, redoing it and later steps"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="walkthrough prints commands instead of running them, asks nothing,"
        " and saves its output to --dry-run-file",
    )
    parser.add_argument(
        "--dry-run-file",
        default=DEFAULT_DRY_RUN_FILE,
        help="where a dry run saves its output",
    )
    session = parser.add_argument_group(
        "share session", "check and troubleshoot the backup share across nodes"
    )
    session.add_argument(
        "--share-session",
        action="store_true",
        help="run a share session as --node coordinator, peer or console",
    )
    session.add_argument(
        "--share-cleanup",
        action="store_true",
        help="on the share owner: remove the firewall rules, temporary account"
        " and share this tool created, never the folder",
    )
    session.add_argument(
        "--node",
        choices=["coordinator", "peer", "console"],
        help="default coordinator with --listen, otherwise peer",
    )
    session.add_argument("--listen", help="coordinator: address:port to listen on")
    session.add_argument(
        "--coordinator", help="peer or console: the coordinator's address:port"
    )
    session.add_argument(
        "--allow",
        action="append",
        help="coordinator: peer address or network allowed to connect, repeatable,"
        " also used for the firewall rules",
    )
    session.add_argument(
        "--share-unc", help=r"coordinator: the backup share, like \\server\share"
    )
    session.add_argument(
        "--share-folder",
        help="coordinator: the local folder of the share, when it is on this host,"
        " to check and set it up",
    )
    session.add_argument(
        "--share-account",
        default=SHARE_ACCOUNT,
        help="coordinator: the temporary local account for the share",
    )
    session.add_argument(
        "--pairing-code",
        help="the pairing code to use, instead of one the coordinator makes up"
        " when BIGFIX_UPGRADE_PSK isn't set",
    )
    session.add_argument("--psk-file", help="file with the preshared key")
    session.add_argument(
        "--masthead-serial", help="the deployment's masthead serial, if not found"
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

    if args.share_cleanup:
        with besapi.plugin_utilities.init_plugin(
            __version__, parser, require_connection=False
        ) as (args, _bes_conn):
            if not host.is_admin():
                raise SystemExit("--share-cleanup needs an elevated prompt")
            state = load_state(args.state_file)
            cleanup_share_owner(
                host,
                state,
                confirm=lambda prompt: _ask(prompt, ["yes", "no"]) == "yes",
                dry_run=args.dry_run,
            )
            save_state(args.state_file, state)
            return 0

    if args.share_session:
        with besapi.plugin_utilities.init_plugin(
            __version__, parser, require_connection=False
        ) as (args, bes_conn):
            return run_share_session(args, bes_conn, host)

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
