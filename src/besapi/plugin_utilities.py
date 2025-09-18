"""This is a set of utility functions for use in multiple plugins.

see example here: https://github.com/jgstew/besapi/blob/master/examples/export_all_sites.py
"""

import argparse
import getpass
import logging
import logging.handlers
import ntpath
import os
import sys
from typing import Union

import besapi

if os.name == "nt":
    import besapi.plugin_utilities_win


# NOTE: This does not work as expected when run from plugin_utilities
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


# NOTE: This does not work as expected when run from plugin_utilities
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


def setup_plugin_argparse(plugin_args_required=False):
    """Setup argparse for plugin use."""
    arg_parser = argparse.ArgumentParser(
        description="Provide command line arguments for REST URL, username, and password"
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


def get_plugin_args(plugin_args_required=False):
    """Get basic args for plugin use."""
    arg_parser = setup_plugin_argparse(plugin_args_required)
    args, _unknown = arg_parser.parse_known_args()
    return args


def get_plugin_logging_config(log_file_path="", verbose=0, console=True):
    """Get config for logging for plugin use.

    use this like: logging.basicConfig(**logging_config)
    """

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

    logging.addLevelName(99, "SESSION")

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


def get_besapi_connection_env_then_config():
    """Get connection to besapi using env vars first, then config file."""
    logging.info("attempting connection to BigFix using ENV method.")
    # try to get connection from env vars:
    bes_conn = besapi.besapi.get_bes_conn_using_env()
    if bes_conn:
        return bes_conn

    logging.info("attempting connection to BigFix using config file method.")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    return bes_conn


def get_besapi_connection_args(
    args: argparse.Namespace,
) -> Union[besapi.besapi.BESConnection, None]:
    """Get connection to besapi using provided args."""
    password = None
    bes_conn = None

    if args.password:
        password = args.password

    # if user was provided as arg but password was not:
    if args.user and not password:
        if os.name == "nt":
            # attempt to get password from windows root server registry:
            # this is specifically for the case where user is provided for a plugin
            password = besapi.plugin_utilities_win.get_win_registry_rest_pass()

    # if user was provided as arg but password was not:
    if args.user and not password:
        logging.warning("Password was not provided, provide REST API password.")
        print("Password was not provided, provide REST API password:")
        password = getpass.getpass()

    if password:
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
                raise AttributeError("args.rest_url is not set.")
            bes_conn = besapi.besapi.BESConnection(args.user, password, rest_url)
        except (
            AttributeError,
            ConnectionRefusedError,
            besapi.besapi.requests.exceptions.ConnectionError,
        ) as e:
            logging.exception(
                "connection to `%s` failed, attempting `%s` instead",
                rest_url,
                args.besserver,
            )
            try:
                if not args.besserver:
                    raise AttributeError("args.besserver is not set.") from e
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
            except BaseException as err:  # pylint: disable=broad-exception-caught
                # always log error
                logging.exception("ERROR: %s", err)
                logging.exception(
                    "----- ERROR: BigFix Connection Failed! Unknown reason ------"
                )
                return None
    else:
        logging.info(
            "No user arg provided, no password found. Cannot create connection."
        )
        return None

    return bes_conn


def get_besapi_connection(
    args: Union[argparse.Namespace, None] = None,
) -> Union[besapi.besapi.BESConnection, None]:
    """Get connection to besapi.

    If on Windows, will attempt to get connection from Windows Registry first.
    If args provided, will attempt to get connection using provided args.
    If no args provided, will attempt to get connection from env vars.
    If no env vars, will attempt to get connection from config file.

    Arguments:
        args: argparse.Namespace object, usually from setup_plugin_argparse()
    Returns:
        A BESConnection object if successful, otherwise None.
    """
    # if windows, try to get connection from windows registry:
    if os.name == "nt":
        bes_conn = besapi.plugin_utilities_win.get_besconn_root_windows_registry()
        if bes_conn:
            return bes_conn

    # if no args provided, try to get connection from env then config file:
    if not args:
        logging.info("no args provided, attempting connection using env then config.")
        return get_besapi_connection_env_then_config()

    # attempt bigfix connection with provided args:
    if args.user:
        bes_conn = get_besapi_connection_args(args)
    else:
        logging.info(
            "no user arg provided, attempting connection using env then config."
        )
        return get_besapi_connection_env_then_config()

    return bes_conn
