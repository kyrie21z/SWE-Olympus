"""Program-owned delegation of declared documentation to a real local author."""

from pathlib import PurePosixPath, PureWindowsPath

from olympus.agents.runtime import OutputSchemaError
from olympus.contracts.models import Plan, Provenance, Task, fingerprint
from olympus.execution.profile import IMPLEMENTATION_TASK_TYPES, READ_ONLY_TASK_TYPES, _file_claim, _path_problem
from olympus.planning.validator import ancestor_map, find_dependency_cycles, unknown_dependencies, validate_plan_semantics

NORMALIZATION_SOURCE = "LOCAL-DOC-AUTHOR-NORMALIZATION"


def _reject(message):
    raise OutputSchemaError("documentation author normalization rejected", hint=message)


def _path(value):
    normalized = value.replace("\\", "/")
    if _path_problem(normalized) or str(PurePosixPath(normalized)) != normalized.rstrip("/"):
        _reject("文档必须使用规范、有界、无隐藏或遍历组件的相对路径；不能自动猜测或修复不安全路径。")
    return normalized.rstrip("/")


def _covers(author, document):
    path = PurePosixPath(document)
    return any(path == PurePosixPath(_path(scope)) or
               (not PurePosixPath(_path(scope)).suffix and path.is_relative_to(PurePosixPath(_path(scope))))
               for scope in author.write_scope)


def _task(task, **changes):
    content = {**task.semantic_content(), **changes}
    return Task(**content, version=task.version, content_hash=fingerprint(content))


