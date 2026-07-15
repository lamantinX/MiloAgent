"""Shared pytest fixtures for the offline test suite.

All fixtures stay local to a per-test temporary directory so the process
working directory is never permanently changed. No fixture performs network
access or loads real account credentials.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def tmp_sqlite_path(tmp_path) -> Path:
    """Return a fresh SQLite database path inside the per-test temp dir."""
    return tmp_path / "test.db"


@pytest.fixture()
def tmp_config_dir(tmp_path, monkeypatch) -> Path:
    """Provide an isolated config/ directory and point the process at it.

    Restores the previous working directory and CWD on teardown so the global
    process state is never permanently mutated.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    cwd = Path.cwd()
    monkeypatch.chdir(tmp_path)
    yield cfg
    monkeypatch.chdir(cwd)


@pytest.fixture()
def tmp_projects_dir(tmp_path) -> Path:
    """Provide an isolated projects/ directory."""
    proj = tmp_path / "projects"
    proj.mkdir()
    return proj


@pytest.fixture()
def tmp_business_dir(tmp_path) -> Path:
    """Provide an isolated business-data directory."""
    biz = tmp_path / "business"
    biz.mkdir()
    return biz
