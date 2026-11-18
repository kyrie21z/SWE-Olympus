"""Bounded source facts derived by the trusted executor without importing task code."""

import ast
from collections.abc import Mapping
import hashlib
import json
from pathlib import PurePosixPath

from .profile import IMPLEMENTATION_FILE_SUFFIXES

MAX_SOURCE_FILE_BYTES = 262144
MAX_SOURCE_BYTES = 30 * MAX_SOURCE_FILE_BYTES


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    default_start = len(positional) - len(arguments.defaults)
    parameters = []

    def parameter(argument, kind, has_default=False):
        parameters.append({"name": argument.arg, "kind": kind,
                           "annotation": ast.unparse(argument.annotation) if argument.annotation else None,
                           "has_default": has_default})

    for index, argument in enumerate(positional):
        kind = "positional_only" if index < len(arguments.posonlyargs) else "positional_or_keyword"
        parameter(argument, kind, index >= default_start)
    if arguments.vararg:
        parameter(arguments.vararg, "var_positional")
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        parameter(argument, "keyword_only", default is not None)
    if arguments.kwarg:
        parameter(arguments.kwarg, "var_keyword")
    return {"parameters": parameters,
            "return_annotation": ast.unparse(node.returns) if node.returns else None}


def _python_facts(source: str) -> dict:
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or "").split(".")[0])
    public_symbols = []
    docstrings = {"module": ast.get_docstring(tree) or ""}
    signatures, annotations = {}, {}

    def declarations(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                annotations[prefix + node.target.id] = ast.unparse(node.annotation)
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
                continue
            name = prefix + node.name
            public_symbols.append(name)
            docstrings[name] = ast.get_docstring(node) or ""
            if isinstance(node, ast.ClassDef):
                declarations(node.body, name + ".")
            else:
                signatures[name] = _signature(node)

    declarations(tree.body)
    return {"import_roots": sorted(imports), "public_symbols": sorted(public_symbols), "docstrings": docstrings,
            "signatures": signatures, "annotations": annotations}


def build_source_metadata(files: Mapping[str, str]) -> dict[str, dict]:
    """Consume only the executor's already authorized, integrity-bound file map."""
    metadata, names, total = {}, set(), 0
    for relative, source in sorted(files.items()):
        path = PurePosixPath(relative)
        if (not relative or "\\" in relative or ":" in relative or path.is_absolute()
                or ".." in path.parts or relative != path.as_posix() or relative.casefold() in names):
            raise ValueError("source metadata requires unique normalized relative paths")
        if not isinstance(source, str):
            raise ValueError("source metadata requires UTF-8 source strings")
        data = source.encode("utf-8")
        total += len(data)
        if len(data) > MAX_SOURCE_FILE_BYTES or total > MAX_SOURCE_BYTES:
            raise ValueError("source metadata exceeds the local artifact size budget")
        names.add(relative.casefold())
        suffix = path.suffix.lower()
        if suffix not in IMPLEMENTATION_FILE_SUFFIXES:
            raise ValueError("source metadata file type is outside the local execution profile")
        entry = {"sha256": hashlib.sha256(data).hexdigest()}
        if suffix == ".py":
            entry.update(kind="python", **_python_facts(source))
        elif suffix in {".md", ".txt"}:
            entry.update(kind="text", text=source)
        elif suffix == ".json":
            entry["kind"] = "json"
            try:
                entry["data"] = json.loads(source)
            except json.JSONDecodeError as exc:
                raise ValueError(f"source metadata requires valid JSON in {relative}") from exc
        metadata[relative] = entry
    return metadata


CHECK_DRIVER = '''import json,runpy,sys
from types import MappingProxyType
sys.dont_write_bytecode=True
def freeze(value):
    if isinstance(value,dict):
        return MappingProxyType({key:freeze(item) for key,item in value.items()})
    if isinstance(value,list):
        return tuple(freeze(item) for item in value)
    return value
with open(sys.argv[3],encoding="utf-8") as stream:
    source_metadata=freeze(json.load(stream))
sys.path.insert(0,sys.argv[1])
script=sys.argv[2]
sys.argv=[script]
runpy.run_path(script,run_name="__main__",init_globals={"source_metadata":source_metadata})
'''
