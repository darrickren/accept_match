"""种子文件的结构模型（Pydantic）。

只负责单条记录的结构和取值校验；跨记录的一致性检查在 loader.py 中完成。
金额和比例必须写成字符串（例如 "8.00"、"0.80"），避免浮点误差。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
)

from acceptmatch.domain.enums import (
    DefectType,
    EvidenceSource,
    ItemStatus,
    NoInspectionOptionKind,
    Point,
    PointStatus,
    Severity,
)

_MONEY_RE = re.compile(r"-?\d+\.\d{2}")
_RATIO_RE = re.compile(r"-?\d+(\.\d{1,2})?")


def _parse_money(value: object) -> Decimal:
    if not isinstance(value, str) or not _MONEY_RE.fullmatch(value):
        raise ValueError('金额必须是两位小数的字符串，例如 "8.00"')
    amount = Decimal(value)
    if amount < 0:
        raise ValueError("金额不能为负")
    return amount


def _parse_ratio(value: object) -> Decimal:
    if not isinstance(value, str) or not _RATIO_RE.fullmatch(value):
        raise ValueError('比例必须是最多两位小数的字符串，例如 "0.80"')
    ratio = Decimal(value)
    if not Decimal("0") <= ratio <= Decimal("1"):
        raise ValueError("比例必须在 0 到 1 之间")
    return ratio


def _must_be_true(value: bool) -> bool:
    if value is not True:
        raise ValueError("合成数据标记必须为 true")
    return value


Money = Annotated[Decimal, BeforeValidator(_parse_money)]
Ratio = Annotated[Decimal, BeforeValidator(_parse_ratio)]
Synthetic = Annotated[StrictBool, AfterValidator(_must_be_true)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- dataset.json


class DatasetMeta(_Model):
    dataset_id: NonEmptyStr
    version: int = Field(ge=1)
    notice: NonEmptyStr
    synthetic: Synthetic


class Product(_Model):
    product_id: Annotated[str, Field(pattern=r"^SYN-[A-Z0-9-]+$")]
    name: NonEmptyStr
    base_price: Money
    currency: Literal["CAD"]
    synthetic: Synthetic


class PriceRules(_Model):
    discount_by_severity: dict[Severity, Ratio]
    floor_ratio: Ratio
    synthetic: Synthetic


class InspectionMethod(_Model):
    point: Point
    method: NonEmptyStr
    synthetic: Synthetic


class Buyer(_Model):
    buyer_id: Annotated[str, Field(pattern=r"^B-[A-Z0-9-]+$")]
    label: NonEmptyStr
    synthetic: Synthetic


class Defect(_Model):
    defect_id: Annotated[str, Field(pattern=r"^DF-[A-Z0-9-]+$")]
    type: DefectType
    severity: Severity
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)


class PointRecord(_Model):
    status: PointStatus
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)
    defects: list[Defect] = Field(default_factory=list)


class Evidence(_Model):
    evidence_id: Annotated[str, Field(pattern=r"^EV-[A-Z0-9-]+$")]
    point: Point
    source: EvidenceSource
    inspector_id: Annotated[str, Field(pattern=r"^SYN-INSPECTOR-[0-9]+$")]
    recorded_at: AwareDatetime
    photo_ref: Annotated[str, Field(pattern=r"^synthetic://")]
    synthetic: Synthetic


class Item(_Model):
    item_id: Annotated[str, Field(pattern=r"^AM-\d{3}$")]
    product_id: NonEmptyStr
    tag_code: Annotated[str, Field(pattern=r"^SYN-T-[A-Z0-9]{4}$")]
    item_status: ItemStatus
    points: dict[Point, PointRecord]
    evidence: list[Evidence]
    synthetic: Synthetic


class InitialReservation(_Model):
    reservation_id: Annotated[str, Field(pattern=r"^RSV-SEED-[0-9]+$")]
    item_id: NonEmptyStr
    buyer_id: NonEmptyStr
    synthetic: Synthetic


class AcceptedDefect(_Model):
    point: Point
    defect_type: DefectType
    max_severity: Severity


class DemoRequest(_Model):
    buyer_id: NonEmptyStr
    product_id: NonEmptyStr
    accepted_defects: list[AcceptedDefect]
    required_conditions: list[Point]
    synthetic: Synthetic


class Dataset(_Model):
    meta: DatasetMeta
    products: list[Product] = Field(min_length=1)
    price_rules: PriceRules
    inspection_methods: list[InspectionMethod]
    buyers: list[Buyer] = Field(min_length=1)
    demo_request: DemoRequest
    items: list[Item] = Field(min_length=1)
    initial_reservations: list[InitialReservation]


# -------------------------------------------------------------- economics.json


class NoInspectionOption(_Model):
    kind: NoInspectionOptionKind
    label: NonEmptyStr
    net: Money


class ItemEconomics(_Model):
    """一件商品的检查经济参数。字段名与 requirements.md 的符号一致。"""

    inspection_points: list[Point] = Field(min_length=1)
    C: Money
    p: Ratio
    H: Money
    R_fail: Money
    no_inspection_options: list[NoInspectionOption] = Field(min_length=1)
    synthetic: Synthetic


class EconomicsBase(_Model):
    budget_initial: Money
    items: dict[NonEmptyStr, ItemEconomics]


class ItemEconomicsOverride(_Model):
    inspection_points: list[Point] | None = None
    C: Money | None = None
    p: Ratio | None = None
    H: Money | None = None
    R_fail: Money | None = None
    no_inspection_options: list[NoInspectionOption] | None = None


class ParameterSetOverride(_Model):
    budget_initial: Money | None = None
    items: dict[NonEmptyStr, ItemEconomicsOverride] = Field(default_factory=dict)


class EconomicsFile(_Model):
    synthetic: Synthetic
    notice: NonEmptyStr
    base: EconomicsBase
    parameter_sets: dict[NonEmptyStr, ParameterSetOverride]
