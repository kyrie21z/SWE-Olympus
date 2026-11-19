"""Expand a compact model response into independently executable frozen checks."""

from collections.abc import Mapping

from olympus.agents.runtime import OutputSchemaError


def assemble_check_scripts(setup: str, checks: Mapping[str, str]) -> dict[str, str]:
    """Preserve source verbatim; callers validate each complete script before freezing."""
    if not isinstance(setup, str):
        raise OutputSchemaError("acceptance setup must be a string",
                                hint="可选 setup 必须是包含共享导入和辅助函数的 Python 源码字符串；未使用时省略或传空字符串。")
    if not isinstance(checks, Mapping):
        raise OutputSchemaError("acceptance checks must be an object",
                                hint="checks 必须是检查 ID 到独立检查主体源码字符串的对象。")
    assembled = {}
    for check_id, body in checks.items():
        if not isinstance(check_id, str) or not isinstance(body, str):
            raise OutputSchemaError("acceptance check IDs and bodies must be strings",
                                    hint="checks 的键必须是当前任务的检查 ID，值必须是 Python 源码字符串。")
        if not body.strip():
            raise OutputSchemaError("acceptance check body must not be empty",
                                    hint="每个 checks 条目都需要非空检查主体；共享 setup 不能替代各项实际检查。")
        assembled[check_id] = setup + "\n\n" + body if setup else body
    return assembled
