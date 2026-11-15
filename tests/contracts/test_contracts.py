import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympus.contracts import models as m

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/contracts"
TYPES = {
    "requirement": m.Requirement,
    "design": m.Design,
    "plan": m.Plan,
    "run": m.Run,
    "attempt": m.Attempt,
    "artifact": m.Artifact,
    "review": m.Review,
    "event": m.Event,
    "command": m.Command,
    "receipt": m.Receipt,
    "execution_record": m.ExecutionRecord,
    "task": m.Task,
    "task_runtime": m.TaskRuntime,
    "confirmation": m.Confirmation,
}


@pytest.mark.parametrize("name,cls", TYPES.items())
def test_each_public_contract_has_valid_and_invalid_fixture(name, cls):
    data = json.loads((FIXTURES / "valid.json").read_text(encoding="utf-8"))
    valid = data.get(name) or json.loads((FIXTURES / f"valid/{name}.json").read_text(encoding="utf-8"))
    assert cls.model_validate(valid)
    with pytest.raises(ValidationError):
        cls.model_validate_json((FIXTURES / f"invalid/{name}.json").read_text(encoding="utf-8"))


def test_eight_tasks_twelve_edges_and_same_sources(bundle):
    tasks = bundle.plan.tasks
    assert len(tasks) == 8
    assert sum(len(t.depends_on) for t in tasks) == 12
    assert next(t for t in tasks if t.task_id == "T05").depends_on == ["T03"]
    assert bundle.requirement.confirmation.origin == "synthetic_fixture"


def test_semantic_hash_and_confirmation_are_separate(bundle):
    raw = bundle.requirement.model_dump(mode="json")
    raw["confirmation"]["actor"] = "another-fixture-person"
    assert m.Requirement.model_validate(raw).content_hash == bundle.requirement.content_hash
    raw["content"]["requirements"][0]["text"] = "changed scope"
    with pytest.raises(ValidationError):
        m.Requirement.model_validate(raw)
    raw["content_hash"] = m.fingerprint(raw["content"])
    with pytest.raises(ValidationError, match="confirmation target is stale"):
        m.Requirement.model_validate(raw)
    assert m.fingerprint({"b": 2, "a": 1}) == m.fingerprint({"a": 1, "b": 2})


def test_task_and_plan_bind_semantics(bundle):
    raw = bundle.plan.model_dump(mode="json")
    raw["tasks"][0]["goal"] = "changed goal"
    with pytest.raises(ValidationError, match="hash"):
        m.Plan.model_validate(raw)


def test_risk_unknown_and_stale_are_not_success(scenarios):
    for name, data in scenarios.items():
        run = m.Run.model_validate(data)
        if name != "completed":
            assert run.status != "completed"
    risk = scenarios["risk_accepted"]["tasks"][0]
    with pytest.raises(ValidationError):
        m.TaskRuntime.model_validate(risk | {"state": "accepted", "verification_status": "passed"})
    attempt = m.Attempt.model_validate_json((FIXTURES / "unknown_attempt.json").read_text(encoding="utf-8"))
    assert attempt.status == "outcome_unknown"


def test_self_review_rejected(bundle):
    with pytest.raises(ValidationError):
        m.Review.model_validate(bundle.review.model_dump() | {"reviewer": bundle.review.author})
