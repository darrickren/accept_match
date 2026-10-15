"""任务 3.3：价格规则（需求 9）。重点：AM-007 检查通过后报价 108.00。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from acceptmatch.domain.enums import DefectType, Point, PointStatus, ReasonCode, Severity
from acceptmatch.domain.models import DefectRecord, PointState
from acceptmatch.domain.money import q
from acceptmatch.domain.pricing import PriceRules, PricingError, price, price_item

D = Decimal
BASE_A = D("120.00")


def test_am007_pass_quote_is_108(snapshots, rules):
    """经济判断用的"通过后报价"：拉链视为无缺陷。"""
    result = price_item(snapshots["AM-007"], BASE_A, rules, assume_unknown_ok=True)
    assert result.amount == D("108.00")
    assert result.currency == "CAD"
    assert result.total_rate == D("0.10") and result.floor_applied is False
    assert [(l.defect_id, l.point, l.rate, l.amount) for l in result.discounts] == [
        ("DF-AM007-BACK-1", Point.BACK, D("0.10"), D("12.00"))
    ]


def test_am007_real_quote_after_inspection_pass_is_108(snapshots, rules):
    """实际报价路径：拉链检查通过后，不需要任何假设也是 108.00。"""
    passed = snapshots["AM-007"].with_points({Point.ZIPPER: PointState(PointStatus.OK, evidence_ids=("EV-0001",))})
    assert price_item(passed, BASE_A, rules).amount == D("108.00")


@pytest.mark.parametrize("item_id", ["AM-007", "AM-008", "AM-009"])
def test_unverified_points_cannot_be_quoted(snapshots, rules, item_id):
    with pytest.raises(PricingError) as exc:
        price_item(snapshots[item_id], BASE_A, rules)
    assert exc.value.code is ReasonCode.UNVERIFIED_POINTS


@pytest.mark.parametrize(
    "item_id,base,expected",
    [
        ("AM-004", "120.00", "90.00"),   # 中等划痕 25%
        ("AM-005", "150.00", "135.00"),  # 型号 B，轻微划痕 10%
        ("AM-010", "120.00", "96.00"),   # 两个轻微缺陷，共 20%
        ("AM-002", "120.00", "90.00"),   # 拉链卡滞（中等）
    ],
)
def test_other_seed_items(snapshots, rules, item_id, base, expected):
    assert price_item(snapshots[item_id], D(base), rules).amount == D(expected)


def test_am007_after_inspection_fail_price(snapshots, rules):
    failed = snapshots["AM-007"].with_points(
        {Point.ZIPPER: PointState(PointStatus.DEFECT, (DefectRecord("DF-X", DefectType.JAM, Severity.MODERATE),), ("EV-1",))}
    )
    assert price_item(failed, BASE_A, rules).amount == D("78.00")  # 120 × (1 − 0.10 − 0.25)


def test_floor_applied(rules):
    severe = [(p, DefectRecord(f"DF-{p}", DefectType.DENT, Severity.SEVERE)) for p in Point]  # 合计 120%
    result = price(BASE_A, severe, rules)
    assert result.floor_applied is True and result.amount == D("60.00")


def test_no_defects_is_base_price(rules):
    result = price(BASE_A, [], rules)
    assert result.amount == BASE_A and result.discounts == () and result.total_rate == 0


def test_missing_rule_reported():
    rules = PriceRules({Severity.MINOR: D("0.10")}, D("0.50"))
    defects = [
        (Point.BACK, DefectRecord("DF-1", DefectType.SCRATCH, Severity.SEVERE)),
        (Point.FRONT, DefectRecord("DF-2", DefectType.STAIN, Severity.MODERATE)),
    ]
    with pytest.raises(PricingError) as exc:
        price(BASE_A, defects, rules)
    assert exc.value.code is ReasonCode.PRICE_RULE_MISSING
    assert exc.value.details == (Severity.MODERATE, Severity.SEVERE)


@pytest.mark.parametrize(
    "base,expected",
    [("99.99", "89.99"), ("100.05", "90.05"), ("0.05", "0.05")],  # 89.991→89.99；90.045→90.05；0.045→0.05
)
def test_round_half_up(base, expected):
    rules = PriceRules({Severity.MINOR: D("0.10")}, D("0.50"))
    defect = [(Point.BACK, DefectRecord("DF-1", DefectType.SCRATCH, Severity.MINOR))]
    assert price(D(base), defect, rules).amount == D(expected)


_money = st.decimals(min_value=D("1.00"), max_value=D("1000.00"), places=2, allow_nan=False, allow_infinity=False)
_defects = st.lists(
    st.tuples(
        st.sampled_from(list(Point)),
        st.builds(DefectRecord, st.just("DF-P"), st.sampled_from(list(DefectType)), st.sampled_from(list(Severity))),
    ),
    max_size=6,
)


@settings(max_examples=300, deadline=None)
@given(base=_money, defects=_defects)
def test_property_price_bounds(rules, base, defects):
    """属性 5：0.50 × base ≤ amount ≤ base，且金额等于公式结果。"""
    result = price(base, defects, rules)
    assert q(base * rules.floor_ratio) <= result.amount <= base
    rate = sum((rules.discount_by_severity[d.severity] for _, d in defects), D(0))
    assert result.amount == q(max(base * (1 - rate), base * rules.floor_ratio))
