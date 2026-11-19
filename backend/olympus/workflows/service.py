"""Persistent host for explicit human gates, model stages and all planned tasks."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from olympus.agents.argus import ArgusReviewer
from olympus.agents.athena import AthenaDesigner
from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.momus.design import DesignReviewer
from olympus.agents.momus.planning import MomusPlanReviewer, PlanningWorkflow
from olympus.agents.momus.planning.reviewer import SemanticFinding
from olympus.agents.runtime import AgentRunner, RunnerPermissionError
from olympus.agents.runtime.models import OutputSchemaError, ProviderNotConfiguredError, ProviderRequestError, RunnerBudget, UnknownOutcomeError
from olympus.agents.runtime.providers import safe_provider_error_details
from olympus.api.errors import ApiError
from olympus.contracts.models import Command, Confirmation, Design, Plan, Provenance, Ref, Requirement, Review, Run, Task, TaskRuntime, fingerprint
from olympus.execution import ArtifactAcceptanceService, LocalPythonExecutor
from olympus.execution.profile import LOCAL_EXECUTION_CAPABILITIES, execution_profile_issues
from olympus.planning.service import PlanService
from olympus.requests import RequestLedger
from olympus.requirements_bridge import RequirementBridge
from olympus.reviews.common import CommonReviewer, ReviewRecord

from .acceptance import generate_acceptance, migrate_acceptance_checkpoint, validate_executable_plan, verify_acceptance_checkpoint
from . import lab_equipment
from .document_normalization import normalize_document_authorship
from .offline import OfflineRequirementUnsupported, make_offline_runner, supports_offline

BUSY = {"design_generating", "plan_generating", "executing"}
DESIGN_REVISION_LIMIT = 3
TRANSITIONS = {
    "approve_requirement": ({"requirement_pending"}, "requirement_approved"),
    "generate_design": ({"requirement_approved", "design_review_blocked"}, "design_generating"),
    "approve_design": ({"design_pending_approval"}, "design_approved"),
    "generate_plan": ({"design_approved", "plan_review_blocked"}, "plan_generating"),
    "generate_plan_revision": ({"error", "plan_review_blocked", "plan_pending_approval"}, "plan_generating"),
    "approve_plan": ({"plan_pending_approval"}, "plan_approved"),
    "execute": ({"plan_approved", "execution_blocked"}, "executing"),
    "accept_run": ({"awaiting_acceptance"}, "completed"),
}
ONLINE_ROLES = {
    "generate_design": ("Athena", "Momus"),
    "generate_plan": ("Daedalus", "Momus"),
    "generate_plan_revision": ("Momus",),
    "execute": ("Hephaestus", "Argus"),
}


def local_execution_findings(plan, req, design):
    return [SemanticFinding(code=issue["code"], severity="blocker", task_id=issue["task_id"],
                            message=issue["message"] + " " + issue["hint"])
            for issue in execution_profile_issues(plan)]


class LocalPlanner(DaedalusPlanner):
    @staticmethod
    def _materialize(output, req, ds, mode):
        plan = DaedalusPlanner._materialize(output, req, ds, mode)
        try:
            plan = normalize_document_authorship(plan, req, ds)
            validate_executable_plan(plan)
        except OutputSchemaError as exc:
            exc.plan_candidate = plan
            raise
        return plan

    def build_input_manifest(self, req, ds):
        manifest = super().build_input_manifest(req, ds)
        manifest["execution_profile"] = {
            "capabilities": LOCAL_EXECUTION_CAPABILITIES,
            "implementation": "interface/implement tasks: executor Hephaestus, reviewer Argus",
            "checking": "test tasks are required for requirement coverage; integrate and doc tasks are optional unless approved deliverables need them. Momus or Hermes runs read-only frozen checks against ancestor artifacts; Argus reviews the resulting reports.",
            "runtime": "pure Python; math and from threading import Lock/RLock only; no third-party packages",
            "scopes": "bounded relative paths; preserve module/function names from design interfaces",
            "inputs": "artifact IDs produced by ancestors only; each task must run with these inputs alone",
            "interface": "use executable .py contracts and built-in Python annotations; validate public signatures/source schema at interface stage, and defer real business return-value tests to the implementation tasks that supply that behavior",
            "shared_files": "tests that later aggregate reports should consume a compatible final implementation version; do not merge artifacts containing conflicting revisions of the same source path",
            "granularity": "For one Python module with no independent implementation consumers, create one implement task with the full public API and one test task consuming it. Do not add a separate interface task that writes the same module. Split independently writable modules when useful.",
            "coverage": "follow coverage_obligations; test/doc delivery is not implemented by adding unrelated sources to an implementation task",
            "repair": "fix all active blockers and important findings in one complete revision; retain existing task IDs and source-backed checks",
            "deliverables": "required implementation and documentation files must be authored by bounded Hephaestus interface/implement tasks; Momus independently authors executable acceptance scripts before implementation, and read-only tasks run them against declared ancestor inputs",
        }
        manifest["prompt"] += "\n所有任务必须遵循 execution_profile，测试与集成任务只读检查前驱产物。"
        return manifest

    def revise_plan(self, plan, findings, req, ds, *, repair_context=None):
        if self._runner.provider.mode != "real_model":
            return super().revise_plan(plan, findings, req, ds)
        proposed = self._plan_via_runner(req, ds, previous=plan, findings=findings, repair_context=repair_context)
        return self.version_revision(plan, proposed)

    @staticmethod
    def version_revision(plan, proposed):
        old_by_id = {task.task_id: task for task in plan.tasks}
        tasks = []
        for new in proposed.tasks:
            old = old_by_id.get(new.task_id)
            if old and old.content_hash == new.content_hash:
                tasks.append(old)
            else:
                tasks.append(Task(**new.semantic_content(), content_hash=new.content_hash,
                                  version=old.version + 1 if old else 1))
        content = {
            "tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                      for task in tasks],
        }
        return Plan.model_validate({
            **proposed.model_dump(mode="json"), "object_id": plan.object_id,
            "version": plan.version + 1, "content": content,
            "content_hash": fingerprint(content), "tasks": tasks,
            "provenance": [*plan.provenance, *[item for item in proposed.provenance if item not in plan.provenance]],
        })


class WorkflowError(ApiError):
    status_code = 409
    code = "WORKFLOW_CONFLICT"


def provider_failure_message(error):
    """Translate only program-owned provider codes into actionable status text."""
    role = error.role_id if error.role_id in {"Athena", "Daedalus", "Momus", "Hephaestus", "Argus", "Hermes"} else "模型"
    task = {"design": "设计生成", "design_review": "设计审查", "plan": "任务分解",
            "plan_review": "计划审查", "acceptance_definition": "验收检查生成",
            "implement": "任务实现", "implementation": "任务实现", "artifact_review": "产物审查"}.get(error.task_kind, "请求")
    codes = {
        "http_400": ("PROVIDER_REQUEST_REJECTED", "请求被模型服务拒绝（HTTP 400）；请检查该 Agent 的模型名称、接口兼容性及请求参数（如结构化输出或模型专用选项）。"),
        "http_401": ("MODEL_AUTHENTICATION_FAILED", "模型服务鉴权失败（HTTP 401）；请检查 API Key 是否有效，并与 API 根地址匹配。"),
        "http_403": ("MODEL_ACCESS_DENIED", "模型服务拒绝调用（HTTP 403）；请检查 API Key 的模型访问范围、所属业务空间和目标模型授权。"),
        "http_404": ("PROVIDER_ENDPOINT_OR_MODEL_NOT_FOUND", "模型服务未找到接口或模型（HTTP 404）；请检查 API 根地址和该 Agent 的模型名称。"),
        "http_413": ("PROVIDER_REQUEST_TOO_LARGE", "模型服务拒绝过大的请求（HTTP 413）；请检查模型的上下文容量与输入限制，选择能接收当前任务的模型。"),
        "http_422": ("PROVIDER_REQUEST_REJECTED", "模型服务未接受请求参数（HTTP 422）；请检查模型、接口兼容性及结构化输出选项。"),
        "http_429": ("PROVIDER_RATE_LIMITED", "模型服务触发限流或额度限制（HTTP 429）；请检查调用额度与服务限制，等待恢复或调整该 Agent 的配置。"),
        "provider_output_incomplete": ("PROVIDER_OUTPUT_INCOMPLETE", "模型响应已结束，但输出长度不足，未返回完整结果；请检查输出长度限制，或选择能完整返回当前任务结果的模型。"),
        "invalid_provider_response": ("PROVIDER_RESPONSE_INVALID", "模型已返回，但响应不是有效或兼容的结构化结果；请检查接口及 JSON 输出兼容性，或更换能稳定输出结构化数据的模型。"),
    }
    code, message = codes.get(error.error_code, ("PROVIDER_REQUEST_FAILED", "模型服务已明确返回失败；请检查服务状态和该 Agent 的模型与接口配置。"))
    return code, f"{role} {task}：{message}已保存完成的阶段，更正后可人工重试；系统未自动重发。"


class WorkflowRunner(AgentRunner):
    """Bind older thin roles to this run and a shared, durable per-run budget."""

    def __init__(self, base, run_id, budget):
        super().__init__(base.provider, ledger=base.ledger, records=base.records)
        self.run_id, self.call_budget = run_id, budget
        self.operation_id = "initial"
        self.on_request = None
        self.on_plan_candidate = None

    def run(self, request, **kwargs):
        if self.on_request:
            self.on_request(request)
        key = "wf-" + fingerprint({
            "run": self.run_id, "operation": self.operation_id,
            "key": request.idempotency_key or request.logical_task_id,
            "inputs": [ref.model_dump(mode="json") for ref in request.input_refs],
        })
        def bind(call_key):
            return request.model_copy(update={
                "run_id": self.run_id, "idempotency_key": call_key,
                "budget": RunnerBudget(max_calls=self.call_budget,
                                       calls_used=self.ledger.total_calls_accounted(self.run_id)),
            })

        plan_request = request.role_id == "Daedalus" and request.task_kind == "plan"
        correction_count, correction_limit = 0, 1
        candidate = None
        call_kwargs = kwargs
        builder = kwargs.get("prompt_builder")
        try:
            while True:
                suffix = "" if correction_count == 0 else "-format" if correction_count == 1 else "-format2"
                try:
                    return super().run(bind(key + suffix), **call_kwargs)
                except OutputSchemaError as exc:
                    proposed = getattr(exc, "plan_candidate", None)
                    if plan_request and isinstance(proposed, Plan):
                        candidate = proposed
                        correction_limit = 2
                        if self.on_plan_candidate:
                            self.on_plan_candidate(candidate)
                    if (self.provider.mode != "real_model" or correction_count >= correction_limit or
                            self.ledger.total_calls_accounted(self.run_id) >= self.call_budget):
                        raise
                    note = exc.hint or "上一条响应未通过此角色的格式校验。按要求的 JSON 字段、类型及限制重新输出。"
                    candidate_tasks = [task.semantic_content() for task in candidate.tasks] if candidate else None
                    previous_output = (getattr(exc, "rejected_output", None)
                                       if request.role_id == "Momus" and request.task_kind == "acceptance_definition"
                                       else None)
                    previous_plan_output = (getattr(exc, "rejected_output", None)
                                            if plan_request and candidate_tasks is None else None)

                    def correction(manifest):
                        original = builder(manifest) if builder else manifest
                        if isinstance(original, dict):
                            values = {**original, "format_correction": note}
                            if isinstance(previous_output, dict):
                                values.update(
                                    previous_output=previous_output,
                                    acceptance_correction=(
                                        "Edit previous_output, the rejected acceptance result for this task. "
                                        "Fix only the violations identified in format_correction; preserve valid "
                                        "assertions, task_id, check IDs, setup and task-specific coverage. "
                                        "Return the complete corrected result. Do not rewrite unrelated checks "
                                        "or invent new requirements, private helpers or metadata imports."
                                    ),
                                )
                            if candidate_tasks is not None:
                                values.update(
                                    candidate_tasks=candidate_tasks, previous_tasks=candidate_tasks,
                                    candidate_correction=(
                                        "Continue from candidate_tasks, the latest structurally valid proposal. "
                                        "Fix all remaining issues in format_correction in this candidate; "
                                        "preserve its already corrected tasks, sources, checks and dependencies. "
                                        "Do not revert to the older approved/reviewed baseline. Return all tasks."
                                    ),
                                )
                            elif isinstance(previous_plan_output, dict):
                                values.update(
                                    previous_plan_output=previous_plan_output,
                                    plan_schema_correction=(
                                        "Edit previous_plan_output in place. Fix all exact fields listed in "
                                        "format_correction; preserve valid task IDs, task order, checks, sources "
                                        "and dependencies. Return the complete corrected proposal; do not "
                                        "regenerate unrelated tasks or discard already valid conditions."
                                    ),
                                )
                            return values
                        extra = "" if candidate_tasks is None else (
                            "\nCandidate tasks (preserve corrected parts; fix only remaining issues): "
                            + json.dumps(candidate_tasks, ensure_ascii=False)
                        )
                        if isinstance(previous_output, dict):
                            extra += ("\nEdit this previous acceptance output in place; preserve valid assertions/IDs: "
                                      + json.dumps(previous_output, ensure_ascii=False))
                        elif isinstance(previous_plan_output, dict):
                            extra += ("\nFix only the specified fields in this previous plan proposal; preserve valid IDs/checks: "
                                      + json.dumps(previous_plan_output, ensure_ascii=False))
                        return f"{original}\n\nFormat correction: {note}{extra}"

                    correction_count += 1
                    call_kwargs = {**kwargs, "prompt_builder": correction}
        except OutputSchemaError as exc:
            exc.role_id = request.role_id
            raise


class WorkflowService:
    def __init__(self, store, registry, runner_factory, *, workers=3, has_online_config=None,
                 missing_online_roles=None):
        self.store, self.registry, self.runner_factory = store, registry, runner_factory
        self.has_online_config = has_online_config
        self.missing_online_roles = missing_online_roles
        self.root = Path(store.path).resolve().parent / "workflows"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._services_lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="olympus-workflow")
        self._runners = {}
        self._executions = {}
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_documents(
                    id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workflow_operations(
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, signature TEXT NOT NULL);
            """)
            rows = db.execute("SELECT id,body FROM workflow_documents").fetchall()
            for workflow_id, body in rows:
                doc = json.loads(body)
                if doc["stage"] in BUSY or doc.get("pending_action"):
                    interrupted_action = doc.get("pending_action")
                    doc["stage"] = "error"
                    doc["error"] = {
                        "code": "INTERRUPTED_REQUIRES_RECONCILIATION",
                        "message": "服务在请求或执行期间重启；结果需核对，未自动重发。已完成的阶段和工件已保留。",
                        "action": interrupted_action,
                    }
                    doc["pending_action"] = None
                    doc["progress"] = None
                    doc["retry_action"], doc["retry_safe"] = None, False
                    db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                               (json.dumps(doc, ensure_ascii=False), workflow_id))

    def close(self):
        self._pool.shutdown(wait=True)

    def _read(self, workflow_id):
        with self.store.connection() as db:
            row = db.execute("SELECT body FROM workflow_documents WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            error = WorkflowError("工作流不存在")
            error.status_code, error.code = 404, "WORKFLOW_NOT_FOUND"
            raise error
        return json.loads(row[0])

    def _save(self, doc):
        doc["updated_at"] = datetime.now(UTC).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                       (json.dumps(doc, ensure_ascii=False), doc["workflow_id"]))

    def create(self, raw, *, allow_synthetic=False, call_budget=120, model_mode="auto"):
        req = Requirement.model_validate(raw)
        synthetic = any(p.kind == "synthetic_fixture" for p in req.provenance) or bool(
            req.confirmation and req.confirmation.origin == "synthetic_fixture"
        )
        if synthetic and not allow_synthetic:
            raise WorkflowError("此文件为构造样例，请明确允许用于测试后再创建运行")
        if not synthetic and any(p.kind not in {"human", "material"} for p in req.provenance):
            raise WorkflowError("正式需求须提供人工或材料来源，并由当前用户接纳")
        workflow_id, run_id = "WF-" + uuid4().hex, "RUN-" + uuid4().hex[:20]
        if model_mode == "offline" and not supports_offline(req):
            raise OfflineRequirementUnsupported("此需求版本没有离线样例，请配置模型 API 后使用在线模式。")
        selected_mode = self._default_model_mode(req) if model_mode == "auto" else model_mode
        doc = {
            "workflow_id": workflow_id, "run_id": run_id, "stage": "requirement_pending",
            "error": None, "requirement": req.model_dump(mode="json"),
            "synthetic": synthetic, "allow_synthetic": allow_synthetic, "model_mode": selected_mode,
            "model_preference": model_mode,
            "design": None, "design_review": None, "plan": None, "plan_review": None,
            "acceptance": {}, "acceptance_plan": None, "progress": None,
            "executions": [], "call_budget": call_budget,
            "retry_safe": False, "retry_action": None,
            "pending_action": None,
            "approvals": {}, "updated_at": datetime.now(UTC).isoformat(),
        }
        self.store.create(Run(run_id=run_id, mode="mock" if selected_mode == "offline" else "real_model",
                              approved_package=[],
                              tasks=[], call_budget=call_budget))
        with self.store.connection() as db:
            db.execute("INSERT INTO workflow_documents VALUES (?,?,?)",
                       (workflow_id, run_id, json.dumps(doc, ensure_ascii=False)))
        return self.get(workflow_id)

    def get(self, workflow_id):
        with self._lock:
            doc = self._read(workflow_id)
        doc.setdefault("model_mode", "online")
        runtime = self.registry.get(doc["run_id"])
        doc["snapshot"] = runtime.ctx.run.model_dump(mode="json")
        doc["plan_bound"] = bool(doc["approvals"].get("plan")) and runtime.scheduler is not None
        requirement = Requirement.model_validate(doc["requirement"])
        doc["offline_supported"] = supports_offline(requirement)
        doc["verified_sample_available"] = lab_equipment.matches(requirement)
        if doc["stage"] in {"design_generating", "plan_generating"}:
            runner = self._runners.get(doc["run_id"])
            entries = runner.ledger.entries() if runner else []
            pending = next((entry for entry in reversed(entries) if entry.status == "sent"), None)
            doc["pending_model_role"] = pending.role_id if pending else None
        doc["allowed_actions"] = [action for action, (stages, _) in TRANSITIONS.items()
                                  if doc["stage"] in stages and action != "generate_plan_revision"]
        if doc.get("retry_safe") and doc.get("retry_action"):
            doc["allowed_actions"].append(doc["retry_action"])
        if doc.get("pending_action"):
            doc["allowed_actions"] = []
        if doc.get("successor_workflow_id"):
            doc["allowed_actions"] = [action for action in doc["allowed_actions"] if action != "execute"]
        return doc

    def act(self, workflow_id, action, operation_id):
        with self._lock:
            doc = self._read(workflow_id)
            signature = fingerprint({"workflow": workflow_id, "action": action})
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT signature FROM workflow_operations WHERE id=?",
                                      (operation_id,)).fetchone()
                if previous:
                    if previous[0] != signature:
                        raise WorkflowError("operation_id 已用于不同操作")
                    db.commit()
                    return self.get(workflow_id)
                allowed, next_stage = TRANSITIONS[action]
                if action == "execute" and doc.get("successor_workflow_id"):
                    raise WorkflowError("此运行已建立后继修订运行；请在后继运行继续，避免重复执行和重复使用预算")
                retry = doc["stage"] == "error" and doc.get("retry_safe") and doc.get("retry_action") == action
                if doc.get("pending_action") or (doc["stage"] not in allowed and not retry):
                    raise WorkflowError(f"当前阶段 {doc['stage']} 不允许此操作")
                doc["error"] = None
                doc["retry_action"], doc["retry_safe"] = None, False
                # Reserve the operation before any external call can be scheduled.
                doc["pending_action"] = action
                doc["updated_at"] = datetime.now(UTC).isoformat()
                if action == "generate_design":
                    doc.pop("design_revision", None)
                if action.startswith("generate_") or action == "execute":
                    doc["stage"] = next_stage
                db.execute("INSERT INTO workflow_operations VALUES (?,?,?)",
                           (operation_id, workflow_id, signature))
                db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                           (json.dumps(doc, ensure_ascii=False), workflow_id))
                db.commit()
            if action.startswith("generate_") or action == "execute":
                self._pool.submit(self._perform, workflow_id, action, operation_id)
            else:
                self._perform(workflow_id, action, operation_id)
        return self.get(workflow_id)

    def _assert_plan_revision_mutable(self, doc, *, working=False):
        stages = {"error", "plan_review_blocked", "plan_pending_approval"}
        if working:
            stages.add("plan_generating")
        pending = doc.get("pending_action")
        if doc["stage"] not in stages or (pending and not (working and pending == "generate_plan_revision")):
            raise WorkflowError("当前阶段不能提交任务计划修订")
        runtime = self.registry.get(doc["run_id"])
        if (not doc.get("plan") or doc.get("approvals", {}).get("plan") or doc.get("executions") or
                runtime.scheduler is not None or runtime.ctx.run.status != "draft" or runtime.ctx.run.tasks):
            raise WorkflowError("仅允许修订尚未批准、绑定或开始执行的现有任务计划")
        if (doc.get("error", {}) or {}).get("code") in {
            "OUTCOME_UNKNOWN", "INTERRUPTED_REQUIRES_RECONCILIATION", "PERSISTENCE_REQUIRES_RECONCILIATION",
        } or any(entry.status in {"sent", "outcome_unknown"} for entry in self._ledger(doc["run_id"]).entries()):
            raise WorkflowError("此运行有结果待核对的请求，不能通过提交修订自动重发")

    @staticmethod
    def _select_acceptance_checkpoint(doc, current):
        """A rejected review does not replace the last valid frozen checkpoint."""
        pending = doc.get("pending_plan_revision") or {}
        candidates = [(doc.get("plan"), doc.get("acceptance_plan"), doc.get("acceptance", {}))]
        if "checkpoint_plan" in pending:
            candidates.append((pending.get("checkpoint_plan"), pending.get("checkpoint_binding"),
                               pending.get("checkpoint_acceptance", {})))
        else:
            candidates.append((pending.get("baseline_plan"), pending.get("baseline_acceptance_plan"),
                               pending.get("baseline_acceptance", {})))
        candidates.extend((item.get("plan"), item.get("binding"), item.get("acceptance", {}))
                          for item in reversed(doc.get("acceptance_history", [])))
        for raw_plan, binding, frozen in candidates:
            if not raw_plan or not frozen:
                continue
            plan = Plan.model_validate(raw_plan)
            if (binding != plan.ref().model_dump(mode="json") or plan.requirement != current.requirement or
                    plan.design != current.design):
                continue
            try:
                if not isinstance(frozen, dict) or set(frozen) - {task.task_id for task in plan.tasks}:
                    raise OutputSchemaError("checkpoint has invalid task IDs", hint="冻结检查点包含无效任务，不能迁移。")
                verified = verify_acceptance_checkpoint(plan, frozen)
            except OutputSchemaError as exc:
                raise WorkflowError(exc.hint or "旧冻结检查点未通过指纹或执行规则校验，不能迁移") from exc
            return {"checkpoint_plan": plan.model_dump(mode="json"), "checkpoint_binding": deepcopy(binding),
                    "checkpoint_acceptance": deepcopy(verified)}
        return {"checkpoint_plan": None, "checkpoint_binding": None, "checkpoint_acceptance": {}}

    def submit_plan_revision(self, workflow_id, tasks, expected_plan_ref, operation_id):
        expected = Ref.model_validate(expected_plan_ref)
        signature = fingerprint({"workflow": workflow_id, "action": "generate_plan_revision",
                                 "expected_plan_ref": expected.model_dump(mode="json"), "tasks": tasks})
        with self._lock:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT signature FROM workflow_operations WHERE id=?", (operation_id,)).fetchone()
                if previous:
                    if previous[0] != signature:
                        raise WorkflowError("operation_id 已用于不同操作或修订内容")
                    db.commit()
                    return self.get(workflow_id)
                row = db.execute("SELECT body FROM workflow_documents WHERE id=?", (workflow_id,)).fetchone()
                if row is None:
                    error = WorkflowError("工作流不存在")
                    error.status_code, error.code = 404, "WORKFLOW_NOT_FOUND"
                    raise error
                doc = json.loads(row[0])
                self._assert_plan_revision_mutable(doc)
                current = Plan.model_validate(doc["plan"])
                if current.ref() != expected:
                    raise WorkflowError("计划版本已变化，请基于当前计划重新提交修订")
                req, design = self._admit(doc), Design.model_validate(doc["design"])
                if current.requirement != req.ref() or current.design != design.ref():
                    raise WorkflowError("现有计划绑定的需求或设计已变化")
                if doc["approvals"].get("design", {}).get("target") != design.ref().model_dump(mode="json"):
                    raise WorkflowError("当前设计尚未批准或批准目标已变化")
                try:
                    proposed = LocalPlanner._materialize({"tasks": tasks}, req, design, "real_model")
                except OutputSchemaError as exc:
                    raise WorkflowError(exc.hint or "修订任务不符合任务结构及本地执行配置，请修正后提交") from exc
                candidate = LocalPlanner.version_revision(current, proposed)
                candidate.provenance = [*candidate.provenance, Provenance(
                    kind="human", source_id="YOU", locator=f"Direct task revision {operation_id}"[:160],
                )]
                checkpoint = self._select_acceptance_checkpoint(doc, current)
                doc["pending_plan_revision"] = {
                    "baseline": expected.model_dump(mode="json"), "plan": candidate.model_dump(mode="json"),
                    "operation_id": operation_id, "review_complete": False,
                    "baseline_plan": current.model_dump(mode="json"),
                    "baseline_acceptance": deepcopy(doc.get("acceptance", {})),
                    "baseline_acceptance_plan": deepcopy(doc.get("acceptance_plan")),
                    **checkpoint,
                }
                if checkpoint["checkpoint_plan"]:
                    doc.setdefault("acceptance_history", []).append({
                        "plan": deepcopy(checkpoint["checkpoint_plan"]), "binding": deepcopy(checkpoint["checkpoint_binding"]),
                        "acceptance": deepcopy(checkpoint["checkpoint_acceptance"]),
                        "revision_operation_id": operation_id, "kind": "preserved_checkpoint_seed",
                    })
                doc.setdefault("acceptance_history", []).append({
                    "plan": current.model_dump(mode="json"), "binding": deepcopy(doc.get("acceptance_plan")),
                    "acceptance": deepcopy(doc.get("acceptance", {})), "revision_operation_id": operation_id,
                })
                doc["pending_action"], doc["stage"] = "generate_plan_revision", "plan_generating"
                doc["error"], doc["retry_action"], doc["retry_safe"] = None, None, False
                doc["updated_at"] = datetime.now(UTC).isoformat()
                db.execute("INSERT INTO workflow_operations VALUES (?,?,?)", (operation_id, workflow_id, signature))
                db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                           (json.dumps(doc, ensure_ascii=False), workflow_id))
                db.commit()
            self._pool.submit(self._perform, workflow_id, "generate_plan_revision", operation_id)
        return self.get(workflow_id)

    def create_plan_revision_run(self, workflow_id, tasks, expected_plan_ref, operation_id):
        expected = Ref.model_validate(expected_plan_ref)
        signature = fingerprint({"workflow": workflow_id, "action": "plan_revision_run",
                                 "expected_plan_ref": expected.model_dump(mode="json"), "tasks": tasks})
        with self._lock:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT signature,workflow_id FROM workflow_operations WHERE id=?",
                                      (operation_id,)).fetchone()
                if previous:
                    if previous[0] != signature:
                        raise WorkflowError("operation_id 已用于不同操作或后继修订内容")
                    db.commit()
                    return self.get(previous[1])
                row = db.execute("SELECT body FROM workflow_documents WHERE id=?", (workflow_id,)).fetchone()
                if row is None:
                    raise WorkflowError("源工作流不存在")
                source = json.loads(row[0])
                if (source["stage"] != "execution_blocked" or source.get("pending_action") or
                        source.get("successor_workflow_id")):
                    raise WorkflowError("仅可从无在途操作且未建立后继的执行阻断运行创建修订运行")
                old = Plan.model_validate(source["plan"])
                if old.ref() != expected:
                    raise WorkflowError("源计划版本已变化，请基于当前计划创建后继修订")
                runtime = self.registry.get(source["run_id"])
                if (runtime.scheduler is None or runtime.scheduler.plan.ref() != old.ref() or
                        not any(task.state in {"failed", "blocked"} for task in runtime.ctx.run.tasks) or
                        any(task.state in {"running", "verifying"} for task in runtime.ctx.run.tasks)):
                    raise WorkflowError("源运行必须已有失败或阻断任务，且无运行中或待复核任务")
                ledgers = [self._ledger(source["run_id"])]
                execution_root = self.root / source["run_id"] / "execution"
                if (execution_root / "requests.json").exists():
                    ledgers.append(RequestLedger(execution_root / "requests.json"))
                if any(entry.status in {"prepared", "sent", "outcome_unknown"} for ledger in ledgers for entry in ledger.entries()):
                    raise WorkflowError("源运行存在在途或未知结果请求，必须先核对后再创建修订运行")
                execution = self._executions.get(source["run_id"])
                if execution and any(not job.done.is_set() for job in execution.executor._jobs.values()):
                    raise WorkflowError("源运行仍有活动本地任务，不能建立后继运行")
                try:
                    for receipt_file in execution_root.glob("LOCAL-*/receipt.json"):
                        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                        if receipt.get("state") not in {"succeeded", "failed", "cancelled"}:
                            raise WorkflowError("源运行仍有未确认结束的本地执行回执")
                except (OSError, ValueError) as exc:
                    raise WorkflowError("源运行执行回执不完整，必须先核对") from exc
                spent = ledgers[0].total_calls_accounted(source["run_id"])
                remaining = source["call_budget"] - spent
                if remaining <= 0:
                    raise WorkflowError("源运行剩余模型预算不足，不能建立后继修订运行")
                req, design = self._admit(source), Design.model_validate(source["design"])
                if (old.requirement != req.ref() or old.design != design.ref() or
                        source["approvals"].get("design", {}).get("target") != design.ref().model_dump(mode="json") or
                        not all(ref in runtime.ctx.run.approved_package for ref in [req.ref(), design.ref(), old.ref()])):
                    raise WorkflowError("源运行的已确认需求、已批准设计或计划绑定不一致")
                try:
                    proposed = LocalPlanner._materialize({"tasks": tasks}, req, design, "real_model")
                except OutputSchemaError as exc:
                    raise WorkflowError(exc.hint or "后继任务不符合任务结构和本地执行配置") from exc
                plan = LocalPlanner.version_revision(old, proposed)
                plan.provenance = [*plan.provenance, Provenance(kind="human", source_id="YOU",
                                                           locator=f"Successor task revision {operation_id}"[:160])]
                child_id, run_id = "WF-" + uuid4().hex, "RUN-" + uuid4().hex[:20]
                timestamp = datetime.now(UTC).isoformat()
                child = {
                    "workflow_id": child_id, "run_id": run_id, "stage": "plan_generating", "error": None,
                    "requirement": deepcopy(source["requirement"]), "design": deepcopy(source["design"]),
                    "design_review": deepcopy(source.get("design_review")),
                    "synthetic": source["synthetic"], "allow_synthetic": source["allow_synthetic"],
                    "model_mode": source.get("model_mode", "online"), "model_preference": source.get("model_preference", "auto"),
                    "plan": old.model_dump(mode="json"), "plan_review": None, "plan_review_record": None,
                    "acceptance": {}, "acceptance_plan": None, "progress": None, "executions": [],
                    "call_budget": remaining, "retry_safe": False, "retry_action": None,
                    "pending_action": "generate_plan_revision", "updated_at": timestamp,
                    "approvals": {key: deepcopy(source["approvals"][key]) for key in ("requirement", "design")},
                    "source_workflow_id": workflow_id, "source_run_id": source["run_id"],
                    "inherited_approval_source": {"workflow_id": workflow_id, "run_id": source["run_id"],
                                                  "stage": "design_approved"},
                    "revision_budget": {"source_run_id": source["run_id"], "source_call_budget": source["call_budget"],
                                        "source_calls_used": spent, "inherited_remaining": remaining,
                                        "calls_consumed_before_run": source.get("revision_budget", {}).get("calls_consumed_before_run", 0) + spent,
                                        "root_call_budget": source.get("revision_budget", {}).get("root_call_budget", source["call_budget"])},
                    "pending_plan_revision": {"baseline": expected.model_dump(mode="json"),
                                              "baseline_plan": old.model_dump(mode="json"),
                                              "baseline_acceptance": deepcopy(source["acceptance"]),
                                              "baseline_acceptance_plan": deepcopy(source.get("acceptance_plan")),
                                              "checkpoint_plan": old.model_dump(mode="json"),
                                              "checkpoint_binding": deepcopy(source.get("acceptance_plan")),
                                              "checkpoint_acceptance": deepcopy(source["acceptance"]),
                                              "plan": plan.model_dump(mode="json"), "operation_id": operation_id,
                                              "review_complete": False},
                }
                run = Run(run_id=run_id, mode="mock" if child["model_mode"] == "offline" else "real_model",
                          approved_package=[req.ref(), design.ref()], tasks=[], call_budget=remaining)
                source["successor_workflow_id"], source["successor_run_id"] = child_id, run_id
                source["successor_operation_id"], source["updated_at"] = operation_id, timestamp
                db.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, 1, run.model_dump_json()))
                db.execute("INSERT INTO workflow_documents VALUES (?,?,?)", (child_id, run_id, json.dumps(child, ensure_ascii=False)))
                db.execute("UPDATE workflow_documents SET body=? WHERE id=?", (json.dumps(source, ensure_ascii=False), workflow_id))
                db.execute("INSERT INTO workflow_operations VALUES (?,?,?)", (operation_id, child_id, signature))
                db.commit()
            self._pool.submit(self._perform, child_id, "generate_plan_revision", operation_id)
        return self.get(child_id)

    def _perform(self, workflow_id, action, operation_id):
        doc = self._read(workflow_id)
        try:
            if action == "generate_design":
                self._select_design_mode(doc)
            if action in ONLINE_ROLES:
                if doc.get("model_mode", "online") == "online" and self.missing_online_roles:
                    roles = ONLINE_ROLES[action]
                    if action in {"generate_design", "generate_plan"}:
                        req = Requirement.model_validate(doc["requirement"])
                        current = doc.get("design")
                        if lab_equipment.matches(req) and (current is None or
                                Design.model_validate(current).ref() == lab_equipment.design_for(req).ref()):
                            roles = ("Momus",)
                    missing = self.missing_online_roles(roles)
                    if missing:
                        raise ProviderNotConfiguredError(f"请先完成 {'、'.join(missing)} 的 API 设置并测试连接。")
                runner = self._runner(doc)
                runner.operation_id = operation_id
                runner.on_request = lambda request: self._request_progress(doc, request)
                runner.on_plan_candidate = lambda candidate: self._record_plan_candidate(doc, candidate)
            getattr(self, "_" + action)(doc, operation_id)
            if not action.startswith("generate_") and action != "execute":
                doc["stage"] = TRANSITIONS[action][1]
        except Exception as exc:
            # Provider responses and credentials are deliberately not reflected to the browser.
            if isinstance(exc, UnknownOutcomeError):
                code = "OUTCOME_UNKNOWN"
                role = exc.role_id if exc.role_id in {"Athena", "Daedalus", "Momus", "Hephaestus", "Argus", "Hermes"} else "模型"
                task_label = {
                    "design": "设计生成", "design_review": "设计审查", "plan": "任务分解",
                    "plan_review": "计划审查", "acceptance_definition": "验收检查生成",
                    "implementation": "任务实现", "artifact_review": "产物审查",
                }.get(exc.task_kind, "请求")
                reason = {"provider_timeout": "响应超时", "provider_transport_error": "连接中断",
                          "provider_stream_interrupted": "响应流中断", "provider_stream_error": "响应流异常",
                          "timeout": "响应超时", "transport_timeout": "响应超时",
                          "connection_error": "连接中断", "transport_error": "连接中断",
                          "incomplete_stream": "响应流中断"}.get(exc.error_code, "请求未完整返回")
                message = f"{role} {task_label}{reason}，结果需核对；已保存完成的阶段和验收检查，系统未自动重发。"
            elif isinstance(exc, OutputSchemaError):
                code = "OUTPUT_SCHEMA_ERROR"
                role = f"{exc.role_id} 返回的" if exc.role_id else "当前阶段的"
                message = f"{role}结果不符合工作流要求。{exc.hint or '请重试此阶段，或更换能稳定输出结构化数据的模型。'}"
            elif isinstance(exc, (WorkflowError, OfflineRequirementUnsupported)):
                code, message = exc.code, exc.message
            elif isinstance(exc, ProviderNotConfiguredError):
                code, message = "AGENT_NOT_CONFIGURED", str(exc)
            elif isinstance(exc, RunnerPermissionError) and str(exc) == "call budget exhausted":
                code = "MODEL_BUDGET_EXHAUSTED"
                message = "本次运行的模型调用预算已用完。已保留当前设计和审查报告，请查看剩余意见。"
            elif isinstance(exc, ProviderRequestError):
                code, message = provider_failure_message(exc)
            else:
                code = type(exc).__name__.upper()
                message = "此阶段未完成，请检查 Agent 配置、模型输出格式及执行证据。"
            doc["stage"] = "error"
            doc["error"] = {"code": code, "message": message, "action": action}
            if isinstance(exc, UnknownOutcomeError):
                doc["error"].update(role_id=exc.role_id, task_kind=exc.task_kind,
                                    transport_code=exc.error_code)
            elif isinstance(exc, ProviderRequestError):
                doc["error"].update(role_id=exc.role_id, task_kind=exc.task_kind,
                                    provider_code=exc.error_code, request_id=exc.request_id)
                details = safe_provider_error_details(exc.error_details)
                if details:
                    doc["error"]["provider_details"] = details
            runner = self._runners.get(doc["run_id"])
            unknown = runner and any(entry.status in {"sent", "outcome_unknown"}
                                     for entry in runner.ledger.entries())
            safe_error = isinstance(exc, (ProviderNotConfiguredError, ProviderRequestError,
                                         OutputSchemaError, OfflineRequirementUnsupported))
            if not unknown and safe_error and (action.startswith("generate_") or
                                               (action == "execute" and isinstance(exc, ProviderNotConfiguredError))):
                doc["retry_action"], doc["retry_safe"] = action, True
        finally:
            try:
                runtime = self.registry.get(doc["run_id"])
                runner = self._runners.get(doc["run_id"])
                with runtime.lock, runtime.scheduler._lock if runtime.scheduler else nullcontext():
                    calls = runner.ledger.total_calls_accounted(doc["run_id"]) if runner else 0
                    if calls > runtime.ctx.run.calls_used:
                        runtime.ctx.commit(reason="记录实际模型调用数量", actor="Zeus", actor_type="program",
                                           mutate=lambda run: setattr(run, "calls_used", calls))
                    runtime.persist()
            except Exception:
                self.registry.forget(doc["run_id"])
                self._executions.pop(doc["run_id"], None)
                doc["stage"] = "error"
                doc["error"] = {"code": "PERSISTENCE_REQUIRES_RECONCILIATION",
                                "message": "此阶段未能确认持久化，已停止推进；请核对数据库和运行状态。"}
                doc["retry_action"], doc["retry_safe"] = None, False
            doc["pending_action"] = None
            doc["progress"] = None
            runner = self._runners.get(doc["run_id"])
            if runner:
                runner.on_request = None
                runner.on_plan_candidate = None
            with self._lock:
                self._save(doc)

    def _record_plan_candidate(self, doc, candidate):
        if (candidate.requirement != Requirement.model_validate(doc["requirement"]).ref()
                or not doc.get("design") or candidate.design != Design.model_validate(doc["design"]).ref()):
            raise WorkflowError("修订草稿绑定的需求或设计不匹配")
        doc["plan_repair_candidate"] = {
            "status": "unreviewed", "plan": candidate.model_dump(mode="json"),
            "baseline": Plan.model_validate(doc["plan"]).ref().model_dump(mode="json") if doc.get("plan") else None,
        }
        with self._lock:
            self._save(doc)

    def _request_progress(self, doc, request):
        labels = {"design": "生成设计规格", "design_review": "审查设计规格", "plan": "分解任务",
                  "plan_review": "审查任务计划", "acceptance_definition": "生成验收检查",
                  "artifact_review": "审查执行产物"}
        previous = doc.get("progress") or {}
        doc["progress"] = {
            "role_id": request.role_id, "task_kind": request.task_kind, "task_id": request.task_id,
            "message": f"{request.role_id} 正在{labels.get(request.task_kind, '处理任务')}",
        }
        if request.task_kind == "acceptance_definition":
            doc["progress"].update({key: previous[key] for key in ("completed", "total") if key in previous})
        with self._lock:
            self._save(doc)

    def _runner(self, doc):
        with self._services_lock:
            return self._make_runner(doc)

    def _default_model_mode(self, requirement):
        if self.has_online_config is None:
            return "online"
        return "offline" if supports_offline(requirement) or not self.has_online_config() else "online"

    def _ledger(self, run_id):
        return RequestLedger(self.root / run_id / "model-requests.json")

    def _select_design_mode(self, doc):
        """Allow old no-config failures to retry offline before any model call was sent."""
        run_id = doc["run_id"]
        if doc.get("model_preference", "auto") != "auto" or doc.get("design") is not None or self._ledger(run_id).entries():
            return
        desired = self._default_model_mode(Requirement.model_validate(doc["requirement"]))
        runtime = self.registry.get(run_id)
        with runtime.lock:
            mode = "mock" if desired == "offline" else "real_model"
            if runtime.ctx.run.mode != mode:
                runtime.ctx.commit(reason=f"选择模型运行模式 {desired}", actor="Zeus", actor_type="program",
                                   mutate=lambda run: setattr(run, "mode", mode))
                runtime.persist()
        with self._services_lock:
            if doc.get("model_mode", "online") != desired:
                self._runners.pop(run_id, None)
            doc["model_mode"] = desired

    def _make_runner(self, doc):
        run_id = doc["run_id"]
        if run_id not in self._runners:
            ledger = self._ledger(run_id)
            if doc.get("model_mode") == "offline":
                base = make_offline_runner(Requirement.model_validate(doc["requirement"]), ledger=ledger)
            else:
                base = self.runner_factory(ledger)
            self._runners[run_id] = WorkflowRunner(base, run_id, doc["call_budget"])
        return self._runners[run_id]

    def _admit(self, doc):
        req = Requirement.model_validate(doc["requirement"])
        bridge = RequirementBridge(
            allow_synthetic=doc["allow_synthetic"], authorized_actors={"YOU"},
            trusted_sources={p.source_id for p in req.provenance},
            trusted_confirmations={req.confirmation.confirmation_id: req.confirmation}
            if req.confirmation else {},
        )
        bridge.admit(req)
        return req

    def _approve_requirement(self, doc, operation_id):
        req = Requirement.model_validate(doc["requirement"])
        req.confirmation = Confirmation(
            confirmation_id=operation_id, actor="YOU",
            origin="synthetic_fixture" if doc["synthetic"] else "human",
            target=req.ref(), dependencies=[], confirmed_at=datetime.now(UTC),
        )
        doc["requirement"] = req.model_dump(mode="json")
        self._admit(doc)
        runtime = self.registry.get(doc["run_id"])
        with runtime.lock:
            runtime.ctx.commit(reason="用户接纳当前需求用于此运行", actor="YOU", actor_type="human",
                               mutate=lambda run: setattr(run, "approved_package", [req.ref()]))
            runtime.persist()
        doc["approvals"]["requirement"] = req.confirmation.model_dump(mode="json")

    def _generate_design(self, doc, operation_id):
        del operation_id
        req, runner = self._admit(doc), self._runner(doc)
        designer, reviewer = AthenaDesigner(runner), DesignReviewer(runner)
        design = Design.model_validate(doc["design"]) if doc.get("design") else None
        if design and design.requirement != req.ref():
            raise WorkflowError("设计草稿所引用的需求已变化")
        template = lab_equipment.design_for(req) if doc["model_mode"] == "online" and lab_equipment.matches(req) else None
        use_template = template is not None and (design is None or design.ref() == template.ref())
        report = ReviewRecord.model_validate(doc["design_review"]) if design and doc.get("design_review") else None
        limit = DESIGN_REVISION_LIMIT if report else max(0, 2 - (design.version if design else 1))
        if use_template:
            limit = 0
            if report and report.review.verdict != "passed":
                report = None
        if report is None:
            design = design or (template if use_template else designer.generate(req, run_id=doc["run_id"]))
            doc["design"] = design.model_dump(mode="json")
            report = reviewer.review(req, design, run_id=doc["run_id"])
        phase_started = doc["updated_at"]
        revision = 0
        while True:
            self._record_design_review(doc, report)
            with self._lock:
                self._save(doc)
            report = reviewer.recheck(req, design, report, run_id=doc["run_id"])
            self._record_design_review(doc, report)
            if report.review.verdict == "passed":
                doc["stage"] = "design_pending_approval"
                return
            if revision >= limit:
                doc["stage"] = "design_review_blocked"
                return
            blockers = [f for f in report.submission.findings if f.finding_id in report.blocking_finding_ids]
            revision += 1
            doc["design_revision"] = {"round": revision, "limit": limit, "started_at": phase_started,
                                      "finding_count": len(blockers)}
            with self._lock:
                self._save(doc)
            candidate = designer.generate(req, run_id=doc["run_id"], previous=design, findings=blockers)
            report = reviewer.review(req, candidate, run_id=doc["run_id"], previous_review=report)
            design = candidate
            doc["design"] = design.model_dump(mode="json")

    @staticmethod
    def _record_design_review(doc, report):
        record = report.model_dump(mode="json")
        doc["design_review"] = record
        history = doc.setdefault("design_review_history", [])
        if not any(row["review"]["review_id"] == report.review.review_id for row in history):
            history.append(record)

    def _approve_design(self, doc, operation_id):
        req, design = self._admit(doc), Design.model_validate(doc["design"])
        report = ReviewRecord.model_validate(doc["design_review"])
        common = CommonReviewer()
        common.validate_and_record(report.submission, current_target=design.ref(),
                                   expected_author="Athena", current_inputs=[req.ref()])
        if report.review.verdict != "passed" or report.review.target != design.ref():
            raise WorkflowError("独立设计审查未通过")
        if design.requirement != req.ref():
            raise WorkflowError("设计绑定的需求版本已失效")
        runtime = self.registry.get(doc["run_id"])
        with runtime.lock:
            runtime.ctx.commit(reason="用户批准当前设计规格", actor="YOU", actor_type="human",
                               mutate=lambda run: setattr(run, "approved_package", [req.ref(), design.ref()]))
            runtime.persist()
        doc["approvals"]["design"] = {"operation_id": operation_id,
            "target": design.ref().model_dump(mode="json"), "at": datetime.now(UTC).isoformat()}

    def _generate_plan(self, doc, operation_id):
        del operation_id
        req, design = self._admit(doc), Design.model_validate(doc["design"])
        if doc["approvals"]["design"]["target"] != design.ref().model_dump(mode="json"):
            raise WorkflowError("设计批准目标不匹配")
        runner = self._runner(doc)
        saved = Plan.model_validate(doc["plan"]) if doc.get("plan") else None
        review = Review.model_validate(doc["plan_review"]) if doc.get("plan_review") else None
        was_passed = bool(saved and review and review.target == saved.ref()
                          and review.reviewer == "Momus" and review.verdict == "passed")
        planner = LocalPlanner(runner)
        reviewer = MomusPlanReviewer(runner=runner, program_checks=local_execution_findings,
                                     program_check_id="program:local-execution-profile",
                                     review_context=LOCAL_EXECUTION_CAPABILITIES)
        workflow = PlanningWorkflow(planner, reviewer)

        def remember_review(plan, report):
            history = doc.setdefault("plan_review_history", [])
            for record in (doc.get("plan_review_record"), report.model_dump(mode="json")):
                if record and not any(row["review"]["review_id"] == record["review"]["review_id"] for row in history):
                    history.append(record)
            doc["plan"] = plan.model_dump(mode="json")
            doc["plan_review"] = report.review.model_dump(mode="json")
            doc["plan_review_record"] = report.model_dump(mode="json")
            with self._lock:
                self._save(doc)

        previous = ReviewRecord.model_validate(doc["plan_review_record"]) if doc.get("plan_review_record") else None
        if (doc["model_mode"] == "online" and lab_equipment.matches(req)
                and design.ref() == lab_equipment.design_for(req).ref()):
            template = planner._materialize({"tasks": lab_equipment.read_json("tasks")}, req, design, "real_model")
            template = template.model_copy(update={"provenance": [lab_equipment.provenance("tasks")]})
            if saved is None or saved.ref() == template.ref():
                doc["sample_plan_ref"] = template.ref().model_dump(mode="json")
                if not was_passed or previous is None:
                    report = reviewer.review(template, req, design)
                    remember_review(template, report.record)
                    if report.review.verdict != "passed":
                        doc["stage"] = "plan_review_blocked"
                        return
                PlanService().prepare(req, design, template)
                self._generate_acceptance(doc, runner, req, design, template)
                doc["stage"] = "plan_pending_approval"
                return
        if saved and previous:
            previous = CommonReviewer().validate_and_record(previous.submission, current_target=saved.ref(),
                                                             expected_author="Daedalus",
                                                             current_inputs=[req.ref(), design.ref()])
            if previous.review != review or saved.requirement != req.ref() or saved.design != design.ref():
                raise WorkflowError("计划修订所引用的审查、需求或设计版本已变化")
            # Rule corrections can invalidate old program blockers without changing
            # the plan. Keep independent semantic evidence on the exact same ref.
            refreshed = reviewer.revalidate_program_checks(saved, req, design, previous)
            previous = refreshed.record
            remember_review(saved, previous)
            active = [finding for finding in previous.submission.findings if finding.status == "open"]
            if refreshed.review.verdict == "passed" and (was_passed or not active):
                validate_executable_plan(saved)
                PlanService().prepare(req, design, saved)
                self._generate_acceptance(doc, runner, req, design, saved)
                doc["stage"] = "plan_pending_approval"
                return
            context = {
                "active_findings": [finding.model_dump(mode="json") for finding in active],
                "resolved_findings": [finding.model_dump(mode="json") for finding in previous.submission.findings
                                      if finding.status == "closed"],
            }
            draft = doc.get("plan_repair_candidate") or {}
            if draft.get("baseline") == saved.ref().model_dump(mode="json") and draft.get("plan"):
                candidate_seed = Plan.model_validate(draft["plan"])
                if candidate_seed.requirement == req.ref() and candidate_seed.design == design.ref():
                    context["candidate_tasks"] = [task.semantic_content() for task in candidate_seed.tasks]
                    context["candidate_profile_issues"] = execution_profile_issues(candidate_seed)
            doc["plan_revision"] = {"baseline": saved.ref().model_dump(mode="json"),
                                    "finding_count": len(active),
                                    "started_at": datetime.now(UTC).isoformat()}
            with self._lock:
                self._save(doc)
            candidate = planner.revise_plan(saved, [finding.message for finding in active], req, design,
                                            repair_context=context)
            if candidate.content_hash == saved.content_hash:
                doc["stage"] = "plan_review_blocked"
                doc["error"] = {"code": "PLAN_REPAIR_NO_PROGRESS", "action": "generate_plan",
                                "message": "此次修改没有改变计划内容，已保留原计划和待处理清单；请按完整清单修改后再复核。"}
                return
            outcome = workflow.run(req, design, max_revisions=0, initial_plan=candidate,
                                   previous_review=previous, on_review=remember_review)
        else:
            outcome = workflow.run(req, design, initial_plan=saved, on_review=remember_review)
        if outcome.status != "passed":
            doc["stage"] = "plan_review_blocked"
            return
        validate_executable_plan(outcome.plan)
        PlanService().prepare(req, design, outcome.plan)
        self._generate_acceptance(doc, runner, req, design, outcome.plan)
        doc["stage"] = "plan_pending_approval"

    def _generate_plan_revision(self, doc, operation_id):
        del operation_id
        self._assert_plan_revision_mutable(doc, working=True)
        proposal = doc.get("pending_plan_revision")
        if not proposal:
            raise WorkflowError("请先提交包含具体任务内容的计划修订")
        plan = Plan.model_validate(proposal["plan"])
        baseline = Ref.model_validate(proposal["baseline"])
        current = Plan.model_validate(doc["plan"])
        if current.ref() not in (baseline, plan.ref()):
            raise WorkflowError("待复核修订所基于的计划版本已变化")
        req, design = self._admit(doc), Design.model_validate(doc["design"])
        if (plan.requirement != req.ref() or plan.design != design.ref() or
                doc["approvals"].get("design", {}).get("target") != design.ref().model_dump(mode="json")):
            raise WorkflowError("待复核修订的需求或已批准设计版本已变化")
        validate_executable_plan(plan)
        runner = self._runner(doc)
        if not proposal.get("review_complete"):
            history = doc.setdefault("plan_review_history", [])
            previous = doc.get("plan_review_record")
            if previous and not any(row["review"]["review_id"] == previous["review"]["review_id"] for row in history):
                history.append(previous)
            doc["plan"], doc["plan_review"], doc["plan_review_record"] = plan.model_dump(mode="json"), None, None
            with self._lock:
                self._save(doc)
            reviewer = MomusPlanReviewer(runner=runner, program_checks=local_execution_findings,
                                         program_check_id="program:local-execution-profile",
                                         review_context=LOCAL_EXECUTION_CAPABILITIES)
            report = reviewer.review(plan, req, design, previous_review=None)
            record = report.record.model_dump(mode="json")
            doc["plan_review"], doc["plan_review_record"] = report.review.model_dump(mode="json"), record
            if not any(row["review"]["review_id"] == report.review.review_id for row in history):
                history.append(record)
            proposal["review_complete"] = True
            with self._lock:
                self._save(doc)
        record = ReviewRecord.model_validate(doc["plan_review_record"])
        checked = CommonReviewer().validate_and_record(record.submission, current_target=plan.ref(),
                                                       expected_author="Daedalus", current_inputs=[req.ref(), design.ref()])
        if checked.review != Review.model_validate(doc["plan_review"]):
            raise WorkflowError("修订计划的独立审查记录不一致")
        if checked.review.verdict != "passed":
            doc["stage"] = "plan_review_blocked"
            return
        PlanService().prepare(req, design, plan)
        self._generate_acceptance(doc, runner, req, design, plan)
        doc["stage"] = "plan_pending_approval"

    def _generate_acceptance(self, doc, runner, req, design, plan):
        binding = plan.ref().model_dump(mode="json")
        if doc.get("acceptance_plan") != binding:
            reused = {}
            proposal = doc.get("pending_plan_revision") or {}
            modern = "checkpoint_plan" in proposal
            checkpoint_plan = proposal.get("checkpoint_plan") if modern else proposal.get("baseline_plan")
            checkpoint_binding = proposal.get("checkpoint_binding") if modern else proposal.get("baseline_acceptance_plan")
            checkpoint_acceptance = proposal.get("checkpoint_acceptance", {}) if modern else proposal.get("baseline_acceptance", {})
            if (proposal.get("plan") and checkpoint_plan and
                    Plan.model_validate(proposal["plan"]).ref() == plan.ref()):
                runtime = self.registry.get(doc["run_id"])
                if (doc.get("approvals", {}).get("plan") or doc.get("executions") or runtime.scheduler is not None
                        or runtime.ctx.run.status != "draft" or runtime.ctx.run.tasks):
                    raise WorkflowError("批准、绑定或已执行的计划不能迁移冻结检查")
                reused = migrate_acceptance_checkpoint(
                    Plan.model_validate(checkpoint_plan), plan, checkpoint_acceptance, checkpoint_binding,
                )
                doc["acceptance_migration"] = {
                    "from_plan": Plan.model_validate(checkpoint_plan).ref().model_dump(mode="json"),
                    "revision_baseline": proposal["baseline"], "to_plan": binding, "reused_tasks": list(reused),
                }
            doc["acceptance"], doc["acceptance_plan"] = reused, binding
            if doc.get("sample_plan_ref") == binding:
                doc["acceptance"] = lab_equipment.frozen_for(plan)
                doc["acceptance_source"] = "verified_sample"
            else:
                doc.pop("acceptance_source", None)
        with self._lock:
            self._save(doc)

        def checkpoint(task_id, frozen):
            doc["acceptance"][task_id] = frozen
            if doc.get("progress"):
                doc["progress"]["completed"] = len(doc["acceptance"])
            with self._lock:
                self._save(doc)

        def progress(task_id, completed, total):
            doc["progress"] = {"role_id": "Momus", "task_kind": "acceptance_definition",
                               "task_id": task_id, "completed": completed, "total": total,
                               "message": "Momus 正在逐项生成验收检查"}
            with self._lock:
                self._save(doc)

        doc["acceptance"] = generate_acceptance(
            runner, req, design, plan, completed=doc["acceptance"], checkpoint=checkpoint, progress=progress,
        )

    def _approve_plan(self, doc, operation_id):
        req, design, plan = self._admit(doc), Design.model_validate(doc["design"]), Plan.model_validate(doc["plan"])
        review = Review.model_validate(doc["plan_review"])
        if review.verdict != "passed" or review.target != plan.ref() or review.reviewer != "Momus":
            raise WorkflowError("独立计划审查未通过")
        if doc.get("plan_review_record"):
            record = ReviewRecord.model_validate(doc["plan_review_record"])
            current = CommonReviewer().validate_and_record(record.submission, current_target=plan.ref(),
                                                            expected_author="Daedalus",
                                                            current_inputs=[req.ref(), design.ref()])
            if current.review != review:
                raise WorkflowError("计划审查结果与当前版本不一致")
        package = PlanService().prepare(req, design, plan)
        for task in plan.tasks:
            FrozenAcceptance.model_validate(doc["acceptance"][task.task_id]).verify(task)
        runtime = self.registry.get(doc["run_id"])
        with runtime.lock:
            def bind(run):
                run.approved_package = [req.ref(), design.ref(), plan.ref()]
                run.tasks = [TaskRuntime(task_id=task.task_id) for task in plan.tasks]
                if doc.get("model_mode") == "offline":
                    run.mode = "real_tool"
                run.calls_used = self._runner(doc).ledger.total_calls_accounted(doc["run_id"])
            runtime.ctx.commit(reason="用户批准计划及冻结验收定义", actor="YOU", actor_type="human", mutate=bind)
            runtime.attach_plan(plan, package.manifest, requirement=req, design=design)
            runtime.persist()
        doc["approvals"]["plan"] = {"operation_id": operation_id,
            "target": plan.ref().model_dump(mode="json"), "at": datetime.now(UTC).isoformat()}

    def _execution(self, doc):
        with self._services_lock:
            return self._make_execution(doc)

    def _make_execution(self, doc):
        run_id = doc["run_id"]
        if run_id not in self._executions:
            runtime = self.registry.get(run_id)
            if runtime.scheduler is None:
                raise WorkflowError("计划尚未批准或绑定")
            runner = self._runner(doc)
            references = (lab_equipment.reference_sources(runtime.scheduler.plan)
                          if doc.get("sample_plan_ref") == runtime.scheduler.plan.ref().model_dump(mode="json") else None)
            executor = LocalPythonExecutor(runtime.scheduler, self.root / run_id / "execution",
                                           HephaestusImplementer(runner, reference_sources=references))
            for task in runtime.scheduler.plan.tasks:
                executor.register_task(task.task_id, FrozenAcceptance.model_validate(doc["acceptance"][task.task_id]),
                    context={runtime.requirement.object_id: runtime.requirement.content,
                             runtime.design.object_id: runtime.design.content})
            self._executions[run_id] = ArtifactAcceptanceService(executor, ArgusReviewer(runner))
        return self._executions[run_id]

    def _execute(self, doc, operation_id):
        runtime = self.registry.get(doc["run_id"])
        service = self._execution(doc)
        with runtime.lock:
            if runtime.ctx.run.status == "draft":
                plan = runtime.scheduler.plan
                receipt = runtime.bus.dispatch(Command(
                    operation_id="start-" + operation_id, run_id=doc["run_id"], actor="YOU",
                    action="start", target_id=doc["run_id"], expected_version=plan.version,
                    expected_state_revision=runtime.ctx.run.state_revision, parameters={},
                ))
                if receipt.status != "accepted":
                    raise WorkflowError("启动运行被拒绝：" + receipt.reason_code)
                runtime.persist()
        scheduler, executor = runtime.scheduler, service.executor
        verified_sample = doc.get("sample_plan_ref") == scheduler.plan.ref().model_dump(mode="json")
        while True:
            with runtime.lock, scheduler._lock:
                if runtime.ctx.run.status != "active":
                    doc["stage"] = "execution_blocked"
                    doc["error"] = {"code": "RUN_NOT_ACTIVE", "message": "运行已暂停，恢复后可继续执行。"}
                    return
                unfinished = [task for task in scheduler.plan.tasks
                              if runtime.ctx.state_of(task.task_id) != "accepted"]
                if not unfinished:
                    doc["stage"] = "awaiting_acceptance"
                    return
                scheduler.reconcile()
                verifying = next((task.task_id for task in unfinished
                                  if runtime.ctx.state_of(task.task_id) == "verifying"
                                  and scheduler.attempt_for(task.task_id)), None)
                if verifying:
                    task_id = verifying
                    attempt = scheduler.attempt_for(task_id)
                else:
                    ready = scheduler.ready()
                    if not ready:
                        runtime.persist()
                        doc["stage"] = "execution_blocked"
                        failed = next((task.task_id for task in unfinished
                                       if runtime.ctx.state_of(task.task_id) == "failed"), None)
                        failed_attempt = scheduler.attempt_for(failed) if failed else None
                        if failed_attempt:
                            failure = executor.query_receipt(failed_attempt)
                            doc["error"] = {"code": "EXECUTION_NOT_ACCEPTED", "task_id": failed,
                                            "message": (failure.error or "任务未通过验收，请查看该任务的执行证据。")[:500]}
                        else:
                            doc["error"] = {"code": "TASKS_BLOCKED", "message": "有任务等待核对，请查看执行证据及运行控制台。"}
                        return
                    task_id = ready[0]
                    if scheduler.dispatch(task_id, executor) is None:
                        raise WorkflowError("任务未取得执行许可")
                    attempt = scheduler.attempt_for(task_id)
                    runtime.persist()
            receipt = executor.wait(attempt, timeout=605)
            status, reason = service.collect(task_id, receipt.attempt_id, retry_review=bool(verifying))
            with runtime.lock, scheduler._lock:
                runtime.persist()
                projected = service.project(task_id, receipt.attempt_id)
                doc["executions"] = [item for item in doc["executions"] if item["task_id"] != task_id] + [projected]
                with self._lock:
                    self._save(doc)
                if status != "accepted":
                    if (verified_sample and scheduler.tasks[task_id].type == "implement"
                            and runtime.ctx.run.status == "active" and runtime.ctx.state_of(task_id) == "failed"
                            and receipt.state == "failed" and any(check["exit_code"] != 0 for check in receipt.check_outputs)
                            and executor.has_attempt_capacity(task_id)
                            and runtime.ctx.run.calls_used < runtime.ctx.run.call_budget):
                        runtime.ctx.transition(task_id, "ready", verification="unverified", actor="Zeus",
                                               reason="已确认实现未通过固定检查，使用失败证据重试")
                        runtime.persist()
                        continue
                    doc["stage"] = "execution_blocked"
                    doc["error"] = {"code": "EXECUTION_NOT_ACCEPTED", "task_id": task_id, "message": reason[:500]}
                    return

    def _accept_run(self, doc, operation_id):
        runtime = self.registry.get(doc["run_id"])
        with runtime.lock, runtime.scheduler._lock:
            receipt = runtime.bus.dispatch(Command(
                operation_id="accept-" + operation_id, run_id=doc["run_id"], actor="YOU",
                action="confirm_delivery", target_id=doc["run_id"], expected_version=runtime.scheduler.plan.version,
                expected_state_revision=runtime.ctx.run.state_revision, parameters={},
            ))
            if receipt.status != "accepted":
                raise WorkflowError("最终验收被拒绝：" + receipt.reason_code)
            runtime.persist()
        doc["approvals"]["delivery"] = {"operation_id": operation_id, "at": datetime.now(UTC).isoformat()}

    def project_run(self, run_id, task_id, attempt_id=None):
        with self.store.connection() as db:
            row = db.execute("SELECT body FROM workflow_documents WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._execution(json.loads(row[0])).project(task_id, attempt_id)

