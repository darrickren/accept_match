"""领域模型（design §3）。不可变、无 I/O；金额统一用 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping

from acceptmatch.domain.enums import DefectType, ItemStatus, Point, PointStatus, Severity


@dataclass(frozen=True)
class DefectRecord:
    defect_id: str
    type: DefectType
    severity: Severity


@dataclass(frozen=True)
class PointState:
    """一个部位的状态。OK/DEFECT 表示已核实；UNKNOWN 表示未核实。"""

    status: PointStatus
    defects: tuple[DefectRecord, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is PointStatus.DEFECT and not self.defects:
            raise ValueError("DEFECT 部位必须至少有一个缺陷")
        if self.status is not PointStatus.DEFECT and self.defects:
            raise ValueError(f"{self.status} 部位不能带缺陷")
        if self.status is PointStatus.UNKNOWN and self.evidence_ids:
            raise ValueError("UNKNOWN 部位不能带 Evidence")


UNKNOWN_POINT = PointState(PointStatus.UNKNOWN)


@dataclass(frozen=True)
class ItemSnapshot:
    """一件商品在某个 Condition_Record 版本下的快照。"""

    item_id: str
    product_id: str
    tag_code: str
    item_status: ItemStatus
    condition_version: int
    points: Mapping[Point, PointState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", MappingProxyType(dict(self.points)))

    def point(self, point: Point) -> PointState:
        """缺失的部位一律视为 UNKNOWN，绝不当作 OK。"""
        return self.points.get(point, UNKNOWN_POINT)

    def unknown_points(self) -> tuple[Point, ...]:
        return tuple(p for p in Point if self.point(p).status is PointStatus.UNKNOWN)

    def verified_defects(self) -> tuple[tuple[Point, DefectRecord], ...]:
        return tuple((p, d) for p in Point for d in self.point(p).defects)

    def with_points(self, updates: Mapping[Point, PointState]) -> ItemSnapshot:
        """返回更新部位后的新版本（版本号加一）。"""
        merged = dict(self.points)
        merged.update(updates)
        return replace(self, points=merged, condition_version=self.condition_version + 1)


@dataclass(frozen=True)
class AcceptedDefect:
    """买家明确接受的一类缺陷：部位相同、类型相同、程度不高于 max_severity 才算覆盖。"""

    point: Point
    defect_type: DefectType
    max_severity: Severity

    def covers(self, point: Point, defect: DefectRecord) -> bool:
        return (
            point is self.point
            and defect.type is self.defect_type
            and defect.severity.rank <= self.max_severity.rank
        )


@dataclass(frozen=True)
class BuyerRequestSpec:
    product_id: str
    accepted_defects: tuple[AcceptedDefect, ...] = ()
    required_conditions: tuple[Point, ...] = ()

    def conflicting_points(self) -> tuple[Point, ...]:
        """同时出现在 Accepted_Defects 和 Required_Conditions 中的部位（需求 2.2）。"""
        accepted = {a.point for a in self.accepted_defects}
        return tuple(p for p in Point if p in accepted and p in self.required_conditions)

    def accepted_index(self, point: Point, defect: DefectRecord) -> int | None:
        """覆盖该缺陷的第一条 Accepted_Defects 的下标；没有则为 None。"""
        for index, accepted in enumerate(self.accepted_defects):
            if accepted.covers(point, defect):
                return index
        return None
