"""Independent, inspectable acceptance definitions produced before implementation."""

import ast
import json
import operator
import re
from collections import Counter, deque
from collections.abc import Callable
from pathlib import PurePosixPath

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import OutputSchemaError, RolePermissions, RoleRunInput
from olympus.contracts.models import fingerprint
from olympus.execution.policy import FORBIDDEN_NAMES
from olympus.execution.profile import (
    IMPLEMENTATION_TASK_TYPES,
    LOCAL_EXECUTION_CAPABILITIES,
    READ_ONLY_TASK_TYPES,
    execution_profile_issues,
)
from olympus.planning.validator import ancestor_map

from .check_scripts import assemble_check_scripts
from .api_contracts import validate_api_contracts


def _attribute_hint(node):
    name = node.attr if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", node.attr) else "受限属性"
    if node.attr == "__class__":
        alternative = "判断返回值是否字典请用 isinstance(result, dict) 或 type(result) is dict；不要读取 result.__class__。"
    elif node.attr == "__dict__":
        alternative = "不要读取或修改对象内部状态；通过构造函数及公开方法建立状态，再调用公开查询方法比较前后快照。"
    elif node.attr == "__annotations__":
        alternative = "不要读取注解元数据；直接调用公开方法，对实际参数校验、返回值及其类型作断言。"
    elif node.attr in {"__name__", "__module__"}:
        alternative = "不要通过对象名称或所属模块推断行为；直接导入已声明的公开接口并验证调用结果。"
    elif node.attr == "__doc__":
        alternative = "检查文档请读取宿主提供的 source_metadata[相对文件路径]['docstrings'][公开符号限定名]；"
        alternative += "不要直接访问 __doc__，不要使用反射或文件读取。"
    else:
        alternative = "不要读取或手工改写内部实现；直接调用公开接口并验证返回值，通过公开操作构造测试所需状态。"
    return f"第 {node.lineno} 行禁止访问属性 {name}。{alternative} 其他以下划线开头的属性及系统属性仍不允许访问。"


def _metadata_root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return isinstance(node, ast.Name) and node.id == "source_metadata"


def _metadata_write(node):
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return isinstance(node.ctx, (ast.Store, ast.Del)) and _metadata_root(node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == "source_metadata"
    if isinstance(node, ast.arg):
        return node.arg == "source_metadata"
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == "source_metadata"
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return node.name == "source_metadata"
    if isinstance(node, ast.MatchMapping):
        return node.rest == "source_metadata"
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
            _metadata_root(node.func.value) and node.func.attr in {
                "clear", "update", "setdefault", "pop", "popitem", "append", "extend", "insert",
                "remove", "sort", "reverse", "add", "discard", "difference_update", "intersection_update",
                "symmetric_difference_update",
            })


def _receiver_rebound(method, receiver):
    name = receiver.arg
    for node in ast.walk(method):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, ast.arg) and node is not receiver and node.arg == name:
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not method and node.name == name:
            return True
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            return True
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return True
    return False


