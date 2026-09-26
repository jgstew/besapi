"""Tests for besapi.plugin_utilities_linux.

These tests do not require a BigFix root server: the CryptoUtility subprocess
call and the credentials file are both faked.
"""

import os
import subprocess
import sys

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import besapi  # noqa: E402
from besapi import plugin_utilities_linux  # noqa: E402

SAMPLE_CREDENTIALS = """
RESTUsername=USER
RESTPassword={aes,1}ENCRYPTEDPASSWORD==
RESTURL=https://localhost:52311/api
# this is a comment
"""


def write_credentials(tmp_path, contents=SAMPLE_CREDENTIALS):
    """Write a MasterOperatorCredentials style file and return its path."""
    file_path = tmp_path / plugin_utilities_linux.CREDENTIALS_FILE_NAME
    file_path.write_text(contents, encoding="utf-8")
    return str(file_path)


def test_find_bes_application_file_search_dirs(tmp_path):
    """Test that the file is found in a provided search dir."""
    file_path = write_credentials(tmp_path)
    found = plugin_utilities_linux.find_bes_application_file(
        plugin_utilities_linux.CREDENTIALS_FILE_NAME, search_dirs=[str(tmp_path)]
    )
    assert found == file_path


def test_find_bes_application_file_not_found(tmp_path):
    """Test that None is returned when the file does not exist."""
    assert (
        plugin_utilities_linux.find_bes_application_file(
            plugin_utilities_linux.CREDENTIALS_FILE_NAME, search_dirs=[str(tmp_path)]
        )
        is None
    )


def test_find_bes_application_file_env_override(tmp_path, monkeypatch):
    """Test that the env var override takes precedence."""
    file_path = write_credentials(tmp_path)
    monkeypatch.setenv("BESAPI_MASTER_OPERATOR_CREDENTIALS", file_path)
    found = plugin_utilities_linux.find_bes_application_file(
        plugin_utilities_linux.CREDENTIALS_FILE_NAME, search_dirs=["/does/not/exist"]
    )
    assert found == file_path


def test_find_bes_application_file_env_override_crypto(tmp_path, monkeypatch):
    """Test that the CryptoUtility env var override is honored."""
    file_path = tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME
    file_path.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("BESAPI_CRYPTO_UTILITY", str(file_path))
    found = plugin_utilities_linux.find_bes_application_file(
        plugin_utilities_linux.CRYPTO_UTILITY_NAME, search_dirs=["/does/not/exist"]
    )
    assert found == str(file_path)


def test_parse_credentials_file(tmp_path):
    """Test parsing of the MasterOperatorCredentials file format."""
    creds = plugin_utilities_linux.parse_credentials_file(write_credentials(tmp_path))
    assert creds["RESTUsername"] == "USER"
    # value contains `=` characters, only the first `=` is a separator:
    assert creds["RESTPassword"] == "{aes,1}ENCRYPTEDPASSWORD=="
    assert creds["RESTURL"] == "https://localhost:52311/api"
    # comment lines are ignored:
    assert len(creds) == 3


def test_parse_credentials_file_whitespace(tmp_path):
    """Test that surrounding whitespace is stripped from keys and values."""
    creds = plugin_utilities_linux.parse_credentials_file(
        write_credentials(tmp_path, "  RESTUsername  =  USER  \n")
    )
    assert creds == {"RESTUsername": "USER"}


def test_parse_credentials_file_missing(tmp_path):
    """Test that a missing file results in an empty dict."""
    missing = str(tmp_path / "does_not_exist")
    assert plugin_utilities_linux.parse_credentials_file(missing) == {}


def test_parse_credentials_file_none_not_found(monkeypatch):
    """Test that an empty dict is returned when the file cannot be located."""
    monkeypatch.setattr(
        plugin_utilities_linux, "find_bes_application_file", lambda *a, **kw: None
    )
    assert plugin_utilities_linux.parse_credentials_file() == {}


def test_crypto_utility_decrypt(monkeypatch, tmp_path):
    """Test that CryptoUtility stdout is returned stripped."""
    crypto_path = str(tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="plaintextpw\n", stderr="")

    monkeypatch.setattr(plugin_utilities_linux.subprocess, "run", fake_run)

    result = plugin_utilities_linux.crypto_utility_decrypt(
        "{aes,1}ENCRYPTEDPASSWORD", crypto_utility_path=crypto_path
    )
    assert result == "plaintextpw"
    cmd, kwargs = calls[0]
    assert cmd == [crypto_path, "-d", "-i", "{aes,1}ENCRYPTEDPASSWORD"]
    # must not go through a shell:
    assert not kwargs.get("shell", False)


