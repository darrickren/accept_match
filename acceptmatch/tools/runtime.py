"""工具运行环境：数据库连接、已校验的种子（只读配置）和时钟。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from acceptmatch.domain.pricing import PriceRules
from acceptmatch.seed import ResolvedEconomics, SeedBundle
from acceptmatch.seed import convert
from acceptmatch.store import repo
from acceptmatch.store.audit import utc_now
from acceptmatch.store.repo import Meta
from acceptmatch.tools.envelope import Rejection, ToolCode


@dataclass
class Runtime:
    conn: sqlite3.Connection
    bundle: SeedBundle
    clock: Callable[[], str] = field(default=utc_now)

    def now(self) -> str:
        return self.clock()

    @property
    def price_rules(self) -> PriceRules:
        return convert.price_rules(self.bundle.dataset.price_rules)

    def base_price(self, product_id: str) -> Decimal:
        return self.bundle.product(product_id).base_price

    def economics(self, meta: Meta) -> ResolvedEconomics:
        return self.bundle.parameter_set(meta.parameter_set)

    def require_meta(self) -> Meta:
        """返回 meta，并确认内存中的种子与上次重置时使用的一致。"""
        meta = repo.get_meta(self.conn)
        if meta is None:
            raise Rejection(ToolCode.INVALID_STATE, message="演示尚未初始化，请先重置")
        if meta.seed_hash != self.bundle.seed_hash:
            raise Rejection(ToolCode.SEED_CHANGED, message="种子文件在上次重置后被修改，请先重置演示")
        return meta

    def buyer_exists(self, buyer_id: str) -> bool:
        return any(b.buyer_id == buyer_id for b in self.bundle.dataset.buyers)

    def product_exists(self, product_id: str) -> bool:
        return any(p.product_id == product_id for p in self.bundle.dataset.products)

    def inspection_method(self, point: str) -> str:
        for m in self.bundle.dataset.inspection_methods:
            if m.point == point:
                return m.method
        raise KeyError(point)