def _local_test_helper_calls(tree):
    """Permit direct calls to helpers defined on the current local test class.

    This does not permit private data, implementation methods, bound-method
    references, receiver aliases/rebinding, dunders or helper-body violations.
    """
    modules, test_cases = {}, set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"unittest", "unittest.case"}:
                    modules[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else "unittest"
        elif isinstance(node, ast.ImportFrom) and node.module in {"unittest", "unittest.case"} and not node.level:
            for alias in node.names:
                if alias.name == "TestCase":
                    test_cases.add(alias.asname or alias.name)

    def testcase_base(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.insert(0, node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return False
        if not parts:
            return node.id in test_cases
        return ".".join([modules.get(node.id, ""), *parts]) in {"unittest.TestCase", "unittest.case.TestCase"}

    permitted = set()
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        if not any(testcase_base(base) for base in cls.bases):
            continue
        methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        helpers = {method.name for method in methods if method.name.startswith("_") and
                   not method.name.startswith("__") and len(method.name) > 1 and not method.decorator_list}
        for method in methods:
            args = [*method.args.posonlyargs, *method.args.args]
            if not args or method.decorator_list or _receiver_rebound(method, args[0]):
                continue
            receiver = args[0].arg

            class Calls(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    pass  # A nested function does not inherit the method-call exception.

                visit_AsyncFunctionDef = visit_FunctionDef
                visit_ClassDef = visit_FunctionDef
                visit_Lambda = visit_FunctionDef

                def visit_Call(self, node):
                    func = node.func
                    if (isinstance(func, ast.Attribute) and isinstance(func.ctx, ast.Load) and
                            isinstance(func.value, ast.Name) and func.value.id == receiver and func.attr in helpers):
                        permitted.add(func)
                    self.generic_visit(node)

            visitor = Calls()
            for statement in method.body:
                visitor.visit(statement)
    return permitted


def _allowed_import_roots(scopes):
    roots = set()
    for scope in scopes:
        path = PurePosixPath(scope.replace("\\", "/"))
        if not path.parts or (path.suffix and path.suffix != ".py"):
            continue
        root = path.parts[0]
        if len(path.parts) == 1 and path.suffix == ".py":
            root = path.stem
        if root.isidentifier() and not root.startswith("_"):
            roots.add(root)
    return {"unittest", "math", "threading", "concurrent", *roots}


def _import_hint(node, names, allowed):
    labels = [name if len(name) <= 120 and all(part.isidentifier() for part in name.split("."))
              else "未注册模块" for name in names]
    rejected = "、".join(labels[:8])
    hint = f"第 {node.lineno} 行导入 {rejected} 不符合当前执行配置。"
    if "typing" in names:
        hint += "验收脚本不需要导入 typing；请直接调用公开接口、用内置类型判断或读取 source_metadata。"
    hint += "允许的导入根为：" + "、".join(sorted(allowed)) + "。"
    hint += "只有可运行的 .py 文件或包目录可提供实现模块，.pyi 类型存根及文档不能作为运行时模块导入。"
    return hint


def _metadata_example_path(scopes):
    return next((scope.replace("\\", "/") for scope in scopes if PurePosixPath(scope).suffix == ".py"),
                "<实际 .py 相对路径>")


def _metadata_import_hint(node, scopes):
    path = json.dumps(_metadata_example_path(scopes), ensure_ascii=False)
    return (f"第 {node.lineno} 行不能导入 source_metadata：它不是模块，而是执行宿主预注入的全局只读映射。"
            f"删除 import source_metadata / from source_metadata import ...，直接读取 source_metadata[{path}]。"
            f"例如 source_metadata[{path}]['public_symbols'] 是公开符号名称元组，不是模块属性；"
            "['docstrings'] 是限定名称到字符串的映射；['signatures'][限定名称]['parameters'] 是参数记录元组，"
            "每条参数记录按 ['name']、['kind']、['annotation']、['has_default'] 索引。不得重新绑定或修改该全局证据。")


_RUNTIME_GUARD = object()
_UNPROVEN_GUARD = object()


def _guard_value(node, bindings):
    """Bounded literal/data-flow facts only; never evaluate source expressions."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        pass
    if isinstance(node, ast.Name):
        return bindings.get(node.id, _RUNTIME_GUARD)
    if isinstance(node, (ast.BoolOp, ast.Compare)) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
        return _guard_truth(node, bindings)
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        values = [_guard_value(node.value, bindings)]
        if isinstance(node, ast.Subscript):
            values.append(_guard_value(node.slice, bindings))
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        values = [_guard_value(node.func.value, bindings), *[_guard_value(arg, bindings) for arg in node.args],
                  *[_guard_value(item.value, bindings) for item in node.keywords]]
    else:
        return _UNPROVEN_GUARD
    if any(value is _UNPROVEN_GUARD for value in values):
        return _UNPROVEN_GUARD
    return _RUNTIME_GUARD if any(value is _RUNTIME_GUARD for value in values) else _UNPROVEN_GUARD


def _guard_truth(node, bindings):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _guard_truth(node.operand, bindings)
        return value if value is _RUNTIME_GUARD or value is _UNPROVEN_GUARD else not value
    if isinstance(node, ast.BoolOp):
        values = [_guard_truth(item, bindings) for item in node.values]
        if isinstance(node.op, ast.And) and any(value is False for value in values):
            return False
        if isinstance(node.op, ast.Or) and any(value is True for value in values):
            return True
        if any(value is _UNPROVEN_GUARD for value in values):
            return _UNPROVEN_GUARD
        if any(value is _RUNTIME_GUARD for value in values):
            return _RUNTIME_GUARD
        return isinstance(node.op, ast.And)
    if isinstance(node, ast.Compare):
        values = [_guard_value(value, bindings) for value in [node.left, *node.comparators]]
        comparisons = []
        for left, right, operation in zip(values[:-1], values[1:], node.ops, strict=True):
            if left is _UNPROVEN_GUARD or right is _UNPROVEN_GUARD:
                comparisons.append(_UNPROVEN_GUARD)
            elif left is _RUNTIME_GUARD or right is _RUNTIME_GUARD:
                comparisons.append(_RUNTIME_GUARD)
            else:
                compare = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
                           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}.get(type(operation))
                try:
                    comparisons.append(bool(compare(left, right)) if compare else _UNPROVEN_GUARD)
                except (TypeError, ValueError):
                    comparisons.append(_UNPROVEN_GUARD)
        if any(value is False for value in comparisons):
            return False
        if any(value is _UNPROVEN_GUARD for value in comparisons):
            return _UNPROVEN_GUARD
        return _RUNTIME_GUARD if any(value is _RUNTIME_GUARD for value in comparisons) else True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not any(isinstance(item, ast.Starred) for item in node.elts):
        return bool(node.elts)
    value = _guard_value(node, bindings)
    return value if value is _RUNTIME_GUARD or value is _UNPROVEN_GUARD else bool(value)


def _conditional_failure_assertions(tree):
    """Permit explicit failure only on a reachable, data-dependent if path."""
    permitted = set()

    def bind(target, value, bindings):
        for name in ast.walk(target):
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                bindings[name.id] = value
            elif isinstance(target, (ast.Attribute, ast.Subscript)) and isinstance(name, ast.Name):
                bindings[name.id] = _UNPROVEN_GUARD

    def visit(statements, bindings, guarded=False, safe=True):
        for node in statements:
            if isinstance(node, ast.Assert):
                if guarded and safe and isinstance(node.test, ast.Constant) and node.test.value is False:
                    permitted.add(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                value = (_UNPROVEN_GUARD if isinstance(node, ast.AugAssign) or node.value is None
                         else _guard_value(node.value, bindings))
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    bind(target, value, bindings)
            elif isinstance(node, ast.If):
                truth = _guard_truth(node.test, bindings)
                if truth is True or truth is False:
                    if visit(node.body if truth else node.orelse, bindings, guarded, safe):
                        return True
                else:
                    branches = [dict(bindings), dict(bindings)]
                    ends = [visit(body, branch, True, safe and truth is _RUNTIME_GUARD)
                            for body, branch in zip((node.body, node.orelse), branches, strict=True)]
                    for key in set(branches[0]) | set(branches[1]):
                        left, right = (branch.get(key, _RUNTIME_GUARD) for branch in branches)
                        same = left is right or (type(left) is type(right) and left == right)
                        bindings[key] = left if same else _UNPROVEN_GUARD
                    if all(ends):
                        return True
            elif isinstance(node, (ast.For, ast.While)):
                value = _guard_value(node.iter, bindings) if isinstance(node, ast.For) else _guard_truth(node.test, bindings)
                known = value is not _RUNTIME_GUARD and value is not _UNPROVEN_GUARD
                if not (known and not bool(value)):
                    loop_bindings = dict(bindings)
                    if isinstance(node, ast.For):
                        bind(node.target, _RUNTIME_GUARD if value is _RUNTIME_GUARD else _UNPROVEN_GUARD, loop_bindings)
                    visit(node.body, loop_bindings, guarded, safe and value is not _UNPROVEN_GUARD)
                    for key in loop_bindings:
                        if key not in bindings or loop_bindings[key] is not bindings[key]:
                            bindings[key] = _UNPROVEN_GUARD
                visit(node.orelse, dict(bindings), guarded, safe)
                if isinstance(node, ast.While) and value is True:
                    return True  # A break-sensitive infinite loop is outside this narrow proof.
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local = dict(bindings)
                if not isinstance(node, ast.ClassDef):
                    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
                    for argument in arguments:
                        local[argument.arg] = _RUNTIME_GUARD
                visit(node.body, local, safe=safe)
                bindings[node.name] = _UNPROVEN_GUARD
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bindings[alias.asname or alias.name.split(".")[0]] = _UNPROVEN_GUARD
            elif isinstance(node, ast.Expr) and any(isinstance(item, ast.Call) for item in ast.walk(node)):
                for key in bindings:
                    bindings[key] = _UNPROVEN_GUARD
            elif isinstance(node, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                return True
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.Try, ast.Match, ast.AsyncFor)):
                # Complex reachability does not earn the literal-false exception.
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                        bindings[child.id] = _UNPROVEN_GUARD
        return False

    visit(tree.body, {})
    return permitted


def _metadata_schema_issues(tree):
    """Recognize signature-record membership, without guessing runtime values.

    Only uniquely bound assignment aliases are followed. Reassigned names,
    parameters and unknown expressions do not acquire a guessed metadata shape.
    """
    signature_keys = {"parameters", "return_annotation"}
    assignments, bound = {}, Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound[node.id] += 1
        elif isinstance(node, ast.arg):
            bound[node.arg] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] += 1
        elif isinstance(node, ast.alias):
            bound[node.asname or node.name.split(".")[0]] += 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bound[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound[node.rest] += 1
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    aliases = {name: value for name, value in assignments.items() if bound[name] == 1}

    def shape(node, seen=frozenset()):
        if len(seen) > 32:
            return None
        if isinstance(node, ast.Name):
            if node.id == "source_metadata":
                return "root"
            if node.id in aliases and node.id not in seen:
                return shape(aliases[node.id], seen | {node.id})
        elif isinstance(node, ast.Subscript):
            base = shape(node.value, seen)
            key = node.slice.value if isinstance(node.slice, ast.Constant) else None
            if base == "root":
                return "python" if isinstance(key, str) and PurePosixPath(key).suffix.lower() == ".py" else None
            if base == "signatures":
                return "signature"
            if base == "python" and key == "signatures":
                return "signatures"
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "keys" and not node.args and not node.keywords):
            return shape(node.func.value, seen)
        return None

    def hint(node):
        return (f"第 {node.lineno} 行：单个签名记录的固定键只有 parameters、return_annotation，不能把业务方法名当作记录键。"
                "检查方法存在请用 qualified_name in facts['signatures'] 或 facts['public_symbols']；"
                "随后 signature = facts['signatures'][qualified_name]，只读取 signature['parameters'] "
                "和 signature['return_annotation']；参数记录使用 parameters[0]['name']、['kind']、"
                "['annotation']、['has_default']。qualified_name 必须使用任务声明的真实限定名。")

    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for left, operation, right in zip([node.left, *node.comparators], node.ops, node.comparators):
                if (isinstance(operation, (ast.In, ast.NotIn)) and isinstance(left, ast.Constant)
                        and shape(right) == "signature" and left.value not in signature_keys):
                    issues.append(hint(node))
    return list(dict.fromkeys(issues))


def validate_check(source: str, scopes: list[str]) -> None:
    """A bounded trusted-local test profile, not a hostile-code OS sandbox."""
    if not isinstance(source, str):
        raise OutputSchemaError("acceptance script must be a string",
                                hint="checks 的每个值必须是包含 Python 源码的 JSON 字符串，不能是列表或对象。")
    if not source.strip():
        raise OutputSchemaError("acceptance script must not be empty",
                                hint="每个检查 ID 都需要非空 Python 脚本，并包含对公开接口实际返回值的断言。")
    if len(source.encode()) > 65536:
        raise OutputSchemaError("acceptance script must contain at most 64 KB of Python",
                                hint="每个检查脚本不得超过 65536 个 UTF-8 字节；删减重复代码并保留实际行为断言。")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise OutputSchemaError("acceptance script is not valid Python",
                                hint=f"检查脚本第 {exc.lineno or 1} 行存在 Python 语法错误；修复语法并以 JSON 字符串返回源码。") from exc
    allowed = _allowed_import_roots(scopes)
    local_helper_calls = _local_test_helper_calls(tree)
    conditional_failures = _conditional_failure_assertions(tree)
    import_hint = "仅可导入以下模块根及其公开子模块：" + "、".join(sorted(allowed)) + "。禁止 inspect、ast 等未注册模块。"
    assertions = 0
    problems = []

    def reject(message, *, hint):
        problems.append((message, hint))

    for hint in _metadata_schema_issues(tree):
        reject("acceptance uses an incompatible source metadata record", hint=hint)

    for node in ast.walk(tree):
        if _metadata_write(node):
            reject("acceptance attempts to change trusted source metadata",
                                    hint="source_metadata 是宿主提供的只读证据；不得重新绑定、遮蔽、赋值、删除或调用修改方法。"
                                         "请只读取其中的文件元数据并作断言，不能自行构造或修改验收证据。")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(name.split(".")[0] == "source_metadata" for name in names):
                reject("source_metadata is injected evidence, not an importable module",
                                        hint=_metadata_import_hint(node, scopes))
            if isinstance(node, ast.ImportFrom) and node.level:
                reject("acceptance uses an unregistered relative import",
                                        hint="不允许相对导入；请按任务 write_scope 使用完整模块路径。" + import_hint)
            rejected = [name for name in names if name.split(".")[0] not in allowed]
            if rejected:
                reject("acceptance uses an unregistered module",
                                        hint=_import_hint(node, rejected, allowed))
            if any(a.name.startswith("_") or a.name == "*" for a in node.names):
                reject("acceptance uses a private or wildcard import",
                                        hint="不允许导入 * 或以下划线开头的名称；请显式导入任务声明的公开类、函数或常量。")
        if isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES or
            (node.id.startswith("__") and node.id != "__name__")
        ):
            name = node.id if node.id in FORBIDDEN_NAMES else "双下划线标识符"
            reject("acceptance uses a privileged builtin",
                                    hint=f"禁止使用 {name}。不要使用 getattr、hasattr、dir、vars 等反射检查接口；"
                                         "直接导入公开类或函数、构造对象并调用声明的方法，对返回值和状态作断言。"
                                         "禁用名称：" + "、".join(sorted(FORBIDDEN_NAMES | {"__import__"})) +
                                         "。禁止文件读写、动态执行、进程和网络操作。")
        if isinstance(node, ast.Attribute) and node not in local_helper_calls and (node.attr.startswith("_") or node.attr in {
            "os", "sys", "subprocess", "socket", "builtins", "importlib", "pathlib", "shutil", "ctypes",
        }):
            reject("acceptance uses private reflection",
                                    hint=_attribute_hint(node))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "__" in node.value and node.value != "__main__":
                reject("acceptance uses an introspection string",
                                        hint="脚本字符串不得包含双下划线，唯一允许的例外是入口判断中的 '__main__'；"
                                             "不要用字符串构造私有属性名或反射访问内部实现。")
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node not in conditional_failures:
                reject("acceptance assertion cannot be an unconditional constant",
                                        hint="禁止 assert True、assert 1 等常量断言；请断言公开接口的实际返回值、状态或异常。"
                                             "负例请直接断言 assert not predicate，例如 assert 'initial' not in params；"
                                             "不要用复杂控制流中的 assert False 代替实际条件断言。")
            elif _guard_truth(node.test, {}) is True:
                reject("acceptance assertion is provably always true",
                       hint="断言不得使用 assert predicate or True、or 非空常量等恒真短路兜底；"
                            "删除恒真分支，直接断言实际返回值、文档内容或报告证据满足所需条件。")
            assertions += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assertions += int(node.func.attr.startswith("assert"))
    if not assertions:
        reject("acceptance must include actual assertions",
                                hint="脚本必须含有实际 assert 或 unittest 的 assert* 检查；print、注释或方法存在性声明不能替代行为验证。")
    if problems:
        hints = list(dict.fromkeys(hint for _, hint in problems))
        visible = hints[:8]
        suffix = f"；另有 {len(hints) - 8} 项不同违规提示。" if len(hints) > 8 else ""
        error = OutputSchemaError(problems[0][0], hint="；".join(visible) + suffix)
        error.validation_hints = tuple(visible)
        raise error


def _input_tasks(plan, task):
    """Include only producers whose files can reach this task through its inputs."""
    ancestors = ancestor_map(plan)
    producers = {}
    for item in plan.tasks:
        for output in item.outputs:
            producers.setdefault(output, []).append(item)
    found = {}
    pending = [task]
    while pending:
        consumer = pending.pop()
        for input_id in consumer.inputs:
            declared = producers.get(input_id, [])
            candidates = [item for item in declared if item.task_id != consumer.task_id and
                          item.task_id in ancestors.get(consumer.task_id, [])]
            closest = [item for item in candidates if not any(
                item.task_id in ancestors.get(other.task_id, []) for other in candidates if item != other
            )]
            if declared and not closest:
                raise OutputSchemaError("acceptance input is not supplied by a dependency ancestor",
                                        hint=f"任务 {consumer.task_id} 的输入 {input_id} 没有合法前驱提供；"
                                             "不能引用自身或后续任务的输出，请修正输入 ID 或依赖关系。")
            if len(closest) > 1:
                raise OutputSchemaError("acceptance input has ambiguous parallel producers",
                                        hint=f"任务 {consumer.task_id} 的输入 {input_id} 有多个并行前驱提供；"
                                             "请为各产物使用唯一 ID，或明确串行依赖以确定当前版本。")
            if closest and closest[0].task_id not in found:
                producer = closest[0]
                found[producer.task_id] = producer
                pending.append(producer)
    return [item for item in plan.tasks if item.task_id in found]


def _implementation_import_scopes(task, inputs):
    return [scope for item in [task, *inputs] if item.type in IMPLEMENTATION_TASK_TYPES and
            item.executor == "Hephaestus" for scope in item.write_scope]


def _available_api_contracts(plan, task, inputs):
    """Use the closest declared ABI for each physical file, never invent aliases."""
    ancestors = ancestor_map(plan)
    declarations = {}
    for item in [*inputs, task]:
        for filename, exports in (item.api_contracts or {}).items():
            declarations.setdefault(filename, []).append((item, exports))
    contracts = {}
    for filename, candidates in declarations.items():
        closest = [(item, exports) for item, exports in candidates if not any(
            item.task_id in ancestors.get(other.task_id, []) for other, _ in candidates if item != other
        )]
        if any(exports != closest[0][1] for _, exports in closest[1:]):
            raise OutputSchemaError("available API contracts have ambiguous parallel declarations",
                                    hint="同一输入文件存在不同的并行 api_contracts；请明确唯一前驱版本，不得合并猜测接口别名。")
        contracts[filename] = closest[0][1]
    return contracts


def verify_acceptance_checkpoint(baseline, completed):
    """Verify immutable historical evidence without applying today's script policy."""
    old = {task.task_id: task for task in baseline.tasks}
    if not isinstance(completed, dict) or set(completed) - set(old):
        raise OutputSchemaError("baseline acceptance checkpoint cannot be safely reused",
                                hint="旧冻结检查点包含无效任务编号，不能迁移。")
    verified = {}
    for task_id, raw in completed.items():
        try:
            frozen = FrozenAcceptance.model_validate(raw)
            frozen.verify(old[task_id])
        except (ValueError, TypeError) as exc:
            raise OutputSchemaError("baseline acceptance checkpoint cannot be safely reused",
                                    hint=f"任务 {task_id} 的旧冻结检查未通过原始指纹、作者或检查编号校验；"
                                         "已保留旧审计记录，不能迁移被修改的证据。") from exc
        verified[task_id] = frozen.model_dump(mode="json")
    return verified


def migrate_acceptance_checkpoint(baseline, plan, completed, binding):
    """Verify all history; apply current policy only to exact candidates for reuse."""
    if (binding != baseline.ref().model_dump(mode="json") or baseline.requirement != plan.requirement or
            baseline.design != plan.design):
        return {}
    verified = verify_acceptance_checkpoint(baseline, completed)
    old = {task.task_id: task for task in baseline.tasks}
    new = {task.task_id: task for task in plan.tasks}
    old_ancestors, new_ancestors = ancestor_map(baseline), ancestor_map(plan)

    def refs(tasks, identifiers):
        return {identifier: (tasks[identifier].version, tasks[identifier].content_hash) for identifier in identifiers}

    reused = {}
    for task_id, raw in verified.items():
        before, after = old.get(task_id), new.get(task_id)
        if before is None or after is None:
            continue
        if refs(old, [task_id]) != refs(new, [task_id]):
            continue
        if refs(old, old_ancestors[task_id]) != refs(new, new_ancestors[task_id]):
            continue
        frozen = FrozenAcceptance.model_validate(raw)
        frozen.verify(after)
        try:
            inputs = _input_tasks(plan, after)
            scopes = _implementation_import_scopes(after, inputs)
            contracts = _available_api_contracts(plan, after, inputs)
            for source in frozen.checks.values():
                validate_check(source, scopes)
                validate_api_contracts(source, contracts)
        except OutputSchemaError:
            # Intact historical scripts can become unsupported as policy evolves.
            # Retain their audit record, but require fresh Momus checks before use.
            continue
        reused[task_id] = frozen.model_dump(mode="json")
    return reused


def _upstream_print_evidence(task, inputs, frozen_inputs):
    """Prefer actual report inputs, then nearby ancestors, within whole-source bounds."""
    by_id = {item.task_id: item for item in inputs}
    distances, pending = {}, deque((identifier, 1) for identifier in task.depends_on)
    while pending:
        identifier, distance = pending.popleft()
        if identifier in distances:
            continue
        distances[identifier] = distance
        if identifier in by_id:
            pending.extend((parent, distance + 1) for parent in by_id[identifier].depends_on)
    declared_inputs = {value.replace("\\", "/") for value in task.inputs}

    def priority(indexed):
        index, item = indexed
        report_path = str(PurePosixPath(item.write_scope[0].replace("\\", "/")) / "check-report.json")
        direct = report_path in declared_inputs or bool(
            {value.replace("\\", "/") for value in item.outputs} & declared_inputs)
        return not direct, distances.get(item.task_id, len(inputs) + 1), index

    evidence, used = [], 0
    for _, item in sorted(enumerate(inputs), key=priority):
        if item.type not in READ_ONLY_TASK_TYPES or item.task_id not in frozen_inputs:
            continue
        frozen = FrozenAcceptance.model_validate(frozen_inputs[item.task_id])
        frozen.verify(item)
        for check_id, source in frozen.checks.items():
            tree = ast.parse(source)
            if not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
                       for node in ast.walk(tree)):
                continue
            size = len(source.encode("utf-8"))
            if len(evidence) >= 12 or used + size > 65536:
                continue  # Never truncate a script into misleading output evidence.
            evidence.append({"task_id": item.task_id, "check_id": check_id,
                             "report_path": str(PurePosixPath(item.write_scope[0].replace("\\", "/")) / "check-report.json"),
                             "source": source})
            used += size
    return evidence


def _task_manifest(requirement, design, task, inputs, *, frozen_inputs=None, api_contracts=None):
    scopes = _implementation_import_scopes(task, inputs)
    import_roots = sorted(_allowed_import_roots(scopes))
    schema = {
        "type": "object", "additionalProperties": False, "required": ["tasks"], "properties": {
            "tasks": {"type": "array", "minItems": 1, "maxItems": 1, "items": {
                "type": "object", "additionalProperties": False, "required": ["task_id", "checks"],
                "properties": {
                    "task_id": {"type": "string", "enum": [task.task_id]},
                    "setup": {"type": "string"},
                    "checks": {"type": "object", "additionalProperties": False,
                               "required": task.checks,
                               "properties": {check: {"type": "string"} for check in task.checks}},
                },
            }},
        },
    }
    manifest = {
        "requirement_content": requirement.content,
        "design_content": design.content,
        "tasks": [task.model_dump(mode="json")],
        "output_schema": schema,
        "allowed_import_roots": import_roots,
        "execution_capabilities": LOCAL_EXECUTION_CAPABILITIES,
        "api_contracts": api_contracts if api_contracts is not None else {
            path: exports for item in [*inputs, task] for path, exports in (item.api_contracts or {}).items()
        },
        "source_metadata_schema": {
            "global": "source_metadata", "access": "deeply read-only mapping indexed by relative file path",
            "file_fields": {"sha256": "string", "kind": "python | text | json",
                            "import_roots": "tuple of module root strings",
                            "public_symbols": "tuple of public qualified symbol strings",
                            "docstrings": "mapping: module, Class, Class.public_method, or public_function -> string",
                            "signatures": {
                                "public_qualified_callable": {
                                    "parameters": "tuple of {name, kind, annotation:string|null, has_default:boolean}; "
                                                  "includes self/cls as declared",
                                    "return_annotation": "unevaluated annotation string or null",
                                },
                            },
                            "annotations": "mapping of public module/class field qualified names to "
                                           "unevaluated annotation strings",
                            "data": "parsed actual JSON for .json files; objects are read-only mappings "
                                    "(not dict instances), arrays are tuples (not list instances)",
                            "text": "source text for .md/.txt files only"},
        },
        "source_metadata_example": {
            "note": "source_metadata is already a global mapping: DO NOT import it or use attribute access. "
                    "The example illustrates real shapes; use only symbols/requirements actually declared in this task.",
            "file_path": _metadata_example_path(scopes),
            "code": (
                "# No import source_metadata; the host already injects this global.\n"
                f"facts = source_metadata[{json.dumps(_metadata_example_path(scopes), ensure_ascii=False)}]\n"
                "symbols = facts['public_symbols']  # tuple[str, ...], membership uses names\n"
                "for symbol in symbols:\n"
                "    if symbol in facts['signatures']:\n"
                "        signature = facts['signatures'][symbol]\n"
                "        parameters = signature['parameters']  # tuple of read-only mappings\n"
                "        names = [parameter['name'] for parameter in parameters]\n"
                "        kinds = [parameter['kind'] for parameter in parameters]\n"
                "        assert all(isinstance(name, str) for name in names)\n"
                "        documentation = facts['docstrings'][symbol]  # str, not an attribute\n"
                "        assert isinstance(documentation, str)\n"
                "# Add the actual task-specific names/kinds/doc markers assertions.\n"
            ),
        },
        "available_inputs": [{
            "task_id": item.task_id, "goal": item.goal,
            "outputs": item.outputs, "write_scope": item.write_scope,
            "checks": item.checks, "api_contracts": item.api_contracts,
            "physical_report_paths": [str(PurePosixPath(item.write_scope[0].replace("\\", "/")) / "check-report.json")]
                                     if item.type in READ_ONLY_TASK_TYPES else [],
        } for item in inputs],
        "upstream_frozen_output_producers": _upstream_print_evidence(task, inputs, frozen_inputs or {}),
        "prompt": (
            "Momus: independently write executable Python acceptance checks BEFORE implementation. "
            "Return ONLY JSON {tasks:[{task_id:string,setup:optional_Python_source_string,"
            "checks:{check_id:Python_body_string}}]}. "
            "Cover ONLY the single task in tasks and exactly every task.checks entry. "
            "Put shared imports, fixtures and helper definitions in setup ONCE; return only the distinct "
            "executable body for each checks entry. Do not repeat a long test suite or shared helpers in every "
            "body. The host prepends setup to EACH body, validates and freezes each complete script, then "
            "runs each script independently. Each body must actually execute its relevant checks; do not "
            "rely on another body's globals or execution. Omit setup or use an empty string when unnecessary; "
            "never return null for setup. The 64 KB per-script limit applies after setup and body are joined. "
            "Use real assertions against the "
            "specified interfaces, edge cases and requirements, never assert True or author claims. "
            "For negative conditions use assert not predicate (for example assert 'initial' not in params), "
            "rather than literal assert False inside complex control flow. "
            "api_contracts gives authoritative exact file paths, exported class names and promised public "
            "method names. Use those names exactly; never invent aliases such as save versus save_document, "
            "search versus search_documents, or additional convenience methods. Exceptions may have an empty "
            "method list. Construct classes directly without testing or calling __init__ explicitly. "
            "available_inputs includes the complete approved ancestor checks and API declarations; honor their "
            "dependency-injection and return-value obligations, not guessed fixtures. "
            "Honor execution_capabilities: .pyi stubs and typing imports are unsupported; interface contracts "
            "must be executable .py artifacts. Read-only checking tasks do not author test modules or README "
            "files; they execute frozen checks and produce their actual report. "
            "The work directory contains this task's files and ancestor input files at write_scope paths. "
            "Import implementation modules by those paths. Test/integrate/doc tasks inspect ancestor "
            "outputs and do not author implementation. Import ONLY the named roots in allowed_import_roots "
            "(concurrent.futures is allowed), using public symbols from the current task/input paths. "
            "Do not import inspect or ast. Do not use getattr, hasattr, dir, vars, globals, locals, "
            "private/dunder attributes or any other reflection to check interface existence. "
            "For source facts (imports, public symbols, docstrings, README/run instructions), use the trusted "
            "source_metadata global described by source_metadata_schema and source_metadata_example. "
            "source_metadata IS NOT A MODULE: never import source_metadata or from source_metadata import anything. "
            "It is already injected as a deeply read-only GLOBAL MAPPING. Use dictionary indexing, not module "
            "attribute access. Index it by the actual declared "
            "relative path; inspect import_roots and public_symbols by membership, assert required documentation "
            "markers in the matching docstrings entry, and assert required instructions in a documentation file's "
            "text. For method parameter/name/kind checks, read signatures[public_qualified_name]['parameters']; "
            "each parameter has name, kind, annotation, has_default; self/cls are included as declared. "
            "Kinds are positional_only, positional_or_keyword, var_positional, keyword_only, var_keyword. "
            "Use signatures[public_qualified_name]['return_annotation'] or annotations for declared type syntax; "
            "annotations are unevaluated strings, not behavior evidence. Do not import typing to inspect them. "
            "For completed ancestor checks, use available_inputs.physical_report_paths and read "
            "source_metadata[actual_report_path]['data']; actual report fields are task, executor, checks, inputs, "
            "and check entries include check_id, exit_code, output. Do not assume nonexistent test_*.py files. "
            "JSON objects here are read-only mappings and JSON arrays are tuples: do NOT use "
            "isinstance(report, dict), isinstance(report['checks'], list) or unittest equivalents. "
            "Use key membership, len and iteration, e.g. assert 'checks' in report; "
            "checks = report['checks']; assert len(checks) > 0; "
            "assert all(check['exit_code'] == 0 for check in checks). Ordinary business API returns still "
            "need the declared dict type; this mapping/tuple rule applies only to source_metadata values. "
            "upstream_frozen_output_producers contains complete bounded Momus scripts that emit report output, "
            "not execution results. Match actual printed markers from those sources instead of guessing "
            "log format. Evidence prioritizes this task's directly declared report inputs and nearby ancestors. "
            "Each check's output format is independent: select the producer matching the exact report_path "
            "and check_id, then parse that entry's output using its own complete source. Never apply one "
            "check's count markers or template to every entry in a report, and never parse an integration "
            "report using an unrelated FR/NFR ancestor's printing format. Do not guess "
            "log format or require arbitrary digit substrings. Five rounds labeled 0 through 4 need not "
            "print a literal '5', and a 100-request test need not print the literal '100'. Verify actual "
            "round records, success/failure counts and invariant results using the emitted schema. "
            "Only ancestor reports are available; the current task's own report is written after its checks pass. "
            "Use public qualified symbol names from the supplied design and real paths from this task or "
            "available_inputs; do not invent file names, symbol names or documentation obligations. "
            "Do not access __doc__, __annotations__ or other private metadata; never rebind, shadow, construct "
            "or mutate source_metadata. Do not replace behavior tests with source metadata assertions. "
            "Instead import public classes/functions, construct the object, directly call its declared "
            "public methods and assert actual return values or state changes; use assertRaises for errors. "
            "When helper functions are needed for behavior tests, use public names such as make_service, "
            "not private names such as _make. Use public names for test fixture attributes and do not call "
            "private methods on implementation objects. Prefer plain top-level assertions over helper classes. "
            "For dictionary result types use isinstance(result, dict) or type(result) is dict, "
            "never result.__class__. Never edit internal object state to set up tests. "
            "A top-level local unittest.TestCase class may directly call its own undecorated, "
            "single-underscore instance helpers via its original receiver (self._helper(...)); "
            "define those helpers in that same class and do not reassign self. Helper bodies obey all "
            "the same restrictions. This does not permit calling private implementation methods or fields. "
            "No filesystem, network, processes, eval/exec or third-party dependencies. "
            "Do not import io, sys or contextlib to capture unittest output. To run a suite use "
            "result = unittest.TestResult(); suite.run(result); assert result.testsRun > 0; "
            "assert result.wasSuccessful(); print('tests_run', result.testsRun). Populate the suite with "
            "real tests declared in this script; do not load nonexistent test modules. "
            "Use top-level assertions, or unittest.main() guarded by __name__ == '__main__'. "
            "Each implementation task must be executable with its own declared inputs; "
            "Only available_inputs lists the upstream files supplied to this task; "
            "do not import code only produced by a future or unrelated task. Return JSON strings for scripts."
        ),
    }
    if task.type == "interface":
        manifest["prompt"] += (
            "\nINTERFACE STAGE: use only flat, top-level assertions on the pre-injected source_metadata mapping "
            "for this task's declared public symbols/signatures/docstrings/import facts. Do not import "
            "source_metadata, unittest or implementation modules. Do not create unittest classes, helper "
            "classes or helper functions (_make or otherwise). Do not test business behavior supplied only "
            "by later implementation tasks. Keep each check body short and specific to its declared check ID."
        )
    if task.type == "doc" or (task.type == "implement" and all(
            PurePosixPath(scope).suffix in {".md", ".txt"} for scope in task.write_scope)):
        manifest["prompt"] += (
            "\nDOCUMENTATION STAGE: verify the declared .md/.txt text through source_metadata and any declared "
            "ancestor report data only. Do not construct business services, dependency-injection fixtures, "
            "helper classes or mock stores merely to check written instructions. Keep these checks short "
            "and directly assert the required document content and evidence."
        )
    return manifest


def generate_acceptance(runner, requirement, design, plan, *, completed=None,
                        checkpoint: Callable[[str, dict], None] | None = None,
                        progress: Callable[[str, int, int], None] | None = None) -> dict[str, dict]:
    """Generate and freeze one task at a time, retaining validated completed tasks."""
    found = {}
    tasks = {task.task_id: task for task in plan.tasks}
    completed = completed or {}
    if set(completed) - set(tasks):
        raise OutputSchemaError("acceptance checkpoint contains an unknown task",
                                hint="已保存的验收定义必须绑定当前计划中的任务；请核对计划版本和验收检查点。")
    for task_id, raw in completed.items():
        task = tasks[task_id]
        acceptance = FrozenAcceptance.model_validate(raw)
        acceptance.verify(task)
        inputs = _input_tasks(plan, task)
        scopes = _implementation_import_scopes(task, inputs)
        contracts = _available_api_contracts(plan, task, inputs)
        for script in acceptance.checks.values():
            validate_check(script, scopes)
            validate_api_contracts(script, contracts)
        found[task_id] = acceptance.model_dump(mode="json")

    for task in plan.tasks:
        if task.task_id in found:
            continue
        if progress:
            progress(task.task_id, len(found), len(tasks))
        inputs = _input_tasks(plan, task)
        scopes = _implementation_import_scopes(task, inputs)
        contracts = _available_api_contracts(plan, task, inputs)
        manifest = _task_manifest(requirement, design, task, inputs, frozen_inputs=found, api_contracts=contracts)

        def validate(output):
            if not isinstance(output, dict) or set(output) != {"tasks"}:
                raise OutputSchemaError("acceptance output needs only tasks",
                                        hint="只返回 JSON 对象 {\"tasks\":[{\"task_id\":当前任务ID,\"checks\":{检查ID:Python源码字符串}}]}，"
                                             "顶层只能有 tasks 字段。")
            rows = output.get("tasks")
            if not isinstance(rows, list) or len(rows) != 1:
                raise OutputSchemaError("acceptance must cover the requested task exactly once",
                                        hint="tasks 必须是仅含一条记录的数组，记录只对应当前请求的单个任务。")
            row = rows[0]
            if (not isinstance(row, dict) or not {"task_id", "checks"} <= set(row) or
                    not set(row) <= {"task_id", "checks", "setup"}):
                raise OutputSchemaError("acceptance row needs task_id and checks",
                                        hint="tasks[0] 必须是对象，必需 task_id 和 checks 两个字段，另可选 setup 字符串字段。")
            if row["task_id"] != task.task_id:
                raise OutputSchemaError("acceptance must match the requested task",
                                        hint=f"tasks[0].task_id 必须原样使用当前任务 ID：{task.task_id}。")
            if not isinstance(row["checks"], dict):
                raise OutputSchemaError("acceptance checks must be an object",
                                        hint="checks 必须是以检查 ID 为键、Python 源码字符串为值的 JSON 对象，不能是数组。")
            if set(row["checks"]) != set(task.checks):
                raise OutputSchemaError("acceptance must cover exactly the approved task checks",
                                        hint="checks 的键必须与当前任务卡完全一致：" + "、".join(task.checks) + "。")
            assembled = assemble_check_scripts(row.get("setup", ""), row["checks"])
            problems = {}
            for check_id, script in assembled.items():
                try:
                    validate_check(script, scopes)
                    validate_api_contracts(script, contracts)
                except OutputSchemaError as exc:
                    for hint in getattr(exc, "validation_hints", (exc.hint,)):
                        problems.setdefault(hint, []).append(check_id)
            if problems:
                hints = [f"检查 {', '.join(dict.fromkeys(check_ids))}：{hint}"
                         for hint, check_ids in list(problems.items())[:8]]
                suffix = f"；另有 {len(problems) - 8} 项不同违规提示。" if len(problems) > 8 else ""
                raise OutputSchemaError("acceptance scripts violate the local execution profile",
                                        hint=f"任务 {task.task_id} 的检查脚本需修正：" + "；".join(hints) + suffix)
            return FrozenAcceptance.freeze(task, assembled).model_dump(mode="json")

        result = runner.run(RoleRunInput(
            role_id="Momus", task_kind="acceptance_definition", task_id=task.task_id,
            logical_task_id=f"acceptance-{task.task_id[:100]}",
            fixture_key=f"Momus:acceptance_definition:{task.task_id}",
            input_manifest=manifest, input_refs=[requirement.ref(), design.ref(), plan.ref()],
            output_schema=manifest["output_schema"],
            idempotency_key="acceptance-" + fingerprint({
                "plan": plan.ref().model_dump(mode="json"), "task_id": task.task_id,
            }),
            permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["acceptance_definition"],
                                        allowed_input_keys=list(manifest)),
        ), output_validator=validate)
        frozen = validate(result.structured_output)
        found[task.task_id] = frozen
        if checkpoint:
            checkpoint(task.task_id, frozen)
    return found


def validate_executable_plan(plan) -> None:
    issues = execution_profile_issues(plan)
    if issues:
        hints = [f"{item['task_id']} [{item['code']}]：{item['hint']}" for item in issues[:12]]
        remaining = f"；另有 {len(issues) - 12} 项执行配置问题。" if len(issues) > 12 else ""
        raise OutputSchemaError("plan violates local execution capabilities",
                                hint="计划必须先修复执行能力冲突：" + "；".join(hints) + remaining)
