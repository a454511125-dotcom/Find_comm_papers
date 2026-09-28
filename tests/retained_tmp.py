"""Opt-in pytest fixture that keeps every artifact; run with -p no:tmpdir.

Unlike pytest's default temporary directory plugin, this module never removes files.
"""
import os
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(request):
    root = Path(os.environ.get("COMM_TEST_ARTIFACTS", "test-artifacts"))
    path = root / (request.node.name[:80].replace("/", "_").replace(":", "_") + "_" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path
