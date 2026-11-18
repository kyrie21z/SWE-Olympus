"""Choose a compatible response format before sending, without hidden HTTP retries."""

from io import BytesIO
import json
import urllib.error

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.runtime import AgentRunner, OutputSchemaError, RealProvider
from olympus.agents.runtime.providers import _strict_schema_compatible

from test_provider_streaming import request


CLOSED = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}


def provider():
    return RealProvider(api_key="test", base_url="https://maas.qianwenaiapi.com/compatible-mode/v1", model="qwen4-max")


def capture(monkeypatch, *, failure=False):
    calls = []

    def transport(req, **kwargs):
        calls.append(json.loads(req.data))
        if failure:
            raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, BytesIO(b'{}'))
        return BytesIO(json.dumps({"id": "fixture", "choices": [{"message": {"content": '{"ok":true}'}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", transport)
    return calls


def test_closed_fully_required_schema_keeps_strict_mode_and_fast_stream_settings(monkeypatch):
    calls = capture(monkeypatch)
    provider().invoke(request().model_copy(update={"output_schema": CLOSED}), {}, step_index=1)
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "olympus_output", "strict": True, "schema": CLOSED,
    }}
    assert calls[0]["stream"] is True and calls[0]["enable_thinking"] is False


@pytest.mark.parametrize("nested", [
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object", "additionalProperties": True},
    {"type": "object", "properties": {"optional": {"type": "string"}}, "additionalProperties": False, "required": []},
    {"type": "string", "default": "value"},
    {"allOf": [{"type": "string"}]},
])
def test_incompatible_nested_schemas_use_json_object_before_exactly_one_request(monkeypatch, nested):
    schema = CLOSED | {"properties": {"ok": nested}}
    calls = capture(monkeypatch)
    result = provider().invoke(request().model_copy(update={"output_schema": schema}), {}, step_index=1)
    assert result.status == "succeeded" and len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_real_daedalus_api_contract_dynamic_dictionary_schema_is_preflighted(monkeypatch):
    schema = DaedalusPlanner.draft_schema()
    calls = capture(monkeypatch)
    with pytest.raises(OutputSchemaError):
        AgentRunner(provider()).run(request().model_copy(update={"output_schema": schema}))
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}


def test_dynamic_dictionary_behind_a_reference_is_not_strict_compatible():
    schema = CLOSED | {"$defs": {"Dynamic": {"type": "object", "additionalProperties": {"type": "string"}}},
                       "properties": {"ok": {"$ref": "#/$defs/Dynamic"}}}
    assert not _strict_schema_compatible(schema)
    compatible = CLOSED | {"$defs": {"Flag": {"type": "boolean"}}, "properties": {"ok": {"$ref": "#/$defs/Flag"}}}
    assert _strict_schema_compatible(compatible)


def test_recursive_or_malformed_schema_safely_falls_back():
    schema = CLOSED | {"properties": {"ok": {"$ref": "#/properties/ok"}}}
    assert not _strict_schema_compatible(schema)
    assert not _strict_schema_compatible(CLOSED | {"properties": {"ok": {"type": [{"unexpected": "type"}]}}})


def test_http_rejection_never_triggers_an_internal_second_request(monkeypatch):
    calls = capture(monkeypatch, failure=True)
    result = provider().invoke(request().model_copy(update={"output_schema": CLOSED}), {}, step_index=1)
    assert result.status == "failed" and result.error_code == "http_400" and len(calls) == 1
