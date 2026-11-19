"""Long reviews consume streamed deltas; a cut connection never becomes success."""

from contextlib import contextmanager
from http.client import IncompleteRead, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import threading
import time
import urllib.error

import pytest

from olympus.agents.runtime import AgentRunner, RealProvider, UnknownOutcomeError
from olympus.agents.runtime.models import RolePermissions, RoleRunInput
from olympus.contracts.models import Ref


def request():
    return RoleRunInput(
        role_id="Momus", task_kind="plan_review", logical_task_id="review-plan", task_id="PLAN-stream",
        input_manifest={"plan": "review this"},
        input_refs=[Ref(object_id="PLAN-stream", version=1, content_hash="a" * 64)],
        permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["plan_review"],
                                    allowed_input_keys=["plan"]),
    )


def event(content=None, *, finish=None, **delta):
    if content is not None:
        delta["content"] = content
    return ("data: " + json.dumps({"id": "review-stream-id", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish},
    ]}, ensure_ascii=False) + "\r\n\r\n").encode()


class Response(BytesIO):
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}


class IdleAfterChunks(Response):
    """A gateway that keeps its connection open after the supplied chunks."""

    def __init__(self, chunks):
        super().__init__(b"")
        self.chunks, self.reads = iter(chunks), 0

    def read1(self, size=-1):
        self.reads += 1
        try:
            return next(self.chunks)
        except StopIteration:
            raise TimeoutError("the gateway left its terminal stream idle") from None


def invoke(monkeypatch, data):
    calls = []

    def open_response(req, **kwargs):
        calls.append(json.loads(req.data))
        return Response(data)

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    provider = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test")
    result = provider.invoke(request(), {}, step_index=1)
    assert len(calls) == 1
    assert calls[0]["stream"] is True
    return result


def test_stream_handles_heartbeats_reasoning_utf8_usage_and_fenced_json(monkeypatch):
    data = (b": keepalive\r\n\r\n" + event(reasoning_content="private reasoning")
            + event('```json\n{"verdict":"passed",') + event('"findings":[],"summary":"通过"}\n```')
            + event(finish="stop")
            + b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8}}\n\n'
            + b"data: [DONE]\n\n")
    result = invoke(monkeypatch, data)
    assert result.status == "succeeded"
    assert result.output == {"verdict": "passed", "findings": [], "summary": "通过"}
    assert result.provider_request_id == "review-stream-id"
    assert result.usage["prompt_tokens"] == 12
    assert result.usage["completion_tokens"] == 8
    assert result.usage["elapsed_ms"] >= 0


@pytest.mark.parametrize("suffix", [b"", b"data: [DONE]\n\n"])
def test_valid_stop_finishes_without_waiting_for_done_or_connection_close(monkeypatch, suffix):
    response = IdleAfterChunks([event('{"verdict":"passed","findings":[]}', finish="stop") + suffix])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    result = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test").invoke(
        request(), {}, step_index=1,
    )
    assert result.status == "succeeded"
    assert result.output == {"verdict": "passed", "findings": []}
    assert response.reads == 1


def test_terminal_stream_retains_buffered_usage_but_does_not_wait_for_later_statistics(monkeypatch):
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8}}\n\n'
    stop = event('{"verdict":"passed","findings":[]}', finish="stop")
    response = IdleAfterChunks([stop + usage])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    provider = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test")
    result = provider.invoke(request(), {}, step_index=1)
    assert result.status == "succeeded"
    assert result.usage["prompt_tokens"] == 12 and result.usage["completion_tokens"] == 8
    assert response.reads == 1

    response = IdleAfterChunks([stop, usage])
    result = provider.invoke(request(), {}, step_index=1)
    assert result.status == "succeeded" and response.reads == 1
    assert "prompt_tokens" not in result.usage and "completion_tokens" not in result.usage


@pytest.mark.parametrize("trailing", [
    b'data: {"choices":[],"usage":{"completion_tokens":8}}\n',
    b'data: {"choices":[],"usage":{"completion_tokens":',
])
def test_terminal_json_ignores_incomplete_optional_statistics_without_another_read(monkeypatch, trailing):
    response = IdleAfterChunks([event('{"verdict":"passed","findings":[]}', finish="stop") + trailing])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    result = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test").invoke(
        request(), {}, step_index=1,
    )
    assert result.status == "succeeded" and response.reads == 1
    assert result.output == {"verdict": "passed", "findings": []}
    assert "completion_tokens" not in result.usage


def test_optional_statistics_cannot_complete_an_unfinished_model_response(monkeypatch):
    response = IdleAfterChunks([event('{"verdict":') + b'data: {"choices":[],"usage":{}}\n'])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    result = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test").invoke(
        request(), {}, step_index=1,
    )
    assert result.status == "outcome_unknown" and result.output is None
    assert result.error_code == "provider_timeout"


def test_complete_model_content_after_stop_is_rejected(monkeypatch):
    result = invoke(monkeypatch, event('{"verdict":"passed","findings":[]}', finish="stop")
                    + event('{"unexpected":"extra model content"}'))
    assert result.status == "failed" and result.error_code == "invalid_provider_response"
    assert result.output is None


