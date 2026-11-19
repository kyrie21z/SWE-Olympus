"""Provider rejection diagnostics stay bounded, useful and safe for the ledger."""

from io import BytesIO
import json
import urllib.error

import pytest

from olympus.agents.runtime.providers import safe_provider_error_details

from test_provider_schema import provider
from test_provider_streaming import request


def reject(monkeypatch, body, *, status=400):
    calls = []
    response = BytesIO(body)

    def transport(req, **kwargs):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, status, "secret response message", {}, response)

    monkeypatch.setattr("urllib.request.urlopen", transport)
    return calls, response


def test_http_400_extracts_only_known_tokens_without_retry_or_message(monkeypatch):
    body = {"error": {"code": "invalid_json_schema", "type": "invalid_request_error",
                      "param": "response_format", "message": "sk-SECRET body and credentials",
                      "request_id": "sk-SECRET", "key": "sk-SECRET"}}
    calls, response = reject(monkeypatch, json.dumps(body).encode())
    result = provider().invoke(request(), {}, step_index=1)
    assert result.status == "failed" and result.error_code == "http_400"
    assert result.error_details == {"code": "invalid_json_schema", "type": "invalid_request_error",
                                    "param": "response_format"}
    assert "SECRET" not in result.model_dump_json()
    assert result.usage["elapsed_ms"] >= 0
    assert len(calls) == 1 and response.closed


@pytest.mark.parametrize("status,expected", [(401, "failed"), (403, "failed"), (404, "failed"),
                                             (429, "failed"), (500, "outcome_unknown")])
def test_rejection_classification_and_top_level_vendor_code_preserved(monkeypatch, status, expected):
    calls, response = reject(monkeypatch, b'{"code":"InvalidParameter","message":"SECRET"}', status=status)
    result = provider().invoke(request(), {}, step_index=1)
    assert result.status == expected and result.error_code == f"http_{status}"
    assert result.error_details == {"code": "invalidparameter"}
    assert len(calls) == 1 and response.closed


@pytest.mark.parametrize("body", [b"not JSON secret", b'{"error":"SECRET"}', b'[]',
                                 b'{"error":{"code":"sk-SECRET","type":"https://secret", "param":"api_key"}}',
                                 b'{"message":"' + b'x' * 65536 + b'"}'],
                         ids=["not-json", "string-error", "array", "unknown-tokens", "oversized"])
def test_bad_or_oversized_diagnostics_do_not_hide_http_failure(monkeypatch, body):
    calls, response = reject(monkeypatch, body)
    result = provider().invoke(request(), {}, step_index=1)
    assert result.status == "failed" and result.error_code == "http_400"
    assert result.error_details == {} and len(calls) == 1 and response.closed


def test_diagnostic_read_returns_at_complete_json_without_waiting_for_eof(monkeypatch):
    class HangingBody(BytesIO):
        def read1(self, count):
            if self.tell():
                raise AssertionError("must not read optional bytes after complete error JSON")
            return super().read1(count)

    body = HangingBody(b'{"error":{"code":"invalid_json_schema"}}')

    def transport(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, body)

    monkeypatch.setattr("urllib.request.urlopen", transport)
    result = provider().invoke(request(), {}, step_index=1)
    assert result.error_details == {"code": "invalid_json_schema"} and body.closed


def test_diagnostic_timeout_keeps_known_rejection(monkeypatch):
    class TimedOutBody(BytesIO):
        def read1(self, count):
            raise TimeoutError("SECRET")

    body = TimedOutBody()

    def transport(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, body)

    monkeypatch.setattr("urllib.request.urlopen", transport)
    result = provider().invoke(request(), {}, step_index=1)
    assert result.status == "failed" and result.error_code == "http_400"
    assert result.error_details == {} and body.closed


def test_sanitizer_requires_known_values_instead_of_identifier_syntax():
    assert safe_provider_error_details({"code": "sk_SECRET", "type": "user_data", "param": "access_token"}) == {}
    assert safe_provider_error_details({"code": " INVALID_JSON_SCHEMA ", "message": "SECRET"}) == {
        "code": "invalid_json_schema",
    }
    assert safe_provider_error_details(None) == {}
