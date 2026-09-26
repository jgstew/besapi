"""
Plugin utilities for Linux systems, including reading the BigFix root server
MasterOperatorCredentials file and decrypting values with the CryptoUtility.

binary shipped with the server.

This is the Linux equivalent of the Windows Registry / DPAPI methods in
`besapi.plugin_utilities_win`, and of this shell one liner:

    /var/opt/BESServer/Applications/CryptoUtility -d -i "$(sed -n \
        's/^[[:space:]]*RESTPassword[[:space:]]*=[[:space:]]*//p' \
        /var/opt/BESServer/Applications/MasterOperatorCredentials | xargs)"

Reading the credentials file normally requires root.
"""

import configparser
import logging
import os
import subprocess  # nosec B404
from typing import Dict, List, Union

import besapi

logger = logging.getLogger(__name__)

# folders to search for the BigFix server Applications files:
BES_APPLICATION_DIRS = [
    "/var/opt/BESServer/Applications",
    "/var/opt/BESWebReportsServer/Applications",
]

CRYPTO_UTILITY_NAME = "CryptoUtility"
CREDENTIALS_FILE_NAME = "MasterOperatorCredentials"

# env var overrides, keyed by the file name they override:
BES_APPLICATION_FILE_ENV_VARS = {
    CRYPTO_UTILITY_NAME: "BESAPI_CRYPTO_UTILITY",
    CREDENTIALS_FILE_NAME: "BESAPI_MASTER_OPERATOR_CREDENTIALS",
}

# section name injected before parsing, the real file has no section header:
_INI_SECTION = "besapi"


def find_bes_application_file(
    file_name: str, search_dirs: Union[List[str], None] = None
) -> Union[str, None]:
    """Find a file in the BigFix server Applications folder.

    An env var override, if set for this file name, takes precedence over
    `search_dirs`.

    Args:
        file_name: The file to look for, such as `CryptoUtility`.
        search_dirs: Folders to search, defaults to BES_APPLICATION_DIRS.

    Returns:
        The path to the first existing file found, otherwise None.
    """
    env_var = BES_APPLICATION_FILE_ENV_VARS.get(file_name)

    if env_var:
        env_path = os.environ.get(env_var)
        if env_path and os.path.isfile(env_path):
            logger.debug("using `%s` from env var %s", file_name, env_var)
            return env_path
        if env_path:
            logger.debug("env var %s is set but `%s` is not a file", env_var, env_path)

    for folder in search_dirs or BES_APPLICATION_DIRS:
        file_path = os.path.join(folder, file_name)
        if os.path.isfile(file_path):
            return file_path

    logger.debug("`%s` not found in any BigFix Applications folder.", file_name)
    return None


def parse_credentials_file(file_path: Union[str, None] = None) -> Dict[str, str]:
    """Parse the MasterOperatorCredentials file.

    The file is `Key=Value` per line with no section header, for example:

        RESTUsername=USER
        RESTPassword={aes,1}ENCRYPTEDPASSWORD
        RESTURL=https://localhost:52311/api

    Args:
        file_path: Path to the credentials file, located automatically if not given.

    Returns:
        A dict of the key value pairs, empty if the file is missing or unreadable.
    """
    if not file_path:
        file_path = find_bes_application_file(CREDENTIALS_FILE_NAME)

    if not file_path:
        return {}

    try:
        with open(file_path, encoding="utf-8") as file_handle:
            contents = file_handle.read()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        logger.debug("credentials file `%s` not found.", file_path)
        return {}
    except PermissionError:
        # reading this file normally requires root:
        logger.debug("no permission to read credentials file `%s`.", file_path)
        return {}
    except (OSError, UnicodeDecodeError) as err:
        logger.error("failed to read credentials file: %s", err)
        return {}

    # configparser treats a leading space as a value continuation line,
    # so strip each line first. This also matches the `sed`/`xargs` behavior
    # of tolerating whitespace around the key and the value:
    stripped = "\n".join(line.strip() for line in contents.splitlines())

    # interpolation is disabled so `%` in a value is not special,
    # `=` is the only delimiter so a value like a URL containing `:` is kept whole,
    # strict is off so a duplicated key does not raise:
    config = configparser.ConfigParser(
        interpolation=None, delimiters=("=",), strict=False
    )
    # preserve the case of the keys, `RESTPassword` not `restpassword`:
    config.optionxform = str  # type: ignore[assignment,method-assign]

    try:
        config.read_string(f"[{_INI_SECTION}]\n" + stripped, source=file_path)
    except configparser.Error as err:
        logger.error("failed to parse credentials file: %s", err)
        return {}

    return dict(config[_INI_SECTION])