def _plan(plan, tasks, *, changed=False):
    content = {"tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                         for task in tasks]}
    provenance = list(plan.provenance)
    if changed and not any(item.source_id == NORMALIZATION_SOURCE for item in provenance):
        provenance.append(Provenance(kind="material", source_id=NORMALIZATION_SOURCE,
                                     locator="Program conversion: bounded documentation authoring delegated to Hephaestus; independent checker retained."))
    return Plan.model_validate({**plan.model_dump(mode="json"), "tasks": tasks, "content": content,
                                "content_hash": fingerprint(content), "provenance": provenance})


def _reachable_producers(plan, consumer):
    ancestors = ancestor_map(plan)
    producers = {}
    for task in plan.tasks:
        for output in task.outputs:
            producers.setdefault(output, []).append(task)
    found, pending = {}, [consumer]
    while pending:
        current = pending.pop()
        for item in current.inputs:
            for producer in producers.get(item, []):
                if producer.task_id in ancestors[current.task_id] and producer.task_id not in found:
                    found[producer.task_id] = producer
                    pending.append(producer)
    return list(found.values())


def _reachable_authors(plan, consumer):
    return [task for task in _reachable_producers(plan, consumer) if task.executor == "Hephaestus" and
            task.type in IMPLEMENTATION_TASK_TYPES and task.reviewer == "Argus"]


def _report_directory(plan, checker, documents):
    original = _path(checker.write_scope[0])
    previous_report = original + "/check-report.json"
    inherited = _reachable_producers(plan, checker)

    def conflict(directory):
        report = directory + "/check-report.json"
        if PurePosixPath(directory).suffix or any(
            PurePosixPath(report) == PurePosixPath(document) or
            PurePosixPath(report).is_relative_to(PurePosixPath(document)) for document in documents
        ):
            return True
        for producer in inherited:
            if producer.type in READ_ONLY_TASK_TYPES and _path(producer.write_scope[0]) + "/check-report.json" == report:
                return True
            if any(_file_claim(output) and output.replace("\\", "/") == report for output in producer.outputs):
                return True
            if any(PurePosixPath(_path(scope)).suffix and _path(scope) == report for scope in producer.write_scope):
                return True
        return False

    if not conflict(original):
        return original
    if previous_report in checker.outputs or any(previous_report in task.inputs for task in plan.tasks):
        _reject("原检查报告路径会覆盖输入或文档且已被声明或消费，不能改名后留下悬空引用；请明确修正报告契约。")
    replacement = "reports/doc-checks/" + fingerprint({"task": checker.task_id})[:16]
    if conflict(replacement):
        _reject("安全报告路径与现有输入冲突，不能猜测或覆盖现有工件。")
    return replacement


def normalize_document_authorship(plan, requirement, design):
    """Split only safe explicit .md/.txt claims; never invent content obligations."""
    if any(task.type in READ_ONLY_TASK_TYPES and PureWindowsPath(output).drive and
           PurePosixPath(output.replace("\\", "/")).suffix in {".md", ".txt"}
           for task in plan.tasks for output in task.outputs):
        _reject("文档文件输出不能使用 Windows 驱动器或网络绝对路径；不得将其误当逻辑工件编号。")
    if not any(task.type in READ_ONLY_TASK_TYPES and task.executor in {"Momus", "Hermes"} and
               any(_file_claim(output) and PurePosixPath(output.replace("\\", "/")).suffix in {".md", ".txt"}
                   for output in task.outputs) for task in plan.tasks):
        return plan
    if unknown_dependencies(plan) or find_dependency_cycles(plan):
        _reject("计划依赖必须先通过无未知前驱、无环校验，才能规范化文档作者。")
    working, changed = plan, False
    for original in plan.tasks:
        checker = next(task for task in working.tasks if task.task_id == original.task_id)
        if checker.type not in READ_ONLY_TASK_TYPES or checker.executor not in {"Momus", "Hermes"}:
            continue
        physical = [output for output in checker.outputs if _file_claim(output)]
        documents = [output for output in physical if PurePosixPath(output.replace("\\", "/")).suffix in {".md", ".txt"}]
        if not documents:
            continue
        documents = [_path(output) for output in documents]
        for scope in checker.write_scope:
            _path(scope)
        actual_report = str(PurePosixPath(checker.write_scope[0].replace("\\", "/")) / "check-report.json")
        if any(output not in documents and output != actual_report for output in physical):
            _reject("只能自动规范化明确声明的 .md/.txt 文档；不能把 Python 测试脚本、其他文件或不支持格式改写成文档。")
        reachable = _reachable_authors(working, checker)
        reachable_ancestors = ancestor_map(working)
        for document in documents:
            providers = [author for author in reachable if _covers(author, document)]
            latest = [author for author in providers if not any(author.task_id in reachable_ancestors[other.task_id]
                      for other in providers if author.task_id != other.task_id)]
            if len(latest) > 1:
                _reject("文档输入存在多个并行可达作者，不能以任一作者存在为由跳过歧义校验。")
        if all(any(_covers(author, document) for author in reachable) for document in documents):
            continue  # An already valid reachable author needs no semantic rewrite.
        report = validate_plan_semantics(working, requirement, design)
        if not report.valid:
            gaps = [f"{issue.task_id or 'plan'} [{issue.code}] {issue.source_id or ''}" for issue in report.issues[:8]]
            _reject("文档规范化前请先修复现有全局语义问题：" + "；".join(gaps))
        ancestors = ancestor_map(working)
        selected, unowned, replacements = {}, [], {}
        for document in documents:
            supplied = [author for author in reachable if _covers(author, document)]
            candidates = supplied or [task for task in working.tasks if task.executor == "Hephaestus" and
                                       task.type in IMPLEMENTATION_TASK_TYPES and task.reviewer == "Argus" and
                                       _covers(task, document)]
            closest = [task for task in candidates if not any(task.task_id in ancestors[other.task_id]
                       for other in candidates if task.task_id != other.task_id)]
            if len(closest) > 1:
                _reject("文档存在多个并行作者，不能自动选择；请明确唯一作者与输入产物。")
            if not closest:
                unowned.append(document)
                continue
            author = replacements.get(closest[0].task_id, closest[0])
            if checker.task_id in ancestors[author.task_id]:
                _reject("现有文档作者依赖当前检查任务，连接会形成环；请先修正依赖。")
            if document not in author.outputs:
                if document not in [_path(scope) for scope in author.write_scope]:
                    _reject("现有作者仅声明宽目录且未明确提供该文档，不能猜测；请显式声明文档文件输出。")
                author = _task(author, outputs=[*author.outputs, document])
                replacements[author.task_id] = author
            selected[document] = author
        added = []
        if unowned:
            author_id = "DOC-AUTHOR-" + fingerprint({"checker": checker.task_id, "documents": sorted(unowned)})[:24]
            if any(task.task_id == author_id for task in working.tasks):
                _reject("自动文档作者的稳定编号已被其他任务占用，不能覆盖现有任务。")
            if len(working.tasks) >= 30:
                _reject("新增真实文档作者会超过 30 项任务上限；请明确合并已有作者，不能删除检查或取消必需项。")
            inputs = [item for item in checker.inputs if item.replace("\\", "/") not in unowned]
            dependencies = list(checker.depends_on)
            for document, author in selected.items():
                if document not in inputs:
                    inputs.append(document)
                if author.task_id not in dependencies:
                    dependencies.append(author.task_id)
            content = {**checker.semantic_content(), "task_id": author_id, "type": "implement",
                       "executor": "Hephaestus", "reviewer": "Argus", "write_scope": unowned,
                       "outputs": unowned, "inputs": inputs, "depends_on": dependencies}
            author = Task(**content, version=1, content_hash=fingerprint(content))
            added.append(author)
            for document in unowned:
                selected[document] = author
        dependencies = list(checker.depends_on)
        for author in selected.values():
            if author.task_id not in dependencies:
                dependencies.append(author.task_id)
        inputs = list(checker.inputs)
        for document in documents:
            if document not in inputs:
                inputs.append(document)
        report_directory = _report_directory(working, checker, documents)
        logical = [output for output in checker.outputs if not _file_claim(output)]
        replacements[checker.task_id] = _task(
            checker, goal="Verify documentation independently against all retained content checks.",
            depends_on=dependencies, inputs=inputs, write_scope=[report_directory],
            outputs=[*logical, report_directory + "/check-report.json"],
        )
        tasks = [replacements.get(task.task_id, task) for task in working.tasks]
        position = next(index for index, task in enumerate(tasks) if task.task_id == checker.task_id)
        tasks[position:position] = added
        previous = working
        working = _plan(working, tasks, changed=True)
        if find_dependency_cycles(working) or unknown_dependencies(working):
            _reject("文档作者转换未能保持无环依赖及完整前驱，已拒绝该转换。")
        after = validate_plan_semantics(working, requirement, design)
        if not after.valid:
            _reject("文档作者转换未通过全局来源、覆盖或输入语义校验，不能用于执行。")
        remaining = {task.task_id: task for task in working.tasks}
        closure = ancestor_map(working)
        for old_task in previous.tasks:
            removed = set(old_task.outputs) - set(remaining[old_task.task_id].outputs)
            for consumer in previous.tasks:
                for item in removed.intersection(consumer.inputs):
                    if not any(item in producer.outputs and producer.task_id in closure[consumer.task_id]
                               for producer in working.tasks):
                        _reject("文档作者转换会使原下游输入失去实际前驱产物，不能保留语义通过但运行缺失的契约。")
        changed = True
    return working if changed else plan
