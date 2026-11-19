"""Shared wire setup reduces repetition while frozen scripts remain standalone and guarded."""

import json

import pytest

from olympus.agents.runtime import OutputSchemaError
from olympus.workflows.acceptance import validate_check
from olympus.workflows.check_scripts import assemble_check_scripts


def test_shared_setup_is_transmitted_once_and_assembled_checks_execute_independently():
    setup = "import math\ndef square(value):\n    return value * value\n"
    checks = {"CHECK-SQUARE": "assert square(3) == 9\n", "CHECK-ROOT": "assert math.sqrt(square(4)) == 4\n"}
    wire = json.dumps({"setup": setup, "checks": checks})
    assert wire.count("import math") == 1
    assembled = assemble_check_scripts(setup, checks)
    assert len(wire) < len(json.dumps({"checks": assembled}))
    for source in assembled.values():
        validate_check(source, [])
        exec(compile(source, "<independent-check>", "exec"), {})
    assert checks["CHECK-SQUARE"] == "assert square(3) == 9\n"


def test_legacy_checks_without_setup_keep_source_exactly():
    checks = {"CHECK-LEGACY": "import math\nassert math.sqrt(4) == 2\n"}
    assert assemble_check_scripts("", checks) == checks


@pytest.mark.parametrize("setup,body,match", [
    ("import os", "assert 2 + 2 == 4", "module"),
    ("value = object().__class__", "assert value is not None", "private"),
])
def test_shared_setup_obeys_the_same_assembled_script_policy(setup, body, match):
    source = assemble_check_scripts(setup, {"CHECK": body})["CHECK"]
    with pytest.raises(OutputSchemaError, match=match):
        validate_check(source, [])


@pytest.mark.parametrize("setup,checks", [
    (None, {"CHECK": "assert 1 + 1 == 2"}),
    ([], {"CHECK": "assert 1 + 1 == 2"}),
    ("", []), ("", {"CHECK": []}), ("", {1: "assert 1 + 1 == 2"}),
    ("import math", {"CHECK": "   "}),
])
def test_shared_setup_and_check_bodies_require_explicit_source_strings(setup, checks):
    with pytest.raises(OutputSchemaError):
        assemble_check_scripts(setup, checks)


def test_assembly_never_truncates_source_and_size_limit_applies_to_complete_script():
    setup = "#" + "x" * 40000 + "\n"
    body = "#" + "y" * 30000 + "\nassert 1 + 1 == 2\n"
    source = assemble_check_scripts(setup, {"CHECK": body})["CHECK"]
    assert source == setup + "\n\n" + body
    with pytest.raises(OutputSchemaError, match="64 KB"):
        validate_check(source, [])
