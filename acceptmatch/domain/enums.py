"""领域枚举（design §3.1）。取值是存储和接口中使用的代码，label 是中文显示名。"""

from __future__ import annotations

from enum import StrEnum


class Point(StrEnum):
    FRONT = "FRONT"
    BACK = "BACK"
    ZIPPER = "ZIPPER"

    @property
    def label(self) -> str:
        return {"FRONT": "正面", "BACK": "背面", "ZIPPER": "拉链"}[self.value]


class DefectType(StrEnum):
    SCRATCH = "SCRATCH"
    STAIN = "STAIN"
    DENT = "DENT"
    JAM = "JAM"

    @property
    def label(self) -> str:
        return {"SCRATCH": "划痕", "STAIN": "污渍", "DENT": "凹陷", "JAM": "卡滞"}[self.value]


class Severity(StrEnum):
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    SEVERE = "SEVERE"

    @property
    def rank(self) -> int:
        """程度顺序：轻微 < 中等 < 严重。"""
        return {"MINOR": 1, "MODERATE": 2, "SEVERE": 3}[self.value]

    @property
    def label(self) -> str:
        return {"MINOR": "轻微", "MODERATE": "中等", "SEVERE": "严重"}[self.value]


class PointStatus(StrEnum):
    OK = "OK"
    DEFECT = "DEFECT"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return {"OK": "已核实完好", "DEFECT": "已核实缺陷", "UNKNOWN": "未核实"}[self.value]


class ItemStatus(StrEnum):
    INTAKE = "INTAKE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DELIVERED = "DELIVERED"

    @property
    def label(self) -> str:
        return {
            "INTAKE": "入库检测中",
            "AVAILABLE": "可售",
            "RESERVED": "已预留",
            "DELIVERED": "已交付",
        }[self.value]


class EvidenceSource(StrEnum):
    INTAKE = "INTAKE"
    INSPECTION = "INSPECTION"

    @property
    def label(self) -> str:
        return {"INTAKE": "入库检测", "INSPECTION": "人工检查"}[self.value]


class MatchResult(StrEnum):
    QUALIFIED = "QUALIFIED"
    NEEDS_INSPECTION = "NEEDS_INSPECTION"
    EXCLUDED = "EXCLUDED"


class Verdict(StrEnum):
    INSPECT = "INSPECT"
    SKIP = "SKIP"


class ReasonCode(StrEnum):
    """领域层原因码（design §6.5 的子集）。定义顺序即输出时的固定顺序。"""

    # 匹配（需求 3）
    PRODUCT_MISMATCH = "PRODUCT_MISMATCH"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    REQUIREMENT_NOT_MET = "REQUIREMENT_NOT_MET"
    UNACCEPTED_DEFECT = "UNACCEPTED_DEFECT"
    # 请求级排除（由经济判断或检查结果写入）
    INSPECTION_OVER_BUDGET = "INSPECTION_OVER_BUDGET"
    INSPECTION_NOT_WORTHWHILE = "INSPECTION_NOT_WORTHWHILE"
    ECONOMICS_INPUT_INVALID = "ECONOMICS_INPUT_INVALID"
    INSPECTION_INCONCLUSIVE = "INSPECTION_INCONCLUSIVE"
    # 价格（需求 9）
    PRICE_RULE_MISSING = "PRICE_RULE_MISSING"
    UNVERIFIED_POINTS = "UNVERIFIED_POINTS"

    @property
    def order(self) -> int:
        return list(ReasonCode).index(self)


class NoInspectionOptionKind(StrEnum):
    """不检查时允许的处置。不包含"向买家报价或预留"（需求 4.4）。"""

    LIQUIDATE_AS_IS = "LIQUIDATE_AS_IS"
    RETURN_TO_VENDOR = "RETURN_TO_VENDOR"

    @property
    def label(self) -> str:
        return {"LIQUIDATE_AS_IS": "按现状清仓", "RETURN_TO_VENDOR": "退回供应商"}[self.value]
