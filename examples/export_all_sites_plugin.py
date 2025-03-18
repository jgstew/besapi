"""
This will export all bigfix sites to a folder called `export`.

This is equivalent of running `python -m besapi export_all_sites`

requires `besapi`, install with command `pip install besapi`

Example Usage:
python export_all_sites.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

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
import subprocess
import sys

import besapi
import besapi.plugin_utilities

__version__ = "1.1.1"
verbose = 0
bes_conn = None
invoke_folder = None

GIT_PATHS = [r"C:\Program Files\Git\bin\git.exe", "/usr/bin/git"]


def get_invoke_folder(verbose=0):
    """Get the folder the script was invoked from."""
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
    """Get the filename the script was invoked from."""
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


def find_executable(path_array, default=None):
    """Find executable from array of paths."""

    for path in path_array:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return default


def main():
    """Execution starts here."""
    print("main() start")

    print("NOTE: this script requires besapi v3.3.3+")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additional arg specific to this script:
    parser.add_argument(
        "-d",
        "--delete",
        help="delete previous export",
        required=False,
        action="store_true",
    )

    parser.add_argument(
        "--repo-subfolder",
        help="subfolder to export to, then attempt to add, commit, push changes",
        required=False,
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

    logging.info("----- Starting New Session ------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    export_folder = os.path.join(invoke_folder, "export")

    if args.repo_subfolder:
        logging.debug("Repo Specified: %s", args.repo_subfolder)
        export_folder = os.path.join(invoke_folder, args.repo_subfolder, "export")

    try:
        os.mkdir(export_folder)
    except FileExistsError:
        logging.warning("Folder already exists!")

    os.chdir(export_folder)

    # this will get changed later:
    result = None

    try:
        if args.repo_subfolder:
            git_path = shutil.which("git")
            if not git_path:
                logging.warning("could not find git on path")
                git_path = find_executable(GIT_PATHS, "git")
            logging.info("Using this path to git: %s", git_path)

            result = subprocess.run(
                [git_path, "fetch", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
            logging.debug(result.stdout)
            result = subprocess.run(
                [git_path, "reset", "--hard", "origin/main"],
                check=True,
                capture_output=True,
                text=True,
            )
            logging.debug(result.stdout)
            logging.info("Now attempting to git pull repo.")
            result = subprocess.run(
                [git_path, "pull"], check=True, capture_output=True, text=True
            )
            logging.debug(result.stdout)

        # if --delete arg used, delete export folder:
        if args.delete:
            shutil.rmtree(export_folder, ignore_errors=True)

        logging.info("Now exporting content to folder.")
        bes_conn.export_all_sites()

        if args.repo_subfolder:
            logging.info("Now attempting to add, commit, and push repo.")
            result = subprocess.run(
                [git_path, "add", "."],
                check=False,
                capture_output=True,
                text=True,
            )
            logging.debug(result.stdout)

            result = subprocess.run(
                [git_path, "commit", "-m", "add changes from export"],
                check=False,
                capture_output=True,
                text=True,
            )
            logging.debug(result.stdout)
            # stop without error if nothing to add, nothing to commit
            if "nothing to commit" in result.stdout:
                logging.info("No changes to commit.")
                logging.info("----- Session Ended ------")
                return 0

            result = subprocess.run(
                [git_path, "push"],
                check=False,
                capture_output=True,
                text=True,
            )
            logging.debug(result.stdout)
    except subprocess.CalledProcessError as err:
        logging.error("Subprocess error: %s", err)
        logging.debug(result.stdout)
        raise
    except BaseException as err:
        logging.error("An error occurred: %s", err)
        logging.debug(result.stdout)
        raise

    logging.info("----- Session Ended ------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
