"""
Setup the root server server plugin service with creds provided

requires `besapi`, install with command `pip install besapi`

Example Usage:
python setup_server_plugin_service.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

References:
- https://developer.bigfix.com/rest-api/api/admin.html
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
- https://github.com/jgstew/tools/blob/master/Python/locate_self.py
"""

import argparse
import getpass
import logging
import logging.handlers
import os
import platform
import sys

import besapi

__version__ = "0.0.1"
verbose = 0
bes_conn = None
invoke_folder = None
config_ini = None


def get_invoke_folder():
    """Get the folder the script was invoked from

    References:
    - https://github.com/jgstew/tools/blob/master/Python/locate_self.py
    """
    # using logging here won't actually log it to the file:

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        if verbose:
            print("running in a PyInstaller bundle")
        invoke_folder = os.path.abspath(os.path.dirname(sys.executable))
    else:
        if verbose:
            print("running in a normal Python process")
        invoke_folder = os.path.abspath(os.path.dirname(__file__))

    if verbose:
        print(f"invoke_folder = {invoke_folder}")

    return invoke_folder


def test_file_exists(path):
    """return true if file exists"""

    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        path = os.path.join(invoke_folder, path)

    logging.info("testing if exists: `%s`", path)

    if os.path.isfile(path) and os.access(path, os.R_OK) and os.access(path, os.W_OK):
        return path

    return False


def main():
    """Execution starts here"""
    print("main() start")

    parser = argparse.ArgumentParser(
        description="Provde command line arguments for REST URL, username, and password"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Set verbose output",
        required=False,
        action="count",
        default=0,
    )
    parser.add_argument(
        "-besserver", "--besserver", help="Specify the BES URL", required=False
    )
    parser.add_argument("-r", "--rest-url", help="Specify the REST URL", required=False)
    parser.add_argument("-u", "--user", help="Specify the username", required=False)
    parser.add_argument("-p", "--password", help="Specify the password", required=False)
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global bes_conn, verbose, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder()

    # set different log levels:
    log_level = logging.INFO
    if verbose:
        log_level = logging.INFO
    if verbose > 1:
        log_level = logging.DEBUG

    # get path to put log file in:
    log_filename = os.path.join(invoke_folder, "setup_server_plugin_service.log")

    print(f"Log File Path: {log_filename}")

    handlers = [
        logging.handlers.RotatingFileHandler(
            log_filename, maxBytes=5 * 1024 * 1024, backupCount=1
        )
    ]

    # log output to console if arg provided:
    if verbose:
        handlers.append(logging.StreamHandler())

    # setup logging:
    logging.basicConfig(
        encoding="utf-8",
        level=log_level,
        format="%(asctime)s %(levelname)s:%(message)s",
        handlers=handlers,
    )
    logging.info("----- Starting New Session ------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    password = args.password

    if not password:
        logging.warning("Password was not provided, provide REST API password.")
        print("Password was not provided, provide REST API password.")
        password = getpass.getpass()

    # process args, setup connection:
    rest_url = args.rest_url

    # normalize url to https://HostOrIP:52311
    if rest_url and rest_url.endswith("/api"):
        rest_url = rest_url.replace("/api", "")

    try:
        bes_conn = besapi.besapi.BESConnection(args.user, password, rest_url)
        # bes_conn.login()
    except (
        AttributeError,
        ConnectionRefusedError,
        besapi.besapi.requests.exceptions.ConnectionError,
    ):
        try:
            # print(args.besserver)
            bes_conn = besapi.besapi.BESConnection(args.user, password, args.besserver)
        # handle case where args.besserver is None
        # AttributeError: 'NoneType' object has no attribute 'startswith'
        except AttributeError:
            bes_conn = besapi.besapi.get_bes_conn_using_config_file()

    root_id = int(
        bes_conn.session_relevance_string(
            "unique value of ids of bes computers whose(root server flag of it)"
        )
    )

    logging.info("Root server computer id: %s", root_id)

    InstallPluginService_id = int(
        bes_conn.session_relevance_string(
            'unique value of ids of fixlets whose(name of it contains "Install BES Server Plugin Service") of bes sites whose(name of it = "BES Support")'
        )
    )

    logging.info(
        "Install BES Server Plugin Service content id: %s", InstallPluginService_id
    )

    ConfigureCredentials_id = int(
        bes_conn.session_relevance_string(
            'unique value of ids of fixlets whose(name of it contains "Configure REST API credentials for BES Server Plugin Service") of bes sites whose(name of it = "BES Support")'
        )
    )

    logging.info(
        "Configure REST API credentials for BES Server Plugin Service content id: %s",
        ConfigureCredentials_id,
    )

    EnableWakeOnLAN_id = int(
        bes_conn.session_relevance_string(
            'unique value of ids of fixlets whose(name of it contains "Enable Wake-on-LAN Medic") of bes sites whose(name of it = "BES Support")'
        )
    )

    logging.info(
        "Enable Wake-on-LAN Medic content id: %s",
        EnableWakeOnLAN_id,
    )

    # NOTE: Work in progress
    XML_String_MultiActionGroup = f"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<MultipleActionGroup>
		<Title>Setup Server Plugin Service</Title>
		<Relevance>exists main gather service</Relevance>
		<MemberAction>
			<Title>install initscripts</Title>
			<Relevance><![CDATA[ unix of operating system AND exists packages "dnf" of rpms AND not exists packages "initscripts" of rpms ]]></Relevance>
			<ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[// start
wait dnf -y install initscripts
// End]]></ActionScript>
			<SuccessCriteria Option="RunToCompletion"></SuccessCriteria>
			<IncludeInGroupRelevance>true</IncludeInGroupRelevance>
		</MemberAction>
        <SourcedMemberAction>
            <SourceFixlet>
                <Sitename>BES Support</Sitename>
                <FixletID>{InstallPluginService_id}</FixletID>
                <Action>Action1</Action>
            </SourceFixlet>
        </SourcedMemberAction>
        <SourcedMemberAction>
            <SourceFixlet>
                <Sitename>BES Support</Sitename>
                <FixletID>{ConfigureCredentials_id}</FixletID>
                <Action>Action1</Action>
            </SourceFixlet>
            <Parameter Name="RESTUsername">{args.user}</Parameter>
            <Parameter Name="RESTURL"><![CDATA[https://localhost:52311/api]]></Parameter>
            <SecureParameter Name="RESTPassword"><![CDATA[{password}]]></SecureParameter>
        </SourcedMemberAction>
        <SourcedMemberAction>
            <SourceFixlet>
                <Sitename>BES Support</Sitename>
                <FixletID>{EnableWakeOnLAN_id}</FixletID>
                <Action>Action1</Action>
            </SourceFixlet>
        </SourcedMemberAction>
        <Settings>
			<HasEndTime>true</HasEndTime>
			<EndDateTimeLocalOffset>P7D</EndDateTimeLocalOffset>
		</Settings>
        <Target>
            <ComputerID>{root_id}</ComputerID>
        </Target>
	</MultipleActionGroup>
</BES>"""

    # create action to setup server plugin service:
    action_result = bes_conn.post("actions", XML_String_MultiActionGroup)

    logging.info(action_result)

    logging.info("----- Ending Session ------")
    print("main() End")


if __name__ == "__main__":
    main()
