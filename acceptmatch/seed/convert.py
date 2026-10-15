"""把已校验的种子记录转换为领域模型（纯函数）。"""

from __future__ import annotations

from decimal import Decimal

from acceptmatch.domain.economics import EconomicsInputs, NoInspectionChoice
from acceptmatch.domain.enums import Point
from acceptmatch.domain.models import (
    AcceptedDefect,
    BuyerRequestSpec,
    DefectRecord,
    ItemSnapshot,
    PointState,
)
from acceptmatch.domain.pricing import PriceRules
from acceptmatch.seed import models as sm


def item_snapshot(item: sm.Item, condition_version: int = 1) -> ItemSnapshot:
    points = {}
    for point, rec in item.points.items():
        defects = tuple(DefectRecord(d.defect_id, d.type, d.severity) for d in rec.defects)
        evidence = tuple(rec.evidence_ids) + tuple(e for d in rec.defects for e in d.evidence_ids)
        points[point] = PointState(status=rec.status, defects=defects, evidence_ids=evidence)
    return ItemSnapshot(
        item_id=item.item_id,
        product_id=item.product_id,
        tag_code=item.tag_code,
        item_status=item.item_status,
        condition_version=condition_version,
        points=points,
    )


def condition_payload(item: sm.Item) -> dict:
    """Condition_Record 版本 1 的存储内容：各部位的状态、缺陷和完整 Evidence 记录。"""
    payload = {}
    for point in Point:
        rec = item.points[point]
        payload[point.value] = {
            "status": rec.status.value,
            "evidence_ids": list(rec.evidence_ids),
            "defects": [
                {
                    "defect_id": d.defect_id,
                    "type": d.type.value,
                    "severity": d.severity.value,
                    "evidence_ids": list(d.evidence_ids),
                }
                for d in rec.defects
            ],
            "evidence": [
                {**e.model_dump(mode="json"), "inspection_task_id": None}
                for e in item.evidence
                if e.point is point
            ],
        }
    return payload


def price_rules(rules: sm.PriceRules, currency: str = "CAD") -> PriceRules:
    return PriceRules(
        discount_by_severity=dict(rules.discount_by_severity),
        floor_ratio=rules.floor_ratio,
        currency=currency,
    )


def buyer_request_spec(req: sm.DemoRequest) -> BuyerRequestSpec:
    return BuyerRequestSpec(
        product_id=req.product_id,
        accepted_defects=tuple(
            AcceptedDefect(a.point, a.defect_type, a.max_severity) for a in req.accepted_defects
        ),
        required_conditions=tuple(req.required_conditions),
    )


def economics_inputs(ie: sm.ItemEconomics, budget: Decimal, pass_quote: Decimal) -> EconomicsInputs:
    return EconomicsInputs(
        C=ie.C,
        p=ie.p,
        H=ie.H,
        R_fail=ie.R_fail,
        B=budget,
        pass_quote=pass_quote,
        options=tuple(NoInspectionChoice(o.kind, o.label, o.net) for o in ie.no_inspection_options),
    )
