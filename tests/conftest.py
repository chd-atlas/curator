"""Tests run against a real atlas checkout, or not at all.

curator never imports ``chd_atlas``; it runs the atlas's CLI and reads its exported
schemas. A suite that silently skipped those tests when the atlas was missing would be
green while checking nothing, so a missing variable stops the run instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REQUIRED = ("CHD_ATLAS_ROOT", "CHD_ATLAS_CLI", "CHD_ATLAS_PYTHON")


def pytest_configure(config: pytest.Config) -> None:
    unset = [name for name in REQUIRED if not os.environ.get(name)]
    if unset:
        raise pytest.UsageError(
            "the atlas is required to run curator's tests; unset: " + ", ".join(unset)
        )


@pytest.fixture(scope="session")
def atlas_root() -> Path:
    return Path(os.environ["CHD_ATLAS_ROOT"])


@pytest.fixture(scope="session")
def atlas_cli() -> Path:
    return Path(os.environ["CHD_ATLAS_CLI"])


@pytest.fixture(scope="session")
def atlas_python() -> Path:
    return Path(os.environ["CHD_ATLAS_PYTHON"])
