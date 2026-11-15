import json
from pathlib import Path

import pytest

from olympus.contracts.models import ContractBundle

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle():
    return ContractBundle.model_validate_json(
        (ROOT / "fixtures/contracts/valid.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def scenarios():
    return json.loads((ROOT / "fixtures/contracts/scenarios.json").read_text(encoding="utf-8"))


@pytest.fixture
def workspace(tmp_path):
    from olympus.common.workspace import temporary_workspace

    with temporary_workspace(tmp_path) as path:
        yield path
