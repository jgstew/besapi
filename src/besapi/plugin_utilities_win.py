"""
Plugin utilities for Windows systems, including DPAPI encryption/decryption
and Windows Registry access.
"""

import base64
import logging
import sys

logger = logging.getLogger(__name__)

try:
    import winreg
except (ImportError, ModuleNotFoundError) as e:
    if not sys.platform.startswith("win"):
        raise RuntimeError(
            "This script requires the 'winreg' module, which is only available on Windows."
        ) from e
    else:
        raise e

try:
    import win32crypt
except (ImportError, ModuleNotFoundError) as e:
    if not sys.platform.startswith("win"):
        raise RuntimeError("This script only works on Windows systems") from e
    raise ImportError(
        "This script requires the pywin32 package. Install it via 'pip install pywin32'."
    ) from e


def win_dpapi_encrypt_str(
    plaintext: str, scope_flags: int = 4, entropy: str | bytes | None = None
) -> str:
    """Encrypt a string using Windows DPAPI and return it as a base64-encoded string.

    Args:
        plaintext (str): The string to encrypt.
        scope_flags (int): The context scope for encryption (default is 4).
        entropy (bytes | None): Optional entropy for encryption.

    Returns:
        str: The base64-encoded encrypted string.
    """
    if not plaintext or plaintext.strip() == "":
        logger.warning("No plaintext provided for encryption.")
        return ""

    # 1. Convert the plaintext string to bytes
    plaintext_bytes = plaintext.encode("utf-8")

    # 2. Call CryptProtectData.
    # The last parameter (flags) is set to 4
    # to indicate that the data should be encrypted in the machine context.
    #
    # The function returns a tuple: (description, encrypted_bytes)
    # We only need the second element.
    _, encrypted_bytes = win32crypt.CryptProtectData(
        plaintext_bytes,
        None,  # Description
        entropy,  # Optional entropy
        None,  # Reserved
        None,  # Prompt Struct
        scope_flags,
    )

    # 3. Encode the encrypted bytes to a Base64 string
    encrypted_b64 = base64.b64encode(encrypted_bytes).decode("utf-8")

    return encrypted_b64


def win_dpapi_decrypt_base64(
    encrypted_b64: str, scope_flags: int = 4, entropy: str | bytes | None = None
) -> str | None:
    """Decrypt a base64-encoded string encrypted with Windows DPAPI.

    Args:
        encrypted_b64 (str): The base64-encoded encrypted string.
        scope_flags (int): The context scope for decryption (default is 4).
        entropy (bytes | None): Optional entropy for decryption.

    Returns:
        str: The decrypted string.
    """
    if not encrypted_b64 or encrypted_b64.strip() == "":
        logger.warning("No encrypted data provided for decryption.")
        return None

    # 1. Decode the Base64 string to get the raw encrypted bytes
    encrypted_bytes = base64.b64decode(encrypted_b64)

    # 2. Call CryptUnprotectData.
    # The last parameter (flags) is set to 4
    # to indicate that the data was encrypted in the machine context.
    #
    # The function returns a tuple: (description, decrypted_bytes)
    # We only need the second element.
    _, decrypted_bytes = win32crypt.CryptUnprotectData(
        encrypted_bytes,
        entropy,  # Optional entropy
        None,  # Reserved
        None,  # Prompt Struct
        scope_flags,
    )

    return decrypted_bytes.decode("utf-8") if decrypted_bytes else None


def win_registry_value_read(hive, subkey, value_name):
    """
    Reads a value from the Windows Registry.

    Args:
        hive: The root hive (e.g., winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER).
        subkey: The path to the subkey (e.g., "SOFTWARE\\Microsoft\\Windows\\CurrentVersion").
        value_name: The name of the value to read.

    Returns:
        The value data if found, otherwise None.
    """
    try:
        # Open the specified registry key
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)

        # Query the value data
        value_data, _ = winreg.QueryValueEx(key, value_name)

        # Close the key
        winreg.CloseKey(key)

        return value_data
    except FileNotFoundError:
        logger.error("Registry key or value '%s\\%s' not found.", subkey, value_name)
        return None
    except Exception as e:
        logger.error("An error occurred: %s", e)
        return None


def get_win_registry_rest_pass():
    """
    Retrieves the base64 encrypted REST Password from the Windows Registry.

    Returns:
        The REST Password if found, otherwise None.
    """
    hive = winreg.HKEY_LOCAL_MACHINE
    # HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig
    subkey = r"SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig"
    value_name = "RESTPassword"

    reg_value = win_registry_value_read(hive, subkey, value_name)

    if not reg_value:
        logger.error("No registry value found for RESTPassword.")
        return None

    # remove {obf} from start of string if present:
    if reg_value and reg_value.startswith("{obf}"):
        reg_value = reg_value[5:]

    if reg_value:
        return win_dpapi_decrypt_base64(reg_value)

    return None
