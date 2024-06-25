"""
This will export all bigfix sites to a folder called `export`

This is equivalent of running `python -m besapi export_all_sites`

requires `besapi`, install with command `pip install besapi`

Example Usage:
python export_all_sites.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

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
import shutil
import sys

import besapi

__version__ = "0.0.1"
verbose = 0
bes_conn = None
invoke_folder = None


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
    parser.add_argument(
        "-d",
        "--delete",
        help="delete previous export",
        required=False,
        action="store_true",
    )
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
    log_filename = os.path.join(invoke_folder, "export_all_sites.log")

    print(f"Log File Path: {log_filename}")

    handlers = [
        logging.handlers.RotatingFileHandler(
            log_filename, maxBytes=5 * 1024 * 1024, backupCount=1
        )
    ]

    # log output to console:
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

    export_folder = os.path.join(invoke_folder, "export")

    # if --delete arg used, delete export folder:
    if args.delete:
        shutil.rmtree(export_folder, ignore_errors=True)

    try:
        os.mkdir(export_folder)
    except FileExistsError:
        logging.warning("Folder already exists!")

    os.chdir(export_folder)

    bes_conn.export_all_sites()


if __name__ == "__main__":
    main()
