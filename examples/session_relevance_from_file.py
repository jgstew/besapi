"""
Example session relevance results from a file.

requires `besapi`, install with command `pip install besapi`
"""

import logging
import ntpath
import os
import platform
import sys

import besapi
import besapi.plugin_utilities

__version__ = "1.2.1"
verbose = 0
invoke_folder = None


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


def main():
    """Execution starts here."""
    print("main()")
    print("NOTE: this script requires besapi v3.3.3+ due to besapi.plugin_utilities")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additional arg specific to this script:
    parser.add_argument(
        "-f",
        "--file",
        help="text file to read session relevance query from",
        required=False,
        type=str,
        default="examples/session_relevance_query_input.txt",
    )
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global verbose, invoke_folder
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

    logging.log(99, "---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("%s's version: %s", get_invoke_file_name(verbose), __version__)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.sys.version)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    # args.file defaults to "examples/session_relevance_query_input.txt"
    with open(args.file) as file:
        session_relevance = file.read()

    result = bes_conn.session_relevance_string(session_relevance)

    logging.debug(result)

    with open("examples/session_relevance_query_output.txt", "w") as file_out:
        file_out.write(result)

    logging.log(99, "---------- END -----------")


if __name__ == "__main__":
    main()
