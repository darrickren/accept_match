"""Agent 调用守卫的辅助部分（design §7.5、§7.7）。

- 每个 Agent 工具的参数模型：不允许多余字段，编号必须符合合成数据格式。
- 数值守卫：Agent 文本中出现、但从未在本次运行的工具结果中出现过的数值，一律视为未经验证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from acceptmatch.tools import state as st

RATIONALE_MAX = 200

RequestId = Annotated[str, Field(pattern=r"^REQ-\d{4}$")]
ItemId = Annotated[str, Field(pattern=r"^AM-\d{3}$")]
DecisionId = Annotated[str, Field(pattern=r"^DEC-\d{4}$")]
PackageId = Annotated[str, Field(pattern=r"^EVP-\d{4}$")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: RequestId


class RequestArgs(_Args):
    pass


class ItemArgs(_Args):
    item_id: ItemId


class CreateTaskArgs(_Args):
    item_id: ItemId
    decision_id: DecisionId


class QuoteArgs(_Args):
    item_id: ItemId
    evidence_package_id: PackageId


ARG_MODELS: dict[str, type[_Args]] = {
    st.GET_STATUS: RequestArgs,
    st.EVALUATE: RequestArgs,
    st.DECIDE: ItemArgs,
    st.CREATE_TASK: CreateTaskArgs,
    st.BUILD_EVIDENCE: ItemArgs,
    st.GENERATE_QUOTE: QuoteArgs,
    st.FINALIZE_NO_MATCH: RequestArgs,
}


def validate_args(tool: str, args: Any) -> tuple[dict[str, Any] | None, str | None]:
    """返回 (规范化参数, 错误说明)。未知工具（包括人工操作）也算参数错误。"""
    model = ARG_MODELS.get(tool)
    if model is None:
        return None, f"未知的 Agent 工具：{tool}（人工操作不能由 Agent 调用）"
    if not isinstance(args, dict):
        return None, "参数必须是对象"
    try:
        return model.model_validate(args).model_dump(), None
    except ValidationError as exc:
        return None, "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


def clean_rationale(rationale: Any) -> tuple[str | None, bool]:
    """返回 (理由, 是否被截断)。理由为空时返回 None。"""
    if not isinstance(rationale, str) or not rationale.strip():
        return None, False
    text = " ".join(rationale.split())
    if len(text) > RATIONALE_MAX:
        return text[:RATIONALE_MAX] + "…", True
    return text, False


# ------------------------------------------------------------- 数值守卫

# 先去掉编号（AM-007、REQ-0001、SYN-T-Q5ZT、EV-AM007-FRONT-1、B-DEMO-01 等），再找数值
_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
_NUM_RE = re.compile(r"[-−]?\d+(?:\.\d+)?")

HIDDEN_TEXT = "说明中含有未经工具验证的数值，已隐藏"


def numbers_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for match in _NUM_RE.finditer(_ID_RE.sub(" ", text)):
        try:
            out.add(abs(Decimal(match.group().replace("−", "-"))))
        except InvalidOperation:
            continue
    return out


@dataclass(frozen=True)
class GuardedText:
    original: str
    display: str
    flagged: bool
    unverified: tuple[str, ...]


class NumberGuard:
    def __init__(self) -> None:
        self.allowed: set[Decimal] = set()

    def learn(self, text: str) -> None:
        self.allowed |= numbers_in(text)

    def check(self, text: str | None) -> GuardedText:
        text = text or ""
        unverified = sorted(numbers_in(text) - self.allowed)
        if unverified:
            return GuardedText(text, HIDDEN_TEXT, True, tuple(str(n) for n in unverified))
        return GuardedText(text, text, False, ())
