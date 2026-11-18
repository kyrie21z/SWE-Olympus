"""Vendor-neutral provider adapters: deterministic fixtures and a real HTTP model."""

from __future__ import annotations

import json
import os
import re
from http.client import HTTPException
import urllib.error
import urllib.request
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue

from olympus.agents.runtime.models import InputWhitelistError, ProviderNotConfiguredError, RoleRunInput
from olympus.contracts.models import Contract, Ref, fingerprint
from olympus.requests.ledger import RequestKind

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FIXTURES_DIR = ROOT / "fixtures" / "agents"

StepStatus = Literal["succeeded", "failed", "outcome_unknown"]
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SPEC_KEYS = frozenset(
    {"steps", "output", "result", "status", "kind", "usage", "late_generation", "review_id"}
)
_MODEL_STUDIO_FAST_MODELS = frozenset({"qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash", "qwen4-max"})
_JSON_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n```\s*\Z", re.IGNORECASE | re.DOTALL)


def _model_studio_options(url: str, model: str | None) -> dict[str, JsonValue]:
    host = urlsplit(url).hostname or ""
    model_studio = host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "maas.qianwenaiapi.com",
    }
    if not model_studio or model not in _MODEL_STUDIO_FAST_MODELS:
        return {}
    # These roles work from supplied sources and validate structured results locally.
    return {"enable_thinking": False, "enable_search": False, "response_format": {"type": "json_object"}}


def safe_provider_error_details(details) -> dict[str, str]:
    """Only known diagnostic tokens may cross the provider/ledger/UI boundary."""
    allowed = {
        "code": {"invalid_json_schema", "invalid_request_error", "invalid_request", "invalid_parameter",
                 "invalid_parameter_error", "unsupported_parameter", "unsupported_value", "model_not_found",
                 "context_length_exceeded", "rate_limit_exceeded", "insufficient_quota", "invalid_api_key",
                 "authentication_error", "permission_denied", "invalid_response_format", "unsupported_response_format",
                 "invalidparameter", "invalidparametervalue", "invalidrequest", "badrequest",
                 "invalidparameter.unsupported", "invalidapikey", "accessdenied", "throttling",
                 "quotaexhausted", "modelnotfound", "datainspectionfailed"},
        "type": {"invalid_request_error", "authentication_error", "permission_error", "rate_limit_error",
                 "server_error", "invalid_parameter_error", "bad_request_error", "validation_error",
                 "request_error", "api_error", "insufficient_quota"},
        "param": {"response_format", "response_format.type", "response_format.json_schema",
                  "response_format.json_schema.schema", "model", "messages", "temperature", "stream",
                  "max_tokens", "max_completion_tokens", "enable_thinking", "enable_search", "tools", "tool_choice"},
    }
    if not isinstance(details, dict):
        return {}
    return {key: value.strip().lower() for key, value in details.items()
            if key in allowed and isinstance(value, str) and len(value) <= 96 and value.strip().lower() in allowed[key]}


def _strict_schema_compatible(schema: dict) -> bool:
    """Use remote strict mode only for a closed, fully required supported subset."""
    if schema.get("type") != "object":
        return False
    allowed = {"type", "properties", "required", "additionalProperties", "items", "anyOf", "enum",
               "$ref", "$defs", "title", "description", "minLength", "maxLength", "pattern",
               "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems"}
    types = {"object", "array", "string", "integer", "number", "boolean", "null"}

    def visit(node, active):
        if not isinstance(node, dict) or set(node) - allowed or id(node) in active:
            return False
        active = {*active, id(node)}
        if "$defs" in node:
            definitions = node["$defs"]
            if not isinstance(definitions, dict) or not all(visit(value, active) for value in definitions.values()):
                return False
        if "$ref" in node:
            reference = node["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/"):
                return False
            target = schema
            for part in reference[2:].split("/"):
                if not isinstance(target, dict) or part.replace("~1", "/").replace("~0", "~") not in target:
                    return False
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return set(node) <= {"$ref", "title", "description"} and visit(target, active)
        if "anyOf" in node:
            alternatives = node["anyOf"]
            return (set(node) <= {"anyOf", "title", "description"} and isinstance(alternatives, list)
                    and bool(alternatives) and all(visit(value, active) for value in alternatives))
        declared = node.get("type")
        declared = [declared] if isinstance(declared, str) else declared
        if not isinstance(declared, list) or not declared or any(not isinstance(value, str) or value not in types for value in declared):
            return False
        if "object" in declared:
            properties, required = node.get("properties"), node.get("required")
            if (node.get("additionalProperties") is not False or not isinstance(properties, dict)
                    or not isinstance(required, list) or any(not isinstance(value, str) for value in required)
                    or len(required) != len(set(required)) or set(required) != set(properties)
                    or not all(visit(value, active) for value in properties.values())):
                return False
        if "array" in declared and not visit(node.get("items"), active):
            return False
        if "enum" in node and (not isinstance(node["enum"], list) or
                               any(isinstance(value, (dict, list)) for value in node["enum"])):
            return False
        return True

    return visit(schema, set())


class _StreamError(Exception):
    def __init__(self, code, *, status="outcome_unknown"):
        self.code, self.status = code, status


def _response_chunks(response, deadline):
    """One socket read at a time, with a shrinking wall-clock budget and byte cap."""
    read = getattr(response, "read1", None)
    total = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _StreamError("provider_timeout")
        # urllib exposes no public setter for the response socket timeout. Its
        # HTTPResponse wraps the socket here (including HTTPS SSLSocket). read1
        # performs at most one recv, unlike read/readline which can drip forever.
        raw = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(65536) if read else response.read()
        if monotonic() >= deadline:
            raise _StreamError("provider_timeout")
        if not chunk:
            return
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _StreamError("provider_response_too_large")
        yield chunk
        if read is None:  # Lightweight non-socket transport stand-ins.
            return


def _response_lines(response, deadline):
    """Yield decoded lines and None at each already-read chunk boundary."""
    pending = b""
    for chunk in _response_chunks(response, deadline):
        pending += chunk
        while match := re.search(rb"\r\n|\r(?!$)|\n", pending):
            yield pending[:match.start()].decode("utf-8")
            pending = pending[match.end():]
        yield None
    if pending:
        yield pending.removesuffix(b"\r").decode("utf-8")


def _read_stream(response, deadline, metadata):
    """Consume SSE heartbeats and deltas without treating partial JSON as success."""
    fragments, event_data = [], []
    usage, finished = {}, False
    for line in _response_lines(response, deadline):
        if line is None:
            # stop is the model's terminal result. Retain any usage already in
            # this chunk, but never wait on another recv for optional [DONE]/usage.
            if finished:
                event_data = []  # An optional trailing statistics event may be incomplete.
                break
            continue
        if line.startswith("data:"):
            event_data.append(line[5:].removeprefix(" "))
            continue
        if line or not event_data:
            continue  # SSE comments, event names and heartbeat lines.
        data, event_data = "\n".join(event_data), []
        if data == "[DONE]":
            if not finished:
                raise _StreamError("provider_stream_interrupted")
            break
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise ValueError("invalid stream event")
        if isinstance(chunk.get("id"), str):
            metadata["id"] = chunk["id"]
        if "error" in chunk:
            raise _StreamError("provider_stream_error")
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("invalid stream choices")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ValueError("invalid stream delta")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str) or finished:
                    raise ValueError("invalid stream content")
                fragments.append(content)
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason != "stop":
                    raise _StreamError("provider_output_incomplete", status="failed")
                finished = True
    if not finished or event_data:
        raise _StreamError("provider_stream_interrupted")
    message = "".join(fragments)
    try:
        fence = _JSON_FENCE.fullmatch(message)
        parsed = json.loads(fence.group(1) if fence else message)
        if not isinstance(parsed, dict):
            raise ValueError("structured response must be an object")
    except (ValueError, TypeError) as exc:
        raise _StreamError("invalid_provider_response", status="failed") from exc
    return {"id": metadata.get("id"), "usage": usage,
            "choices": [{"message": {"content": message}}]}


def _http_error_details(response, deadline):
    """Read bounded diagnostics from this rejection, without retrying the request."""
    try:
        raw = bytearray()
        for chunk in _response_chunks(response, min(deadline, monotonic() + 1)):
            if len(raw) + len(chunk) > 65536:
                return {}
            raw.extend(chunk)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(body, dict):
                return {}
            return safe_provider_error_details(body.get("error", body))
        return {}
    except (_StreamError, OSError, HTTPException, ValueError, TypeError, AttributeError):
        return {}  # A diagnostic read cannot change a known HTTP rejection.
    finally:
        try:
            response.close()
        except (OSError, HTTPException):
            pass


class ProviderStepResult(Contract):
    kind: RequestKind
    status: StepStatus = "succeeded"
    output: dict[str, JsonValue] | None = None
    tool: dict[str, JsonValue] | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    error_code: str | None = None
    error_details: dict[str, str] = Field(default_factory=dict)
    late_generation: int | None = None


def _usage(step: dict[str, JsonValue]) -> dict[str, int]:
    raw = step.get("usage")
    if not isinstance(raw, dict):
        return {}
    return {
        key: int(value)
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


class MockProvider:
    """Deterministic fixture provider. Every response is a pure function of its key."""

    mode: Literal["mock"] = "mock"

    def __init__(
        self,
        fixtures: dict[str, JsonValue] | None = None,
        fixtures_dir: str | Path | None = None,
    ):
        if fixtures is not None:
            self.fixtures = dict(fixtures)
        else:
            self.fixtures = self._load_fixtures(Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES_DIR)

    @staticmethod
    def _load_fixtures(directory: Path) -> dict[str, JsonValue]:
        fixtures: dict[str, JsonValue] = {}
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError(f"fixture {path.name} must contain a JSON object")
            nested = data.get("fixtures")
            if isinstance(nested, dict):
                fixtures.update(nested)
            elif _SPEC_KEYS & set(data):
                fixtures[path.stem] = data
            else:
                fixtures.update(data)
        return fixtures

    def has_fixture(self, key: str) -> bool:
        return key in self.fixtures

    def resolve(self, request: RoleRunInput) -> JsonValue:
        candidates = [
            request.fixture_key,
            request.role_id,
            f"{request.role_id}:{request.task_kind}",
            request.task_kind,
        ]
        for key in candidates:
            if key and key in self.fixtures:
                spec = self.fixtures[key]
                if isinstance(spec, dict) and "input_refs" in spec:
                    declared = [Ref.model_validate(ref) for ref in spec["input_refs"]]
                    if any(ref not in request.input_refs for ref in declared):
                        raise InputWhitelistError("mock fixture is bound to different input versions")
                return spec
        raise KeyError(f"no mock fixture for role={request.role_id!r} task_kind={request.task_kind!r}")

    def steps(self, request: RoleRunInput) -> list[dict[str, JsonValue]]:
        spec = self.resolve(request)
        if isinstance(spec, list):
            raw_steps: JsonValue = spec
        elif isinstance(spec, dict) and isinstance(spec.get("steps"), list):
            raw_steps = spec["steps"]
        elif isinstance(spec, dict):
            raw_steps = [spec]
        else:
            raise TypeError(f"mock fixture for {request.role_id!r} is not an object or list")
        steps: list[dict[str, JsonValue]] = []
        for step in raw_steps:
            if not isinstance(step, dict):
                raise TypeError("mock fixture step must be an object")
            steps.append(step)
        if not steps:
            raise ValueError(f"mock fixture for {request.role_id!r} has no steps")
        return steps

    def invoke(
        self,
        request: RoleRunInput,
        step: dict[str, JsonValue],
        *,
        step_index: int,
        prompt: Any = None,
    ) -> ProviderStepResult:
        del prompt
        kind = step.get("kind", "model")
        if kind not in ("model", "tool"):
            raise ValueError(f"invalid mock step kind: {kind}")
        status = step.get("status", "succeeded")
        if status not in ("succeeded", "failed", "outcome_unknown"):
            raise ValueError(f"invalid mock step status: {status}")
        output = step.get("output")
        if output is not None and not isinstance(output, dict):
            raise ValueError("mock model output must be an object")
        tool = None
        if kind == "tool":
            result = step.get("result")
            tool = {
                "name": step.get("name", "tool"),
                "arguments": step.get("arguments", {}),
                "result": result if result is not None else {},
            }
        seed = {
            "role_id": request.role_id,
            "logical_task_id": request.logical_task_id,
            "task_id": request.task_id,
            "generation": request.generation,
            "step_index": step_index,
        }
        late = step.get("late_generation")
        return ProviderStepResult(
            kind=kind,
            status=status,
            output=output,
            tool=tool,
            usage=_usage(step),
            provider_request_id=step.get("provider_request_id") or f"mock-{fingerprint(seed)[:16]}",
            error_code=step.get("error_code"),
            late_generation=late if isinstance(late, int) else None,
        )


class RealProvider:
    """Minimal vendor-neutral chat-completions client.

    Configuration is explicit or environment based. It never fabricates success when
    unconfigured, and a transport failure is reported as an unknown outcome rather
    than a known failure.
    """

    mode: Literal["real_model"] = "real_model"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 120,
    ):
        self.api_key = api_key or os.environ.get("OLYMPUS_MODEL_API_KEY")
        self.base_url = base_url or os.environ.get("OLYMPUS_MODEL_BASE_URL")
        self.model = model or os.environ.get("OLYMPUS_MODEL_NAME")
        self.timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def require_configured(self) -> None:
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "RealProvider is not configured: missing OLYMPUS_MODEL_API_KEY, "
                "OLYMPUS_MODEL_BASE_URL, or OLYMPUS_MODEL_NAME"
            )

    def steps(self, request: RoleRunInput) -> list[dict[str, JsonValue]]:
        del request
        return [{"kind": "model"}]

    def invoke(
        self,
        request: RoleRunInput,
        step: dict[str, JsonValue],
        *,
        step_index: int,
        prompt: Any = None,
    ) -> ProviderStepResult:
        del step, step_index
        self.require_configured()
        if isinstance(prompt, str):
            user_content = prompt
        else:
            user_content = json.dumps(prompt, ensure_ascii=False, sort_keys=True, default=str)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": (
                    f"You are the {request.role_id} role. Return only the concise required result. "
                    "Do not emit reasoning, commentary, repeated background or unrequested alternatives. "
                    "Keep generated code and checks focused on this task; use local helpers instead of "
                    "repeating full suites and long explanations in every check."
                )},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "stream": True,
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        fast_options = _model_studio_options(url, self.model)
        if fast_options:
            payload.update(fast_options)
            payload["messages"][0]["content"] += " Return a JSON object without Markdown or surrounding prose."
            schema = request.output_schema
            if _strict_schema_compatible(schema):
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "olympus_output", "strict": True, "schema": schema,
                }}
        http_request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        metadata = {}
        started = monotonic()
        deadline = started + self.timeout_seconds
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                content_type = getattr(response, "headers", {}).get("Content-Type", "")
                if "text/event-stream" in content_type.lower():
                    body = _read_stream(response, deadline, metadata)
                else:
                    # Some compatible gateways still return a single JSON response.
                    body = json.loads(b"".join(_response_chunks(response, deadline)))
        except urllib.error.HTTPError as exc:
            details = _http_error_details(exc, deadline)
            return ProviderStepResult(
                kind="model", status="outcome_unknown" if exc.code >= 500 else "failed",
                error_code=f"http_{exc.code}", error_details=details,
                usage={"elapsed_ms": round((monotonic() - started) * 1000)},
            )
        except _StreamError as exc:
            return ProviderStepResult(
                kind="model", status=exc.status, error_code=exc.code,
                provider_request_id=metadata.get("id"),
                usage={"elapsed_ms": round((monotonic() - started) * 1000)},
            )
        except (OSError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            code = "provider_timeout" if isinstance(reason, TimeoutError) else "provider_transport_error"
            return ProviderStepResult(
                kind="model", status="outcome_unknown", error_code=code,
                provider_request_id=metadata.get("id"),
                usage={"elapsed_ms": round((monotonic() - started) * 1000)},
            )
        except (ValueError, UnicodeError):
            return ProviderStepResult(kind="model", status="failed", error_code="invalid_provider_response")
        try:
            message = body["choices"][0]["message"]["content"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("empty provider response")
            finish_reason = body["choices"][0].get("finish_reason")
            if finish_reason is not None and finish_reason != "stop":
                return ProviderStepResult(kind="model", status="failed", error_code="provider_output_incomplete")
        except (ValueError, KeyError, IndexError, TypeError):
            return ProviderStepResult(kind="model", status="failed", error_code="invalid_provider_response")
        try:
            fence = _JSON_FENCE.fullmatch(message)
            parsed = json.loads(fence.group(1) if fence else message)
            output = parsed if isinstance(parsed, dict) else {"content": message}
        except (json.JSONDecodeError, TypeError):
            output = {"content": message}
        raw_usage = body.get("usage") if isinstance(body, dict) else None
        usage: dict[str, int] = {"elapsed_ms": round((monotonic() - started) * 1000)}
        if isinstance(raw_usage, dict):
            for source, target in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
            ):
                value = raw_usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[target] = value
        return ProviderStepResult(
            kind="model",
            status="succeeded",
            output=output,
            usage=usage,
            provider_request_id=body.get("id") if isinstance(body, dict) else None,
        )


Provider = MockProvider | RealProvider
