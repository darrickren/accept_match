"""逐件匹配（需求 3，design §4.1）。纯函数。

规则：
- 记录所有适用的原因码，而不是只记第一个（需求 3.8）。
- Required_Conditions 部位上的缺陷记为 REQUIREMENT_NOT_MET；其余部位上未被
  Accepted_Defects 覆盖的缺陷记为 UNACCEPTED_DEFECT。
- UNKNOWN 部位既不算 OK，也不算买家已接受（需求 3.9）：未被排除但有 UNKNOWN
  部位的商品只能是 NEEDS_INSPECTION。
- 排名：QUALIFIED 在前，NEEDS_INSPECTION 在后，同类按 Item_ID 升序（需求 3.10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from acceptmatch.domain.enums import ItemStatus, MatchResult, Point, PointStatus, ReasonCode
from acceptmatch.domain.models import BuyerRequestSpec, ItemSnapshot

# 只能通过请求级排除项追加的原因码
REQUEST_EXCLUSION_CODES = frozenset(
    {
        ReasonCode.INSPECTION_OVER_BUDGET,
        ReasonCode.INSPECTION_NOT_WORTHWHILE,
        ReasonCode.ECONOMICS_INPUT_INVALID,
        ReasonCode.INSPECTION_INCONCLUSIVE,
    }
)


@dataclass(frozen=True)
class ExclusionDetail:
    code: ReasonCode
    point: Point | None = None
    defect_id: str | None = None


@dataclass(frozen=True)
class ItemMatch:
    item_id: str
    condition_version: int
    result: MatchResult
    reason_codes: tuple[ReasonCode, ...]
    details: tuple[ExclusionDetail, ...]
    unknown_points: tuple[Point, ...]


@dataclass(frozen=True)
class MatchEvaluation:
    results: tuple[ItemMatch, ...]  # 按 Item_ID 升序
    ranking: tuple[str, ...]

    def result_for(self, item_id: str) -> ItemMatch:
        for match in self.results:
            if match.item_id == item_id:
                return match
        raise KeyError(item_id)

    @property
    def qualified(self) -> tuple[str, ...]:
        return tuple(i for i in self.ranking if self.result_for(i).result is MatchResult.QUALIFIED)

    @property
    def needs_inspection(self) -> tuple[str, ...]:
        return tuple(i for i in self.ranking if self.result_for(i).result is MatchResult.NEEDS_INSPECTION)


def evaluate_item(
    item: ItemSnapshot,
    request: BuyerRequestSpec,
    exclusions: Iterable[ReasonCode] = (),
) -> ItemMatch:
    details: list[ExclusionDetail] = []

    if item.product_id != request.product_id:
        details.append(ExclusionDetail(ReasonCode.PRODUCT_MISMATCH))
    if item.item_status is not ItemStatus.AVAILABLE:
        details.append(ExclusionDetail(ReasonCode.NOT_AVAILABLE))

    required = set(request.required_conditions)
    for point in Point:
        state = item.point(point)
        if point in required:
            if state.status is PointStatus.DEFECT:
                details.append(ExclusionDetail(ReasonCode.REQUIREMENT_NOT_MET, point=point))
            continue
        for defect in state.defects:
            if request.accepted_index(point, defect) is None:
                details.append(
                    ExclusionDetail(ReasonCode.UNACCEPTED_DEFECT, point=point, defect_id=defect.defect_id)
                )

    for code in exclusions:
        code = ReasonCode(code)
        if code not in REQUEST_EXCLUSION_CODES:
            raise ValueError(f"{code} 不能作为请求级排除项")
        details.append(ExclusionDetail(code))

    details.sort(key=lambda d: (d.code.order, d.point.value if d.point else "", d.defect_id or ""))
    unique_details = tuple(dict.fromkeys(details))
    reason_codes = tuple(dict.fromkeys(d.code for d in unique_details))
    unknown = item.unknown_points()

    if reason_codes:
        result = MatchResult.EXCLUDED
    elif unknown:
        result = MatchResult.NEEDS_INSPECTION
    else:
        result = MatchResult.QUALIFIED

    return ItemMatch(
        item_id=item.item_id,
        condition_version=item.condition_version,
        result=result,
        reason_codes=reason_codes,
        details=unique_details,
        unknown_points=unknown,
    )


_RANK_GROUP = {MatchResult.QUALIFIED: 0, MatchResult.NEEDS_INSPECTION: 1}


def evaluate(
    items: Iterable[ItemSnapshot],
    request: BuyerRequestSpec,
    exclusions: Mapping[str, Sequence[ReasonCode]] | None = None,
) -> MatchEvaluation:
    exclusions = exclusions or {}
    snapshots = sorted(items, key=lambda i: i.item_id)
    ids = [i.item_id for i in snapshots]
    if len(ids) != len(set(ids)):
        raise ValueError("商品 Item_ID 重复")

    results = tuple(evaluate_item(i, request, exclusions.get(i.item_id, ())) for i in snapshots)
    ranking = tuple(
        m.item_id
        for m in sorted(
            (m for m in results if m.result in _RANK_GROUP),
            key=lambda m: (_RANK_GROUP[m.result], m.item_id),
        )
    )
    return MatchEvaluation(results=results, ranking=ranking)
