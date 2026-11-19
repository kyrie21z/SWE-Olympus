"""Configuration survives restart, stays secret-free and routes real HTTP calls by role."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.error import URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from olympus.agents.runtime import ProviderNotConfiguredError, RealProvider, RolePermissions, RoleRunInput
from olympus.api.errors import install_error_handlers
from olympus.api.settings_routes import router
from olympus.contracts.models import Ref, fingerprint
from olympus.settings import AGENT_ROLES, AgentSettingsStore, AgentSettingsUpdate, make_runner


def client_for(path):
    app = FastAPI()
    app.state.agent_settings = AgentSettingsStore(path)
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


@contextmanager
def model_server():
    calls = []
    behavior = {"status": 200, "content": '{"ok": true}'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            self.send_response(behavior.get("models_status", 200))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {"data": [{"id": "design-model"}, {"id": "design-model"}, {"id": "another-model"}]}
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "authorization": self.headers.get("Authorization"), **payload})
            self.send_response(behavior["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {"choices": [{"message": {"content": behavior["content"]}}], "id": "local-model-test"}
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls, behavior
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def role_request(role_id):
    return RoleRunInput(
        role_id=role_id, task_kind="review", logical_task_id=f"TASK-{role_id}", task_id=f"TASK-{role_id}",
        input_manifest={"document": "sample"},
        input_refs=[Ref(object_id="REQ-SETTINGS-TEST", version=1, content_hash=fingerprint({}))],
        permissions=RolePermissions(
            role_id=role_id, allowed_task_kinds=["review"], allowed_input_keys=["document"],
        ),
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
    )


def test_settings_persist_without_exposing_keys_and_blank_edits_preserve_them(tmp_path):
    path = tmp_path / "state.sqlite3"
    client = client_for(path)
    initial = client.get("/api/settings/agents")
    assert initial.status_code == 200
    assert [item["role_id"] for item in initial.json()["agents"]] == list(AGENT_ROLES)
    assert not any(item["configured"] for item in initial.json()["agents"])
    updated = client.put("/api/settings/agents/Athena", json={
        "base_url": "https://model.example/v1/", "model": "model-a", "api_key": "private-settings-key",
        "timeout_seconds": 42,
    })
    assert updated.status_code == 200
    assert updated.json()["base_url"] == "https://model.example/v1"
    assert updated.json()["has_api_key"] is True
    assert "private-settings-key" not in updated.text
    assert "api_key" not in updated.json()

    # A new application/store proves this is a durable server-side setting.
    restarted = client_for(path)
    for payload in [{"model": "model-b"}, {"api_key": "   "}]:
        changed = restarted.put("/api/settings/agents/Athena", json=payload)
        assert changed.json()["configured"] is True
        assert changed.json()["has_api_key"] is True
    listing = restarted.get("/api/settings/agents")
    assert "private-settings-key" not in listing.text
    assert listing.json()["agents"][0]["model"] == "model-b"
    assert not listing.json()["agents"][1]["configured"]
    assert AgentSettingsStore(path).get("Athena").api_key.get_secret_value() == "private-settings-key"
    cleared = restarted.put("/api/settings/agents/Athena", json={"clear_api_key": True})
    assert cleared.json()["has_api_key"] is False
    assert cleared.json()["configured"] is False


@pytest.mark.parametrize("payload", [
    {"base_url": "ftp://example.org/v1"},
    {"base_url": "https://user:password@example.org/v1"},
    {"base_url": "https://example.org/v1?key=secret"},
    {"base_url": "https://example.org/v1#fragment"},
    {"base_url": "https://example.org:wrong/v1"},
    {"base_url": "https://exa mple.org/v1"},
    {"timeout_seconds": 0},
    {"timeout_seconds": 901},
    {"timeout_seconds": True},
    {"timeout_seconds": 1.5},
])
def test_invalid_endpoint_and_timeout_are_rejected_before_saving(tmp_path, payload):
    client = client_for(tmp_path / "state.sqlite3")
    result = client.put("/api/settings/agents/Athena", json=payload)
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/api/settings/agents").json()["agents"][0]["configured"] is False


def test_non_model_role_and_unconfigured_connection_are_explicit_errors(tmp_path):
    client = client_for(tmp_path / "state.sqlite3")
    unknown = client.put("/api/settings/agents/Zeus", json={"model": "a"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "UNKNOWN_AGENT"
    missing = client.post("/api/settings/agents/Athena/test")
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"


def test_model_discovery_uses_draft_without_saving_or_sending_old_key_to_a_new_address(tmp_path):
    client = client_for(tmp_path / "state.sqlite3")
    path = "/api/settings/agents/Athena"
    with model_server() as (url, calls, behavior):
        client.put(path, json={"base_url": "https://old.example/v1", "api_key": "old-key"})
        missing = client.post(f"{path}/models", json={"base_url": url})
        assert missing.status_code == 409
        assert not calls
        payload = {"base_url": url, "api_key": "draft-key"}
        listed = client.post(f"{path}/models", json=payload)
        assert listed.json() == {"models": ["another-model", "design-model"]}
        assert calls == [{"path": "/v1/models", "authorization": "Bearer draft-key"}]
        saved = client.get("/api/settings/agents").json()["agents"][0]
        assert saved["base_url"] == "https://old.example/v1"
        behavior["models_status"] = 404
        unsupported = client.post(f"{path}/models", json=payload)
        assert unsupported.status_code == 502
        assert "手动填写模型 ID" in unsupported.json()["error"]["message"]


def test_role_provider_uses_independent_endpoints_and_one_runner_ledger(tmp_path):
    settings = AgentSettingsStore(tmp_path / "state.sqlite3")
    with model_server() as (athena_url, athena_calls, _), model_server() as (momus_url, momus_calls, _):
        settings.update("Athena", AgentSettingsUpdate(
            base_url=athena_url, model="athena-model", api_key="athena-key",
        ))
        settings.update("Momus", AgentSettingsUpdate(
            base_url=momus_url, model="momus-model", api_key="momus-key",
        ))
        runner = make_runner(settings)
        first = runner.run(role_request("Athena"))
        second = runner.run(role_request("Momus"))
        assert first.mode == second.mode == "real_model"
        assert first.structured_output == second.structured_output == {"ok": True}
        assert runner.ledger.total_calls_accounted() == 2
        assert athena_calls[0]["path"] == "/v1/chat/completions"
        assert athena_calls[0]["model"] == "athena-model"
        assert athena_calls[0]["authorization"] == "Bearer athena-key"
        assert momus_calls[0]["model"] == "momus-model"
        assert momus_calls[0]["authorization"] == "Bearer momus-key"
        # An unused role is allowed to remain empty, but running it cannot silently
        # use another role's credentials or the repository mock fixtures.
        with pytest.raises(ProviderNotConfiguredError, match="Hephaestus"):
            runner.run(role_request("Hephaestus"))
        assert runner.ledger.total_calls_accounted() == 2


def test_saved_connection_test_calls_http_and_sanitizes_upstream_errors(tmp_path):
    client = client_for(tmp_path / "state.sqlite3")
    with model_server() as (url, calls, behavior):
        client.put("/api/settings/agents/Argus", json={
            "base_url": url, "model": "argus-model", "api_key": "private-argus-key",
        })
        connected = client.post("/api/settings/agents/Argus/test")
        assert connected.status_code == 200
        assert connected.json()["ok"] is True
        assert connected.json()["model"] == "argus-model"
        assert connected.json()["elapsed_ms"] >= 0
        assert len(calls) == 1
        behavior["status"] = 401
        rejected = client.post("/api/settings/agents/Argus/test")
        assert rejected.status_code == 502
        assert rejected.json()["error"]["code"] == "AGENT_CONNECTION_FAILED"
        assert "private-argus-key" not in rejected.text
        behavior["status"] = 200
        behavior["content"] = "upstream malformed output with private-argus-key"
        malformed = client.post("/api/settings/agents/Argus/test")
        assert malformed.status_code == 502
        assert malformed.json()["error"]["detail"]["reason"] == "invalid_model_output"
        assert "private-argus-key" not in malformed.text


@pytest.mark.parametrize(("root", "model", "fast"), [
    ("https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "qwen3.8-max", True),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash", True),
    ("https://maas.qianwenaiapi.com/compatible-mode/v1", "qwen4-max", True),
    ("https://maas.qianwenaiapi.com/compatible-mode/v1", "qwen3.8-flash", True),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen4-max", True),
    ("https://example.net/v1", "qwen4-max", False),
    ("https://example.net/v1", "qwen3.8-max", False),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "another-model", False),
])
def test_model_studio_json_requests_skip_extra_thinking_only_for_supported_models(monkeypatch, root, model, fast):
    requests = []

    def timed_out(request, *, timeout):
        requests.append(json.loads(request.data))
        assert timeout == 7
        raise URLError(TimeoutError())

    monkeypatch.setattr("urllib.request.urlopen", timed_out)
    provider = RealProvider(base_url=root, model=model, api_key="test-only", timeout_seconds=7)
    result = provider.invoke(role_request("Athena"), {}, step_index=1, prompt="Return a JSON object.")
    assert len(requests) == 1
    payload = requests[0]
    if fast:
        assert payload["enable_thinking"] is False
        assert payload["enable_search"] is False
        assert payload["response_format"] == {"type": "json_object"}
    else:
        assert not {"enable_thinking", "enable_search", "response_format"} & payload.keys()
    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_timeout"


def test_long_review_timeout_is_configurable_and_saved_values_are_preserved(tmp_path):
    client = client_for(tmp_path / "state.sqlite3")
    agents = {row["role_id"]: row for row in client.get("/api/settings/agents").json()["agents"]}
    assert agents["Momus"]["timeout_seconds"] == 300
    assert agents["Athena"]["timeout_seconds"] == 120
    saved = client.put("/api/settings/agents/Momus", json={"timeout_seconds": 900})
    assert saved.status_code == 200
    assert saved.json()["timeout_seconds"] == 900
    changed = client.put("/api/settings/agents/Momus", json={"model": "updated-model"})
    assert changed.json()["timeout_seconds"] == 900


def test_settings_repr_hides_secret_and_provider_never_falls_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_MODEL_API_KEY", "global-key")
    monkeypatch.setenv("OLYMPUS_MODEL_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("OLYMPUS_MODEL_NAME", "global-model")
    store = AgentSettingsStore(tmp_path / "state.sqlite3")
    private = store.update("Athena", AgentSettingsUpdate(api_key="hidden-in-repr"))
    assert "hidden-in-repr" not in repr(private)
    assert "hidden-in-repr" not in json.dumps(private.public())
    with pytest.raises(ProviderNotConfiguredError):
        private.provider()
