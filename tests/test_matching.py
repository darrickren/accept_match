"""任务 3.2：逐件匹配（需求 3）。

重点：未知拉链不能算通过；所有缺陷都必须被买家明确接受。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from acceptmatch.domain.enums import (
    DefectType,
    ItemStatus,
    MatchResult,
    Point,
    PointStatus,
    ReasonCode,
    Severity,
)
from acceptmatch.domain.matching import evaluate, evaluate_item
from acceptmatch.domain.models import (
    AcceptedDefect,
    BuyerRequestSpec,
    DefectRecord,
    ItemSnapshot,
    PointState,
)

F, B, Z = Point.FRONT, Point.BACK, Point.ZIPPER
OK = PointState(PointStatus.OK, evidence_ids=("EV-X",))
UNKNOWN = PointState(PointStatus.UNKNOWN)
R = ReasonCode


def defect_state(*defects: tuple[DefectType, Severity]) -> PointState:
    return PointState(
        PointStatus.DEFECT,
        defects=tuple(DefectRecord(f"DF-T-{i}", t, s) for i, (t, s) in enumerate(defects)),
        evidence_ids=("EV-X",),
    )


def make_item(item_id="AM-100", product="SYN-BAG-A", status=ItemStatus.AVAILABLE, **points) -> ItemSnapshot:
    return ItemSnapshot(item_id, product, "SYN-T-TEST", status, 1, {Point(k.upper()): v for k, v in points.items()})


# ------------------------------------------------------- 演示请求下的十件商品

# requirements.md 商品表中"对演示请求的预期结果"
EXPECTED = {
    "AM-001": (MatchResult.EXCLUDED, [R.REQUIREMENT_NOT_MET], [(F, None)]),
    "AM-002": (MatchResult.EXCLUDED, [R.REQUIREMENT_NOT_MET], [(Z, None)]),
    "AM-003": (MatchResult.EXCLUDED, [R.UNACCEPTED_DEFECT], [(B, "DF-AM003-BACK-1")]),
    "AM-004": (MatchResult.EXCLUDED, [R.UNACCEPTED_DEFECT], [(B, "DF-AM004-BACK-1")]),
    "AM-005": (MatchResult.EXCLUDED, [R.PRODUCT_MISMATCH], [(None, None)]),
    "AM-006": (MatchResult.EXCLUDED, [R.NOT_AVAILABLE], [(None, None)]),
    "AM-007": (MatchResult.NEEDS_INSPECTION, [], []),
    "AM-008": (MatchResult.EXCLUDED, [R.UNACCEPTED_DEFECT], [(B, "DF-AM008-BACK-1")]),
    "AM-009": (MatchResult.EXCLUDED, [R.NOT_AVAILABLE], [(None, None)]),
    "AM-010": (MatchResult.EXCLUDED, [R.UNACCEPTED_DEFECT], [(B, "DF-AM010-BACK-2")]),
}


def test_demo_request_matches_requirements_table(snapshots, demo_spec):
    ev = evaluate(snapshots.values(), demo_spec)
    assert [m.item_id for m in ev.results] == sorted(EXPECTED)
    for item_id, (result, codes, details) in EXPECTED.items():
        m = ev.result_for(item_id)
        assert m.result is result, item_id
        assert list(m.reason_codes) == codes, item_id
        assert [(d.point, d.defect_id) for d in m.details] == details, item_id
    assert ev.ranking == ("AM-007",)
    assert ev.qualified == () and ev.needs_inspection == ("AM-007",)
    assert ev.result_for("AM-007").unknown_points == (Z,)


def test_am008_unknown_front_does_not_rescue_unaccepted_defect(snapshots, demo_spec):
    m = evaluate_item(snapshots["AM-008"], demo_spec)
    assert m.result is MatchResult.EXCLUDED  # 已核实的凹陷不被接受，不需要再检查正面
    assert m.unknown_points == (F,)


# ----------------------------------------------------- 未知拉链不能算通过


def test_unknown_zipper_is_never_qualified(snapshots, demo_spec):
    am7 = snapshots["AM-007"]
    assert evaluate_item(am7, demo_spec).result is MatchResult.NEEDS_INSPECTION

    # 买家不要求拉链：仍需检查，未知不等于完好
    no_zip_requirement = replace(demo_spec, required_conditions=(F,))
    assert evaluate_item(am7, no_zip_requirement).result is MatchResult.NEEDS_INSPECTION

    # 买家甚至接受任何程度的拉链卡滞：仍需检查，未知不等于已接受
    accepts_any_jam = replace(
        demo_spec,
        required_conditions=(F,),
        accepted_defects=demo_spec.accepted_defects + (AcceptedDefect(Z, DefectType.JAM, Severity.SEVERE),),
    )
    assert evaluate_item(am7, accepts_any_jam).result is MatchResult.NEEDS_INSPECTION


def test_missing_point_is_treated_as_unknown(demo_spec):
    item = make_item(front=OK, back=defect_state((DefectType.SCRATCH, Severity.MINOR)))  # 没有 ZIPPER
    m = evaluate_item(item, demo_spec)
    assert m.result is MatchResult.NEEDS_INSPECTION and m.unknown_points == (Z,)


def test_am007_after_inspection(snapshots, demo_spec):
    am7 = snapshots["AM-007"]

    passed = am7.with_points({Z: PointState(PointStatus.OK, evidence_ids=("EV-0001",))})
    ev = evaluate([passed], demo_spec)
    assert ev.result_for("AM-007").result is MatchResult.QUALIFIED
    assert ev.result_for("AM-007").condition_version == 2
    assert ev.ranking == ("AM-007",) and ev.qualified == ("AM-007",)

    failed = am7.with_points({Z: defect_state((DefectType.JAM, Severity.MODERATE))})
    m = evaluate_item(failed, demo_spec)
    assert m.result is MatchResult.EXCLUDED
    assert m.reason_codes == (R.REQUIREMENT_NOT_MET,)
    assert [(d.code, d.point) for d in m.details] == [(R.REQUIREMENT_NOT_MET, Z)]

    # 检查无法确定：拉链保持 UNKNOWN，以请求级排除项排除
    ev = evaluate([am7], demo_spec, {"AM-007": [R.INSPECTION_INCONCLUSIVE]})
    assert ev.result_for("AM-007").result is MatchResult.EXCLUDED
    assert ev.result_for("AM-007").reason_codes == (R.INSPECTION_INCONCLUSIVE,)
    assert ev.ranking == ()


@pytest.mark.parametrize(
    "code", [R.INSPECTION_OVER_BUDGET, R.INSPECTION_NOT_WORTHWHILE, R.ECONOMICS_INPUT_INVALID]
)
def test_economics_skip_excludes_item(snapshots, demo_spec, code):
    ev = evaluate(snapshots.values(), demo_spec, {"AM-007": [code]})
    assert ev.result_for("AM-007").result is MatchResult.EXCLUDED
    assert ev.result_for("AM-007").reason_codes == (code,)
    assert ev.ranking == ()


def test_only_request_level_codes_can_be_exclusions(snapshots, demo_spec):
    with pytest.raises(ValueError):
        evaluate_item(snapshots["AM-007"], demo_spec, [R.REQUIREMENT_NOT_MET])


# ----------------------------------------------- 所有缺陷都必须被买家接受


def test_one_unaccepted_defect_is_enough_to_exclude(snapshots, demo_spec):
    m = evaluate_item(snapshots["AM-010"], demo_spec)  # 背面划痕已接受，污渍未接受
    assert m.result is MatchResult.EXCLUDED
    assert [d.defect_id for d in m.details] == ["DF-AM010-BACK-2"]

    also_stain = replace(
        demo_spec,
        accepted_defects=demo_spec.accepted_defects + (AcceptedDefect(B, DefectType.STAIN, Severity.MINOR),),
    )
    assert evaluate_item(snapshots["AM-010"], also_stain).result is MatchResult.QUALIFIED


def test_acceptance_requires_same_point_type_and_severity(snapshots, demo_spec):
    # 正面接受污渍，不能覆盖背面的污渍
    wrong_point = replace(
        demo_spec,
        required_conditions=(Z,),
        accepted_defects=demo_spec.accepted_defects + (AcceptedDefect(F, DefectType.STAIN, Severity.SEVERE),),
    )
    assert evaluate_item(snapshots["AM-003"], wrong_point).result is MatchResult.EXCLUDED

    # 接受划痕，不能覆盖污渍
    assert evaluate_item(snapshots["AM-003"], demo_spec).reason_codes == (R.UNACCEPTED_DEFECT,)

    # 程度：最高轻微不覆盖中等，最高中等可以
    assert evaluate_item(snapshots["AM-004"], demo_spec).result is MatchResult.EXCLUDED
    moderate_ok = replace(demo_spec, accepted_defects=(AcceptedDefect(B, DefectType.SCRATCH, Severity.MODERATE),))
    assert evaluate_item(snapshots["AM-004"], moderate_ok).result is MatchResult.QUALIFIED


@pytest.mark.parametrize(
    "max_severity,actual,covered",
    [
        (Severity.MINOR, Severity.MINOR, True),
        (Severity.MINOR, Severity.MODERATE, False),
        (Severity.MODERATE, Severity.SEVERE, False),
        (Severity.SEVERE, Severity.SEVERE, True),
        (Severity.SEVERE, Severity.MINOR, True),
    ],
)
def test_severity_boundary(max_severity, actual, covered):
    accepted = AcceptedDefect(B, DefectType.SCRATCH, max_severity)
    assert accepted.covers(B, DefectRecord("DF-1", DefectType.SCRATCH, actual)) is covered


def test_no_acceptance_means_any_defect_excludes():
    spec = BuyerRequestSpec("SYN-BAG-A", accepted_defects=(), required_conditions=())
    item = make_item(front=OK, back=defect_state((DefectType.SCRATCH, Severity.MINOR)), zipper=OK)
    assert evaluate_item(item, spec).reason_codes == (R.UNACCEPTED_DEFECT,)


def test_defect_on_required_point_is_requirement_not_met_even_if_listed_as_accepted():
    # 冲突请求会在提交时被拒（需求 2.2）；即使到了匹配层，也按"要求完好"处理
    spec = BuyerRequestSpec(
        "SYN-BAG-A",
        accepted_defects=(AcceptedDefect(F, DefectType.SCRATCH, Severity.SEVERE),),
        required_conditions=(F,),
    )
    assert spec.conflicting_points() == (F,)
    item = make_item(front=defect_state((DefectType.SCRATCH, Severity.MINOR)), back=OK, zipper=OK)
    assert evaluate_item(item, spec).reason_codes == (R.REQUIREMENT_NOT_MET,)


def test_all_applicable_reason_codes_are_recorded(demo_spec):
    item = make_item(
        product="SYN-BAG-B",
        status=ItemStatus.RESERVED,
        front=defect_state((DefectType.SCRATCH, Severity.MINOR)),
        back=defect_state((DefectType.STAIN, Severity.MINOR)),
        zipper=UNKNOWN,
    )
    m = evaluate_item(item, demo_spec, [R.INSPECTION_NOT_WORTHWHILE])
    assert m.reason_codes == (
        R.PRODUCT_MISMATCH, R.NOT_AVAILABLE, R.REQUIREMENT_NOT_MET, R.UNACCEPTED_DEFECT, R.INSPECTION_NOT_WORTHWHILE,
    )
    assert m.result is MatchResult.EXCLUDED and m.unknown_points == (Z,)


# --------------------------------------------------------------- 排名


def test_ranking_qualified_first_then_item_id(demo_spec):
    qualified = dict(front=OK, back=OK, zipper=OK)
    needs = dict(front=OK, back=OK, zipper=UNKNOWN)
    items = [
        make_item("AM-020", **qualified),
        make_item("AM-011", **needs),
        make_item("AM-015", **qualified),
        make_item("AM-012", status=ItemStatus.INTAKE, **qualified),
    ]
    ev = evaluate(items, demo_spec)
    assert ev.ranking == ("AM-015", "AM-020", "AM-011")
    assert [m.item_id for m in ev.results] == ["AM-011", "AM-012", "AM-015", "AM-020"]


def test_duplicate_item_ids_rejected(demo_spec):
    with pytest.raises(ValueError):
        evaluate([make_item("AM-001", front=OK), make_item("AM-001", front=OK)], demo_spec)


# ------------------------------------------------------- 基于属性的测试

_defect = st.builds(
    DefectRecord,
    defect_id=st.sampled_from(["DF-1", "DF-2", "DF-3"]),
    type=st.sampled_from(list(DefectType)),
    severity=st.sampled_from(list(Severity)),
)
_point_state = st.one_of(
    st.just(UNKNOWN),
    st.just(OK),
    st.lists(_defect, min_size=1, max_size=2).map(
        lambda ds: PointState(PointStatus.DEFECT, tuple(ds), ("EV-X",))
    ),
)
_item = st.builds(
    ItemSnapshot,
    item_id=st.sampled_from([f"AM-{n:03d}" for n in range(1, 16)]),
    product_id=st.sampled_from(["SYN-BAG-A", "SYN-BAG-B"]),
    tag_code=st.just("SYN-T-TEST"),
    item_status=st.sampled_from(list(ItemStatus)),
    condition_version=st.just(1),
    points=st.fixed_dictionaries({p: _point_state for p in Point}),
)
_items = st.lists(_item, max_size=8, unique_by=lambda i: i.item_id)


@st.composite
def _request(draw) -> BuyerRequestSpec:
    required = draw(st.sets(st.sampled_from(list(Point))))
    remaining = [p for p in Point if p not in required]
    accepted = ()
    if remaining:
        accepted = tuple(
            draw(
                st.lists(
                    st.builds(
                        AcceptedDefect,
                        point=st.sampled_from(remaining),
                        defect_type=st.sampled_from(list(DefectType)),
                        max_severity=st.sampled_from(list(Severity)),
                    ),
                    max_size=4,
                )
            )
        )
    return BuyerRequestSpec(
        product_id=draw(st.sampled_from(["SYN-BAG-A", "SYN-BAG-B"])),
        accepted_defects=accepted,
        required_conditions=tuple(p for p in Point if p in required),
    )


@settings(max_examples=300, deadline=None)
@given(items=_items, request=_request(), data=st.data())
def test_property_deterministic_and_order_independent(items, request, data):
    """属性 1：相同输入得到相同结果，与商品输入顺序无关。"""
    shuffled = data.draw(st.permutations(items))
    assert evaluate(items, request) == evaluate(shuffled, request) == evaluate(items, request)


@settings(max_examples=300, deadline=None)
@given(items=_items, request=_request())
def test_property_unknown_never_qualified(items, request):
    """属性 2：含 UNKNOWN 部位的商品永远不是 QUALIFIED。"""
    ev = evaluate(items, request)
    for item in items:
        if item.unknown_points():
            assert ev.result_for(item.item_id).result is not MatchResult.QUALIFIED


def _should_qualify(item: ItemSnapshot, request: BuyerRequestSpec) -> bool:
    return (
        item.product_id == request.product_id
        and item.item_status is ItemStatus.AVAILABLE
        and not item.unknown_points()
        and all(item.point(p).status is PointStatus.OK for p in request.required_conditions)
        and all(any(a.covers(p, d) for a in request.accepted_defects) for p, d in item.verified_defects())
    )


@settings(max_examples=300, deadline=None)
@given(items=_items, request=_request())
def test_property_qualified_iff_every_defect_accepted_and_required_ok(items, request):
    """属性 3：QUALIFIED 当且仅当型号相符、可售、全部核实、要求部位 OK、每个缺陷都被接受。"""
    ev = evaluate(items, request)
    for item in items:
        qualified = ev.result_for(item.item_id).result is MatchResult.QUALIFIED
        assert qualified == _should_qualify(item, request), item


@settings(max_examples=200, deadline=None)
@given(items=_items, request=_request(), data=st.data())
def test_property_exclusions_and_ranking(items, request, data):
    excluded_ids = data.draw(st.sets(st.sampled_from([i.item_id for i in items]))) if items else set()
    ev = evaluate(items, request, {i: [R.INSPECTION_NOT_WORTHWHILE] for i in excluded_ids})
    for item_id in excluded_ids:
        assert ev.result_for(item_id).result is MatchResult.EXCLUDED
    groups = [ev.result_for(i).result for i in ev.ranking]
    assert groups == sorted(groups, key=lambda r: r is MatchResult.NEEDS_INSPECTION)
    for result in (MatchResult.QUALIFIED, MatchResult.NEEDS_INSPECTION):
        ids = [i for i in ev.ranking if ev.result_for(i).result is result]
        assert ids == sorted(ids)
    assert set(ev.ranking) == {m.item_id for m in ev.results if m.result is not MatchResult.EXCLUDED}