def test_crypto_utility_decrypt_failure(monkeypatch, tmp_path):
    """Test that a non-zero return code results in None."""
    crypto_path = str(tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME)

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad input")

    monkeypatch.setattr(plugin_utilities_linux.subprocess, "run", fake_run)
    assert (
        plugin_utilities_linux.crypto_utility_decrypt(
            "{aes,1}BAD", crypto_utility_path=crypto_path
        )
        is None
    )


def test_crypto_utility_decrypt_timeout(monkeypatch, tmp_path):
    """Test that a subprocess timeout results in None."""
    crypto_path = str(tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME)

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(plugin_utilities_linux.subprocess, "run", fake_run)
    assert (
        plugin_utilities_linux.crypto_utility_decrypt(
            "{aes,1}ENCRYPTEDPASSWORD", crypto_utility_path=crypto_path
        )
        is None
    )


def test_crypto_utility_decrypt_no_utility(monkeypatch):
    """Test that a missing CryptoUtility results in None."""
    monkeypatch.setattr(
        plugin_utilities_linux, "find_bes_application_file", lambda *a, **kw: None
    )
    assert plugin_utilities_linux.crypto_utility_decrypt("{aes,1}X") is None


def test_crypto_utility_decrypt_empty_value():
    """Test that an empty encrypted value results in None."""
    assert plugin_utilities_linux.crypto_utility_decrypt("") is None


def test_get_linux_credentials_rest_pass(monkeypatch, tmp_path):
    """Test that the encrypted password is passed to CryptoUtility as-is."""
    decrypted = []

    def fake_decrypt(encrypted_value, **_kwargs):
        decrypted.append(encrypted_value)
        return "plaintextpw"

    monkeypatch.setattr(plugin_utilities_linux, "crypto_utility_decrypt", fake_decrypt)

    password = plugin_utilities_linux.get_linux_credentials_rest_pass(
        write_credentials(tmp_path)
    )
    assert password == "plaintextpw"
    # the `{aes,1}` prefix is included, matching the shell equivalent:
    assert decrypted == ["{aes,1}ENCRYPTEDPASSWORD=="]


def test_get_linux_credentials_rest_pass_too_short(monkeypatch, tmp_path):
    """Test that an implausibly short decrypted password results in None."""
    monkeypatch.setattr(
        plugin_utilities_linux, "crypto_utility_decrypt", lambda *a, **kw: "ab"
    )
    assert (
        plugin_utilities_linux.get_linux_credentials_rest_pass(
            write_credentials(tmp_path)
        )
        is None
    )


def test_get_linux_credentials_rest_pass_no_password(tmp_path):
    """Test that a credentials file without RESTPassword results in None."""
    assert (
        plugin_utilities_linux.get_linux_credentials_rest_pass(
            write_credentials(tmp_path, "RESTUsername=USER\n")
        )
        is None
    )


def test_get_besconn_root_linux(monkeypatch, tmp_path):
    """Test that a BESConnection is created with a normalized URL."""
    recorded = {}

    class FakeConnection:
        def __init__(self, user, password, rest_url):
            recorded["args"] = (user, password, rest_url)

    monkeypatch.setattr(besapi.besapi, "BESConnection", FakeConnection)
    monkeypatch.setattr(
        plugin_utilities_linux, "crypto_utility_decrypt", lambda *a, **kw: "plaintextpw"
    )

    conn = plugin_utilities_linux.get_besconn_root_linux(write_credentials(tmp_path))
    assert isinstance(conn, FakeConnection)
    # `/api` is stripped from the REST URL:
    assert recorded["args"] == ("USER", "plaintextpw", "https://localhost:52311")


def test_get_besconn_root_linux_no_credentials(tmp_path):
    """Test that a missing credentials file results in None."""
    assert (
        plugin_utilities_linux.get_besconn_root_linux(str(tmp_path / "missing")) is None
    )


