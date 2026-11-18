"""Per-agent model configuration; all responses are secret-free projections."""

from __future__ import annotations

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
        raise AgentConnectionFailed("模型连接未成功，请核对配置或稍后重试", detail={
            "role_id": role_id, "reason": reason,
        })
    if not result.output or result.output.get("ok") is not True:
        raise AgentConnectionFailed("模型已响应，但未返回要求的 JSON 对象", detail={
            "role_id": role_id, "reason": "invalid_model_output",
        })
    return {"role_id": role_id, "ok": True, "message": "连接成功，模型已返回有效 JSON。"}
