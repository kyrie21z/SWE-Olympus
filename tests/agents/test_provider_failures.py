"""Known failures carry safe typed evidence; raw provider bodies never become error text."""

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, ProviderRequestError, UnknownOutcomeError
from olympus.agents.runtime.models import RolePermissions, RoleRunInput
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.contracts.models import Ref

SECRET = "sk-PRIVATE_RESPONSE_BODY"


class FailedProvider(MockProvider):
    def __init__(self, code, *, unknown=False, details=None):
        super().__init__(fixtures={})
        self.code, self.unknown, self.details, self.calls = code, unknown, details or {}, []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, **kwargs):
        self.calls.append(request)
        return ProviderStepResult(kind="model", status="outcome_unknown" if self.unknown else "failed",
                                  error_code=self.code, error_details=self.details,
                                  output={"message": SECRET, "error": {"message": SECRET}})


def request():
    return RoleRunInput(role_id="Momus", task_kind="acceptance_definition", task_id="TASK-failure",
                        logical_task_id="known-provider-failure", input_manifest={"prompt": "return JSON"},
                        input_refs=[Ref(object_id="PLAN-failure", version=1, content_hash="a" * 64)],
                        permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["acceptance_definition"],
                                                    allowed_input_keys=["prompt"]))


def test_known_failure_has_role_task_request_and_allowlisted_details_without_body_or_auto_retry():
    provider = FailedProvider("http_400", details={"code": "invalid_request_error", "type": "invalid_request_error",
                                                 "param": "response_format", "message": SECRET, "key": SECRET})
    runner = AgentRunner(provider)
    with pytest.raises(ProviderRequestError) as failure:
        runner.run(request())
    error = failure.value
    assert error.error_code == "http_400" and error.role_id == "Momus"
    assert error.task_kind == "acceptance_definition" and error.request_id == runner.ledger.entries()[0].request_id
    assert error.error_details == {"code": "invalid_request_error", "type": "invalid_request_error", "param": "response_format"}
    entry = runner.ledger.entries()[0]
    assert entry.status == "failed" and entry.error_code == "http_400"
    assert entry.response_summary["provider_error_details"] == error.error_details
    assert len(provider.calls) == 1
    assert SECRET not in str(error) + str(entry.model_dump()) + str(runner.records.list_by_role("Momus"))


def test_unknown_failure_remains_unknown_and_does_not_become_a_retryable_known_rejection():
    provider = FailedProvider("provider_timeout", unknown=True)
    runner = AgentRunner(provider)
    with pytest.raises(UnknownOutcomeError):
        runner.run(request())
    assert len(provider.calls) == 1 and runner.ledger.entries()[0].status == "outcome_unknown"


def test_unrecognized_failed_provider_codes_and_detail_values_are_not_reflected():
    provider = FailedProvider(SECRET, details={"code": SECRET, "type": SECRET, "param": SECRET})
    runner = AgentRunner(provider)
    with pytest.raises(ProviderRequestError) as failure:
        runner.run(request())
    assert failure.value.error_code == "provider_request_failed" and failure.value.error_details == {}
    assert SECRET not in str(failure.value) + str(runner.ledger.entries()[0].model_dump())
