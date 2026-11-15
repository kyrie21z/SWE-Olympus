"""Registered local Python execution, with real subprocess evidence and query/cancel.

Implementation uses a restricted pure-Python profile, sanitized environment and isolated
work directory. This is a classroom adapter for reviewed code, not an OS security sandbox.
"""

from __future__ import annotations

import difflib
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime.models import RunnerBudget, RunnerPermissionError, UnknownOutcomeError
from olympus.common.workspace import resolve_scoped
from olympus.contracts.models import Artifact, ExecutionRecord, Plan, Ref, Task, fingerprint
from olympus.requests import RequestLedger
from olympus.reviews.common import ReviewRecord

from .models import ExecutionReceipt, Job
from .policy import validate_python


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks are outside the execution profile")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = bytes_hash(path.read_bytes())
    return result


class LocalPythonExecutor:
    def __init__(self, scheduler, root: Path, implementer, *, ledger=None, max_tool_calls=50,
                 timeout_cap=600):
        self.scheduler = scheduler
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.implementer = implementer
        self.ledger = ledger if ledger is not None else RequestLedger(self.root / "requests.json")
        self.max_tool_calls = max_tool_calls
        if not 0 < timeout_cap <= 600:
            raise ValueError("timeout cap must be between 0 and 600 seconds")
        self.timeout_cap = timeout_cap
        self._lock = threading.RLock()
        self._model_lock = threading.RLock()
        self._definitions: dict[str, tuple[FrozenAcceptance, dict]] = {}
        self._jobs: dict[str, Job] = {}
        self._operations: dict[str, str] = {}
        self._identities: dict[tuple[str, int], str] = {}
        self._load_receipts()

    def register_task(self, task_id: str, acceptance: FrozenAcceptance, *, context=None) -> None:
        """Host-only registration. Freeze scripts BEFORE dispatch; never accept them from Hephaestus."""
        task = self.scheduler.tasks[task_id]
        implementation = task.executor == "Hephaestus" and task.type in {"interface", "implement"}
        checks_only = task.executor in {"Momus", "Hermes"} and task.type in {"test", "integrate", "doc"}
        if task.reviewer != "Argus" or not (implementation or checks_only):
            raise ValueError("unregistered executor/type or independent-review profile")
        acceptance.verify(task)
        with self._lock:
            if any(j.task.task_id == task_id and not j.done.is_set() for j in self._jobs.values()):
                raise ValueError("cannot replace acceptance while task is in flight")
            self._definitions[task_id] = (acceptance.model_copy(deep=True), dict(context or {}))

    def _upstream(self, task_id: str):
        task = self.scheduler.tasks[task_id]
        found = {}
        ancestors = set(self.scheduler.manifest.ancestors.get(task_id, []))
        for job in self._jobs.values():
            if (job.task.task_id not in ancestors or job.receipt.state != "succeeded" or
                    self.scheduler.ctx.state_of(job.task.task_id) != "accepted" or
                    self.scheduler.ctx.runtime(job.task.task_id).generation != job.receipt.generation or
                    self.scheduler.attempt_for(job.task.task_id) != job.receipt.attempt_id):
                continue
            for artifact in job.receipt.artifacts:
                if artifact.artifact_id in task.inputs:
                    self.verify_integrity(job.receipt.attempt_id)
                    found[artifact.artifact_id] = (artifact, job)
        if set(task.inputs) - set(found):
            raise ValueError("accepted upstream artifact bytes are unavailable: " +
                             ", ".join(sorted(set(task.inputs) - set(found))))
        return found

    def _inputs(self, task_id: str) -> list[Ref]:
        plan = Plan.model_validate(self.scheduler.plan.model_dump(mode="json"))
        refs = [plan.requirement, plan.design, plan.ref()]
        if any(ref not in self.scheduler.ctx.run.approved_package for ref in refs):
            raise ValueError("run does not bind the approved REQ/DS/PLAN package")
        if self.scheduler.manifest.plan_ref != plan.ref():
            raise ValueError("compiled manifest is stale")
        return refs + [Ref(object_id=artifact.artifact_id, version=job.receipt.generation,
                           content_hash=artifact.content_hash)
                       for artifact, job in self._upstream(task_id).values()]

    def dispatch(self, task_id: str, generation: int) -> ExecutionReceipt:
        return self.submit(task_id, generation)

    def validate_dispatch(self, task_id: str) -> None:
        """Reject unsupported jobs before Scheduler acquires scarce execution resources."""
        with self.scheduler._lock, self._lock:
            self._validate_dispatch(task_id)

    def _validate_dispatch(self, task_id: str) -> None:
        if self.scheduler.ctx.run.mode not in {"real_tool", "real_model"}:
            raise ValueError("real local execution requires an explicit real_tool/real_model run")
        if task_id not in self._definitions:
            raise ValueError("task has no registered frozen acceptance definition")
        self._definitions[task_id][0].verify(self.scheduler.tasks[task_id])
        inputs = self._inputs(task_id)
        for object_id, content in self._definitions[task_id][1].items():
            ref = next((r for r in inputs if r.object_id == object_id), None)
            if ref is None or fingerprint(content) != ref.content_hash:
                raise ValueError("context content does not match the approved input fingerprint")
        task = self.scheduler.tasks[task_id]
        if any(scope in {".", "", "/"} for scope in task.write_scope):
            raise ValueError("unbounded implementation scope")
        for scope in task.write_scope:
            resolve_scoped(self.root, scope)
        if sum(j.task.task_id == task_id for j in self._jobs.values()) >= 1 + task.policy.max_auto_retries:
            raise ValueError("task execution attempt limit reached")
        if len(self.ledger.entries()) >= self.max_tool_calls:
            raise ValueError("local execution tool budget exhausted")

    @contextmanager
    def model_budget(self, runner=None):
        """Serialize P4 model sends and account actual P2 ledger usage in the P3 run."""
        with self._model_lock:
            ctx = self.scheduler.ctx
            budget = RunnerBudget(max_calls=ctx.run.call_budget, calls_used=ctx.run.calls_used)
            if budget.calls_used >= budget.max_calls:
                raise RunnerPermissionError("run model budget exhausted")
            ledger = (runner or self.implementer.runner).ledger
            before = ledger.total_calls_accounted(ctx.run.run_id)
            try:
                yield budget
            finally:
                spent = ledger.total_calls_accounted(ctx.run.run_id) - before
                if spent:
                    with self.scheduler._lock:
                        ctx.commit(reason=f"P4 model calls accounted: {spent}", actor="Zeus",
                                   actor_type="program",
                                   mutate=lambda snapshot: setattr(snapshot, "calls_used", snapshot.calls_used + spent))

    def submit(self, task_id: str, generation: int, *, operation_id: str | None = None):
        with self.scheduler._lock, self._lock:
            task = Task.model_validate(self.scheduler.tasks[task_id].model_dump(mode="json"))
            if task_id not in self._definitions:
                raise ValueError("task has no registered frozen acceptance definition")
            acceptance, context = self._definitions[task_id]
            acceptance.verify(task)
            inputs = self._inputs(task_id)
            for object_id, content in context.items():
                ref = next((r for r in inputs if r.object_id == object_id), None)
                if ref is None or fingerprint(content) != ref.content_hash:
                    raise ValueError("context content does not match the approved input fingerprint")
            input_hash = fingerprint({"task": task.content_hash, "inputs": [r.model_dump(mode="json")
                                      for r in inputs], "acceptance": acceptance.content_hash,
                                      "context": context})
            operation_id = operation_id or f"submit-{task_id}-g{generation}"
            existing_id = self._operations.get(operation_id) or self._identities.get((task_id, generation))
            if existing_id:
                old = self._jobs[existing_id].receipt
                if old.input_hash != input_hash or old.generation != generation or old.task_id != task_id:
                    raise ValueError("submission idempotency conflict")
                if operation_id != old.operation_id and operation_id not in old.operation_aliases:
                    old.operation_aliases.append(operation_id)
                    self._operations[operation_id] = existing_id
                    self._save(self._jobs[existing_id])
                return old.model_copy(deep=True)
            permit = self.scheduler.permit_for(task_id)
            if (permit is None or permit.generation != generation or
                    self.scheduler.ctx.state_of(task_id) != "running" or
                    self.scheduler.ctx.run.status != "active"):
                raise ValueError("current scheduler execution permit is required")
            if self.scheduler.ctx.run.mode not in {"real_tool", "real_model"}:
                raise ValueError("real local execution requires an explicit real_tool/real_model run")
            if any(scope in {".", "", "/"} for scope in task.write_scope):
                raise ValueError("unbounded implementation scope")
            for scope in task.write_scope:
                resolve_scoped(self.root, scope)
            if len(self.ledger.entries()) >= self.max_tool_calls:
                raise ValueError("local execution tool budget exhausted")
            if sum(j.task.task_id == task_id for j in self._jobs.values()) >= 1 + task.policy.max_auto_retries:
                raise ValueError("task execution attempt limit reached")
            attempt_id = "LOCAL-" + uuid4().hex[:20]
            entry = self.ledger.prepare(
                attempt_id=attempt_id, logical_task_id=f"local-{task_id}",
                run_id=self.scheduler.ctx.run.run_id, task_id=task_id,
                role_id=task.executor, task_kind="local_python", input_hash=input_hash,
                idempotency_key="local-" + fingerprint({"run": self.scheduler.ctx.run.run_id,
                                                       "operation": operation_id}),
                generation=generation, kind="tool", mode="real_tool",
            )
            receipt = ExecutionReceipt(
                run_id=self.scheduler.ctx.run.run_id,
                attempt_id=attempt_id, task_id=task_id, generation=generation,
                operation_id=operation_id, request_id=entry.request_id, input_hash=input_hash,
                task_hash=task.content_hash, acceptance_hash=acceptance.content_hash,
                evidence_id=f"evidence:{attempt_id}",
                model_mode=(self.implementer.runner.provider.mode if task.executor == "Hephaestus"
                            else "not_called"),
            )
            job = Job(task, inputs, acceptance.model_copy(deep=True), receipt, done=threading.Event())
            self._jobs[attempt_id] = job
            self._operations[operation_id] = attempt_id
            self._identities[(task_id, generation)] = attempt_id
            directory = self.root / attempt_id
            directory.mkdir()
            (directory / "definition.json").write_text(json.dumps({
                "run_id": receipt.run_id, "task": task.model_dump(mode="json"),
                "inputs": [r.model_dump(mode="json") for r in inputs],
                "acceptance": acceptance.model_dump(mode="json"),
            }, ensure_ascii=False), encoding="utf-8")
            self._save(job)
            threading.Thread(target=self._execute, args=(job, context), daemon=True,
                             name=attempt_id).start()
            return receipt.model_copy(deep=True)

    def query(self, attempt_id: str) -> str:
        return self.query_receipt(attempt_id).state

    def query_receipt(self, attempt_id: str) -> ExecutionReceipt:
        with self._lock:
            job = self._jobs[attempt_id]
            if (job.receipt.state == "outcome_unknown" and job.done.is_set() and
                    job.process is not None and job.process.poll() is not None):
                if job.receipt.cancel_requested:
                    job.receipt.state = "cancelled"
                    job.receipt.cancel_confirmed = True
                    self.ledger.cancel(job.receipt.request_id, provider_cancel_hook=lambda _: "cancelled")
                else:
                    job.receipt.state = "failed"
                    job.receipt.error = "local process terminated without a complete verified check run"
                    self.ledger.record_outcome(job.receipt.request_id, "failed")
                self._save(job)
            return job.receipt.model_copy(deep=True)

    def wait(self, attempt_id: str, timeout: float = 30) -> ExecutionReceipt:
        if not self._jobs[attempt_id].done.wait(timeout):
            raise TimeoutError("local execution has not finished")
        return self.query_receipt(attempt_id)

    def cancel(self, attempt_id: str) -> ExecutionReceipt:
        with self._lock:
            job = self._jobs[attempt_id]
            if job.receipt.state in {"succeeded", "failed", "cancelled"}:
                return job.receipt.model_copy(deep=True)
            job.receipt.cancel_requested = True
            if job.process is None:
                # A model request or a restarted process cannot be declared stopped locally.
                job.receipt.state = "outcome_unknown"
            else:
                try:
                    job.process.kill()
                    job.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    job.receipt.state = "outcome_unknown"
                else:
                    job.receipt.state = "cancelled"
                    job.receipt.cancel_confirmed = True
            self._save(job)
            return job.receipt.model_copy(deep=True)

    def _save(self, job: Job) -> None:
        directory = self.root / job.receipt.attempt_id
        target = directory / "receipt.json"
        temporary = directory / "receipt.tmp"
        temporary.write_text(job.receipt.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        if job.evidence_hashes:
            (directory / "integrity.json").write_text(json.dumps(job.evidence_hashes), encoding="utf-8")

    def _load_receipts(self) -> None:
        for path in self.root.glob("LOCAL-*/receipt.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if "run_id" not in raw:
                raw["run_id"] = self.ledger.to_attempt(raw["request_id"]).run_id
            receipt = ExecutionReceipt.model_validate(raw)
            if receipt.run_id != self.scheduler.ctx.run.run_id:
                continue
            if receipt.attempt_id != path.parent.name:
                raise ValueError("receipt attempt does not match its persisted directory")
            definition = json.loads((path.parent / "definition.json").read_text(encoding="utf-8"))
            job = Job(Task.model_validate(definition["task"]),
                      [Ref.model_validate(r) for r in definition["inputs"]],
                      FrozenAcceptance.model_validate(definition["acceptance"]), receipt,
                      done=threading.Event())
            if receipt.state not in {"succeeded", "failed", "cancelled"}:
                receipt.state = "outcome_unknown"
                receipt.error = "executor restarted; in-flight outcome requires reconciliation"
            else:
                job.done.set()
            integrity = path.parent / "integrity.json"
            if integrity.exists():
                job.evidence_hashes = json.loads(integrity.read_text(encoding="utf-8"))
            self._jobs[receipt.attempt_id] = job
            self._operations[receipt.operation_id] = receipt.attempt_id
            for alias in receipt.operation_aliases:
                self._operations[alias] = receipt.attempt_id
            self._identities[(receipt.task_id, receipt.generation)] = receipt.attempt_id

    def _execute(self, job: Job, context: dict) -> None:
        started = time.monotonic()
        directory = self.root / job.receipt.attempt_id
        work, checks_dir = directory / "work", directory / "checks"
        try:
            work.mkdir()
            checks_dir.mkdir()
            context = dict(context)
            original_files = {}
            with self.scheduler._lock, self._lock:
                upstream = self._upstream(job.task.task_id)
                for artifact, producer in upstream.values():
                    files = self.verify_integrity(producer.receipt.attempt_id)
                    context[artifact.artifact_id] = files
                    for relative, source in files.items():
                        if relative in original_files and original_files[relative] != source:
                            raise ValueError("upstream input files conflict")
                        original_files[relative] = source
                        destination = resolve_scoped(work, relative)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(source, encoding="utf-8")
            checks_only = job.task.type in {"test", "integrate", "doc"}
            if checks_only:
                # Momus/Hermes run trusted, pre-frozen scripts against immutable inputs.
                # They do not silently become Hephaestus or get to rewrite expectations.
                changes = []
                job.receipt.model_mode = "not_called"
            else:
                with self.model_budget() as budget:
                    result = self.implementer.propose(job.task, job.inputs,
                        run_id=self.scheduler.ctx.run.run_id, generation=job.receipt.generation,
                        context=context, budget=budget)
                job.receipt.model_mode = result.mode
                changes = result.structured_output["changes"]
            job.acceptance.verify(job.task)
            normalized = {}
            for change in changes:
                path = resolve_scoped(work, change["path"])
                relative = path.relative_to(work).as_posix()
                if relative.casefold() in {p.casefold() for p in normalized}:
                    raise ValueError("duplicate or case-colliding implementation path")
                if not any(path == resolve_scoped(work, s) or path.is_relative_to(resolve_scoped(work, s))
                           for s in job.task.write_scope):
                    raise ValueError("implementation write outside task scope")
                if path.suffix not in {".py", ".json", ".md", ".txt"}:
                    raise ValueError("unregistered implementation file type")
                if any(part.startswith(".") for part in Path(relative).parts):
                    raise ValueError("hidden/control files are not implementation outputs")
                if path.suffix == ".py":
                    validate_python(change["content"])
                normalized[relative] = change["content"]
            with self._lock:
                if job.receipt.cancel_requested:
                    job.receipt.state = "cancelled"
                    job.receipt.cancel_confirmed = True
                    return
                for relative, source in normalized.items():
                    path = resolve_scoped(work, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source, encoding="utf-8")
                job.receipt.diff = "\n".join("".join(difflib.unified_diff(
                    original_files.get(relative, "").splitlines(keepends=True),
                    source.splitlines(keepends=True), fromfile=relative, tofile=relative))
                    for relative, source in normalized.items())
                job.receipt.file_hashes = tree_hashes(work)
            (checks_dir / "fixtures.json").write_text(json.dumps(job.acceptance.fixtures), encoding="utf-8")
            for index, script in enumerate(job.acceptance.checks.values()):
                (checks_dir / f"check_{index}.py").write_text(script, encoding="utf-8")
            driver = checks_dir / "driver.py"
            driver.write_text(
                "import runpy,sys\nsys.dont_write_bytecode=True\n"
                "sys.path.insert(0,sys.argv[1])\nscript=sys.argv[2]\nsys.argv=[script]\n"
                "runpy.run_path(script,run_name='__main__')\n", encoding="utf-8")
            frozen_checks = tree_hashes(checks_dir)
            self.ledger.mark_sent(job.receipt.request_id, job.receipt.attempt_id)
            for index, check_id in enumerate(job.acceptance.checks):
                deadline = started + min(job.task.policy.timeout_seconds, self.timeout_cap)
                output = directory / f"check_{index}.log"
                command = [sys.executable, "-I", "-S", "-B", str(driver), str(work),
                           str(checks_dir / f"check_{index}.py")]
                env = {k: v for k, v in os.environ.items()
                       if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT"}}
                env.update(TEMP=str(work), TMP=str(work), PYTHONIOENCODING="utf-8")
                with output.open("wb") as stream:
                    with self._lock:
                        if job.receipt.cancel_requested:
                            job.receipt.state = "cancelled"
                            job.receipt.cancel_confirmed = True
                            return
                        job.process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                                       stdout=stream, stderr=subprocess.STDOUT,
                                                       creationflags=(subprocess.CREATE_NO_WINDOW
                                                                      if os.name == "nt" else 0))
                        job.receipt.tool_invocations += 1
                    while job.process.poll() is None:
                        if time.monotonic() >= deadline or output.stat().st_size > 1048576:
                            job.process.kill()
                            job.process.wait(timeout=2)
                            raise TimeoutError("check timeout or output limit exceeded")
                        time.sleep(0.02)
                returncode = job.process.returncode
                text = output.read_bytes()[:65536].decode("utf-8", "replace")
                job.receipt.check_outputs.append({"check_id": check_id, "command": command,
                    "exit_code": returncode, "output": text, "log_hash": bytes_hash(output.read_bytes())})
                if job.receipt.cancel_requested:
                    job.receipt.state = "cancelled"
                    job.receipt.cancel_confirmed = True
                    return
                if tree_hashes(work) != job.receipt.file_hashes or tree_hashes(checks_dir) != frozen_checks:
                    raise ValueError("implementation or acceptance was modified during checking")
                if returncode != 0:
                    raise ValueError("actual acceptance check failed: " + check_id)
                job.receipt.checks_passed.append(check_id)
            job.evidence_hashes = {"checks/" + p: h for p, h in frozen_checks.items()}
            job.evidence_hashes.update({p.name: bytes_hash(p.read_bytes()) for p in directory.glob("check_*.log")})
            if checks_only:
                proof = resolve_scoped(work, job.task.write_scope[0] + "/check-report.json")
                proof.parent.mkdir(parents=True, exist_ok=True)
                if proof.exists():
                    raise ValueError("check report would overwrite an immutable input")
                text = json.dumps({"task": job.task.task_id, "executor": job.task.executor,
                                   "checks": job.receipt.check_outputs,
                                   "inputs": [r.model_dump(mode="json") for r in job.inputs]},
                                  ensure_ascii=False, indent=2)
                proof.write_text(text, encoding="utf-8")
                job.receipt.diff = text
                job.receipt.file_hashes = tree_hashes(work)
            implementation_hash = fingerprint(job.receipt.file_hashes)
            artifact_hash = fingerprint({"files": job.receipt.file_hashes, "task": job.task.content_hash,
                                         "inputs": [r.model_dump(mode="json") for r in job.inputs],
                                         "acceptance": job.acceptance.content_hash})
            job.receipt.artifacts = [Artifact(
                artifact_id=output_id, run_id=self.scheduler.ctx.run.run_id, task_id=job.task.task_id,
                attempt_id=job.receipt.attempt_id, location=f"{job.receipt.attempt_id}/work",
                content_hash=artifact_hash, inputs=job.inputs,
                implementation_version=implementation_hash, test_version=job.acceptance.content_hash,
                fixture_version=fingerprint(job.acceptance.fixtures), mode="real_tool",
            ) for output_id in job.task.outputs]
            with self._lock:
                if job.receipt.cancel_requested:
                    job.receipt.state = "cancelled"
                    job.receipt.cancel_confirmed = True
                else:
                    job.receipt.state = "succeeded"
        except UnknownOutcomeError as exc:
            job.receipt.state = "outcome_unknown"
            job.receipt.error = str(exc)
        except Exception as exc:  # Adapter failures become inspectable results, never accepted tasks.
            if job.process is not None and job.process.poll() is None:
                job.receipt.state = "outcome_unknown"
            elif job.receipt.state != "cancelled":
                job.receipt.state = "failed"
            job.receipt.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self._finish(job, started)
            except Exception as exc:
                with self._lock:
                    job.receipt.state = ("outcome_unknown" if job.process is not None and
                                         job.process.poll() is None else "failed")
                    job.receipt.error = f"evidence_persist_failed: {type(exc).__name__}: {exc}"
                    try:
                        self.ledger.record_outcome(job.receipt.request_id, job.receipt.state)
                        self._save(job)
                    except OSError:
                        pass  # In-memory query still reports failure; never claim durable success.
            finally:
                job.done.set()

    def _finish(self, job, started):
        with self._lock:
            job.receipt.model_request_ids = [e.request_id for e in self.implementer.runner.ledger.entries()
                if e.task_id == job.task.task_id and e.generation == job.receipt.generation
                and e.role_id == job.task.executor and e.run_id == job.receipt.run_id]
            if job.receipt.state == "cancelled":
                self.ledger.cancel(job.receipt.request_id, provider_cancel_hook=lambda _: "cancelled")
            else:
                self.ledger.record_outcome(job.receipt.request_id,
                    "succeeded" if job.receipt.state == "succeeded" else
                    ("outcome_unknown" if job.receipt.state == "outcome_unknown" else "failed"))
            self.implementer.runner.records.record(ExecutionRecord(
                record_id=f"EXEC-{job.receipt.attempt_id}", attempt_id=job.receipt.attempt_id,
                task_id=job.task.task_id, inputs=job.inputs,
                tools=[{"name": "registered_local_python", "checks": job.receipt.check_outputs}],
                artifacts=[a.artifact_id for a in job.receipt.artifacts], diff=job.receipt.diff,
                check_outputs=[json.dumps(v, ensure_ascii=False) for v in job.receipt.check_outputs],
                elapsed_ms=int((time.monotonic()-started)*1000), call_count=job.receipt.tool_invocations,
                retry_history=[], mode="real_tool"), role_id=job.task.executor,
                logical_task_id=f"local-{job.task.task_id}", run_id=job.receipt.run_id)
            self._save(job)

    def verify_integrity(self, attempt_id: str) -> dict[str, str]:
        """Rehash actual files and frozen check material, including after a restart."""
        with self._lock:
            job = self._jobs[attempt_id]
            directory = self.root / attempt_id
            job.acceptance.verify(job.task)
            if tree_hashes(directory / "work") != job.receipt.file_hashes:
                raise ValueError("artifact changed after program checks")
            if not job.evidence_hashes:
                raise ValueError("missing frozen evidence integrity manifest")
            for relative, expected in job.evidence_hashes.items():
                if bytes_hash(resolve_scoped(directory, relative).read_bytes()) != expected:
                    raise ValueError("frozen acceptance or check evidence changed")
            if len(job.receipt.check_outputs) != len(job.acceptance.checks):
                raise ValueError("missing actual program-check receipts")
            for index, (check_id, script) in enumerate(job.acceptance.checks.items()):
                entry = job.receipt.check_outputs[index]
                log = directory / f"check_{index}.log"
                if (entry.get("check_id") != check_id or entry.get("exit_code") != 0 or
                        entry.get("log_hash") != bytes_hash(log.read_bytes()) or
                        entry.get("output") != log.read_bytes()[:65536].decode("utf-8", "replace") or
                        (directory / f"checks/check_{index}.py").read_text(encoding="utf-8") != script):
                    raise ValueError("actual check output or frozen script does not match its receipt")
            return {relative: resolve_scoped(directory / "work", relative).read_text(encoding="utf-8")
                    for relative in job.receipt.file_hashes}

    def project(self, attempt_id: str) -> dict:
        return self.query_receipt(attempt_id).model_dump(mode="json")

    def record_review(self, attempt_id: str, record: ReviewRecord) -> None:
        """Persist the independent report beside its exact execution, for P5/P6 queries."""
        with self._lock:
            job = self._jobs[attempt_id]
            target = Ref(object_id=job.receipt.artifacts[0].artifact_id,
                         version=job.receipt.generation,
                         content_hash=job.receipt.artifacts[0].content_hash)
            if record.review.target != target or record.submission.artifact_ref != target:
                raise ValueError("independent report does not target this artifact")
            job.receipt.review = record.model_copy(deep=True)
            self._save(job)
