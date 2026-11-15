"""只读查询服务。

把 `Store` 的读取能力收成一个明确接口，让 HTTP 层只做协议转换。
三个边界在这里固定下来，路由层不再重复判断：

1. 运行不存在 → `RUN_NOT_FOUND`（404），不是 500，也不是空对象。
2. 游标为负 → `INVALID_CURSOR`（422），不做静默纠正。
3. 游标超过已提交序号 → `CURSOR_AHEAD`（409），要求重新取快照。

第 3 条就是"有界恢复"的服务端一半：宁可让客户端重取，也不返回一个
"看起来没有新事件"的空列表——那会让界面永远停在旧状态。
"""

from __future__ import annotations

import os
from pathlib import Path

from olympus.api.errors import CursorAhead, InvalidCursor, RunNotFound
from olympus.contracts.models import Event, Run
from olympus.storage.sqlite import Store


def default_db_path() -> Path:
    """数据库位置由环境变量决定，默认落在已被 .gitignore 排除的 .olympus/。"""
    return Path(os.environ.get("OLYMPUS_DB", ".olympus/state.sqlite3"))


def build_service(store: Store | None = None) -> RunQueryService:
    """构造服务；延迟到第一次请求时才打开数据库，避免导入即产生文件。"""
    return RunQueryService(store if store is not None else Store(default_db_path()))


class RunQueryService:
    """`Store` 上的一层只读门面；不写任何状态。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store

    def snapshot(self, run_id: str) -> Run:
        try:
            return self._store.snapshot(run_id)
        except KeyError as exc:
            raise RunNotFound(f"运行 {run_id} 不存在") from exc

    def events(self, run_id: str, after_seq: int) -> list[Event]:
        if after_seq < 0:
            raise InvalidCursor("after_seq 不能为负数", detail={"after_seq": after_seq})

        # 先确认运行存在，再比较游标：否则未知运行会被误报成 CURSOR_AHEAD。
        run = self.snapshot(run_id)
        if after_seq > run.event_seq:
            raise CursorAhead(
                "after_seq 超过服务端已提交序号，请重新获取快照",
                detail={"after_seq": after_seq, "latest_seq": run.event_seq},
            )
        return self._store.events(run_id, after_seq)

    def latest_seq(self, run_id: str) -> int:
        return self.snapshot(run_id).event_seq
