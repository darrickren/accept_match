"""检查前的经济判断（需求 4，design §4.3）。纯函数。

    R_pass     = pass_quote − H
    s1         = p × R_pass
    s2         = (1 − p) × R_fail
    EV_inspect = s1 + s2 − C
    V_none     = max(不检查选项的 net)
    ΔEV        = EV_inspect − V_none

每一步四舍五入到 0.01。判定 INSPECT 当且仅当参数有效、C ≤ B 且 ΔEV > 0。
参数缺失或无效时判定 SKIP（ECONOMICS_INPUT_INVALID），不使用任何默认值。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from acceptmatch.domain.enums import NoInspectionOptionKind, ReasonCode, Verdict
from acceptmatch.domain.money import fmt, q


@dataclass(frozen=True)
class NoInspectionChoice:
    kind: NoInspectionOptionKind
    label: str
    net: Decimal


@dataclass(frozen=True)
class EconomicsInputs:
    """None 表示参数缺失。所有值都应来自合成参数表，不由 Agent 或模型估计。"""

    C: Decimal | None
    p: Decimal | None
    H: Decimal | None
    R_fail: Decimal | None
    B: Decimal | None
    pass_quote: Decimal | None
    options: tuple[NoInspectionChoice, ...]
    # 调用方发现的其他输入问题（例如参数中的待检查部位与商品的未核实部位不一致）
    input_problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    name: str
    expr: str
    value: Decimal
    option: NoInspectionOptionKind | None = None


@dataclass(frozen=True)
class BudgetCheck:
    C: Decimal
    B: Decimal
    ok: bool


@dataclass(frozen=True)
class InspectionDecisionResult:
    verdict: Verdict
    reason_codes: tuple[ReasonCode, ...]
    steps: tuple[Step, ...]
    r_pass: Decimal | None
    ev_inspect: Decimal | None
    v_none: NoInspectionChoice | None
    delta_ev: Decimal | None
    budget_check: BudgetCheck | None
    recommended_disposition: NoInspectionChoice | None
    invalid_inputs: tuple[str, ...]


def _validate(inputs: EconomicsInputs) -> list[str]:
    problems: list[str] = list(inputs.input_problems)
    for name in ("C", "p", "H", "R_fail", "B", "pass_quote"):
        value = getattr(inputs, name)
        if value is None:
            problems.append(f"{name}：缺失")
        elif not isinstance(value, Decimal) or not value.is_finite():
            problems.append(f"{name}：不是有效数值")
        elif name == "p":
            if not Decimal("0") <= value <= Decimal("1"):
                problems.append("p：必须在 0 到 1 之间")
        elif value < 0:
            problems.append(f"{name}：不能为负")
    problems.extend(_validate_options(inputs.options))
    return problems


def _validate_options(options: tuple[NoInspectionChoice, ...]) -> list[str]:
    if not options:
        return ["options：至少需要一个不检查选项"]
    problems: list[str] = []
    for index, option in enumerate(options):
        if not isinstance(option.kind, NoInspectionOptionKind):
            problems.append(f"options[{index}].kind：不允许的处置类型 {option.kind!r}")
        if not isinstance(option.net, Decimal) or not option.net.is_finite():
            problems.append(f"options[{index}].net：不是有效数值")
        elif option.net < 0:
            problems.append(f"options[{index}].net：不能为负")
    return problems


def best_option(options: tuple[NoInspectionChoice, ...]) -> NoInspectionChoice:
    """净回收最高的选项；并列时取列表中靠前的一个。"""
    best = options[0]
    for option in options[1:]:
        if option.net > best.net:
            best = option
    return best


def decide(inputs: EconomicsInputs) -> InspectionDecisionResult:
    problems = _validate(inputs)
    if problems:
        recommended = None if _validate_options(inputs.options) else best_option(inputs.options)
        return InspectionDecisionResult(
            verdict=Verdict.SKIP,
            reason_codes=(ReasonCode.ECONOMICS_INPUT_INVALID,),
            steps=(),
            r_pass=None,
            ev_inspect=None,
            v_none=recommended,
            delta_ev=None,
            budget_check=None,
            recommended_disposition=recommended,
            invalid_inputs=tuple(problems),
        )

    C, p, H, R_fail, B, pass_quote = (
        inputs.C, inputs.p, inputs.H, inputs.R_fail, inputs.B, inputs.pass_quote,
    )
    assert None not in (C, p, H, R_fail, B, pass_quote)

    one_minus_p = Decimal("1") - p
    r_pass = q(pass_quote - H)
    s1 = q(p * r_pass)
    s2 = q(one_minus_p * R_fail)
    ev = q(s1 + s2 - C)
    v_none = best_option(inputs.options)
    delta = q(ev - v_none.net)

    steps = (
        Step("R_pass", f"{fmt(pass_quote)} − {fmt(H)}", r_pass),
        Step("s1", f"{p:.2f} × {fmt(r_pass)}", s1),
        Step("s2", f"{one_minus_p:.2f} × {fmt(R_fail)}", s2),
        Step("EV_inspect", f"{fmt(s1)} + {fmt(s2)} − {fmt(C)}", ev),
        Step("V_none", "max(" + ", ".join(fmt(o.net) for o in inputs.options) + ")", q(v_none.net),
             option=v_none.kind),
        Step("ΔEV", f"{fmt(ev)} − {fmt(v_none.net)}", delta),
    )

    budget = BudgetCheck(C=C, B=B, ok=C <= B)
    reasons: list[ReasonCode] = []
    if not budget.ok:
        reasons.append(ReasonCode.INSPECTION_OVER_BUDGET)
    if delta <= 0:
        reasons.append(ReasonCode.INSPECTION_NOT_WORTHWHILE)

    verdict = Verdict.SKIP if reasons else Verdict.INSPECT
    return InspectionDecisionResult(
        verdict=verdict,
        reason_codes=tuple(reasons),
        steps=steps,
        r_pass=r_pass,
        ev_inspect=ev,
        v_none=v_none,
        delta_ev=delta,
        budget_check=budget,
        recommended_disposition=v_none if verdict is Verdict.SKIP else None,
        invalid_inputs=(),
    )
