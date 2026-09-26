"""Shared pytest fixtures, found by pytest without importing them."""

import os
import sys

import pytest

# Ensure the local `src/` is first on sys.path so tests import the workspace package
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from besapi.besapi import BESConnection  # noqa: E402


@pytest.fixture
def offline_login(monkeypatch):
    """Skip the network login BESConnection does on creation."""
    monkeypatch.setattr(BESConnection, "login", lambda self, *a, **k: True)


@pytest.fixture
def export_conn(offline_login):  # pylint: disable=redefined-outer-name,unused-argument
    """An offline connection with a site listing and two task items routed."""
    # pylint: disable=import-outside-toplevel
    from test_besapi_coverage import (
        API,
        GIT_TITLE,
        PY_TITLE,
        SITE_CONTENT_XML,
        task_xml,
    )
    from test_besapi_sdk import make_conn

    conn = make_conn()
    conn.session.routes = {
        f"{API}/site/custom/Demo/content": (SITE_CONTENT_XML, 200),
        f"{API}/task/custom/Demo/12724": (task_xml(GIT_TITLE), 200),
        f"{API}/task/custom/Demo/12725": (task_xml(PY_TITLE), 200),
    }
    return conn
