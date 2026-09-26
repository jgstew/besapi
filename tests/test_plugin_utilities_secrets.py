"""Tests for protecting plugin config secrets in besapi.plugin_utilities.

The platform specific utilities module is faked, so these run on any OS.
"""

import logging
import os
import sys
import types

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from besapi import plugin_utilities, plugin_utilities_linux  # noqa: E402

FAKE_PREFIX = "{cryptoutility}"

CONFIG_YAML = """\
---
# top comment, must survive a rewrite
mqtt:
  host: homeassistant.local
  port: 1883
  username: mqttuser
  # the password comment, must survive a rewrite
  password: plaintextpw
  tls: false
"""


# a protected value starts with `{`, so YAML requires it to be quoted:
PROTECTED_LINE = "password: '{cryptoutility}wptxetnialp'"


def fake_protect(plaintext):
    """Reversible stand in for a platform protect_secret."""
    return FAKE_PREFIX + plaintext[::-1]


def fake_unprotect(protected):
    """Reverses fake_protect, None if not protected."""
    if not protected.startswith(FAKE_PREFIX):
        return None
    return protected[len(FAKE_PREFIX) :][::-1]


def make_platform(
    protect=fake_protect, unprotect=fake_unprotect, rest_pass="restpassword"
):
    """A fake platform specific utilities module."""
    module = types.ModuleType("fake_platform_utilities")
    module.protect_secret = protect
    module.unprotect_secret = unprotect
    module.get_linux_credentials_rest_pass = lambda: rest_pass
    module.get_win_registry_rest_pass = lambda: rest_pass
    return module


@pytest.fixture
def root_server(monkeypatch):
    """Pretend this is a root server with working platform utilities."""
    platform = make_platform()
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", platform)
    return platform


def write_config(tmp_path, contents=CONFIG_YAML):
    """Write a plugin config file and return its path."""
    config_path = tmp_path / "plugin.config.yaml"
    config_path.write_text(contents, encoding="utf-8")
    return str(config_path)


def test_prefixes_include_platform_prefixes():
    """Test that the known prefixes match the platform modules."""
    assert (
        plugin_utilities_linux.PROTECTED_SECRET_PREFIX
        in plugin_utilities.PROTECTED_SECRET_PREFIXES
    )
    assert "{dpapi}" in plugin_utilities.PROTECTED_SECRET_PREFIXES


def test_is_protected_secret():
    """Test detection of a protected secret by its prefix."""
    assert plugin_utilities.is_protected_secret("{dpapi}AAAA")
    assert plugin_utilities.is_protected_secret("{cryptoutility}{aes,1}AAAA")
    assert not plugin_utilities.is_protected_secret("plaintextpw")
    assert not plugin_utilities.is_protected_secret(None)
    assert not plugin_utilities.is_protected_secret(1883)


def test_protect_secret(root_server):  # pylint: disable=unused-argument
    """Test that a root server protects a secret."""
    assert plugin_utilities.protect_secret("plaintextpw") == fake_protect("plaintextpw")


def test_protect_secret_not_root_server(monkeypatch):
    """Test that nothing is protected without root server credentials.

    This is how a plugin run by an ordinary user, not the plugin service, is
    told apart, so a config file is never encrypted with the wrong keys.
    """
    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_platform(rest_pass=None)
    )
    assert plugin_utilities.protect_secret("plaintextpw") is None


def test_protect_secret_no_platform(monkeypatch):
    """Test that nothing is protected without platform utilities, like macOS."""
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", None)
    assert plugin_utilities.protect_secret("plaintextpw") is None


def test_protect_secret_bad_round_trip(monkeypatch):
    """Test that a secret that does not decrypt back is never returned."""
    monkeypatch.setattr(
        plugin_utilities,
        "PLATFORM_UTILITIES",
        make_platform(unprotect=lambda _protected: "something else"),
    )
    assert plugin_utilities.protect_secret("plaintextpw") is None


