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
import besapi.plugin_utilities

__version__ = "0.0.1"
verbose = 0
bes_conn = None
invoke_folder = None


def main():
    """Execution starts here"""
    print("main() start")

    print("NOTE: this script requires besapi v3.3.3+")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additonal arg specific to this script:
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
    invoke_folder = besapi.plugin_utilities.get_invoke_folder()

    besapi.plugin_utilities.setup_plugin_logging()

    logging.info("----- Starting New Session ------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

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
