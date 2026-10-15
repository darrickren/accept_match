"""任务 3.4：检查前的经济判断（需求 4）。重点：三组合成参数的判定。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from acceptmatch.domain.economics import (
    EconomicsInputs,
    NoInspectionChoice,
    decide,
)
from acceptmatch.domain.enums import NoInspectionOptionKind, ReasonCode, Verdict
from acceptmatch.domain.money import q
from acceptmatch.domain.pricing import price_item
from acceptmatch.seed import convert

D = Decimal
R = ReasonCode
LIQ = NoInspectionOptionKind.LIQUIDATE_AS_IS
RTV = NoInspectionOptionKind.RETURN_TO_VENDOR


def seed_inputs(seed, snapshots, rules, set_name: str) -> EconomicsInputs:
    """与运行时一致：用价格规则算 AM-007 的通过后报价，再加上该参数组的合成参数。"""
    econ = seed.parameter_set(set_name)
    item = snapshots["AM-007"]
    pass_quote = price_item(item, seed.product(item.product_id).base_price, rules, assume_unknown_ok=True).amount
    return convert.economics_inputs(econ.items["AM-007"], econ.budget_initial, pass_quote)


def values(result) -> dict[str, Decimal]:
    return {s.name: s.value for s in result.steps}


# ----------------------------------------------------------- 三组参数


def test_default_parameter_set_inspects(seed, snapshots, rules):
    r = decide(seed_inputs(seed, snapshots, rules, "default"))
    assert r.verdict is Verdict.INSPECT and r.reason_codes == ()
    assert values(r) == {
        "R_pass": D("98.00"), "s1": D("78.40"), "s2": D("8.00"),
        "EV_inspect": D("78.40"), "V_none": D("45.00"), "ΔEV": D("33.40"),
    }
    assert [s.expr for s in r.steps] == [
        "108.00 − 10.00", "0.80 × 98.00", "0.20 × 40.00",
        "78.40 + 8.00 − 8.00", "max(45.00, 35.00)", "78.40 − 45.00",
    ]
    assert r.v_none.kind is LIQ and r.steps[4].option is LIQ
    assert (r.budget_check.C, r.budget_check.B, r.budget_check.ok) == (D("8.00"), D("20.00"), True)
    assert r.recommended_disposition is None and r.invalid_inputs == ()


def test_budget_low_parameter_set_skips_over_budget(seed, snapshots, rules):
    r = decide(seed_inputs(seed, snapshots, rules, "budget_low"))
    assert r.verdict is Verdict.SKIP
    assert r.reason_codes == (R.INSPECTION_OVER_BUDGET,)
    assert r.ev_inspect == D("78.40") and r.delta_ev == D("33.40")  # ΔEV 仍然计算并展示
    assert (r.budget_check.B, r.budget_check.ok) == (D("5.00"), False)
    assert r.recommended_disposition.kind is LIQ and r.recommended_disposition.net == D("45.00")


def test_low_pass_parameter_set_skips_not_worthwhile(seed, snapshots, rules):
    r = decide(seed_inputs(seed, snapshots, rules, "low_pass"))
    assert r.verdict is Verdict.SKIP
    assert r.reason_codes == (R.INSPECTION_NOT_WORTHWHILE,)
    assert values(r)["s1"] == D("9.80") and values(r)["s2"] == D("36.00")
    assert r.ev_inspect == D("37.80") and r.delta_ev == D("-7.20")
    assert [s.expr for s in r.steps][1:4] == ["0.10 × 98.00", "0.90 × 40.00", "9.80 + 36.00 − 8.00"]
    assert r.steps[5].expr == "37.80 − 45.00"
    assert r.budget_check.ok is True
    assert r.recommended_disposition.kind is LIQ


# ----------------------------------------------------------- 判定边界


def base_inputs(**overrides) -> EconomicsInputs:
    inputs = EconomicsInputs(
        C=D("8.00"), p=D("0.80"), H=D("10.00"), R_fail=D("40.00"), B=D("20.00"), pass_quote=D("108.00"),
        options=(NoInspectionChoice(LIQ, "按现状清仓", D("45.00")), NoInspectionChoice(RTV, "退回供应商", D("35.00"))),
    )
    return replace(inputs, **overrides)


def test_both_reasons_recorded_together():
    r = decide(base_inputs(p=D("0.10"), B=D("5.00")))
    assert r.verdict is Verdict.SKIP
    assert r.reason_codes == (R.INSPECTION_OVER_BUDGET, R.INSPECTION_NOT_WORTHWHILE)


def test_cost_equal_to_budget_is_within_budget():
    r = decide(base_inputs(B=D("8.00")))
    assert r.budget_check.ok is True and r.verdict is Verdict.INSPECT


def test_zero_incremental_value_is_not_worthwhile():
    # p = 1：EV = 53.00 − 0 − 8.00 = 45.00 = V_none，ΔEV = 0
    r = decide(base_inputs(p=D("1.00"), pass_quote=D("53.00"), H=D("0.00")))
    assert r.delta_ev == D("0.00")
    assert r.verdict is Verdict.SKIP and r.reason_codes == (R.INSPECTION_NOT_WORTHWHILE,)


def test_best_option_tie_takes_first():
    r = decide(base_inputs(options=(NoInspectionChoice(RTV, "退回", D("45.00")), NoInspectionChoice(LIQ, "清仓", D("45.00")))))
    assert r.v_none.kind is RTV


def test_same_inputs_same_decision():
    assert decide(base_inputs()) == decide(base_inputs())


# -------------------------------------------------------- 参数无效


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"p": None}, "p：缺失"),
        ({"C": None}, "C：缺失"),
        ({"pass_quote": None}, "pass_quote：缺失"),
        ({"p": D("1.50")}, "p：必须在 0 到 1 之间"),
        ({"p": D("-0.10")}, "p：必须在 0 到 1 之间"),
        ({"C": D("-8.00")}, "C：不能为负"),
        ({"B": D("NaN")}, "B：不是有效数值"),
        ({"options": ()}, "至少需要一个不检查选项"),
        ({"options": (NoInspectionChoice("SELL_TO_BUYER", "向买家报价", D("108.00")),)}, "不允许的处置类型"),
    ],
)
def test_invalid_inputs_skip_without_defaults(overrides, fragment):
    r = decide(base_inputs(**overrides))
    assert r.verdict is Verdict.SKIP
    assert r.reason_codes == (R.ECONOMICS_INPUT_INVALID,)
    assert any(fragment in msg for msg in r.invalid_inputs), r.invalid_inputs
    assert r.steps == () and r.ev_inspect is None and r.delta_ev is None and r.budget_check is None


def test_invalid_inputs_still_recommend_best_valid_option():
    r = decide(base_inputs(p=None))
    assert r.recommended_disposition.kind is LIQ
    r = decide(base_inputs(options=()))
    assert r.recommended_disposition is None


# ------------------------------------------------------ 基于属性的测试

_amount = st.decimals(min_value=D("0.00"), max_value=D("300.00"), places=2, allow_nan=False, allow_infinity=False)
_prob = st.decimals(min_value=D("0.00"), max_value=D("1.00"), places=2, allow_nan=False, allow_infinity=False)
_options = st.lists(
    st.builds(NoInspectionChoice, kind=st.sampled_from(list(NoInspectionOptionKind)), label=st.just("x"), net=_amount),
    min_size=1,
    max_size=2,
)


@settings(max_examples=400, deadline=None)
@given(C=_amount, p=_prob, H=_amount, R_fail=_amount, B=_amount, pass_quote=_amount, options=_options)
def test_property_verdict_matches_rule_and_steps_recompute(C, p, H, R_fail, B, pass_quote, options):
    """属性 6：INSPECT 当且仅当 C ≤ B 且 ΔEV > 0；ΔEV 可由 steps 复算。"""
    r = decide(EconomicsInputs(C, p, H, R_fail, B, pass_quote, tuple(options)))
    v = values(r)
    assert v["R_pass"] == q(pass_quote - H)
    assert v["s1"] == q(p * v["R_pass"]) and v["s2"] == q((1 - p) * R_fail)
    assert v["EV_inspect"] == q(v["s1"] + v["s2"] - C)
    assert v["V_none"] == max(o.net for o in options)
    assert v["ΔEV"] == q(v["EV_inspect"] - v["V_none"]) == r.delta_ev
    assert (r.verdict is Verdict.INSPECT) == (C <= B and r.delta_ev > 0)
    assert (r.recommended_disposition is not None) == (r.verdict is Verdict.SKIP)
