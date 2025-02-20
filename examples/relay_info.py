"""
This will get info about relays in the environment

requires `besapi`, install with command `pip install besapi`

Example Usage:
python relay_info.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

References:
- https://developer.bigfix.com/rest-api/api/admin.html
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
- https://github.com/jgstew/tools/blob/master/Python/locate_self.py
"""

import logging
import logging.handlers
import ntpath
import os
import platform
import shutil
import sys

import besapi
import besapi.plugin_utilities

__version__ = "1.1.1"
verbose = 0
bes_conn = None
invoke_folder = None


def get_invoke_folder(verbose=0):
    """Get the folder the script was invoked from"""
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


def get_invoke_file_name(verbose=0):
    """Get the filename the script was invoked from"""
    # using logging here won't actually log it to the file:

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        if verbose:
            print("running in a PyInstaller bundle")
        invoke_file_path = sys.executable
    else:
        if verbose:
            print("running in a normal Python process")
        invoke_file_path = __file__

    if verbose:
        print(f"invoke_file_path = {invoke_file_path}")

    # get just the file name, return without file extension:
    return os.path.splitext(ntpath.basename(invoke_file_path))[0]


def main():
    """Execution starts here"""
    print("main() start")

    print("NOTE: this script requires besapi v3.3.3+ due to besapi.plugin_utilities")
    print(
        "WARNING: results may be incorrect if not run as a MO or an account without scope of all computers"
    )

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additonal arg specific to this script:
    parser.add_argument(
        "-d",
        "--days",
        help="last report days to filter on",
        required=False,
        type=int,
        default=900,
    )
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global bes_conn, verbose, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder()

    log_file_path = os.path.join(
        get_invoke_folder(verbose), get_invoke_file_name(verbose) + ".log"
    )

    print(log_file_path)

    logging_config = besapi.plugin_utilities.get_plugin_logging_config(
        log_file_path, verbose, args.console
    )

    logging.basicConfig(**logging_config)

    logging.info("---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    # get relay info:
    last_report_days_filter = args.days

    session_relevance = f"""(multiplicity of it, it) of unique values of (it as string) of (relay selection method of it | "NoRelayMethod" , relay server of it | "NoRelayHostname") of bes computers whose(now - last report time of it < {last_report_days_filter} * day)"""
    results = bes_conn.session_relevance_string(session_relevance)

    logging.info("Relay Info:\n" + results)

    session_relevance = f"""(multiplicity of it, it) of unique values of (it as string) of (relay selection method of it | "NoRelayMethod" , relay server of it | "NoRelayServer", relay hostname of it | "NoRelayHostname") of bes computers whose(now - last report time of it < {last_report_days_filter} * day AND relay server flag of it)"""
    results = bes_conn.session_relevance_string(session_relevance)

    logging.info("Info on Relays:\n" + results)

    session_relevance = """unique values of values of client settings whose(name of it = "_BESClient_Relay_NameOverride") of bes computers"""
    results = bes_conn.session_relevance_string(session_relevance)

    logging.info("Relay name override values:\n" + results)

    logging.info("---------- Ending Session -----------")


if __name__ == "__main__":
    main()
