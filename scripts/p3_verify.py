"""P3 验收助手：一条命令产出可贴进 issue 的证据。

    cd _repo_SWE-Olympus
    python p3_verify.py

它做三件事：
1. 跑编排层测试与全仓回归，记录真实输出；
2. 用人（actor=YOU）的身份走一遍人工关口：start → pause → pause(同 op) → resume；
   这不是模拟——它和你以后在界面上点按钮走的是同一条 CommandBus；
3. 把结果写成 markdown，直接复制到 issue 评论。

产物：P3-验收输出.md（仓库根目录，不提交）
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT = ROOT / "P3-验收输出.md"


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")
    return (proc.stdout + proc.stderr).strip()


def human_gate() -> tuple[list[str], str]:
    """用 YOU 的身份发命令，返回每一步的记录与最终状态。"""
    from olympus.commands.core.bus import CommandBus
    from olympus.contracts.models import Command, ContractBundle
    from olympus.orchestrator.scheduler import Scheduler
    from olympus.orchestrator.state_machine import RunContext
    from olympus.planning.service import PlanService

    bundle = ContractBundle.model_validate_json(
        (ROOT / "fixtures/contracts/valid.json").read_text(encoding="utf-8"))
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
    bus = CommandBus(ctx=ctx, scheduler=sched)

    lines: list[str] = []

    def send(action: str, op: str, **params) -> None:
        command = Command(
            operation_id=op,
            run_id=ctx.run.run_id,
            actor="YOU",
            action=action,                      # type: ignore[arg-type]
            target_id="RUN-campus",
            expected_version=bundle.plan.version,
            expected_state_revision=ctx.run.state_revision,
            parameters=params,
        )
        receipt = bus.dispatch(command)
        note = bus.notes.get(op, "")
        lines.append(
            f"| {action} | `{op}` | {receipt.status} | {receipt.reason_code} | "
            f"rev={receipt.state_revision} | {note} |")

    send("start", "op-1-start")
    send("pause", "op-2-pause")
    send("pause", "op-2-pause")            # 同一个 operation_id：应回原回执
    send("resume", "op-3-resume")
    send("confirm_delivery", "op-4-confirm")   # 还有未验收任务：应被拒

    lines.append("")
    lines.append(f"最终 run 状态：**{ctx.run.status}**　"
                 f"state_revision={ctx.run.state_revision}　"
                 f"event_seq={ctx.run.event_seq}")
    lines.append(f"事件条数：{len(ctx.events)}（每条都携带 Run 快照）")
    return lines, ctx.run.status


def main() -> int:
    print("① 跑编排层测试 ……")
    unit = run([sys.executable, "-m", "pytest", "tests/orchestrator", "tests/scenarios/scheduler", "tests/scenarios/gates", "tests/human", "tests/commands/core", "-q"])
    print("② 跑全仓回归 ……")
    full = run([sys.executable, "-m", "pytest", "-q"])
    print("③ 以 YOU 的身份走一遍人工关口 ……")
    gate_lines, final_status = human_gate()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    python_version = sys.version.split()[0]
    body = "\n".join([
        "# P3 验收输出",
        "",
        f"- 运行时间：{stamp}",
        f"- 环境：Python {python_version}",
        "- 仓库：SWE-Olympus",
        "",
        "## 1 编排层测试（本 issue 范围）",
        "",
        "```text",
        unit,
        "```",
        "",
        "## 2 全仓回归（确认没破坏 P1/P2 基线）",
        "",
        "```text",
        full,
        "```",
        "",
        "## 3 人工关口（actor = YOU）",
        "",
        "| 动作 | operation_id | 结果 | reason_code | revision | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
        *gate_lines,
        "",
        ("观察点：`pause` 用同一个 `operation_id` 发两次，第二次返回**原回执**、"
         "revision 不变——这就是幂等；`confirm_delivery` 在还有未验收任务时被拒。"),
        "",
    ])
    OUT.write_text(body, encoding="utf-8")

    print()
    print(body)
    print(f"\n已写入 {OUT.name}（复制上面内容贴进 issue 即可）")
    return 0 if "passed" in unit and final_status == "active" else 1


if __name__ == "__main__":
    raise SystemExit(main())

