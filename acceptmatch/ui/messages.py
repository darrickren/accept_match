"""面向买家的消息模板（需求 4.15、7.4、12.7）。

- 按原因码和工具数据生成，从不使用 Agent 文本。
- 不包含任何经济参数（成本、概率、预算、预期价值等）。
"""

from __future__ import annotations

from typing import Any, Iterable

from acceptmatch.domain.enums import DefectType, Point, ReasonCode, Severity
from acceptmatch.domain.models import BuyerRequestSpec


def point_label(point: str) -> str:
    return Point(point).label


def points_label(points: Iterable[str]) -> str:
    return "、".join(point_label(p) for p in points)


def defect_label(defect_type: str, severity: str) -> str:
    return f"{DefectType(defect_type).label}（{Severity(severity).label}）"


def request_echo(spec: BuyerRequestSpec) -> str:
    if spec.accepted_defects:
        accepted = "；".join(
            f"{a.point.label}{a.defect_type.label}（最高{a.max_severity.label}）" for a in spec.accepted_defects
        )
        first = f"您接受的缺陷：{accepted}。"
    else:
        first = "您不接受任何缺陷。"
    if spec.required_conditions:
        second = f"要求完好的部位：{points_label(spec.required_conditions)}。"
    else:
        second = "没有指定必须完好的部位。"
    return first + second + "未列出的缺陷一律视为不接受。"


def waiting_text(item_id: str, points: Iterable[str]) -> str:
    return f"正在等待人工检查：{item_id} 的{points_label(points)}。检查完成前不会报价或预留。"


_NOT_INSPECTED = {
    ReasonCode.INSPECTION_OVER_BUDGET,
    ReasonCode.INSPECTION_NOT_WORTHWHILE,
    ReasonCode.ECONOMICS_INPUT_INVALID,
}


def item_reason_lines(entry: dict[str, Any]) -> list[str]:
    """把一件商品的排除原因转换为买家可读的句子。"""
    lines: list[str] = []
    codes = [ReasonCode(c) for c in entry["reason_codes"]]
    details = entry.get("details", [])
    unknown = entry.get("unknown_points", [])
    for code in codes:
        if code is ReasonCode.PRODUCT_MISMATCH:
            lines.append("型号与您的需求不符")
        elif code is ReasonCode.NOT_AVAILABLE:
            lines.append("该商品当前不可售")
        elif code is ReasonCode.REQUIREMENT_NOT_MET:
            for d in details:
                if d["code"] == code:
                    lines.append(f"{point_label(d['point'])}不是完好状态，不符合您的要求")
        elif code is ReasonCode.UNACCEPTED_DEFECT:
            for d in details:
                if d["code"] == code:
                    lines.append(f"有您未接受的缺陷：{point_label(d['point'])}"
                                 f"{defect_label(d['defect_type'], d['severity'])}")
        elif code in _NOT_INSPECTED:
            if f"{points_label(unknown)}未核实，本次不安排检查" not in lines:
                lines.append(f"{points_label(unknown)}未核实，本次不安排检查")
        elif code is ReasonCode.INSPECTION_INCONCLUSIVE:
            lines.append(f"人工检查无法确定{points_label(unknown)}的状态")
    conclusion = entry.get("inspection_conclusion")
    if conclusion:
        for f in conclusion["findings"]:
            if f["conclusion"] == "OK":
                lines.append(f"人工检查结论：{point_label(f['point'])}完好")
            elif f["conclusion"] == "DEFECT":
                lines.append(f"人工检查结论：{point_label(f['point'])}{defect_label(f['defect_type'], f['severity'])}")
            else:
                lines.append(f"人工检查结论：{point_label(f['point'])}无法确定")
    return lines


def no_match_summary() -> str:
    return "目前没有符合您条件的商品。以下是每件商品不合适的原因："
