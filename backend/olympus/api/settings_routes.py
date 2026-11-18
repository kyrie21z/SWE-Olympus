"""Per-agent model configuration; all responses are secret-free projections."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request as ProviderRequest, build_opener
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from olympus.agents.runtime.models import ProviderNotConfiguredError, RolePermissions, RoleRunInput
from olympus.api.errors import ApiError
from olympus.contracts.models import Ref, fingerprint
from olympus.settings import AGENT_ROLES, AgentSettingsStore, AgentSettingsUpdate

router = APIRouter(prefix="/api/settings/agents", tags=["agent-settings"])


class UnknownAgent(ApiError):
    code = "UNKNOWN_AGENT"
    status_code = 404


class AgentNotConfigured(ApiError):
    code = "AGENT_NOT_CONFIGURED"
    status_code = 409


class AgentConnectionFailed(ApiError):
    code = "AGENT_CONNECTION_FAILED"
    status_code = 502


class NoModelListRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the provider credential to a redirect target.
        return None


def get_settings(request: Request) -> AgentSettingsStore:
    settings = getattr(request.app.state, "agent_settings", None)
    if settings is None:
        # Use the very same store path as the run service, including injected test
        # stores and OLYMPUS_DB. Do not create a second unrelated default database.
        from olympus.api.routes import get_runs

        settings = AgentSettingsStore(get_runs(request).store.path)
        request.app.state.agent_settings = settings
    return settings


def require_role(role_id: str) -> None:
    if role_id not in AGENT_ROLES:
        raise UnknownAgent("未知的模型 Agent", detail={"role_id": role_id})


@router.get("")
def list_agent_settings(settings: AgentSettingsStore = Depends(get_settings)) -> dict:
    return {"agents": settings.public_list()}


@router.put("/{role_id}")
def update_agent_settings(
    role_id: str,
    update: AgentSettingsUpdate,
    settings: AgentSettingsStore = Depends(get_settings),
) -> dict:
    require_role(role_id)
    return settings.update(role_id, update).public()


@router.post("/{role_id}/models")
def list_provider_models(
    role_id: str, draft: AgentSettingsUpdate, settings: AgentSettingsStore = Depends(get_settings),
) -> dict:
    require_role(role_id)
    saved = settings.get(role_id)
    base_url = saved.base_url if draft.base_url is None else draft.base_url
    key = draft.api_key.get_secret_value() if draft.api_key else ""
    if not key and base_url == saved.base_url and not draft.clear_api_key:
        key = saved.api_key.get_secret_value()
    if not base_url or not key:
        raise AgentNotConfigured("请填写 API 根地址和该地址的 API Key，再获取模型列表。")

    request = ProviderRequest(f"{base_url}/models", headers={
        "Accept": "application/json", "Authorization": f"Bearer {key}",
    })
    try:
        with build_opener(NoModelListRedirect()).open(request, timeout=10) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("model list exceeds size limit")
            data = json.loads(raw)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            message = f"提供方拒绝了模型列表访问（HTTP {exc.code}），请检查密钥和权限。"
        elif exc.code in {404, 405}:
            message = "提供方未开放当前地址的模型列表，请按其文档手动填写模型 ID。"
        else:
            message = f"模型列表请求失败（HTTP {exc.code}），请核对 API 根地址或手动填写模型 ID。"
        raise AgentConnectionFailed(message) from exc
    except OSError as exc:
        raise AgentConnectionFailed("获取模型列表时连接失败或超时，请检查网络或手动填写模型 ID。") from exc
    except (ValueError, RecursionError) as exc:
        raise AgentConnectionFailed("模型列表格式无法读取，请手动填写模型 ID。") from exc

    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise AgentConnectionFailed("提供方未返回兼容的模型列表，请手动填写模型 ID。")
    models = {row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return {"models": sorted(model for model in models if model != key and 0 < len(model) <= 160
                             and model == model.strip() and all(ord(char) >= 32 for char in model))}


@router.post("/{role_id}/test")
def test_agent_connection(role_id: str, settings: AgentSettingsStore = Depends(get_settings)) -> dict:
    require_role(role_id)
    try:
        provider = settings.get(role_id).provider()
    except ProviderNotConfiguredError as exc:
        raise AgentNotConfigured("请先填写 API 地址、模型和密钥", detail={"role_id": role_id}) from exc
    request = RoleRunInput(
        role_id=role_id, task_kind="connection_test", logical_task_id=f"settings-{uuid4().hex}",
        task_id="connection-test", run_id="settings-check", input_manifest={},
        input_refs=[Ref(object_id="agent-settings", version=1, content_hash=fingerprint({"role_id": role_id}))],
        output_schema={"type": "object", "required": ["ok"], "additionalProperties": False,
                       "properties": {"ok": {"type": "boolean"}}},
        permissions=RolePermissions(
            role_id=role_id, allowed_task_kinds=["connection_test"], allowed_input_keys=[],
        ),
    )
    try:
        result = provider.invoke(
            request, {"kind": "model"}, step_index=1,
            prompt='API connection test. Reply with a JSON object exactly: {"ok": true}',
        )
    except Exception as exc:
        # Transport exceptions can contain request URLs or authorization details.
        # Never include their string or the remote response body in an HTTP error.
        raise AgentConnectionFailed("模型连接失败，请核对 API 地址、模型、密钥及网络", detail={
            "role_id": role_id, "reason": "provider_error",
        }) from exc
    if result.status != "succeeded":
        reason = "outcome_unknown" if result.status == "outcome_unknown" else "provider_rejected"
        error_code = result.error_code or ""
        if error_code in {"provider_timeout", "provider_transport_error"}:
            message = "连接失败或超时，请检查网络、API 根地址和超时设置。"
        elif error_code in {"http_401", "http_403"}:
            message = "提供方拒绝了请求，请检查 API Key 及该模型的调用权限。"
        elif error_code in {"http_400", "http_404", "http_422"}:
            message = "提供方拒绝了模型请求，请核对模型 ID 和 API 根地址。"
        else:
            message = "模型连接未成功，请查看提供方的服务状态或请求记录。"
        raise AgentConnectionFailed(message, detail={
            "role_id": role_id, "reason": reason,
        })
    if not result.output or result.output.get("ok") is not True:
        raise AgentConnectionFailed("模型已响应，但未返回要求的 JSON 对象", detail={
            "role_id": role_id, "reason": "invalid_model_output",
        })
    return {
        "role_id": role_id, "ok": True, "message": "连接成功，模型已返回有效 JSON。",
        "model": provider.model, "elapsed_ms": result.usage.get("elapsed_ms"),
    }
