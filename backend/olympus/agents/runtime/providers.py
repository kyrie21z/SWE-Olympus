"""Vendor-neutral provider adapters: deterministic fixtures and a real HTTP model."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from http.client import HTTPException
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
_SPEC_KEYS = frozenset(
    {"steps", "output", "result", "status", "kind", "usage", "late_generation", "review_id"}
)
_MODEL_STUDIO_FAST_MODELS = frozenset({"qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash"})
_JSON_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n```\s*\Z", re.IGNORECASE | re.DOTALL)


def _model_studio_options(url: str, model: str | None) -> dict[str, JsonValue]:
    host = urlsplit(url).hostname or ""
    model_studio = host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
    }
    if not model_studio or model not in _MODEL_STUDIO_FAST_MODELS:
        return {}
    # These roles work from supplied sources and validate structured results locally.
    return {"enable_thinking": False, "enable_search": False, "response_format": {"type": "json_object"}}


class ProviderStepResult(Contract):
    kind: RequestKind
    status: StepStatus = "succeeded"
    output: dict[str, JsonValue] | None = None
    tool: dict[str, JsonValue] | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    error_code: str | None = None
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
                {"role": "system", "content": f"You are the {request.role_id} role."},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        fast_options = _model_studio_options(url, self.model)
        if fast_options:
            payload.update(fast_options)
            payload["messages"][0]["content"] += " Return a JSON object without Markdown or surrounding prose."
            schema = request.output_schema
            if schema.get("additionalProperties") is False and schema.get("properties"):
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
        started = monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                return ProviderStepResult(
                    kind="model", status="outcome_unknown", error_code=f"http_{exc.code}"
                )
            return ProviderStepResult(kind="model", status="failed", error_code=f"http_{exc.code}")
        except (OSError, HTTPException) as exc:
            timed_out = isinstance(exc, TimeoutError) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
            )
            return ProviderStepResult(
                kind="model", status="outcome_unknown",
                error_code="provider_timeout" if timed_out else "provider_transport_error",
                usage={"elapsed_ms": round((monotonic() - started) * 1000)},
            )
        try:
            body = json.loads(raw)
            message = body["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, UnicodeError, KeyError, IndexError, TypeError):
            return ProviderStepResult(kind="model", status="failed", error_code="invalid_provider_response")
        try:
            fence = _JSON_FENCE.fullmatch(message) if isinstance(message, str) else None
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
