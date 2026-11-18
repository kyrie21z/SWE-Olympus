"""Persistent host for explicit human gates, model stages and all planned tasks."""

from __future__ import annotations

import json
import threading
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
from olympus.agents.runtime import AgentRunner
from olympus.agents.runtime.models import OutputSchemaError, ProviderNotConfiguredError, RunnerBudget, UnknownOutcomeError
from olympus.api.errors import ApiError
from olympus.contracts.models import Command, Confirmation, Design, Plan, Requirement, Review, Run, TaskRuntime, fingerprint
from olympus.execution import ArtifactAcceptanceService, LocalPythonExecutor
from olympus.planning.service import PlanService
from olympus.requests import RequestLedger
from olympus.requirements_bridge import RequirementBridge
from olympus.reviews.common import CommonReviewer, ReviewRecord

from .acceptance import generate_acceptance, validate_executable_plan
from .offline import OfflineRequirementUnsupported, make_offline_runner

BUSY = {"design_generating", "plan_generating", "executing"}
TRANSITIONS = {
    "approve_requirement": ({"requirement_pending"}, "requirement_approved"),
    "generate_design": ({"requirement_approved"}, "design_generating"),
    "approve_design": ({"design_pending_approval"}, "design_approved"),
    "generate_plan": ({"design_approved"}, "plan_generating"),
    "approve_plan": ({"plan_pending_approval"}, "plan_approved"),
    "execute": ({"plan_approved", "execution_blocked"}, "executing"),
    "accept_run": ({"awaiting_acceptance"}, "completed"),
}


class LocalPlanner(DaedalusPlanner):
    def build_input_manifest(self, req, ds):
        manifest = super().build_input_manifest(req, ds)
        manifest["execution_profile"] = {
            "implementation": "interface/implement tasks: executor Hephaestus, reviewer Argus",
            "checking": "test/integrate/doc tasks: executor Momus or Hermes, reviewer Argus",
            "runtime": "pure Python; math and from threading import Lock/RLock only; no third-party packages",
            "scopes": "bounded relative paths; preserve module/function names from design interfaces",
            "inputs": "artifact IDs produced by ancestors only; each task must run with these inputs alone",
        }
        manifest["prompt"] += "\n所有任务必须遵循 execution_profile，测试与集成任务只读检查前驱产物。"
        return manifest


class WorkflowError(ApiError):
    status_code = 409
    code = "WORKFLOW_CONFLICT"


class WorkflowRunner(AgentRunner):
    """Bind older thin roles to this run and a shared, durable per-run budget."""

    def __init__(self, base, run_id, budget):
        super().__init__(base.provider, ledger=base.ledger, records=base.records)
        self.run_id, self.call_budget = run_id, budget
        self.operation_id = "initial"

    def run(self, request, **kwargs):
        values = {
            "run_id": self.run_id,
            "budget": RunnerBudget(max_calls=self.call_budget,
                                   calls_used=self.ledger.total_calls_accounted(self.run_id)),
        }
        values["idempotency_key"] = "wf-" + fingerprint({
            "run": self.run_id, "operation": self.operation_id,
            "key": request.idempotency_key or request.logical_task_id,
            "inputs": [ref.model_dump(mode="json") for ref in request.input_refs],
        })
        return super().run(request.model_copy(update=values), **kwargs)