def test_protect_secret_platform_error(monkeypatch):
    """Test that an error in the platform utilities results in None."""

    def broken_protect(_plaintext):
        raise OSError("CryptoUtility exploded")

    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_platform(protect=broken_protect)
    )
    assert plugin_utilities.protect_secret("plaintextpw") is None


def test_protect_secret_platform_error_is_logged(monkeypatch, caplog):
    """Test that an error encrypting shows at the default plugin log level.

    Plugins log at WARNING by default, so a DEBUG message would hide why a
    secret was left as plaintext.
    """

    def broken_protect(_plaintext):
        raise OSError("DPAPI exploded")

    monkeypatch.setattr(
        plugin_utilities, "PLATFORM_UTILITIES", make_platform(protect=broken_protect)
    )

    with caplog.at_level(logging.WARNING):
        assert plugin_utilities.protect_secret("plaintextpw") is None

    assert any(
        record.levelno >= logging.ERROR and "DPAPI exploded" in record.getMessage()
        for record in caplog.records
    )


def test_unprotect_secret_platform_error_is_logged(monkeypatch, caplog):
    """Test that an error decrypting shows at the default plugin log level."""

    def broken_unprotect(_protected):
        raise OSError("CryptoUtility exploded")

    monkeypatch.setattr(
        plugin_utilities,
        "PLATFORM_UTILITIES",
        make_platform(unprotect=broken_unprotect),
    )

    with caplog.at_level(logging.WARNING):
        assert plugin_utilities.unprotect_secret(FAKE_PREFIX + "x") is None

    assert any(
        record.levelno >= logging.ERROR
        and "CryptoUtility exploded" in record.getMessage()
        for record in caplog.records
    )


def test_unprotect_secret(root_server):  # pylint: disable=unused-argument
    """Test that a protected secret is decrypted by the platform utilities."""
    assert plugin_utilities.unprotect_secret(fake_protect("plaintextpw")) == (
        "plaintextpw"
    )


