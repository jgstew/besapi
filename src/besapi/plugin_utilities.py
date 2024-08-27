"""This is a set of utility functions for use in multiple plugins"""

import argparse
import logging
import logging.handlers
import ntpath
import os
import sys


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


def setup_plugin_argparse(plugin_args_required=False):
    """setup argparse for plugin use"""
    arg_parser = argparse.ArgumentParser(
        description="Provde command line arguments for REST URL, username, and password"
    )
    arg_parser.add_argument(
        "-v",
        "--verbose",
        help="Set verbose output",
        required=False,
        action="count",
        default=0,
    )
    arg_parser.add_argument(
        "-c",
        "--console",
        help="log output to console",
        required=False,
        action="store_true",
    )
    arg_parser.add_argument(
        "-besserver", "--besserver", help="Specify the BES URL", required=False
    )
    arg_parser.add_argument(
        "-r", "--rest-url", help="Specify the REST URL", required=plugin_args_required
    )
    arg_parser.add_argument(
        "-u", "--user", help="Specify the username", required=plugin_args_required
    )
    arg_parser.add_argument(
        "-p", "--password", help="Specify the password", required=False
    )

    return arg_parser


def setup_plugin_logging(log_file_name="", verbose=0, console=True):
    """setup logging for plugin use"""
    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder(verbose)

    if not log_file_name or log_file_name == "":
        log_file_name = get_invoke_file_name() + ".log"

    # set different log levels:
    log_level = logging.WARNING
    if verbose:
        log_level = logging.INFO
    if verbose > 1:
        log_level = logging.DEBUG

    # get path to put log file in:
    log_filename = os.path.join(invoke_folder, log_file_name)

    handlers = [
        logging.handlers.RotatingFileHandler(
            log_filename, maxBytes=5 * 1024 * 1024, backupCount=1
        )
    ]

    # log output to console if arg provided:
    if console:
        handlers.append(logging.StreamHandler())

    # setup logging:
    logging.basicConfig(
        encoding="utf-8",
        level=log_level,
        format="%(asctime)s %(levelname)s:%(message)s",
        handlers=handlers,
    )


# if __name__ == "__main__":
#     print(get_invoke_folder())
#     print(get_invoke_file_name())
#     setup_plugin_logging(console=True)
#     logging.error("test logging")
#     parser = setup_plugin_argparse()
#     # allow unknown args to be parsed instead of throwing an error:
#     args, _unknown = parser.parse_known_args()

#     logging.error(args)
