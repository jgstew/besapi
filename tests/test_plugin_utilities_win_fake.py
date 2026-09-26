"""Tests for besapi.plugin_utilities_win that run on any OS.

The Windows only `winreg` and `win32crypt` modules are faked, and the module
under test is loaded as a private copy so the real import state is untouched.
The real DPAPI round trip is covered on Windows by tests/test_besapi.py.
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WIN_MODULE_PATH = os.path.join(ROOT, "src", "besapi", "plugin_utilities_win.py")

# stands in for DPAPI encrypted bytes, a reversible but non-trivial transform:
FAKE_DPAPI_MARKER = b"FAKEDPAPI:"


def fake_crypt_protect_data(data, _desc, _entropy, _reserved, _prompt, _flags):
    """Fake win32crypt.CryptProtectData, returns the encrypted bytes."""
    return FAKE_DPAPI_MARKER + data[::-1]


def fake_crypt_unprotect_data(data, _entropy, _reserved, _prompt, _flags):
    """Fake win32crypt.CryptUnprotectData, returns (description, bytes)."""
    if not data.startswith(FAKE_DPAPI_MARKER):
        raise ValueError("not fake DPAPI data")
    return None, data[len(FAKE_DPAPI_MARKER) :][::-1]


@pytest.fixture
def win_utils(monkeypatch):
    """A private copy of plugin_utilities_win, loaded with faked Windows modules."""
    fake_win32crypt = types.ModuleType("win32crypt")
    fake_win32crypt.CryptProtectData = fake_crypt_protect_data
    fake_win32crypt.CryptUnprotectData = fake_crypt_unprotect_data
    monkeypatch.setitem(sys.modules, "win32crypt", fake_win32crypt)
    monkeypatch.setitem(sys.modules, "winreg", types.ModuleType("winreg"))

    spec = importlib.util.spec_from_file_location(
        "plugin_utilities_win_fake", WIN_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_protect_secret(win_utils):
    """Test that a protected secret is base64 DPAPI data with a prefix."""
    protected = win_utils.protect_secret("plaintextpw")
    assert protected.startswith("{dpapi}")
    assert protected == "{dpapi}" + win_utils.win_dpapi_encrypt_str("plaintextpw")


def test_protect_secret_empty(win_utils):
    """Test that an empty plaintext results in None, not a bare prefix."""
    assert win_utils.protect_secret("") is None


def test_unprotect_secret_round_trip(win_utils):
    """Test that unprotect_secret reverses protect_secret."""
    protected = win_utils.protect_secret("plaintextpw")
    assert win_utils.unprotect_secret(protected) == "plaintextpw"


def test_unprotect_secret_not_protected(win_utils):
    """Test that a value without the prefix is not decrypted."""
    assert win_utils.unprotect_secret("plaintextpw") is None


def test_unprotect_secret_bad_data(win_utils):
    """Test that undecryptable data results in None, not an exception."""
    assert win_utils.unprotect_secret("{dpapi}bm90IGRwYXBp") is None
