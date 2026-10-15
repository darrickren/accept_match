"""任务 2：合成种子数据与校验。

- 十件商品、买家、预留与 requirements.md "合成演示数据" 一节一致
- AM-006、AM-007 的初始状态
- 用种子参数独立复算三组经济算例（这里只核对数据，经济判断模块在任务 3.4 实现）
- 各类无效种子都会被 SEED_INVALID 拒绝
"""

from __future__ import annotations

import copy
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

import pytest

from acceptmatch.config import ALLOWED_SEED_ROOT, ConfigError
from acceptmatch.seed import SeedBundle, SeedValidationError, load_seed, parse_seed

D = Decimal

# requirements.md「合成演示数据 / 商品（10 件）」
EXPECTED_ITEMS = {
    "AM-001": ("SYN-BAG-A", "SYN-T-K3P9", "AVAILABLE",
               {"FRONT": ("DEFECT", [("SCRATCH", "MINOR")]), "BACK": ("OK", []), "ZIPPER": ("OK", [])}),
    "AM-002": ("SYN-BAG-A", "SYN-T-7XQ2", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("OK", []), "ZIPPER": ("DEFECT", [("JAM", "MODERATE")])}),
    "AM-003": ("SYN-BAG-A", "SYN-T-M8D4", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("STAIN", "MINOR")]), "ZIPPER": ("OK", [])}),
    "AM-004": ("SYN-BAG-A", "SYN-T-2RVN", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("SCRATCH", "MODERATE")]), "ZIPPER": ("OK", [])}),
    "AM-005": ("SYN-BAG-B", "SYN-T-W6HJ", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("SCRATCH", "MINOR")]), "ZIPPER": ("OK", [])}),
    "AM-006": ("SYN-BAG-A", "SYN-T-9CFL", "RESERVED",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("SCRATCH", "MINOR")]), "ZIPPER": ("OK", [])}),
    "AM-007": ("SYN-BAG-A", "SYN-T-Q5ZT", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("SCRATCH", "MINOR")]), "ZIPPER": ("UNKNOWN", [])}),
    "AM-008": ("SYN-BAG-A", "SYN-T-H1BE", "AVAILABLE",
               {"FRONT": ("UNKNOWN", []), "BACK": ("DEFECT", [("DENT", "MODERATE")]), "ZIPPER": ("OK", [])}),
    "AM-009": ("SYN-BAG-A", "SYN-T-D7YA", "INTAKE",
               {"FRONT": ("UNKNOWN", []), "BACK": ("UNKNOWN", []), "ZIPPER": ("UNKNOWN", [])}),
    "AM-010": ("SYN-BAG-A", "SYN-T-6NUG", "AVAILABLE",
               {"FRONT": ("OK", []), "BACK": ("DEFECT", [("SCRATCH", "MINOR"), ("STAIN", "MINOR")]),
                "ZIPPER": ("OK", [])}),
}


@pytest.fixture(scope="module")
def bundle() -> SeedBundle:
    return load_seed()


@pytest.fixture()
def raw() -> tuple[dict, dict]:
    ds = json.loads((ALLOWED_SEED_ROOT / "dataset.json").read_text(encoding="utf-8"))
    econ = json.loads((ALLOWED_SEED_ROOT / "economics.json").read_text(encoding="utf-8"))
    return ds, econ


def _parse(ds: dict, econ: dict) -> SeedBundle:
    return parse_seed(json.dumps(ds).encode(), json.dumps(econ).encode())


def _item(ds: dict, item_id: str) -> dict:
    return next(i for i in ds["items"] if i["item_id"] == item_id)


# ------------------------------------------------------------ 种子内容核对


def test_ten_items_match_requirements(bundle):
    items = {i.item_id: i for i in bundle.dataset.items}
    assert list(items) == [f"AM-{n:03d}" for n in range(1, 11)]
    for item_id, (product_id, tag, status, points) in EXPECTED_ITEMS.items():
        item = items[item_id]
        assert (item.product_id, item.tag_code, item.item_status) == (product_id, tag, status), item_id
        actual = {
            str(pt): (str(rec.status), [(str(d.type), str(d.severity)) for d in rec.defects])
            for pt, rec in item.points.items()
        }
        assert actual == points, item_id


def test_all_records_marked_synthetic(bundle):
    ds = bundle.dataset
    assert ds.meta.synthetic and ds.price_rules.synthetic and ds.demo_request.synthetic
    for group in (ds.products, ds.buyers, ds.items, ds.inspection_methods, ds.initial_reservations):
        assert all(x.synthetic for x in group)
    assert all(e.synthetic and e.photo_ref.startswith("synthetic://") for i in ds.items for e in i.evidence)
    for econ in bundle.economics.values():
        assert econ.synthetic and all(ie.synthetic for ie in econ.items.values())


