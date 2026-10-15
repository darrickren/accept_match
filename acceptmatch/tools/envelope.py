"""统一返回结构（design §6.1）与工具层原因码（design §6.5）。

所有 Agent 工具和人工操作都返回 ToolResult：
- ok：请求的业务变更是否已执行
- reason_codes：拒绝原因或业务结果代码
- recorded：本次调用持久化的记录编号（ok 为 False 时也可能非空）
- allowed_next_actions / awaiting_human：由与工具校验共用的前置条件算出
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from acceptmatch.store.db import dumps, loads


class ToolCode(StrEnum):
    # 工具前置条件
    INVALID_STATE = "INVALID_STATE"
    EVALUATION_STALE = "EVALUATION_STALE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    NOT_TOP_QUALIFIED = "NOT_TOP_QUALIFIED"
    DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
    DECISION_NOT_INSPECT = "DECISION_NOT_INSPECT"
    DECISION_STALE = "DECISION_STALE"
    EVIDENCE_PACKAGE_MISMATCH = "EVIDENCE_PACKAGE_MISMATCH"
    CANDIDATES_REMAIN = "CANDIDATES_REMAIN"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    # 检查
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_OPEN = "TASK_NOT_OPEN"
    TAG_MISMATCH = "TAG_MISMATCH"
    FINDING_INCOMPLETE = "FINDING_INCOMPLETE"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    # 买家与预留
    ACTOR_NOT_ALLOWED = "ACTOR_NOT_ALLOWED"
    REQUEST_CONFLICT_POINT = "REQUEST_CONFLICT_POINT"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    UNKNOWN_BUYER = "UNKNOWN_BUYER"
    CONFIRMATION_NOT_FROM_BUYER = "CONFIRMATION_NOT_FROM_BUYER"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    QUOTE_NOT_OWNED = "QUOTE_NOT_OWNED"
    QUOTE_ITEM_MISMATCH = "QUOTE_ITEM_MISMATCH"
    QUOTE_NOT_ACTIVE = "QUOTE_NOT_ACTIVE"
    ACKNOWLEDGEMENT_INCOMPLETE = "ACKNOWLEDGEMENT_INCOMPLETE"
    CONDITION_VERSION_CHANGED = "CONDITION_VERSION_CHANGED"
    ITEM_NOT_AVAILABLE = "ITEM_NOT_AVAILABLE"
    # 交付（需求 11）
    RESERVATION_NOT_ACTIVE = "RESERVATION_NOT_ACTIVE"
    DELIVERY_MISMATCH = "DELIVERY_MISMATCH"
    CONDITION_MISMATCH = "CONDITION_MISMATCH"
    DELIVERY_CHECK_PENDING = "DELIVERY_CHECK_PENDING"
    DELIVERY_CHECK_NOT_PENDING = "DELIVERY_CHECK_NOT_PENDING"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    # 通用
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    SEED_CHANGED = "SEED_CHANGED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Awaiting(StrEnum):
    INSPECTOR = "INSPECTOR"
    BUYER = "BUYER"
    DELIVERY = "DELIVERY"
    OPERATOR = "OPERATOR"


@dataclass(frozen=True)
class Actor:
    actor_type: str  # operator / buyer / inspector / delivery / agent / system
    actor_id: str


AGENT = Actor("agent", "SYN-AGENT")


@dataclass(frozen=True)
class NextAction:
    tool: str
    args: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": dict(self.args)}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    request_id: str | None
    reason_codes: tuple[str, ...] = ()
    data: dict[str, Any] | None = None
    recorded: tuple[str, ...] = ()
    replayed: bool = False
    request_status: str | None = None
    allowed_next_actions: tuple[NextAction, ...] = ()
    awaiting_human: str | None = None
    message: str | None = None
    synthetic: bool = field(default=True)

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好的结构：金额、概率为两位小数字符串，枚举为代码。"""
        return loads(
            dumps(
                {
                    "ok": self.ok,
                    "tool": self.tool,
                    "request_id": self.request_id,
                    "replayed": self.replayed,
                    "reason_codes": list(self.reason_codes),
                    "data": self.data,
                    "recorded": list(self.recorded),
                    "request_status": self.request_status,
                    "allowed_next_actions": [a.to_dict() for a in self.allowed_next_actions],
                    "awaiting_human": self.awaiting_human,
                    "message": self.message,
                    "synthetic": self.synthetic,
                }
            )
        )


class Rejection(Exception):
    """在写事务内抛出：回滚所有业务写入，然后单独记录拒绝审计。"""

    def __init__(self, *codes: str, message: str | None = None):
        if not codes:
            raise ValueError("Rejection 至少需要一个原因码")
        self.codes = tuple(str(c) for c in codes)
        self.message = message
        super().__init__(f"{', '.join(self.codes)}: {message or ''}")