@pytest.mark.parametrize("message", ['{"verdict":', '[]', 'null', '```json\n{"broken":\n```'])
def test_stop_does_not_accept_truncated_or_nonobject_json(monkeypatch, message):
    response = IdleAfterChunks([event(message, finish="stop")])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    result = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test").invoke(
        request(), {}, step_index=1,
    )
    assert result.status == "failed" and result.error_code == "invalid_provider_response"
    assert result.output is None and response.reads == 1


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls"])
def test_nonstop_terminal_reason_rejects_even_complete_json(monkeypatch, reason):
    result = invoke(monkeypatch, event('{"verdict":"passed","findings":[]}', finish=reason))
    assert result.status == "failed" and result.error_code == "provider_output_incomplete"
    assert result.output is None


@pytest.mark.parametrize("suffix", [b"", b"data: [DONE]\n\n", b"data: {broken"])
def test_complete_json_without_terminal_event_is_unknown(monkeypatch, suffix):
    result = invoke(monkeypatch, event('{"verdict":"passed","findings":[]}') + suffix)
    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_stream_interrupted"
    assert result.provider_request_id == "review-stream-id"
    assert result.output is None


def test_token_limit_is_an_explicit_failure(monkeypatch):
    result = invoke(monkeypatch, event('{"verdict":') + event(finish="length"))
    assert result.status == "failed"
    assert result.error_code == "provider_output_incomplete"


@pytest.mark.parametrize("failure,code", [
    (TimeoutError(), "provider_timeout"),
    (urllib.error.URLError(TimeoutError()), "provider_timeout"),
    (RemoteDisconnected(), "provider_transport_error"),
    (ConnectionResetError(), "provider_transport_error"),
    (IncompleteRead(b"partial"), "provider_transport_error"),
])
def test_transport_errors_keep_budget_reserved_and_include_role(monkeypatch, failure, code):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail)
    runner = AgentRunner(RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test"))
    with pytest.raises(UnknownOutcomeError) as raised:
        runner.run(request())
    assert len(calls) == 1
    assert (raised.value.role_id, raised.value.task_kind, raised.value.error_code) == ("Momus", "plan_review", code)
    entry, = runner.ledger.entries()
    assert entry.status == "outcome_unknown"
    assert entry.error_code == code
    assert entry.request_id == raised.value.request_id
    assert runner.ledger.total_calls_accounted("RUN-default") == 1


def test_heartbeat_stream_obeys_total_deadline(monkeypatch):
    ticks = iter([0, 0, 1, 2, 4])
    monkeypatch.setattr("olympus.agents.runtime.providers.monotonic", lambda: next(ticks, 4))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(b": heartbeat\n\n" * 10))
    result = RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test",
                          timeout_seconds=3).invoke(request(), {}, step_index=1)
    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_timeout"


@contextmanager
def streaming_server(data, content_type="text/event-stream"):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            for frame in data:
                if isinstance(frame, float):
                    time.sleep(frame)
                    continue
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("complete", [True, False])
def test_real_http_stream_checks_terminal_response(complete):
    frames = [b": keepalive\n\n", event('{"verdict":"passed","findings":[]}')]
    if complete:
        frames += [event(finish="stop"), b"data: [DONE]\n\n"]
    with streaming_server(frames) as (url, calls):
        provider = RealProvider(api_key="test", base_url=url, model="test", timeout_seconds=2)
        result = provider.invoke(request(), {}, step_index=1)
    assert len(calls) == 1
    assert result.status == ("succeeded" if complete else "outcome_unknown")
    if complete:
        assert result.output == {"verdict": "passed", "findings": []}


def test_real_http_terminal_json_finishes_while_gateway_keeps_connection_open():
    frames = [event('{"verdict":"passed","findings":[]}', finish="stop"), 0.8]
    with streaming_server(frames) as (url, calls):
        started = time.monotonic()
        result = RealProvider(api_key="test", base_url=url, model="test", timeout_seconds=0.5).invoke(
            request(), {}, step_index=1,
        )
        elapsed = time.monotonic() - started
    assert len(calls) == 1 and result.status == "succeeded"
    assert elapsed < 0.4


@pytest.mark.parametrize("content_type", ["text/event-stream", "application/json"])
def test_real_slow_drip_without_newlines_cannot_extend_deadline(content_type):
    frames = [b"x", 0.03] * 30
    with streaming_server(frames, content_type) as (url, calls):
        started = time.monotonic()
        result = RealProvider(api_key="test", base_url=url, model="test", timeout_seconds=0.15).invoke(
            request(), {}, step_index=1,
        )
        elapsed = time.monotonic() - started
    assert len(calls) == 1
    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_timeout"
    assert elapsed < 0.4


def test_response_size_limit_does_not_accept_partial_output(monkeypatch):
    monkeypatch.setattr("olympus.agents.runtime.providers.MAX_RESPONSE_BYTES", 32)
    result = invoke(monkeypatch, event('{"verdict":"passed","findings":[]}'))
    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_response_too_large"
