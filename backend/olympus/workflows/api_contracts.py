"""Conservative AST checks for explicitly declared project API names; never execute source."""

import ast
from dataclasses import dataclass

from olympus.agents.runtime import OutputSchemaError


def _module(path):
    return path.replace("\\", "/").removesuffix(".py").replace("/", ".")


def _raise(problems):
    if problems:
        hints = list(dict.fromkeys(problems))[:8]
        error = OutputSchemaError("source violates the declared project API contract", hint="；".join(hints))
        error.validation_hints = tuple(hints)
        raise error


@dataclass(frozen=True)
class _Module:
    name: str


@dataclass(frozen=True)
class _Class:
    module: str
    name: str


@dataclass(frozen=True)
class _Instance:
    cls: _Class


@dataclass(frozen=True)
class _Method:
    cls: _Class
    name: str


@dataclass
class _Function:
    node: object
    closure: dict


@dataclass
class _LocalObject:
    attributes: dict


class _Analyzer:
    def __init__(self, contracts):
        self.modules = {_module(path): classes for path, classes in contracts.items()}
        self.problems, self.active = [], set()

    def known_module(self, name):
        return name in self.modules or any(module.startswith(name + ".") for module in self.modules)

    def export(self, module, name, node):
        classes = self.modules.get(module, {})
        if name in classes:
            return _Class(module, name)
        if self.known_module(module + "." + name):
            return _Module(module + "." + name)
        if not self.known_module(module):
            return None
        available = [module + "." + item for item in classes]
        if not available:
            available = sorted(item for item in self.modules if item.startswith(module + "."))
        self.problems.append(f"第 {node.lineno} 行：{module}.{name} 不是已声明导出；"
                             "正式导出为 " + "、".join(available) + "。请使用正式名称，不要新增兼容别名。")
        primary = [item for item in classes if not item.endswith(("Error", "Exception"))]
        # Retain an intended receiver only when the module has one unambiguous
        # service class, so one correction can report its other misspelled calls.
        return _Class(module, primary[0]) if len(primary) == 1 and not name.endswith(("Error", "Exception")) else None

    def method(self, symbol, node):
        methods = self.modules[symbol.cls.module][symbol.cls.name]
        if symbol.name not in methods and not symbol.cls.name.endswith(("Error", "Exception")):
            prefix = symbol.cls.module + "." + symbol.cls.name + "."
            allowed = "、".join(prefix + name for name in methods) or "无已声明公开方法"
            self.problems.append(f"第 {node.lineno} 行：{prefix}{symbol.name} 不在接口契约中；正式方法为 {allowed}。"
                                 "请修改调用以匹配正式方法，不要给实现新增兼容方法或别名。")

    def expression(self, node, env):
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            value = self.expression(node.value, env)
            if isinstance(value, _Module):
                return self.export(value.name, node.attr, node)
            if isinstance(value, (_Class, _Instance)):
                return _Method(value.cls if isinstance(value, _Instance) else value, node.attr)
            if isinstance(value, _LocalObject):
                return value.attributes.get(node.attr)
            return None
        if isinstance(node, ast.Call):
            function = self.expression(node.func, env)
            args = [self.expression(argument, env) for argument in node.args]
            keywords = {item.arg: self.expression(item.value, env) for item in node.keywords if item.arg}
            if isinstance(function, _Class):
                return _Instance(function)
            if isinstance(function, _Method):
                self.method(function, node)
            elif isinstance(function, _Function):
                return self.function(function, args, keywords)
            return None
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(self.expression(item, env) for item in node.elts)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            symbols = [self.expression(operand, env) for operand in operands]
            for index, operation in enumerate(node.ops):
                if not isinstance(operation, (ast.Is, ast.IsNot)):
                    continue
                for position, other in ((index, index + 1), (index + 1, index)):
                    operand = operands[position]
                    method = symbols[position]
                    if (isinstance(operand, ast.Attribute) and isinstance(method, _Method)
                            and method.name in self.modules[method.cls.module][method.cls.name]
                            and isinstance(self.expression(operand.value, env), _Instance)
                            and not isinstance(operands[other], ast.Constant)):
                        self.problems.append(f"第 {node.lineno} 行：不要通过 is/is not 比较实例的绑定方法 {method.name}；"
                                             "每次取方法属性可能产生新的对象。分别按签名直接调用公开方法。")
            return None
        for child in ast.iter_child_nodes(node):
            self.expression(child, env)
        return None

    def bind(self, target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = value if isinstance(value, tuple) and len(value) == len(target.elts) else [None] * len(target.elts)
            for child, item in zip(target.elts, values, strict=True):
                self.bind(child, item, env)
        elif isinstance(target, ast.Attribute):
            receiver = self.expression(target.value, env)
            if isinstance(receiver, _LocalObject):
                receiver.attributes[target.attr] = value

    def function(self, function, args=(), keywords=None, receiver=None):
        node = function.node
        if id(node) in self.active:
            return None
        self.active.add(id(node))
        try:
            env = dict(function.closure)
            parameters = [*node.args.posonlyargs, *node.args.args]
            for index, parameter in enumerate(parameters):
                value = args[index] if index < len(args) else (keywords or {}).get(parameter.arg)
                if index == 0 and receiver is not None:
                    value = receiver
                if value is None and parameter.annotation is not None:
                    annotation = self.expression(parameter.annotation, function.closure)
                    value = _Instance(annotation) if isinstance(annotation, _Class) else None
                env[parameter.arg] = value
            for parameter in node.args.kwonlyargs:
                env[parameter.arg] = (keywords or {}).get(parameter.arg)
            for parameter in (node.args.vararg, node.args.kwarg):
                if parameter is not None:
                    env[parameter.arg] = None
            returns = []
            self.statements(node.body, env, returns)
            return returns[0] if returns and all(value == returns[0] for value in returns) else None
        finally:
            self.active.remove(id(node))

    @staticmethod
    def merge(env, branches):
        for name in set().union(*branches):
            values = [branch.get(name) for branch in branches]
            env[name] = values[0] if all(value == values[0] for value in values) else None

    def statements(self, body, env, returns=None):
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name if alias.asname else alias.name.split(".")[0]
                    env[alias.asname or name] = _Module(name) if self.known_module(alias.name) else None
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    env[alias.asname or alias.name] = self.export(node.module or "", alias.name, node) if not node.level else None
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.expression(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self.bind(target, value, env)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = _Function(node, env)
                env[node.name] = function
                self.function(function)
            elif isinstance(node, ast.ClassDef):
                attributes = {}
                methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
                initializers = [item for item in methods if item.name in {"setUp", "__init__"}]
                for method in initializers:
                    self.function(_Function(method, env), receiver=_LocalObject(attributes))
                for method in methods:
                    if method not in initializers:
                        self.function(_Function(method, env), receiver=_LocalObject(dict(attributes)))
                env[node.name] = None
            elif isinstance(node, ast.Return):
                value = self.expression(node.value, env)
                if returns is not None:
                    returns.append(value)
                return
            elif isinstance(node, ast.If):
                self.expression(node.test, env)
                branches = [dict(env), dict(env)]
                for branch, statements in zip(branches, (node.body, node.orelse), strict=True):
                    self.statements(statements, branch, returns)
                self.merge(env, branches)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                value = self.expression(node.iter, env)
                local = dict(env)
                self.bind(node.target, value[0] if isinstance(value, tuple) and value and all(item == value[0] for item in value) else None, local)
                self.statements(node.body, local, returns)
                self.merge(env, [dict(env), local])
                self.statements(node.orelse, env, returns)
            elif isinstance(node, ast.While):
                self.expression(node.test, env)
                local = dict(env)
                self.statements(node.body, local, returns)
                self.merge(env, [dict(env), local])
                self.statements(node.orelse, env, returns)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                local = dict(env)
                for item in node.items:
                    self.expression(item.context_expr, env)
                    if item.optional_vars:
                        self.bind(item.optional_vars, None, local)
                self.statements(node.body, local, returns)
                env.update(local)
            elif isinstance(node, ast.Try):
                branches = [dict(env)]
                self.statements(node.body, branches[0], returns)
                self.statements(node.orelse, branches[0], returns)
                for handler in node.handlers:
                    local = dict(env)
                    if handler.name:
                        local[handler.name] = None
                    self.statements(handler.body, local, returns)
                    branches.append(local)
                self.merge(env, branches)
                self.statements(node.finalbody, env, returns)
            else:
                self.expression(node, env)


def validate_api_contracts(source: str, contracts: dict) -> None:
    if not contracts:
        return
    analyzer = _Analyzer(contracts)
    analyzer.statements(ast.parse(source).body, {})
    _raise(analyzer.problems)


def validate_implementation_contracts(source: str, path: str, contracts: dict) -> None:
    if not contracts:
        return
    normalized = path.replace("\\", "/")
    declared = next((classes for filename, classes in contracts.items()
                     if filename.replace("\\", "/") == normalized), None)
    if declared is None:
        return
    classes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}

    def methods(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen or name not in classes:
            return set()
        seen.add(name)
        node = classes[name]
        result = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for base in node.bases:
            if isinstance(base, ast.Name):
                result.update(methods(base.id, seen))
        return result

    problems = []
    for name, required in declared.items():
        if name not in classes:
            problems.append(f"{normalized} 必须直接声明正式类 {name}，不能用其他类名或兼容别名代替。")
            continue
        for method in required:
            if method not in methods(name):
                problems.append(f"{normalized} 缺少正式方法 {name}.{method}；请按接口契约声明该方法，不要新增替代别名。")
        for member in classes[name].body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [*member.args.posonlyargs, *member.args.args]
            if not positional or any(isinstance(item, ast.Name) and item.id == "staticmethod"
                                     for item in member.decorator_list):
                continue
            receiver = positional[0].arg
            for target in ast.walk(member):
                if (isinstance(target, ast.Attribute) and isinstance(target.ctx, ast.Store)
                        and isinstance(target.value, ast.Name) and target.value.id == receiver
                        and target.attr in required):
                    problems.append(f"{normalized} 第 {target.lineno} 行：{receiver}.{target.attr} 的赋值遮蔽了"
                                    f"公开方法 {name}.{target.attr}；请为存储字段另取名称，保留公开方法可调用。")
    _raise(problems)