def test_get_plugin_config_never_rewrites_plaintext_secret(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that loading never encrypts, the secret is not known to work yet.

    protect_plugin_config_secrets() does that, once the plugin has used it.
    """
    config_path = write_config(tmp_path)

    config = plugin_utilities.get_plugin_config(
        config_path, secret_keys=[("mqtt", "password")]
    )

    assert config["mqtt"]["password"] == "plaintextpw"
    assert open(config_path, encoding="utf-8").read() == CONFIG_YAML


def test_get_plugin_config_decrypts_protected_secret(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that an already protected secret is decrypted and not rewritten."""
    contents = CONFIG_YAML.replace("password: plaintextpw", PROTECTED_LINE)
    config_path = write_config(tmp_path, contents)

    config = plugin_utilities.get_plugin_config(
        config_path, secret_keys=[("mqtt", "password")]
    )

    assert config["mqtt"]["password"] == "plaintextpw"
    assert open(config_path, encoding="utf-8").read() == contents


def test_get_plugin_config_keeps_plaintext_when_not_root_server(monkeypatch, tmp_path):
    """Test that the file is untouched when the secret cannot be protected."""
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", None)
    config_path = write_config(tmp_path)

    config = plugin_utilities.get_plugin_config(
        config_path, secret_keys=[("mqtt", "password")]
    )

    assert config["mqtt"]["password"] == "plaintextpw"
    assert open(config_path, encoding="utf-8").read() == CONFIG_YAML


def test_get_plugin_config_protected_secret_cannot_decrypt(monkeypatch, tmp_path):
    """Test that an undecryptable secret is an error, not sent as a password."""
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", None)
    config_path = write_config(
        tmp_path, CONFIG_YAML.replace("plaintextpw", "'{dpapi}AAAA'")
    )

    with pytest.raises(ValueError, match="mqtt.password"):
        plugin_utilities.get_plugin_config(
            config_path, secret_keys=[("mqtt", "password")]
        )


def test_get_plugin_config_missing_secret_key(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that a secret key missing from the config is skipped."""
    contents = CONFIG_YAML.replace("  password: plaintextpw\n", "")
    config_path = write_config(tmp_path, contents)

    config = plugin_utilities.get_plugin_config(
        config_path, secret_keys=[("mqtt", "password"), ("other", "password")]
    )

    assert "password" not in config["mqtt"]
    assert open(config_path, encoding="utf-8").read() == contents


def test_protect_plugin_config_secrets(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that a plaintext secret is rewritten encrypted, keeping comments."""
    config_path = write_config(tmp_path)

    protected = plugin_utilities.protect_plugin_config_secrets(
        [("mqtt", "password")], config_path
    )

    assert protected == ["mqtt.password"]
    rewritten = open(config_path, encoding="utf-8").read()
    assert "plaintextpw" not in rewritten
    assert PROTECTED_LINE in rewritten
    assert "# top comment, must survive a rewrite" in rewritten
    assert "# the password comment, must survive a rewrite" in rewritten
    # the rest of the file is untouched:
    assert rewritten == CONFIG_YAML.replace("password: plaintextpw", PROTECTED_LINE)
    # and it loads back as the plaintext:
    config = plugin_utilities.get_plugin_config(
        config_path, secret_keys=[("mqtt", "password")]
    )
    assert config["mqtt"]["password"] == "plaintextpw"


def test_protect_plugin_config_secrets_already_protected(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that an already protected secret is left alone."""
    contents = CONFIG_YAML.replace("password: plaintextpw", PROTECTED_LINE)
    config_path = write_config(tmp_path, contents)

    assert (
        plugin_utilities.protect_plugin_config_secrets(
            [("mqtt", "password")], config_path
        )
        == []
    )
    assert open(config_path, encoding="utf-8").read() == contents


def test_protect_plugin_config_secrets_not_root_server(monkeypatch, tmp_path):
    """Test that the file is untouched when the secret cannot be protected."""
    monkeypatch.setattr(plugin_utilities, "PLATFORM_UTILITIES", None)
    config_path = write_config(tmp_path)

    assert (
        plugin_utilities.protect_plugin_config_secrets(
            [("mqtt", "password")], config_path
        )
        == []
    )
    assert open(config_path, encoding="utf-8").read() == CONFIG_YAML


def test_protect_plugin_config_secrets_missing_key(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that a secret key missing from the config is skipped."""
    contents = CONFIG_YAML.replace("  password: plaintextpw\n", "")
    config_path = write_config(tmp_path, contents)

    assert (
        plugin_utilities.protect_plugin_config_secrets(
            [("mqtt", "password"), ("other", "password")], config_path
        )
        == []
    )
    assert open(config_path, encoding="utf-8").read() == contents


def test_protect_plugin_config_secrets_keeps_file_mode(
    root_server, tmp_path
):  # pylint: disable=unused-argument
    """Test that the rewritten config keeps its restrictive permissions."""
    config_path = write_config(tmp_path)
    os.chmod(config_path, 0o600)

    plugin_utilities.protect_plugin_config_secrets([("mqtt", "password")], config_path)

    assert os.stat(config_path).st_mode & 0o777 == 0o600


def test_protect_plugin_config_secrets_write_failure(
    root_server, tmp_path, monkeypatch
):  # pylint: disable=unused-argument
    """Test that a failed write is not fatal and leaves the file intact.

    The plugin has already done its work by this point, with the plaintext.
    """
    config_path = write_config(tmp_path)

    def broken_replace(*_args):
        raise PermissionError("read only")

    monkeypatch.setattr(plugin_utilities.os, "replace", broken_replace)

    assert (
        plugin_utilities.protect_plugin_config_secrets(
            [("mqtt", "password")], config_path
        )
        == []
    )
    assert open(config_path, encoding="utf-8").read() == CONFIG_YAML
    # no temp file is left behind:
    assert os.listdir(tmp_path) == ["plugin.config.yaml"]
