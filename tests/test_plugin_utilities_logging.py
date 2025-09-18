import logging
import os
import sys
import tempfile

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from besapi import plugin_utilities


def test_get_plugin_logging_config_default(tmp_path, capsys):
    """Test default logging config with no verbosity and no console."""
    # Use an explicit log file path in a temp dir to avoid touching real files
    log_file = tmp_path / "test.log"

    cfg = plugin_utilities.get_plugin_logging_config(
        str(log_file), verbose=0, console=False
    )

    # handlers should include a RotatingFileHandler only
    handlers = cfg.get("handlers")
    assert handlers, "handlers should be present"
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)

    # level should be WARNING when verbose=0
    assert cfg.get("level") == logging.WARNING


def test_get_plugin_logging_config_verbose_and_console(tmp_path, capsys):
    """Test logging config with verbosity and console logging."""
    # ensure the function prints info when verbose and console True
    log_file = tmp_path / "test2.log"

    cfg = plugin_utilities.get_plugin_logging_config(
        str(log_file), verbose=1, console=True
    )

    handlers = cfg.get("handlers")
    # should have file handler + stream handler
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)

    # verbose=1 -> INFO
    assert cfg.get("level") == logging.INFO

    # get printed output
    captured = capsys.readouterr()
    assert "INFO: Log File Path:" in captured.out
    assert "INFO: also logging to console" in captured.out


def test_get_plugin_logging_config_debug_level(tmp_path):
    """Test logging config with debug level verbosity."""
    log_file = tmp_path / "test3.log"

    cfg = plugin_utilities.get_plugin_logging_config(
        str(log_file), verbose=2, console=False
    )

    # verbose>1 -> DEBUG
    assert cfg.get("level") == logging.DEBUG


def test_plugin_logging_config_registers_session_level(tmp_path):
    """Test that get_plugin_logging_config registers the custom SESSION log level
    (99).
    """
    log_file = tmp_path / "test_session.log"
    plugin_utilities.get_plugin_logging_config(str(log_file), verbose=0, console=False)
    # After calling, the level name for 99 should be 'SESSION'
    assert logging.getLevelName(99) == "SESSION"
