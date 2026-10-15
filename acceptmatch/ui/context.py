"""页面运行环境：配置、数据库连接、工具运行时和编排器。

页面只通过这里拿到的服务对象工作：
- 写操作：human_ops（人工操作）、flow.continue_after（Agent 运行）、reset_demo（重置）
- 读操作：ui.views（只读查询）
页面代码中不出现 SQL，也不直接写数据库。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from acceptmatch.agent.orchestrator import Orchestrator
from acceptmatch.config import Settings, load_settings
from acceptmatch.seed import SeedBundle, load_seed
from acceptmatch.services.reset import InitStatus, ResetResult, ensure_initialized, reset_demo
from acceptmatch.store.db import connect
from acceptmatch.tools.runtime import Runtime


@dataclass
class AppContext:
    settings: Settings
    conn: sqlite3.Connection
    rt: Runtime
    orch: Orchestrator
    init: InitStatus

    def loader(self) -> SeedBundle:
        return load_seed(self.settings.seed_dir)

    def reset(self, parameter_set: str = "default") -> ResetResult:
        result = reset_demo(self.conn, parameter_set, loader=self.loader)
        self.rt.bundle = result.bundle  # 重置后使用刚校验过的种子
        return result


def open_context(settings: Settings | None = None) -> AppContext:
    settings = settings or load_settings()
    conn = connect(settings.db_path)
    init = ensure_initialized(conn, loader=lambda: load_seed(settings.seed_dir))
    rt = Runtime(conn, init.bundle)
    return AppContext(settings=settings, conn=conn, rt=rt, orch=Orchestrator(rt, settings), init=init)
