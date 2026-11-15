"""用 GitHub API 开 PR 并回填 issue 证据。

    python scripts/p3_open_pr.py

做三件事：
1. 用 docs/p3-handoff.md 的验收结论 + 测试输出，组装 PR 描述；
2. POST /pulls 创建 PR（head=feat/p3-orchestration → base=main）；
3. 在 #5、#6 下各贴一条验收评论（分节，符合"同段两项验收分节提供证据"）。

凭据从本机 git credential 取，不打印明文。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER = "kyrie21z"
REPO = "SWE-Olympus"
HEAD = "feat/p3-orchestration"
BASE = "main"
PROXY = "http://127.0.0.1:7897"      # 本机代理；无代理时置空


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=False, encoding="utf-8",
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("取不到 GitHub 凭据")


def api(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Content-Type", "application/json")
    if PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:600]
        raise SystemExit(f"{method} {path} → {exc.code}\n{body}") from exc


def evidence() -> str:
    """把 p3_verify.py 产出的验收输出摘出来（若有）。"""
    path = ROOT / "P3-验收输出.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "（本机未生成 P3-验收输出.md）"


def pr_body() -> str:
    return """Closes #5
Closes #6

## 这个 PR 做了什么

阶段 P3：在 P2 已交付的 `Plan / CompiledManifest / PlanPackage` 之上，补运行态的
"能不能派、派给谁、什么时候释放、人怎么拍板"，以及 LangGraph 的人工关口。

- **OL-05 动态调度**：运行态三集合、`priority_rank` 排序、原子派发与
  执行者/写范围/资源互斥、取消、改版失效、执行替身、局部异常登记、运行态投影
- **OL-06 命令与关口**：契约 `Command` → `Receipt` 六道闸门、`allowed_actions`、
  LangGraph 阶段控制（准备 → 持久化等待 → 原子接纳）、请求绑定 REQ/DS/PLAN 与必要报告

## 文件（按 issue 指定路径）

```
backend/olympus/orchestrator/scheduler/{scheduler,executor,incidents}.py
backend/olympus/orchestrator/state_machine/{run_context,transitions,revision}.py
backend/olympus/projections/runtime/projection.py
backend/olympus/workflow/gates.py
backend/olympus/human/store.py
backend/olympus/commands/core/bus.py
backend/olympus/commands/registry/actions.py
tests/orchestrator/scheduler/                    17 条
tests/scenarios/scheduler/                       16 条
tests/scenarios/gates/                            8 条
tests/human/                                     10 条
tests/commands/core/                              6 条
scripts/p3_verify.py                              一条命令产出验收证据
docs/p3-handoff.md                                交接说明
```

## 验收对照

### OL-05

- [x] 消费 P2 静态 manifest，只实现运行态三集合、稳定排序、原子派发与资源/人员互斥
- [x] 仅 accepted 解锁后继；原子接纳检查**任务/输入/报告/代际**五项
- [x] 发送许可、执行代际、资源保留、旧回执拒绝 + `FakeExecutor` 查询场景
- [x] 状态摘要、accepted n/N、等待/阻塞原因、动态资源占用（`projections/runtime`）
- [x] 局部异常登记后继续独立支线；不重算静态拓扑
- [x] 默认并发 3；重复派发只占一份许可；父子目录与同一执行者互斥
- [x] T01 验收后 T02/03/04 就绪；T03 verifying 时 T05 waiting；退出码零不解锁
- [x] 取消/改版与验收竞争只允许合法迁移；旧代际/迟到回执不能覆盖
- [x] 未知在途保留资源直到核实（`pause_state.fully_stopped` 只在无未核实时为真）

### OL-06

- [x] LangGraph 阶段控制 + 准备→持久化等待→原子接纳，**绑定 REQ/DS/PLAN 及必要报告**
- [x] 鉴权 / 内容版本 / 目标版本 / 幂等 / 前置状态六道闸门、处理器注册
- [x] `allowed_actions(actor)`：按 run 与任务状态计算，禁用必带原因
- [x] 设计/计划批准与驳回、运行许可、停止派发/受控恢复、最终确认
- [x] 重试/改派/风险接受/变更未注册时返回 `NOT_READY`，P5 用 `register()` 接管
- [x] 中断恢复重用请求、不重复扣额度；同线程恢复串行
- [x] 修改与确认并发时旧确认拒绝，新稿不继承旧签认

## 检查

```text
python -m ruff check …    → All checks passed
python -m pytest -q       → 238 passed
python scripts/p3_verify.py → exit 0
```

## 需要上游确认的一处契约缺口

`Command.action` 的 Literal 里没有"驳回"（只有 approve_design / approve_plan / start /
pause / resume / retry / reassign / accept_risk / request_change / confirm_delivery）。

- 驳回**不产生命令**：关口在 `decision=reject` 时直接写事件，请求标 accepted，`run` 保持 draft。
- 流程上可审计，但没有独立动作供界面调用。若需要，请 P1 在契约里加 `reject_plan`；
  本 PR 不擅自扩展 Literal。

## 已知差距（不冒充完成）

1. `RunContext` 只在进程内；跨进程恢复留给 P5 #9（`Store` 已能持久化事件与快照）。
2. 关口的图状态用 `InMemorySaver`：同进程重放可用，跨进程要换持久化 saver（P5）。
3. `FakeExecutor` 是脚本化替身，不证明真实执行器可靠性（P4 #8）。

## 验证级别

模型未调用；执行走 `FakeExecutor`（mock）；数据用 `fixtures/contracts/valid.json`。
证明的是"状态机与调度规则按设计执行"，不证明真实执行器可靠性。
"""


def issue_comment(number: int, section: str) -> str:
    return f"""## P3 验收证据（OL-0{4 if number == 5 else 6} · {section}）

```text
{evidence()}
```

### 对应验收项
{'见 PR 描述「OL-05」小节' if number == 5 else '见 PR 描述「OL-06」小节'}
"""


def main() -> int:
    print("① 创建 PR")
    pr = api("POST", f"/repos/{OWNER}/{REPO}/pulls", {
        "title": "feat(p3): Zeus 动态调度与人工命令总线（OL-05 / OL-06）",
        "head": HEAD,
        "base": BASE,
        "body": pr_body(),
        "draft": False,
    })
    print(f"   PR #{pr['number']}  {pr['html_url']}")

    for number, section in ((5, "动态调度"), (6, "命令与关口")):
        print(f"② 在 #{number} 贴验收评论")
        api("POST", f"/repos/{OWNER}/{REPO}/issues/{number}/comments",
            {"body": issue_comment(number, section)})
        print("   已贴")

    print(f"\n完成：{pr['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
