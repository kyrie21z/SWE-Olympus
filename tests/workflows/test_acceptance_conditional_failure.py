"""Guarded explicit failures reject bad documents; they are not vacuous assertions."""

import pytest

from olympus.agents.runtime import OutputSchemaError
from olympus.workflows.acceptance import validate_check

DOCUMENT_CHECK = (
    "assert 'Python' in text\n"
    "assert 'unittest' in text\n"
    "assert len(text) > 10\n"
    "for line in text.split('\\n'):\n"
    "    if 'test_' in line and '.py' in line:\n"
    "        assert False, f'Claimed nonexistent test module: {line}'\n"
)


def test_document_negative_check_is_valid_and_actually_fails_only_for_the_bad_claim():
    validate_check(DOCUMENT_CHECK, [])
    exec(compile(DOCUMENT_CHECK, "<readme-check>", "exec"), {"text": "Python 3.12\nunittest frozen harness checks\n"})
    with pytest.raises(AssertionError, match="nonexistent test module"):
        exec(compile(DOCUMENT_CHECK, "<readme-check>", "exec"), {"text": "Python 3.12\nunittest\nrun test_fake.py\n"})


@pytest.mark.parametrize("source", [
    "if invalid:\n    assert False\nelse:\n    assert value == expected\n",
    "if valid:\n    assert value == expected\nelse:\n    assert False\n",
    "def check(value):\n    if value != expected:\n        assert False\ncheck(actual)\n",
    "if invalid:\n    if True:\n        assert False\n",
    "invalid = 'bad' in text\nif invalid:\n    assert False\n",
])
def test_literal_false_can_fail_in_either_runtime_if_branch(source):
    validate_check(source, [])


@pytest.mark.parametrize("source", [
    "assert False\n", "assert True\n", "assert 1\n", "assert 0\n",
    "if False:\n    assert False\n", "if True:\n    assert False\n",
    "if False:\n    assert True\n", "if valid:\n    assert True\n",
    "if valid:\n    if False:\n        assert False\n",
    "if False:\n    if valid:\n        assert False\n",
    "if False and valid:\n    assert False\n",
    "if True or valid:\n    pass\nelse:\n    assert False\n",
    "if []:\n    assert False\n", "if 1 == 2:\n    assert False\n",
    "if valid:\n    def check():\n        assert False\ncheck()\n",
    "while False:\n    if invalid:\n        assert False\n",
    "for item in []:\n    if item:\n        assert False\n",
    "items = ()\nfor item in items:\n    if item:\n        assert False\n",
    "disabled = False\nif disabled:\n    assert False\n",
    "disabled = False\nalias = disabled\nif alias:\n    assert False\n",
    "enabled = True\nif enabled:\n    assert False\n",
    "if 1 == 2 and invalid:\n    assert False\n",
    "if 0 > 1 < invalid:\n    assert False\n",
    "if 1 == 1 or invalid:\n    pass\nelse:\n    assert False\n",
    "if [actual]:\n    assert False\n",
    "if len([]) == 0:\n    assert False\n",
    "if 1 + 1 == 3 and invalid:\n    assert False\n",
    "while True:\n    pass\nif invalid:\n    assert False\n",
    "def check(value):\n    return\n    if value:\n        assert False\ncheck(actual)\n",
    "for item in values:\n    break\n    if item:\n        assert False\n",
    "if 1 + 1 == 3:\n    def check(value):\n        if value:\n            assert False\n",
])
def test_constant_success_unconditional_failure_and_dead_branch_noops_remain_rejected(source):
    with pytest.raises(OutputSchemaError):
        validate_check(source, [])


@pytest.mark.parametrize("source", [
    "while False:\n    if invalid:\n        assert False\n",
    "for item in []:\n    if item:\n        assert False\n",
    "disabled = False\nif disabled:\n    assert False\n",
    "if 1 == 2 and invalid:\n    assert False\n",
])
def test_reproduced_false_green_sources_execute_without_a_check_but_are_not_valid_acceptance(source):
    exec(compile(source, "<formerly-false-green>", "exec"), {})
    with pytest.raises(OutputSchemaError):
        validate_check(source, [])


def test_metadata_text_binding_keeps_real_readme_negative_check_data_dependent():
    source = "text = source_metadata['docs/README.md']['text']\n" + DOCUMENT_CHECK
    validate_check(source, [])
    exec(compile(source, "<readme-check>", "exec"), {
        "source_metadata": {"docs/README.md": {"text": "Python 3.12\nunittest frozen harness checks\n"}},
    })
    with pytest.raises(AssertionError, match="nonexistent test module"):
        exec(compile(source, "<readme-check>", "exec"), {
            "source_metadata": {"docs/README.md": {"text": "Python 3.12\nunittest\nrun test_fake.py\n"}},
        })


@pytest.mark.parametrize("source", [
    "import re\nif invalid:\n    assert False\n",
    "if invalid:\n    assert False, service.__dict__\n",
    "source_metadata = {}\nif invalid:\n    assert False\n",
])
def test_conditional_failure_exception_does_not_bypass_any_other_policy(source):
    with pytest.raises(OutputSchemaError):
        validate_check(source, [])
