"""Reviewed inputs for the versioned lab equipment online example."""

import json
from pathlib import Path

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.contracts.models import Design, Provenance, fingerprint

DIRECTORY = Path(__file__).resolve().parents[3] / "fixtures/requirements/lab-equipment-lending"
BINDING = ("REQ-LAB-EQUIPMENT-LENDING", 3, "8b97d81ddb298a956f8622b221ba85742f5cde0d770d5348d3e82a4e100c93c7")


def matches(requirement):
    return (requirement.object_id, requirement.version, requirement.content_hash) == BINDING


def read_json(name):
    return json.loads((DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))


def provenance(name):
    return Provenance(kind="material", source_id="LAB-EQUIPMENT-LENDING-DEMO-V3",
                      locator=f"fixtures/requirements/lab-equipment-lending/{name}.json")


def design_for(requirement):
    content = read_json("design")
    return Design(object_id=f"DS-{requirement.object_id}", version=1, content=content,
                  content_hash=fingerprint(content), requirement=requirement.ref(), provenance=[provenance("design")])


def frozen_for(plan):
    checks = read_json("checks")
    return {task.task_id: FrozenAcceptance.freeze(task, checks[task.task_id]).model_dump(mode="json")
            for task in plan.tasks}


def reference_sources(plan):
    return {task.task_id: {path: (DIRECTORY / "reference" / path).read_text(encoding="utf-8") for path in task.outputs}
            for task in plan.tasks if task.type == "implement"}
