"""A deliberately small trusted-local code profile, NOT a hostile-process sandbox."""

import ast

FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview",
}


def validate_python(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(a.name.startswith("_") or a.name == "*" for a in node.names):
                raise ValueError("private or wildcard imports are not registered")
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("relative imports are outside the local execution profile")
            lock_import = (isinstance(node, ast.ImportFrom) and node.module == "threading" and
                           all(a.name in {"Lock", "RLock"} for a in node.names))
            if not lock_import and any(name != "math" for name in modules):
                raise ValueError("only the math import is registered for implementation code")
        if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in FORBIDDEN_NAMES):
            raise ValueError("dynamic evaluation or privileged builtin is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private/introspection attributes are not allowed")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            raise ValueError("introspection strings are not allowed")
