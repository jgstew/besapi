"""This is a set of utility functions for use in multiple plugins

see example here: https://github.com/jgstew/besapi/blob/master/examples/export_all_sites.py
"""

import argparse
import getpass
import logging
import logging.handlers
import ntpath
import os
import sys

import besapi


# NOTE: This does not work as expected when run from plugin_utilities
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


# NOTE: This does not work as expected when run from plugin_utilities
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


def get_plugin_logging_config(log_file_path="", verbose=0, console=True):
    """get config for logging for plugin use

    use this like: logging.basicConfig(**logging_config)"""

    if not log_file_path or log_file_path == "":
        log_file_path = os.path.join(
            get_invoke_folder(verbose), get_invoke_file_name(verbose) + ".log"
        )

    # set different log levels:
    log_level = logging.WARNING
    if verbose:
        log_level = logging.INFO
        print("INFO: Log File Path:", log_file_path)
    if verbose > 1:
        log_level = logging.DEBUG

    handlers = [
        logging.handlers.RotatingFileHandler(
            log_file_path, maxBytes=5 * 1024 * 1024, backupCount=1
        )
    ]

    # log output to console if arg provided:
    if console:
        handlers.append(logging.StreamHandler())
        print("INFO: also logging to console")

    # return logging config:
    return {
        "encoding": "utf-8",
        "level": log_level,
        "format": "%(asctime)s %(levelname)s:%(message)s",
        "handlers": handlers,
        "force": True,
    }


def get_besapi_connection(args):
    """get connection to besapi using either args or config file if args not provided"""

    password = args.password

    # if user was provided as arg but password was not:
    if args.user and not password:
        logging.warning("Password was not provided, provide REST API password.")
        print("Password was not provided, provide REST API password:")
        password = getpass.getpass()

    if args.user:
        logging.debug("REST API Password Length: %s", len(password))

    # process args, setup connection:
    rest_url = args.rest_url

    # normalize url to https://HostOrIP:52311
    if rest_url and rest_url.endswith("/api"):
        rest_url = rest_url.replace("/api", "")

    # attempt bigfix connection with provided args:
    if args.user and password:
        try:
            if not rest_url:
                raise AttributeError
            bes_conn = besapi.besapi.BESConnection(args.user, password, rest_url)
        except (
            AttributeError,
            ConnectionRefusedError,
            besapi.besapi.requests.exceptions.ConnectionError,
        ):
            logging.exception(
                "connection to `%s` failed, attempting `%s` instead",
                rest_url,
                args.besserver,
            )
            try:
                if not args.besserver:
                    raise AttributeError
                bes_conn = besapi.besapi.BESConnection(
                    args.user, password, args.besserver
                )
            # handle case where args.besserver is None
            # AttributeError: 'NoneType' object has no attribute 'startswith'
            except AttributeError:
                logging.exception("----- ERROR: BigFix Connection Failed ------")
                logging.exception(
                    "attempts to connect to BigFix using rest_url and besserver both failed"
                )
                return None
            except BaseException as err:
                # always log error and stop the current process
                logging.exception("ERROR: %s", err)
                logging.exception(
                    "----- ERROR: BigFix Connection Failed! Unknown reason ------"
                )
                return None
    else:
        logging.info(
            "attempting connection to BigFix using config file method as user command arg was not provided"
        )
        bes_conn = besapi.besapi.get_bes_conn_using_config_file()

    return bes_conn
