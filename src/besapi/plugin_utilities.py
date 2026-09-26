"""Utility functions shared by BigFix plugins, such as Server Plugin Services.

A plugin is a script run on a schedule, often by the BigFix Server Plugin
Service on the root server (as root on Linux, SYSTEM on Windows), with an
unrelated working directory and a minimal PATH. These utilities handle the
setup every plugin needs, so a plugin can be as short as:

    import besapi.plugin_utilities

    with besapi.plugin_utilities.init_plugin(__version__) as (args, bes_conn):
        config = besapi.plugin_utilities.get_plugin_config()
        ...

Main areas:

- Setup: init_plugin() parses the standard args, configures logging to
  `<plugin name>.log` next to the plugin, logs session start and end banners
  and makes the BigFix connection. setup_plugin_argparse(),
  get_plugin_logging_config() and plugin_session() are the parts it uses.
- Connection: get_besapi_connection() tries explicit args first, then the
  local root server's own credentials, then env vars, then the besapi config
  file. The root server credentials come from the platform specific module,
  besapi.plugin_utilities_win (Windows Registry and DPAPI) or
  besapi.plugin_utilities_linux (MasterOperatorCredentials and CryptoUtility),
  and are only used if this happens to be a root server.
- Files: resolve_plugin_path() finds files relative to the plugin, not the
  working directory. get_plugin_config() loads `<plugin name>.config.yaml`,
  and consume_trigger_file() lets an action request a plugin run.
- Secrets: protect_plugin_config_secrets() encrypts plaintext secrets in the
  config file in place, with the same keys as the root server's own REST API
  password, once the plugin has used them successfully.
  get_plugin_config(secret_keys=...) decrypts them again.
- Commands: find_executable() and run_logged() run external tools with
  their output logged.

The platform specific module is imported here only if available, and a
failure in it never prevents the other connection methods.

See examples/bigfix_plugin_mqtt_homeassistant.py for a complete plugin.
"""

import argparse
import contextlib
import getpass
import logging
import logging.handlers
import ntpath
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from typing import Any, Iterator, List, Sequence, Tuple, Union

import besapi

# the platform specific root server utilities module, or None if unavailable.
# NOTE: these are conveniences for plugins running on a root server,
# so a failure to import must never prevent the other connection methods:
PLATFORM_UTILITIES = None

try:
    if os.name == "nt":
        import besapi.plugin_utilities_win

        PLATFORM_UTILITIES = besapi.plugin_utilities_win
    # NOTE: linux only, not all posix. macOS is never a root server,
    # so none of these files will be in place there:
    elif sys.platform.startswith("linux"):
        import besapi.plugin_utilities_linux

        PLATFORM_UTILITIES = besapi.plugin_utilities_linux
except BaseException as import_error:  # pylint: disable=broad-exception-caught
    logging.debug("platform specific plugin utilities unavailable: %s", import_error)


# marks a secret encrypted by protect_secret(), one prefix per platform module,
# matching PROTECTED_SECRET_PREFIX in plugin_utilities_linux and _win:
PROTECTED_SECRET_PREFIXES = ("{cryptoutility}", "{dpapi}")

# custom log level for session start / end banners in plugin logs:
SESSION_LOG_LEVEL = 99

# default requests timeout for plugin connections: (connect, read) seconds
# NOTE: long read timeout since some REST API calls, like exports, are slow
DEFAULT_PLUGIN_TIMEOUT = (30, 600)


def get_invoke_path(verbose=0) -> Union[str, None]:
    """Get the path of the running plugin script or frozen executable.

    NOTE: `__file__` cannot be used here, it would be this module, not the
    plugin that imported it. The running plugin is `__main__` instead.

    Returns:
        The absolute path, or None if it cannot be determined (interactive).
    """
    # using logging here won't actually log it to the file:

    # frozen by PyInstaller or similar, the plugin is the executable itself:
    if getattr(sys, "frozen", False):
        if verbose:
            print("running in a frozen bundle")
        return os.path.abspath(sys.executable)

    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file:
        return os.path.abspath(main_file)

    # fallback, such as a script run with `python -c` or embedded:
    if sys.argv and sys.argv[0] and sys.argv[0] != "-c":
        return os.path.abspath(sys.argv[0])

    return None


