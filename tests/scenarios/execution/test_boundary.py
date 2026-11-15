from pathlib import Path

import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.execution.policy import validate_python


@pytest.mark.scenario
def test_frozen_classroom_acceptance_is_separate_from_generated_implementation(bundle):
    root = Path(__file__).absolute().parents[3]
    source = (root / "fixtures/execution/campus_service.py").read_text(encoding="utf-8")
    script = (root / "fixtures/execution/check_campus.py").read_text(encoding="utf-8")
    validate_python(source)
    task = bundle.plan.tasks[2]
    frozen = FrozenAcceptance.freeze(task, {c: script for c in task.checks})
    frozen.verify(task)
    assert source != script and "ThreadPoolExecutor" in script
    with pytest.raises(ValueError):
        validate_python("from math import __loader__ as loader")