class WorkflowService:
    def __init__(self, store, registry, runner_factory, *, workers=3, has_online_config=None):
        self.store, self.registry, self.runner_factory = store, registry, runner_factory
        self.has_online_config = has_online_config
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
                    doc["stage"] = "error"
                    doc["error"] = {
                        "code": "INTERRUPTED_REQUIRES_RECONCILIATION",
                        "message": "服务在请求或执行期间重启；结果需核对，未自动重发。已完成的阶段和工件已保留。",
                    }
                    doc["pending_action"] = None
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

    def create(self, raw, *, allow_synthetic=False, call_budget=80):
        req = Requirement.model_validate(raw)
        synthetic = any(p.kind == "synthetic_fixture" for p in req.provenance) or bool(
            req.confirmation and req.confirmation.origin == "synthetic_fixture"
        )
        if synthetic and not allow_synthetic:
            raise WorkflowError("此文件为构造样例，请明确允许用于测试后再创建运行")
        if not synthetic and any(p.kind not in {"human", "material"} for p in req.provenance):
            raise WorkflowError("正式需求须提供人工或材料来源，并由当前用户接纳")
        workflow_id, run_id = "WF-" + uuid4().hex, "RUN-" + uuid4().hex[:20]
        model_mode = self._default_model_mode()
        doc = {
            "workflow_id": workflow_id, "run_id": run_id, "stage": "requirement_pending",
            "error": None, "requirement": req.model_dump(mode="json"),
            "synthetic": synthetic, "allow_synthetic": allow_synthetic, "model_mode": model_mode,
            "design": None, "design_review": None, "plan": None, "plan_review": None,
            "acceptance": {}, "executions": [], "call_budget": call_budget,
            "retry_safe": False, "retry_action": None,
            "pending_action": None,
            "approvals": {}, "updated_at": datetime.now(UTC).isoformat(),
        }
        self.store.create(Run(run_id=run_id, mode="mock" if model_mode == "offline" else "real_model",
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
        doc["snapshot"] = self.registry.get(doc["run_id"]).ctx.run.model_dump(mode="json")
        doc["allowed_actions"] = [action for action, (stages, _) in TRANSITIONS.items()
                                  if doc["stage"] in stages]
        if doc.get("retry_safe") and doc.get("retry_action"):
            doc["allowed_actions"].append(doc["retry_action"])
        if doc.get("pending_action"):
            doc["allowed_actions"] = []
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
                retry = doc["stage"] == "error" and doc.get("retry_safe") and doc.get("retry_action") == action
                if doc.get("pending_action") or (doc["stage"] not in allowed and not retry):
                    raise WorkflowError(f"当前阶段 {doc['stage']} 不允许此操作")
                doc["error"] = None
                doc["retry_action"], doc["retry_safe"] = None, False
                # Reserve the operation before any external call can be scheduled.
                doc["pending_action"] = action
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

    def _perform(self, workflow_id, action, operation_id):
        doc = self._read(workflow_id)
        try:
            if action == "generate_design":
                self._select_design_mode(doc)
            if action.startswith("generate_") or action == "execute":
                self._runner(doc).operation_id = operation_id
            getattr(self, "_" + action)(doc, operation_id)
            if not action.startswith("generate_") and action != "execute":
                doc["stage"] = TRANSITIONS[action][1]
        except Exception as exc:
            # Provider responses and credentials are deliberately not reflected to the browser.
            if isinstance(exc, UnknownOutcomeError):
                code, message = "OUTCOME_UNKNOWN", "模型请求结果未知；请核对提供方记录，系统未自动重发。"
            elif isinstance(exc, (WorkflowError, OfflineRequirementUnsupported)):
                code, message = exc.code, exc.message
            elif isinstance(exc, ProviderNotConfiguredError):
                code, message = "AGENT_NOT_CONFIGURED", str(exc)
            else:
                code = type(exc).__name__.upper()
                message = "此阶段未完成，请检查 Agent 配置、模型输出格式及执行证据。"
            doc["stage"] = "error"
            doc["error"] = {"code": code, "message": message}
            runner = self._runners.get(doc["run_id"])
            unknown = runner and any(entry.status in {"sent", "outcome_unknown"}
                                     for entry in runner.ledger.entries())
            safe_error = isinstance(exc, (ProviderNotConfiguredError, OutputSchemaError, OfflineRequirementUnsupported)) or (
                isinstance(exc, RuntimeError) and str(exc).startswith("provider request failed: http_4")
            )
            if not unknown and safe_error and action.startswith("generate_"):
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
            with self._lock:
                self._save(doc)

    def _runner(self, doc):
        with self._services_lock:
            return self._make_runner(doc)

    def _default_model_mode(self):
        return "online" if self.has_online_config is None or self.has_online_config() else "offline"

    def _ledger(self, run_id):
        return RequestLedger(self.root / run_id / "model-requests.json")

    def _select_design_mode(self, doc):
        """Allow old no-config failures to retry offline before any model call was sent."""
        run_id = doc["run_id"]
        if doc.get("design") is not None or self._ledger(run_id).entries():
            return
        desired = self._default_model_mode()
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
        design = designer.generate(req, run_id=doc["run_id"])
        for revision in range(2):
            report = reviewer.review(req, design, run_id=doc["run_id"])
            doc["design"] = design.model_dump(mode="json")
            doc["design_review"] = report.model_dump(mode="json")
            if report.review.verdict == "passed":
                doc["stage"] = "design_pending_approval"
                return
            if revision == 0:
                design = designer.generate(req, run_id=doc["run_id"], previous=design,
                                           findings=report.review.findings)
        doc["stage"] = "design_review_blocked"

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
        outcome = PlanningWorkflow(LocalPlanner(runner), MomusPlanReviewer(runner=runner)).run(req, design)
        doc["plan"] = outcome.plan.model_dump(mode="json")
        doc["plan_review"] = outcome.review.model_dump(mode="json")
        if outcome.status != "passed":
            doc["stage"] = "plan_review_blocked"
            return
        validate_executable_plan(outcome.plan)
        PlanService().prepare(req, design, outcome.plan)
        doc["acceptance"] = generate_acceptance(runner, req, design, outcome.plan)
        doc["stage"] = "plan_pending_approval"

    def _approve_plan(self, doc, operation_id):
        req, design, plan = self._admit(doc), Design.model_validate(doc["design"]), Plan.model_validate(doc["plan"])
        review = Review.model_validate(doc["plan_review"])
        if review.verdict != "passed" or review.target != plan.ref() or review.reviewer != "Momus":
            raise WorkflowError("独立计划审查未通过")
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
            executor = LocalPythonExecutor(runtime.scheduler, self.root / run_id / "execution",
                                           HephaestusImplementer(runner))
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
                ready = scheduler.ready()
                if not ready:
                    runtime.persist()
                    doc["stage"] = "execution_blocked"
                    doc["error"] = {"code": "TASKS_BLOCKED", "message": "有任务失败或等待核对，请查看执行证据及运行控制台。"}
                    return
                task_id = ready[0]
                if scheduler.dispatch(task_id, executor) is None:
                    raise WorkflowError("任务未取得执行许可")
                attempt = scheduler.attempt_for(task_id)
                runtime.persist()
            receipt = executor.wait(attempt, timeout=605)
            status, reason = service.collect(task_id, receipt.attempt_id)
            with runtime.lock, scheduler._lock:
                runtime.persist()
                projected = service.project(task_id, receipt.attempt_id)
                doc["executions"] = [item for item in doc["executions"] if item["task_id"] != task_id] + [projected]
                with self._lock:
                    self._save(doc)
                if status != "accepted":
                    doc["stage"] = "execution_blocked"
                    doc["error"] = {"code": "EXECUTION_NOT_ACCEPTED", "message": reason[:500]}
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