def get_invoke_folder(verbose=0):
    """Get the folder the plugin was invoked from.

    Falls back to the current working directory if not running from a file.
    """
    invoke_path = get_invoke_path(verbose)
    invoke_folder = os.path.dirname(invoke_path) if invoke_path else os.getcwd()

    if verbose:
        print(f"invoke_folder = {invoke_folder}")

    return invoke_folder


def get_invoke_file_name(verbose=0):
    """Get the file name of the plugin, without file extension.

    Falls back to `besapi_plugin` if not running from a file.
    """
    invoke_path = get_invoke_path(verbose)
    if not invoke_path:
        return "besapi_plugin"

    # get just the file name, return without file extension:
    return os.path.splitext(ntpath.basename(invoke_path))[0]


def setup_plugin_argparse(plugin_args_required=False, description=None):
    """Setup argparse for plugin use."""
    arg_parser = argparse.ArgumentParser(
        description=description
        or "Provide command line arguments for REST URL, username, and password"
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

    logging.addLevelName(SESSION_LOG_LEVEL, "SESSION")

    # log output to console if arg provided:
    if console:
        handlers.append(logging.StreamHandler())
        if verbose:
            print("INFO: also logging to console")

    # return logging config:
    return {
        "encoding": "utf-8",
        "level": log_level,
        "format": "%(asctime)s %(levelname)s:%(message)s",
        "handlers": handlers,
        "force": True,
    }


@contextlib.contextmanager
def plugin_session(plugin_version: str = "") -> Iterator[None]:
    """Log the start and end of a plugin run, and any uncaught error.

    Configure logging first, then use this like:

        with besapi.plugin_utilities.plugin_session(__version__):
            ...

    Arguments:
        plugin_version: the plugin's own version, logged for troubleshooting.
    """
    logging.log(SESSION_LOG_LEVEL, "----- Starting New Session ------")
    logging.debug("invoke folder: %s", get_invoke_folder())
    logging.debug("%s version: %s", get_invoke_file_name(), plugin_version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.python_version())
    try:
        yield
    except Exception:
        # NOTE: SystemExit and KeyboardInterrupt are not errors, not logged here
        logging.exception("----- ERROR: uncaught exception in plugin ------")
        raise
    finally:
        logging.log(SESSION_LOG_LEVEL, "----- Ending Session ------")


def resolve_plugin_path(path: str) -> Union[str, None]:
    """Find a file as given, otherwise relative to the plugin's folder.

    A plugin run as a service usually has an unrelated working directory, so
    files that ship next to the plugin (config, trigger files) must be found
    relative to the plugin itself.

    Returns:
        The absolute path of the file found, otherwise None.
    """
    if os.path.isfile(path):
        return os.path.abspath(path)

    plugin_relative = os.path.join(get_invoke_folder(), path)
    if os.path.isfile(plugin_relative):
        return plugin_relative

    return None


def _find_plugin_config(file_name: Union[str, None] = None) -> str:
    """Find the plugin's YAML config file, see get_plugin_config()."""
    file_name = file_name or get_invoke_file_name() + ".config.yaml"

    config_path = resolve_plugin_path(file_name)
    if not config_path:
        raise FileNotFoundError(f"plugin config file not found: {file_name}")

    return config_path


def _import_ruamel_yaml():
    """Import the optional ruamel.yaml library, with a helpful error."""
    try:
        import ruamel.yaml  # pylint: disable=import-outside-toplevel
    except ImportError as err:
        raise ImportError(
            "ruamel.yaml is required to read plugin config files, "
            "install it with: pip install besapi[plugins]"
        ) from err

    return ruamel.yaml


def _get_config_parent(config: Any, key_path: Sequence[str]) -> Any:
    """Get the dict holding the last key of key_path, None if it is not set."""
    parent = config
    for key in key_path[:-1]:
        parent = parent.get(key) if isinstance(parent, dict) else None

    if not isinstance(parent, dict) or not parent.get(key_path[-1]):
        return None

    return parent


def get_plugin_config(
    file_name: Union[str, None] = None,
    secret_keys: Union[Sequence[Sequence[str]], None] = None,
) -> Any:
    """Load the plugin's YAML config file.

    Requires the optional `ruamel.yaml` library: `pip install besapi[plugins]`

    Arguments:
        file_name: config file, resolved with resolve_plugin_path(). Defaults
            to `<plugin name>.config.yaml` next to the plugin.
        secret_keys: key paths of secrets in the config, such as
            `[("mqtt", "password")]`. Each one encrypted by
            protect_plugin_config_secrets() is returned decrypted. The file is
            never changed here, since a plaintext secret is not known to work.

    Returns:
        The parsed YAML, usually a dict.

    Raises:
        ValueError: if a secret is encrypted but cannot be decrypted, since
            sending the encrypted value as a password can never work.
    """
    config_path = _find_plugin_config(file_name)
    ruamel_yaml = _import_ruamel_yaml()

    logging.info("loading config from: `%s`", config_path)
    with open(config_path, encoding="utf-8") as stream:
        config = ruamel_yaml.YAML(typ="safe", pure=True).load(stream)

    for key_path in secret_keys or []:
        parent = _get_config_parent(config, key_path)
        if parent is None or not is_protected_secret(parent[key_path[-1]]):
            continue

        plaintext = unprotect_secret(parent[key_path[-1]])
        if not plaintext:
            name = ".".join(str(key) for key in key_path)
            raise ValueError(
                f"failed to decrypt `{name}` in plugin config `{config_path}`, "
                "replace it with the plaintext value to have it encrypted again"
            )
        parent[key_path[-1]] = plaintext

    return config


def protect_plugin_config_secrets(
    secret_keys: Sequence[Sequence[str]], file_name: Union[str, None] = None
) -> List[str]:
    """Encrypt plaintext secrets in the plugin's YAML config file, in place.

    Call this only after the plugin has used the secrets successfully, such as
    after logging in with a password, so a wrong one is never encrypted and is
    left as plaintext to fix. Encrypting only works on a root server with the
    privileges of the plugin service, see protect_secret(), otherwise the file
    is left alone. Comments and layout in the file are kept.

    This is best effort, a failure is logged and never raised, since the
    plugin has already done its work with the plaintext.

    Arguments:
        secret_keys: key paths of secrets in the config, such as
            `[("mqtt", "password")]`.
        file_name: config file, see get_plugin_config().

    Returns:
        The dotted names of the secrets encrypted, such as `["mqtt.password"]`.
    """
    protected_names: List[str] = []

    try:
        config_path = _find_plugin_config(file_name)
        ruamel_yaml = _import_ruamel_yaml()

        # round trip mode, unlike the safe loader, keeps comments:
        yaml = ruamel_yaml.YAML()
        yaml.preserve_quotes = True

        with open(config_path, encoding="utf-8") as stream:
            contents = stream.read()

        data = yaml.load(contents)
        # round trip mode does not keep the `---` document start by itself:
        yaml.explicit_start = contents.lstrip().startswith("---")

        for key_path in secret_keys:
            parent = _get_config_parent(data, key_path)
            if parent is None or is_protected_secret(parent[key_path[-1]]):
                continue

            protected = protect_secret(str(parent[key_path[-1]]))
            if not protected:
                continue

            parent[key_path[-1]] = protected
            protected_names.append(".".join(str(key) for key in key_path))

        if not protected_names:
            logging.debug("no plaintext secrets encrypted in plugin config.")
            return []

        # write beside the original then swap, so a failure never truncates it:
        file_handle, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(config_path), prefix=".", suffix=".tmp"
        )
        try:
            with os.fdopen(file_handle, "w", encoding="utf-8") as stream:
                yaml.dump(data, stream)
            shutil.copymode(config_path, temp_path)
            os.replace(temp_path, config_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except Exception as err:  # pylint: disable=broad-exception-caught
        logging.warning("failed to encrypt secrets in plugin config: %s", err)
        return []

    logging.info("encrypted plaintext %s in plugin config.", protected_names)
    return protected_names


def consume_trigger_file(path: str) -> bool:
    """Check for a trigger file, and delete it if found.

    This lets an action on the root server request a plugin run by creating
    the trigger file. The file is resolved with resolve_plugin_path().

    Returns:
        True if the trigger file existed (it is now deleted), otherwise False.
    """
    trigger_path = resolve_plugin_path(path)
    if not trigger_path:
        logging.info("trigger file `%s` does not exist.", path)
        return False

    logging.info("trigger file found, removing: `%s`", trigger_path)
    os.remove(trigger_path)
    return True


def find_executable(
    name: str,
    extra_paths: Union[Sequence[str], None] = None,
    default: Union[str, None] = None,
) -> Union[str, None]:
    """Find an executable on the PATH, then in extra_paths.

    Arguments:
        name: executable name to look for on the PATH, such as `git`.
        extra_paths: full paths to try if not on the PATH, such as
            `C:\\Program Files\\Git\\bin\\git.exe`, since services often run
            with a minimal PATH.
        default: returned if nothing is found.
    """
    found = shutil.which(name)
    if found:
        return found

    for path in extra_paths or []:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    logging.debug("executable `%s` not found, using default: %s", name, default)
    return default


def run_logged(
    cmd: List[str], check: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    """Run a command, logging its stdout and stderr.

    Output is logged at DEBUG, or stderr at WARNING if the command fails.
    Many tools (like git) write progress and errors to stderr, which is easy
    to lose otherwise.

    Arguments:
        cmd: the command and its arguments. No shell is used.
        check: raise subprocess.CalledProcessError on a non zero exit code.
        kwargs: passed to subprocess.run
    """
    logging.debug("running: %s", cmd)
    result = subprocess.run(  # nosec B603
        cmd, capture_output=True, text=True, check=False, **kwargs
    )

    if result.stdout:
        logging.debug("stdout: %s", result.stdout)

    stderr_level = logging.WARNING if result.returncode else logging.DEBUG
    if result.stderr:
        logging.log(stderr_level, "stderr: %s", result.stderr)

    if result.returncode:
        logging.warning("command exited with code %s: %s", result.returncode, cmd)
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=result.stdout, stderr=result.stderr
            )

    return result


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


def _try_platform_utility(
    function_name: str, *args, failure_log_level: int = logging.DEBUG
):
    """Call a function from the platform specific utilities module, if possible.

    These are best effort conveniences for the case where the plugin happens to
    be running on a root server. If anything at all goes wrong, this is simply
    not a usable root server, so the caller falls back to the other methods.

    Args:
        function_name: The name of the function to call.
        *args: Arguments to pass to the function.
        failure_log_level: The level to log an error raised by the function at.
            DEBUG by default, since that usually just means not a root server.

    Returns:
        Whatever the function returned, or None if it could not be used.
    """
    if not PLATFORM_UTILITIES:
        logging.debug("no platform specific plugin utilities available.")
        return None

    platform_function = getattr(PLATFORM_UTILITIES, function_name, None)

    if not platform_function:
        logging.debug(
            "`%s` not found in %s", function_name, PLATFORM_UTILITIES.__name__
        )
        return None

    try:
        return platform_function(*args)
    except BaseException as err:  # pylint: disable=broad-exception-caught
        # NOTE: intentionally broad, this must never prevent the other methods:
        logging.log(failure_log_level, "`%s` failed, ignoring: %s", function_name, err)
        return None


def get_root_server_rest_pass() -> Union[str, None]:
    """Get the REST API password from the local root server, if this is one.

    On Windows this reads the registry, otherwise it reads the
    MasterOperatorCredentials file. Returns None if this is not a root server,
    or if the attempt failed for any reason.
    """
    if os.name == "nt":
        return _try_platform_utility("get_win_registry_rest_pass")

    return _try_platform_utility("get_linux_credentials_rest_pass")


def is_protected_secret(value: Any) -> bool:
    """Check if a value is a secret encrypted by protect_secret()."""
    return isinstance(value, str) and value.startswith(PROTECTED_SECRET_PREFIXES)


def protect_secret(plaintext: str) -> Union[str, None]:
    """Encrypt a secret, such as a password in a plugin config file.

    This only works on a root server, with the privileges of the plugin service
    (root on Linux, SYSTEM or an administrator on Windows), since the keys used
    are the same as for the server's own REST API password. The result is
    decrypted again before it is returned, so it is known to be usable.

    NOTE: this checks what the process can do, not which user it runs as, by
    requiring get_root_server_rest_pass() to succeed. A check for root or
    SYSTEM would allow a machine that is not a root server, which lacks the
    keys, and would refuse an administrator on a Windows root server, where
    machine scope DPAPI works and SYSTEM can still decrypt the result. On
    Linux, reading the server's credentials file means root in practice.

    Returns:
        The encrypted secret, otherwise None if it could not be encrypted.
    """
    # proves this is a root server, and that the keys are readable:
    if not get_root_server_rest_pass():
        logging.debug("not a usable root server, cannot protect secret.")
        return None

    # NOTE: this is a root server by now, so an error here is a real problem:
    protected = _try_platform_utility(
        "protect_secret", plaintext, failure_log_level=logging.ERROR
    )

    if not protected:
        return None

    # never trust an encrypted value that cannot be decrypted back:
    if unprotect_secret(protected) != plaintext:
        logging.warning("encrypted secret did not decrypt back, not using it.")
        return None

    return protected


def unprotect_secret(protected: str) -> Union[str, None]:
    """Decrypt a secret from protect_secret(), otherwise return None."""
    return _try_platform_utility(
        "unprotect_secret", protected, failure_log_level=logging.ERROR
    )


def get_besconn_root_server() -> Union[besapi.besapi.BESConnection, None]:
    """Get a connection using local root server credentials, if this is one.

    Returns None if this is not a root server, or if the attempt failed for
    any reason.
    """
    if os.name == "nt":
        return _try_platform_utility("get_besconn_root_windows_registry")

    return _try_platform_utility("get_besconn_root_linux")


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
        # attempt to get password from the local root server:
        # this is specifically for the case where user is provided for a plugin
        password = get_root_server_rest_pass()

    # if user was provided as arg but password was not:
    if args.user and not password:
        # a plugin run as a service has no terminal, prompting would hang:
        if not (sys.stdin and sys.stdin.isatty()):
            logging.error(
                "Password was not provided and there is no terminal to prompt for it."
            )
            return None
        logging.warning("Password was not provided, provide REST API password.")
        print("Password was not provided, provide REST API password:")
        password = getpass.getpass()

    if password:
        logging.debug("REST API Password Length: %s", len(password))

    # process args, setup connection:
    rest_url = args.rest_url

    # normalize url to https://HostOrIP:52311
    # NOTE: only strip a trailing /api, the host itself may contain "/api"
    if rest_url:
        rest_url = rest_url.rstrip("/").removesuffix("/api")

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

    If a user is provided in args, will attempt to connect using the args
    first, then fall back to the local root server credentials.
    Otherwise, will attempt the local root server credentials first:
    on Windows from the Windows Registry, otherwise from the root server
    MasterOperatorCredentials file.
    Then, if no user in args, will attempt to get connection from env vars.
    If no env vars, will attempt to get connection from config file.

    Arguments:
        args: argparse.Namespace object, usually from setup_plugin_argparse()
    Returns:
        A BESConnection object if successful, otherwise None.
    """
    user_provided = args is not None and bool(args.user)

    # explicit args always win, even on a root server:
    if args is not None and user_provided:
        bes_conn = get_besapi_connection_args(args)
        if bes_conn:
            return bes_conn
        logging.warning(
            "connection using provided args failed, trying root server credentials."
        )

    # if this is a root server, try its local credentials:
    # (windows registry, or the linux MasterOperatorCredentials file)
    bes_conn = get_besconn_root_server()
    if bes_conn:
        return bes_conn

    # NOTE: if a user was provided, don't silently connect as someone else:
    if user_provided:
        return None

    logging.info("no user arg provided, attempting connection using env then config.")
    return get_besapi_connection_env_then_config()


@contextlib.contextmanager
def init_plugin(
    plugin_version: str = "",
    parser: Union[argparse.ArgumentParser, None] = None,
    log_file_path: str = "",
    require_connection: bool = True,
    timeout=DEFAULT_PLUGIN_TIMEOUT,
) -> Iterator[Tuple[argparse.Namespace, Union[besapi.besapi.BESConnection, None]]]:
    """Set up a plugin run: args, logging, session banners and connection.

    Use this like:

        with besapi.plugin_utilities.init_plugin(__version__) as (args, bes_conn):
            ...

    Arguments:
        plugin_version: the plugin's own version, logged for troubleshooting.
        parser: from setup_plugin_argparse() with any plugin specific args
            added, defaults to setup_plugin_argparse().
        log_file_path: defaults to `<plugin name>.log` next to the plugin.
        require_connection: if True and no connection can be made, log an
            error and exit with code 1 instead of running the plugin body.
        timeout: request timeout for the connection, replacing the besapi
            wide default, so a plugin service can never hang forever.
    """
    parser = parser or setup_plugin_argparse()
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    logging.basicConfig(
        **get_plugin_logging_config(log_file_path, args.verbose, args.console)
    )

    with plugin_session(plugin_version):
        bes_conn = get_besapi_connection(args)

        if not bes_conn and require_connection:
            logging.error("----- ERROR: BigFix connection failed, exiting ------")
            raise SystemExit(1)

        # NOTE: only for connections that support a default timeout (BESConnection)
        if timeout and bes_conn is not None and hasattr(bes_conn, "timeout"):
            bes_conn.timeout = timeout

        yield args, bes_conn