def test_am007_initial_state(bundle):
    item = bundle.item("AM-007")
    assert item.tag_code == "SYN-T-Q5ZT"
    assert item.item_status == "AVAILABLE"
    front, back, zipper = item.points["FRONT"], item.points["BACK"], item.points["ZIPPER"]
    assert front.status == "OK" and front.evidence_ids == ["EV-AM007-FRONT-1"]
    assert back.status == "DEFECT" and len(back.defects) == 1
    defect = back.defects[0]
    assert (defect.defect_id, defect.type, defect.severity) == ("DF-AM007-BACK-1", "SCRATCH", "MINOR")
    assert defect.evidence_ids == ["EV-AM007-BACK-1"]
    assert zipper.status == "UNKNOWN" and zipper.evidence_ids == [] and zipper.defects == []
    assert {e.point for e in item.evidence} == {"FRONT", "BACK"}
    # 只有 AM-007 配置了检查经济参数，且待检查部位是拉链
    assert list(bundle.parameter_set("default").items) == ["AM-007"]
    assert bundle.parameter_set("default").items["AM-007"].inspection_points == ["ZIPPER"]


def test_am006_initial_state(bundle):
    item = bundle.item("AM-006")
    assert item.tag_code == "SYN-T-9CFL"
    assert item.item_status == "RESERVED"
    assert item.points["FRONT"].status == "OK"
    assert [(d.type, d.severity) for d in item.points["BACK"].defects] == [("SCRATCH", "MINOR")]
    assert item.points["ZIPPER"].status == "OK"
    reservations = [r for r in bundle.dataset.initial_reservations if r.item_id == "AM-006"]
    assert len(reservations) == 1 and reservations[0].buyer_id == "B-DEMO-02"
    assert len(bundle.dataset.initial_reservations) == 1


def test_buyers_products_and_demo_request(bundle):
    ds = bundle.dataset
    assert [b.buyer_id for b in ds.buyers] == ["B-DEMO-01", "B-DEMO-02"]
    assert {p.product_id: p.base_price for p in ds.products} == {"SYN-BAG-A": D("120.00"), "SYN-BAG-B": D("150.00")}
    assert ds.price_rules.discount_by_severity == {"MINOR": D("0.10"), "MODERATE": D("0.25"), "SEVERE": D("0.40")}
    assert ds.price_rules.floor_ratio == D("0.50")
    req = ds.demo_request
    assert (req.buyer_id, req.product_id) == ("B-DEMO-01", "SYN-BAG-A")
    assert [(a.point, a.defect_type, a.max_severity) for a in req.accepted_defects] == [("BACK", "SCRATCH", "MINOR")]
    assert req.required_conditions == ["FRONT", "ZIPPER"]


# ------------------------------------------------- 经济算例（数据核对，独立复算）