def test_get_besconn_root_linux_connection_error(monkeypatch, tmp_path):
    """Test that a failed connection results in None rather than an exception."""

    def fake_connection(*_args, **_kwargs):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(besapi.besapi, "BESConnection", fake_connection)
    monkeypatch.setattr(
        plugin_utilities_linux, "crypto_utility_decrypt", lambda *a, **kw: "plaintextpw"
    )
    assert (
        plugin_utilities_linux.get_besconn_root_linux(write_credentials(tmp_path))
        is None
    )


def test_parse_credentials_file_not_utf8(tmp_path):
    """Test that a non utf-8 file results in an empty dict rather than raising."""
    file_path = tmp_path / plugin_utilities_linux.CREDENTIALS_FILE_NAME
    file_path.write_bytes(b"RESTUsername=\xff\xfe\x00binary\n")
    assert plugin_utilities_linux.parse_credentials_file(str(file_path)) == {}


def test_get_besconn_root_linux_not_utf8(tmp_path):
    """Test that an unreadable credentials file results in None."""
    file_path = tmp_path / plugin_utilities_linux.CREDENTIALS_FILE_NAME
    file_path.write_bytes(b"\xff\xfe\x00\n")
    assert plugin_utilities_linux.get_besconn_root_linux(str(file_path)) is None


def test_crypto_utility_encrypt(monkeypatch, tmp_path):
    """Test that plaintext is passed to CryptoUtility with the encrypt args."""
    crypto_path = str(tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="{aes,1}ENC==\n", stderr="")

    monkeypatch.setattr(plugin_utilities_linux.subprocess, "run", fake_run)

    result = plugin_utilities_linux.crypto_utility_encrypt(
        "plaintextpw", crypto_utility_path=crypto_path
    )
    assert result == "{aes,1}ENC=="
    cmd, kwargs = calls[0]
    # encrypting is the CryptoUtility default, there is no `-e` flag:
    assert cmd == [crypto_path, "-i", "plaintextpw"]
    # must not go through a shell:
    assert not kwargs.get("shell", False)


def test_crypto_utility_encrypt_failure(monkeypatch, tmp_path):
    """Test that a non-zero return code results in None."""
    crypto_path = str(tmp_path / plugin_utilities_linux.CRYPTO_UTILITY_NAME)

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad input")

    monkeypatch.setattr(plugin_utilities_linux.subprocess, "run", fake_run)
    assert (
        plugin_utilities_linux.crypto_utility_encrypt(
            "plaintextpw", crypto_utility_path=crypto_path
        )
        is None
    )


def test_crypto_utility_encrypt_empty_value():
    """Test that an empty plaintext results in None."""
    assert plugin_utilities_linux.crypto_utility_encrypt("") is None


def test_protect_secret(monkeypatch):
    """Test that a protected secret is the CryptoUtility output with a prefix."""
    monkeypatch.setattr(
        plugin_utilities_linux,
        "crypto_utility_encrypt",
        lambda plaintext, **_kw: "{aes,1}ENC_" + plaintext,
    )
    assert (
        plugin_utilities_linux.protect_secret("plaintextpw")
        == "{cryptoutility}{aes,1}ENC_plaintextpw"
    )


def test_protect_secret_failure(monkeypatch):
    """Test that a failed encryption results in None, not a bare prefix."""
    monkeypatch.setattr(
        plugin_utilities_linux, "crypto_utility_encrypt", lambda *_a, **_kw: None
    )
    assert plugin_utilities_linux.protect_secret("plaintextpw") is None


def test_unprotect_secret(monkeypatch):
    """Test that the prefix is removed before decrypting with CryptoUtility."""
    decrypted = []

    def fake_decrypt(encrypted_value, **_kwargs):
        decrypted.append(encrypted_value)
        return "plaintextpw"

    monkeypatch.setattr(plugin_utilities_linux, "crypto_utility_decrypt", fake_decrypt)

    assert (
        plugin_utilities_linux.unprotect_secret("{cryptoutility}{aes,1}ENC==")
        == "plaintextpw"
    )
    assert decrypted == ["{aes,1}ENC=="]


def test_unprotect_secret_not_protected(monkeypatch):
    """Test that a value without the prefix is not sent to CryptoUtility."""

    def fake_decrypt(*_args, **_kwargs):
        raise AssertionError("must not decrypt a value without the prefix")

    monkeypatch.setattr(plugin_utilities_linux, "crypto_utility_decrypt", fake_decrypt)
    assert plugin_utilities_linux.unprotect_secret("plaintextpw") is None
