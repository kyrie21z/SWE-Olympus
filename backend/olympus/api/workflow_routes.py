"""Browser workflow actions are explicit and durable; model work runs off-request."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from olympus.api.routes import get_runs
from olympus.api.settings_routes import get_settings
from olympus.contracts.models import Requirement
from olympus.settings import make_runner
from olympus.workflows import WorkflowService

router = APIRouter(prefix="/api/workflows", tags=["workflow"])


class CreateWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: Requirement
    allow_synthetic: bool = False
    call_budget: int = Field(default=80, ge=1, le=200)
    model_mode: Literal["auto", "offline", "online"] = "auto"


class WorkflowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve_requirement", "generate_design", "approve_design", "generate_plan",
                    "approve_plan", "execute", "accept_run"]
    operation_id: str = Field(min_length=1, max_length=140)


def get_workflows(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflows", None)
    if service is None:
        with request.app.state.services_lock:
            service = getattr(request.app.state, "workflows", None)
            if service is None:
                runs, settings = get_runs(request), get_settings(request)
                factory = getattr(request.app.state, "workflow_runner_factory", None)
                def has_online_config():
                    return any(row["configured"] for row in settings.public_list())

                # An injected runner is itself an explicit model configuration.
                selector = has_online_config if factory is None else None
                factory = factory or (lambda ledger: make_runner(settings, ledger=ledger))
                service = WorkflowService(
                    runs.store, runs.registry, factory, has_online_config=selector,
                    missing_online_roles=(lambda roles: [role for role in roles
                                                        if not settings.get(role).configured]) if selector else None,
                )
                request.app.state.workflows = service
    return service


@router.post("")
def create_workflow(body: CreateWorkflow, workflows=Depends(get_workflows)):
    return workflows.create(body.requirement.model_dump(mode="json"),
                            allow_synthetic=body.allow_synthetic, call_budget=body.call_budget,
                            model_mode=body.model_mode)


@router.get("/{workflow_id}")
def read_workflow(workflow_id: str, workflows=Depends(get_workflows)):
    return workflows.get(workflow_id)


@router.post("/{workflow_id}/actions")
def act_workflow(workflow_id: str, body: WorkflowAction, workflows=Depends(get_workflows)):
    return workflows.act(workflow_id, body.action, body.operation_id)