def _q(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _example(bundle: SeedBundle, set_name: str) -> dict:
    econ = bundle.parameter_set(set_name)
    ie = econ.items["AM-007"]
    item = bundle.item("AM-007")
    product = bundle.product(item.product_id)
    rules = bundle.dataset.price_rules
    # 检查通过 = 未知部位没有新缺陷，只剩已核实的缺陷
    rate_sum = sum((rules.discount_by_severity[d.severity] for r in item.points.values() for d in r.defects), D(0))
    pass_quote = _q(max(product.base_price * (1 - rate_sum), product.base_price * rules.floor_ratio))
    r_pass = _q(pass_quote - ie.H)
    s1 = _q(ie.p * r_pass)
    s2 = _q((1 - ie.p) * ie.R_fail)
    ev = _q(s1 + s2 - ie.C)
    best = max(ie.no_inspection_options, key=lambda o: o.net)
    return {
        "pass_quote": pass_quote, "R_pass": r_pass, "s1": s1, "s2": s2, "EV": ev,
        "V_none": best.net, "V_none_kind": best.kind, "dEV": _q(ev - best.net),
        "C": ie.C, "B": econ.budget_initial,
    }


def test_economics_default_example(bundle):
    x = _example(bundle, "default")
    assert x["pass_quote"] == D("108.00") and x["R_pass"] == D("98.00")
    assert (x["s1"], x["s2"], x["EV"]) == (D("78.40"), D("8.00"), D("78.40"))
    assert (x["V_none"], x["V_none_kind"]) == (D("45.00"), "LIQUIDATE_AS_IS")
    assert x["dEV"] == D("33.40") and x["C"] == D("8.00") and x["B"] == D("20.00")
    assert x["C"] <= x["B"] and x["dEV"] > 0  # 应判定 INSPECT
    assert x["B"] - x["C"] == D("12.00")  # 创建任务后的预算


def test_economics_budget_low_example(bundle):
    x = _example(bundle, "budget_low")
    assert (x["EV"], x["dEV"], x["B"]) == (D("78.40"), D("33.40"), D("5.00"))
    assert x["C"] > x["B"]  # 应判定 SKIP：INSPECTION_OVER_BUDGET


def test_economics_low_pass_example(bundle):
    x = _example(bundle, "low_pass")
    assert (x["s1"], x["s2"]) == (D("9.80"), D("36.00"))
    assert (x["EV"], x["dEV"], x["B"]) == (D("37.80"), D("-7.20"), D("20.00"))
    assert x["C"] <= x["B"] and x["dEV"] <= 0  # 应判定 SKIP：INSPECTION_NOT_WORTHWHILE


def test_parameter_sets_are_fixed(bundle):
    assert bundle.parameter_sets == ("default", "budget_low", "low_pass")
    with pytest.raises(ValueError):
        bundle.parameter_set("nope")


# ------------------------------------------------------------ 加载与路径


def test_seed_hash_is_stable_and_content_sensitive(raw):
    ds, econ = raw
    h1, h2 = _parse(ds, econ).seed_hash, _parse(ds, econ).seed_hash
    assert h1 == h2 and len(h1) == 64
    ds2 = copy.deepcopy(ds)
    ds2["meta"]["notice"] += "。"
    assert _parse(ds2, econ).seed_hash != h1


def test_load_seed_rejects_dir_outside_repo_seed(tmp_path):
    (tmp_path / "dataset.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_seed(tmp_path)


def test_invalid_json_reported():
    with pytest.raises(SeedValidationError) as exc:
        parse_seed(b"{not json", b"{}")
    assert exc.value.code == "SEED_INVALID"
    assert any("JSON 解析失败" in p for p in exc.value.problems)


# ------------------------------------------------------------ 无效种子


def _set(path: list, value) -> Callable[[dict, dict], None]:
    """生成修改函数：path 第一个元素是 'ds' 或 'econ'；遇到列表时按 item_id 定位商品。"""

    def mutate(ds: dict, econ: dict) -> None:
        node = ds if path[0] == "ds" else econ
        keys = path[1:]
        for key in keys[:-1]:
            node = next(i for i in node if i.get("item_id") == key) if isinstance(node, list) else node[key]
        node[keys[-1]] = value

    return mutate


INVALID_CASES = [
    ("重复 Tag_Code", (lambda ds, e: _item(ds, "AM-002").update(tag_code="SYN-T-K3P9")), "tag_code 重复"),
    ("重复 Item_ID", (lambda ds, e: _item(ds, "AM-002").update(item_id="AM-001")), "item_id 重复"),
    ("OK 部位没有 Evidence", _set(["ds", "items", "AM-001", "points", "BACK", "evidence_ids"], []),
     "状态为 OK，但没有 Evidence"),
    ("缺陷没有 Evidence", (lambda ds, e: _item(ds, "AM-007")["points"]["BACK"]["defects"][0].update(evidence_ids=[])),
     "缺陷 DF-AM007-BACK-1 没有 Evidence"),
    ("引用不存在的 Evidence", _set(["ds", "items", "AM-007", "points", "FRONT", "evidence_ids"], ["EV-NOPE-1"]),
     "引用的 Evidence 不存在"),
    ("Evidence 部位不一致", _set(["ds", "items", "AM-007", "points", "FRONT", "evidence_ids"], ["EV-AM007-BACK-1"]),
     "与 FRONT 不一致"),
    ("UNKNOWN 部位带 Evidence", _set(["ds", "items", "AM-007", "points", "ZIPPER", "evidence_ids"], ["EV-AM007-FRONT-1"]),
     "状态为 UNKNOWN"),
    ("缺少部位", (lambda ds, e: _item(ds, "AM-007")["points"].pop("ZIPPER")), "缺少部位 ZIPPER"),
    ("商品合成标记为 false", _set(["ds", "items", "AM-003", "synthetic"], False), "合成数据标记必须为 true"),
    ("Evidence 缺少合成标记", (lambda ds, e: _item(ds, "AM-003")["evidence"][0].pop("synthetic")),
     "evidence[0].synthetic"),
    ("照片引用不是 synthetic://", (lambda ds, e: _item(ds, "AM-003")["evidence"][0].update(photo_ref="/Users/x/a.jpg")),
     "photo_ref"),
    ("种子 Evidence 来源不是 INTAKE", (lambda ds, e: _item(ds, "AM-003")["evidence"][0].update(source="INSPECTION")),
     "来源必须是 INTAKE"),
    ("Tag_Code 不是合成格式", _set(["ds", "items", "AM-003", "tag_code"], "ABC-123"), "tag_code"),
    ("未知字段", _set(["ds", "items", "AM-003", "note"], "x"), "Extra inputs are not permitted"),
    ("商品型号不存在", _set(["ds", "items", "AM-003", "product_id"], "SYN-BAG-Z"), "product_id 不存在"),
    ("RESERVED 商品没有预留", _set(["ds", "initial_reservations"], []), "没有对应的初始预留"),
    ("预留指向非 RESERVED 商品", (lambda ds, e: ds["initial_reservations"][0].update(item_id="AM-007")),
     "不是 RESERVED"),
    ("预留买家不存在", (lambda ds, e: ds["initial_reservations"][0].update(buyer_id="B-NOPE")), "买家不存在"),
    ("检查方法缺少部位", (lambda ds, e: ds.update(inspection_methods=ds["inspection_methods"][:2])),
     "inspection_methods 缺少部位 ZIPPER"),
    ("演示请求部位冲突", _set(["ds", "demo_request", "required_conditions"], ["FRONT", "ZIPPER", "BACK"]),
     "同时出现在 accepted_defects 和 required_conditions"),
    ("金额写成数字", (lambda ds, e: ds["products"][0].update(base_price=120.0)), "两位小数的字符串"),
    ("金额三位小数", (lambda ds, e: ds["products"][0].update(base_price="120.000")), "两位小数的字符串"),
    ("检查成本为负", _set(["econ", "base", "items", "AM-007", "C"], "-8.00"), "金额不能为负"),
    ("参数组中 p 超出范围", _set(["econ", "parameter_sets", "low_pass", "items", "AM-007", "p"], "1.50"),
     "parameter_sets.low_pass.items.AM-007"),
    ("不检查选项包含向买家报价", (lambda ds, e: e["base"]["items"]["AM-007"]["no_inspection_options"][0].update(
        kind="SELL_TO_BUYER")), "no_inspection_options[0].kind"),
    ("不检查选项类型重复", (lambda ds, e: e["base"]["items"]["AM-007"]["no_inspection_options"][1].update(
        kind="LIQUIDATE_AS_IS")), "不检查选项类型重复"),
    ("缺少参数组", (lambda ds, e: e["parameter_sets"].pop("low_pass")), "缺少参数组 low_pass"),
    ("参数组覆盖未知商品", _set(["econ", "parameter_sets", "low_pass", "items", "AM-999"], {"p": "0.10"}),
     "base 中没有该商品的参数"),
    ("参数组覆盖未知字段", _set(["econ", "parameter_sets", "low_pass", "items", "AM-007", "q"], "0.10"),
     "Extra inputs are not permitted"),
    ("经济参数商品不在数据集中", (lambda ds, e: e["base"]["items"].update(
        {"AM-999": copy.deepcopy(e["base"]["items"]["AM-007"])})), "dataset.json 中没有商品 AM-999"),
    ("待检查部位在种子中不是 UNKNOWN", _set(["econ", "base", "items", "AM-007", "inspection_points"], ["FRONT"]),
     "应为 UNKNOWN"),
    ("经济参数合成标记为 false", _set(["econ", "base", "items", "AM-007", "synthetic"], False), "合成数据标记必须为 true"),
    ("经济文件缺少合成标记", (lambda ds, e: e.pop("synthetic")), "economics.json: synthetic"),
]


@pytest.mark.parametrize("name,mutate,fragment", INVALID_CASES, ids=[c[0] for c in INVALID_CASES])
def test_invalid_seed_rejected(raw, name, mutate, fragment):
    ds, econ = copy.deepcopy(raw[0]), copy.deepcopy(raw[1])
    mutate(ds, econ)
    with pytest.raises(SeedValidationError) as exc:
        _parse(ds, econ)
    assert exc.value.code == "SEED_INVALID"
    assert any(fragment in p for p in exc.value.problems), exc.value.problems


def test_all_problems_reported_together(raw):
    ds, econ = copy.deepcopy(raw[0]), copy.deepcopy(raw[1])
    _item(ds, "AM-002")["tag_code"] = "SYN-T-K3P9"
    econ["base"]["items"]["AM-007"]["C"] = "-8.00"
    econ["parameter_sets"].pop("budget_low")
    with pytest.raises(SeedValidationError) as exc:
        _parse(ds, econ)
    problems = exc.value.problems
    assert any("tag_code 重复" in p for p in problems)
    assert any("金额不能为负" in p for p in problems)
    assert any("缺少参数组 budget_low" in p for p in problems)
