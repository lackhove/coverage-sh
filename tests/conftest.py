#  SPDX-License-Identifier: MIT
#  Copyright (c) 2023 Kilian Lackhove

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def resources_dir():
    return Path(__file__).parent / "resources"


@pytest.fixture
def dummy_project_dir(resources_dir: Path, tmp_path: Path) -> Path:
    """Fixture for a temporary copy of `testproject`"""
    source = resources_dir / "testproject"
    dest = tmp_path / "testproject"
    shutil.copytree(source, dest)

    return dest