def crypto_utility_decrypt(
    encrypted_value: str,
    crypto_utility_path: Union[str, None] = None,
    timeout: int = 30,
) -> Union[str, None]:
    """Decrypt a value using the BigFix server CryptoUtility binary.

    Equivalent to: `CryptoUtility -d -i "<encrypted_value>"`

    Args:
        encrypted_value: The encrypted value, including its `{aes,1}` prefix.
        crypto_utility_path: Path to CryptoUtility, located automatically if not given.
        timeout: Seconds to wait for CryptoUtility before giving up.

    Returns:
        The decrypted string, otherwise None.
    """
    if not encrypted_value or encrypted_value.strip() == "":
        logger.warning("No encrypted value provided for decryption.")
        return None

    if not crypto_utility_path:
        crypto_utility_path = find_bes_application_file(CRYPTO_UTILITY_NAME)
        if not crypto_utility_path:
            logger.debug("CryptoUtility not found, cannot decrypt.")
            return None

    # NOTE: the command is a list and shell is False,
    # so the encrypted value is never interpreted by a shell:
    try:
        result = subprocess.run(  # nosec B603
            [crypto_utility_path, "-d", "-i", encrypted_value],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    # TimeoutExpired covers a hung CryptoUtility,
    # OSError covers it being missing or not executable:
    except (subprocess.TimeoutExpired, OSError) as err:
        logger.error("failed to run CryptoUtility: %s", err)
        return None

    if result.returncode != 0:
        # NOTE: never log stdout here, it would contain the plaintext:
        logger.error(
            "CryptoUtility failed with return code %s: %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return None

    decrypted = (result.stdout or "").strip()

    if not decrypted:
        logger.debug("CryptoUtility returned no data.")
        return None

    # NOTE: only the length is logged, never the plaintext:
    logger.debug("CryptoUtility decrypted value length: %s", len(decrypted))
    return decrypted


def get_linux_credentials_rest_pass(
    file_path: Union[str, None] = None,
) -> Union[str, None]:
    """Get the decrypted REST Password from the MasterOperatorCredentials file.

    Args:
        file_path: Path to the credentials file, located automatically if not given.

    Returns:
        The REST Password if found and decrypted, otherwise None.
    """
    credentials = parse_credentials_file(file_path)

    encrypted_password = credentials.get("RESTPassword")

    if not encrypted_password:
        logger.debug("No RESTPassword found in credentials file.")
        return None

    # NOTE: the `{aes,1}` prefix is part of what CryptoUtility expects:
    password = crypto_utility_decrypt(encrypted_password)

    if password and len(password) > 3:
        return password

    logger.debug("Decryption failed or decrypted password length is too short.")
    return None


def get_besconn_root_linux(
    file_path: Union[str, None] = None,
) -> Union[besapi.besapi.BESConnection, None]:
    """
    Attempts to create a BESConnection using credentials from the Linux root
    server MasterOperatorCredentials file.

    Args:
        file_path: Path to the credentials file, located automatically if not given.

    Returns:
        A BESConnection object if successful, otherwise None.
    """
    credentials = parse_credentials_file(file_path)

    user = credentials.get("RESTUsername")
    rest_url = credentials.get("RESTURL")

    if not user or not rest_url:
        logger.debug("RESTUsername or RESTURL not found in credentials file.")
        return None

    password = get_linux_credentials_rest_pass(file_path)

    if not password:
        return None

    # normalize url to https://HostOrIP:52311
    if rest_url.endswith("/api"):
        rest_url = rest_url.replace("/api", "")

    try:
        return besapi.besapi.BESConnection(user, password, rest_url)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Failed to create BESConnection from credentials file: %s", e)
        return None
