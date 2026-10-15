"""价格规则（需求 9，design §4.2）。纯函数。

报价 = 基础价格 ×（1 − 所有 Verified_Defect 折扣率之和），不低于基础价格 × floor_ratio，
ROUND_HALF_UP 到 0.01。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from acceptmatch.domain.enums import DefectType, Point, ReasonCode, Severity
from acceptmatch.domain.models import DefectRecord, ItemSnapshot
from acceptmatch.domain.money import q


class PricingError(Exception):
    def __init__(self, code: ReasonCode, message: str, details: Sequence[str] = ()):
        self.code = code
        self.details = tuple(details)
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PriceRules:
    discount_by_severity: Mapping[Severity, Decimal]
    floor_ratio: Decimal
    currency: str = "CAD"


@dataclass(frozen=True)
class DiscountLine:
    defect_id: str
    point: Point
    defect_type: DefectType
    severity: Severity
    rate: Decimal
    amount: Decimal  # base_price × rate，四舍五入到 0.01


@dataclass(frozen=True)
class PriceResult:
    base_price: Decimal
    discounts: tuple[DiscountLine, ...]
    total_rate: Decimal
    floor_applied: bool
    amount: Decimal
    currency: str


def price(
    base_price: Decimal,
    defects: Sequence[tuple[Point, DefectRecord]],
    rules: PriceRules,
) -> PriceResult:
    missing = sorted({d.severity for _, d in defects if d.severity not in rules.discount_by_severity},
                     key=lambda s: s.rank)
    if missing:
        raise PricingError(
            ReasonCode.PRICE_RULE_MISSING,
            "价格规则中缺少这些程度的折扣率：" + "、".join(missing),
            details=missing,
        )

    lines = tuple(
        DiscountLine(
            defect_id=d.defect_id,
            point=p,
            defect_type=d.type,
            severity=d.severity,
            rate=rules.discount_by_severity[d.severity],
            amount=q(base_price * rules.discount_by_severity[d.severity]),
        )
        for p, d in defects
    )
    total_rate = sum((line.rate for line in lines), Decimal("0"))
    raw = base_price * (Decimal("1") - total_rate)
    floor = base_price * rules.floor_ratio
    floor_applied = raw < floor
    return PriceResult(
        base_price=base_price,
        discounts=lines,
        total_rate=total_rate,
        floor_applied=floor_applied,
        amount=q(floor if floor_applied else raw),
        currency=rules.currency,
    )


def price_item(
    item: ItemSnapshot,
    base_price: Decimal,
    rules: PriceRules,
    *,
    assume_unknown_ok: bool = False,
) -> PriceResult:
    """按商品当前的 Verified_Defect 计算价格。

    默认要求所有部位都已核实；有 UNKNOWN 部位时抛出 UNVERIFIED_POINTS。
    assume_unknown_ok=True 时把 UNKNOWN 部位视为无缺陷，只用于经济判断中的
    "通过后报价"（需求 4.3），不能用来生成 Quote。
    """
    unknown = item.unknown_points()
    if unknown and not assume_unknown_ok:
        raise PricingError(
            ReasonCode.UNVERIFIED_POINTS,
            f"{item.item_id} 有未核实的部位，不能报价：" + "、".join(unknown),
            details=unknown,
        )
    return price(base_price, item.verified_defects(), rules)
