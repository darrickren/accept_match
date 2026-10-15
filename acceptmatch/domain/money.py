"""金额与比例的舍入和显示。所有计算使用 Decimal，每步 ROUND_HALF_UP 到 0.01。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def q(value: Decimal) -> Decimal:
    """四舍五入到 0.01（ROUND_HALF_UP）。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    """两位小数显示；负数使用 Unicode 减号，与需求文档一致。"""
    text = f"{q(value):.2f}"
    return "−" + text[1:] if text.startswith("-") else text
