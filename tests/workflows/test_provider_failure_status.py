"""Known terminal provider rejections are explicit and require a human retry."""

from fastapi.testclient import TestClient
import pytest

from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.app import create_app
from olympus.storage.sqlite import Store

from test_browser_host import action, create


class RejectedProvider(MockProvider):
    mode = "real_model"

    def __init__(self, code, unknown=False):
        super().__init__(fixtures={})
        self.code, self.unknown, self.calls = code, unknown, []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, **kwargs):
        self.calls.append(request)
        return ProviderStepResult(kind="model", status="outcome_unknown" if self.unknown else "failed",
                                  error_code=self.code,
                                  error_details={"code": "invalid_request_error", "param": "response_format",
                                                 "message": "PRIVATE_ERROR_BODY", "key": "sk-PRIVATE"},
                                  output={"message": "PRIVATE_ERROR_BODY"})


@pytest.mark.parametrize(("code", "status", "text"), [
    ("http_400", "PROVIDER_REQUEST_REJECTED", "HTTP 400"),
    ("http_404", "PROVIDER_ENDPOINT_OR_MODEL_NOT_FOUND", "API 根地址"),
    ("http_429", "PROVIDER_RATE_LIMITED", "限流"),
    ("provider_output_incomplete", "PROVIDER_OUTPUT_INCOMPLETE", "输出长度"),
    ("invalid_provider_response", "PROVIDER_RESPONSE_INVALID", "结构化结果"),
])
def test_known_provider_failure_has_actionable_safe_status_and_no_automatic_resend(tmp_path, bundle, code, status, text):
    provider = RejectedProvider(code)
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"),
                               runner_factory=lambda ledger: AgentRunner(provider, ledger=ledger))) as client:
        workflow_id = create(client, bundle.requirement)["workflow_id"]
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        failed = action(client, workflow_id, "generate_design", "error")
        error = failed["error"]
        assert error["code"] == status and text in error["message"]
        assert "Athena" in error["message"] and "设计生成" in error["message"]
        assert error["provider_code"] == code and error["request_id"]
        assert error["provider_details"] == {"code": "invalid_request_error", "param": "response_format"}
        assert failed["retry_safe"] is True and failed["allowed_actions"] == ["generate_design"]
        assert len(provider.calls) == 1
        assert "PRIVATE" not in str(error) and "RUNTIMEERROR" not in str(error)
        action(client, workflow_id, "generate_design", "error", operation_id="explicit-human-retry")
        assert len(provider.calls) == 2


def test_unknown_provider_outcome_still_forbids_retry_and_does_not_resend(tmp_path, bundle):
    provider = RejectedProvider("provider_timeout", unknown=True)
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"),
                               runner_factory=lambda ledger: AgentRunner(provider, ledger=ledger))) as client:
        workflow_id = create(client, bundle.requirement)["workflow_id"]
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        failed = action(client, workflow_id, "generate_design", "error")
        assert failed["error"]["code"] == "OUTCOME_UNKNOWN"
        assert failed["retry_safe"] is False
        rejected = client.post(f"/api/workflows/{workflow_id}/actions", json={
            "action": "generate_design", "operation_id": "forbidden-blind-resend"})
        assert rejected.status_code == 409 and len(provider.calls) == 1
