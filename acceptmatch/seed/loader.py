"""种子数据加载与校验（需求 1.1–1.5、4.4、4.10）。

- load_seed：确认种子目录位于仓库 data/seed/ 下，只读取 dataset.json 和 economics.json。
- parse_seed：纯校验，不访问文件系统。收集全部问题后一次性抛出 SeedValidationError（SEED_INVALID）。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from acceptmatch.config import ALLOWED_SEED_ROOT, ConfigError, validate_seed_dir
from acceptmatch.domain.enums import EvidenceSource, ItemStatus, Point, PointStatus
from acceptmatch.seed.models import Dataset, EconomicsFile, Item, ItemEconomics, Product

DATASET_FILE = "dataset.json"
ECONOMICS_FILE = "economics.json"
REQUIRED_PARAMETER_SETS = ("default", "budget_low", "low_pass")

M = TypeVar("M", bound=BaseModel)


class SeedValidationError(Exception):
    """种子数据无效。problems 列出所有发现的问题。"""

    code = "SEED_INVALID"

    def __init__(self, problems: Iterable[str]):
        self.problems: list[str] = list(problems)
        super().__init__(f"{self.code}: " + "；".join(self.problems))


@dataclass(frozen=True)
class ResolvedEconomics:
    """某个参数组合并后的经济参数（全部为合成值）。"""

    parameter_set: str
    budget_initial: Decimal
    items: Mapping[str, ItemEconomics]
    synthetic: bool = True


@dataclass(frozen=True)
class SeedBundle:
    dataset: Dataset
    economics: Mapping[str, ResolvedEconomics]
    seed_hash: str

    @property
    def parameter_sets(self) -> tuple[str, ...]:
        return tuple(self.economics)

    def parameter_set(self, name: str) -> ResolvedEconomics:
        try:
            return self.economics[name]
        except KeyError:
            raise ValueError(f"未知参数组 {name!r}，可选：{', '.join(self.economics)}") from None

    def item(self, item_id: str) -> Item:
        for item in self.dataset.items:
            if item.item_id == item_id:
                return item
        raise KeyError(item_id)

    def product(self, product_id: str) -> Product:
        for product in self.dataset.products:
            if product.product_id == product_id:
                return product
        raise KeyError(product_id)


# --------------------------------------------------------------------- 入口


def load_seed(seed_dir: Path | str | None = None) -> SeedBundle:
    directory = validate_seed_dir(seed_dir if seed_dir is not None else ALLOWED_SEED_ROOT)
    problems: list[str] = []
    contents: dict[str, bytes] = {}
    for name in (DATASET_FILE, ECONOMICS_FILE):
        resolved = (directory / name).resolve()
        try:
            # 防止通过符号链接指向种子目录以外的文件
            validate_seed_dir(resolved.parent)
        except ConfigError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if not resolved.is_file():
            problems.append(f"{name}: 文件不存在（{resolved}）")
            continue
        contents[name] = resolved.read_bytes()
    if problems:
        raise SeedValidationError(problems)
    return parse_seed(contents[DATASET_FILE], contents[ECONOMICS_FILE])


def parse_seed(dataset_bytes: bytes, economics_bytes: bytes) -> SeedBundle:
    problems: list[str] = []
    dataset_data = _load_json(dataset_bytes, DATASET_FILE, problems)
    economics_data = _load_json(economics_bytes, ECONOMICS_FILE, problems)
    dataset = _validate(Dataset, dataset_data, DATASET_FILE, problems)
    economics_file = _validate(EconomicsFile, economics_data, ECONOMICS_FILE, problems)

    # 在原始 JSON 上检查必需参数组，这样即使其他字段无效也能一并报告
    if isinstance(economics_data, dict) and isinstance(economics_data.get("parameter_sets"), dict):
        for name in REQUIRED_PARAMETER_SETS:
            if name not in economics_data["parameter_sets"]:
                problems.append(f"{ECONOMICS_FILE}: 缺少参数组 {name}")

    if dataset is not None:
        problems.extend(_check_dataset(dataset))

    resolved: dict[str, ResolvedEconomics] = {}
    if economics_file is not None:
        resolved, econ_problems = _resolve_parameter_sets(economics_file)
        problems.extend(econ_problems)
        if dataset is not None:
            problems.extend(_check_economics_against_dataset(economics_file, resolved, dataset))

    if problems:
        raise SeedValidationError(problems)
    assert dataset is not None
    return SeedBundle(
        dataset=dataset,
        economics=MappingProxyType(resolved),
        seed_hash=_seed_hash(dataset_bytes, economics_bytes),
    )


# ------------------------------------------------------------------ 解析辅助


_INVALID_JSON = object()


def _load_json(raw: bytes, file_name: str, problems: list[str]) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{file_name}: JSON 解析失败（{exc}）")
        return _INVALID_JSON


def _validate(model: type[M], data: object, file_name: str, problems: list[str]) -> M | None:
    if data is _INVALID_JSON:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.extend(_format_errors(exc, file_name))
        return None


def _format_errors(exc: ValidationError, prefix: str) -> list[str]:
    return [f"{prefix}: {_loc(err['loc'])}: {err['msg']}" for err in exc.errors()]


def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<根>"


def _duplicates(values: Iterable[str]) -> list[str]:
    return [value for value, count in Counter(values).items() if count > 1]


def _seed_hash(dataset_bytes: bytes, economics_bytes: bytes) -> str:
    digest = hashlib.sha256()
    for name, content in ((DATASET_FILE, dataset_bytes), (ECONOMICS_FILE, economics_bytes)):
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


# ------------------------------------------------------------ dataset 一致性


def _check_dataset(ds: Dataset) -> list[str]:
    p: list[str] = []
    f = DATASET_FILE

    for label, values in (
        ("product_id", [x.product_id for x in ds.products]),
        ("buyer_id", [x.buyer_id for x in ds.buyers]),
        ("item_id", [x.item_id for x in ds.items]),
        ("tag_code", [x.tag_code for x in ds.items]),
        ("evidence_id", [e.evidence_id for x in ds.items for e in x.evidence]),
        ("defect_id", [d.defect_id for x in ds.items for r in x.points.values() for d in r.defects]),
        ("reservation_id", [x.reservation_id for x in ds.initial_reservations]),
    ):
        for dup in _duplicates(values):
            p.append(f"{f}: {label} 重复：{dup}")

    product_ids = {x.product_id for x in ds.products}
    buyer_ids = {x.buyer_id for x in ds.buyers}
    items_by_id = {x.item_id: x for x in ds.items}

    method_points = [m.point for m in ds.inspection_methods]
    for dup in _duplicates(method_points):
        p.append(f"{f}: inspection_methods 中部位重复：{dup}")
    for missing in [pt for pt in Point if pt not in method_points]:
        p.append(f"{f}: inspection_methods 缺少部位 {missing}")

    req = ds.demo_request
    if req.buyer_id not in buyer_ids:
        p.append(f"{f}: demo_request.buyer_id 不存在：{req.buyer_id}")
    if req.product_id not in product_ids:
        p.append(f"{f}: demo_request.product_id 不存在：{req.product_id}")
    conflicts = sorted({a.point for a in req.accepted_defects} & set(req.required_conditions))
    for pt in conflicts:
        p.append(f"{f}: demo_request 中部位 {pt} 同时出现在 accepted_defects 和 required_conditions")
    for dup in _duplicates(req.required_conditions):
        p.append(f"{f}: demo_request.required_conditions 部位重复：{dup}")

    for item in ds.items:
        p.extend(_check_item(item, product_ids))

    reserved_counts = Counter(r.item_id for r in ds.initial_reservations)
    for r in ds.initial_reservations:
        where = f"{f}: initial_reservations[{r.reservation_id}]"
        target = items_by_id.get(r.item_id)
        if target is None:
            p.append(f"{where}: 商品不存在：{r.item_id}")
        elif target.item_status is not ItemStatus.RESERVED:
            p.append(f"{where}: 商品 {r.item_id} 的状态是 {target.item_status}，不是 RESERVED")
        if r.buyer_id not in buyer_ids:
            p.append(f"{where}: 买家不存在：{r.buyer_id}")
    for item_id, count in reserved_counts.items():
        if count > 1:
            p.append(f"{f}: 商品 {item_id} 有 {count} 条初始预留，最多只能有一条")
    for item in ds.items:
        if item.item_status is ItemStatus.RESERVED and reserved_counts[item.item_id] == 0:
            p.append(f"{f}: items[{item.item_id}]: 状态为 RESERVED，但没有对应的初始预留")
    return p


def _check_item(item: Item, product_ids: set[str]) -> list[str]:
    p: list[str] = []
    where = f"{DATASET_FILE}: items[{item.item_id}]"

    if item.product_id not in product_ids:
        p.append(f"{where}: product_id 不存在：{item.product_id}")

    missing = [pt for pt in Point if pt not in item.points]
    for pt in missing:
        p.append(f"{where}: 缺少部位 {pt}")

    evidence_by_id = {e.evidence_id: e for e in item.evidence}
    referenced: set[str] = set()

    for pt, rec in item.points.items():
        pw = f"{where}.points.{pt}"
        refs: list[str] = list(rec.evidence_ids)
        if rec.status is PointStatus.OK:
            if rec.defects:
                p.append(f"{pw}: 状态为 OK，但列出了缺陷")
            if not rec.evidence_ids:
                p.append(f"{pw}: 状态为 OK，但没有 Evidence")
        elif rec.status is PointStatus.DEFECT:
            if not rec.defects:
                p.append(f"{pw}: 状态为 DEFECT，但没有列出缺陷")
            for d in rec.defects:
                if not d.evidence_ids:
                    p.append(f"{pw}: 缺陷 {d.defect_id} 没有 Evidence")
                refs.extend(d.evidence_ids)
        else:  # UNKNOWN
            if rec.defects or rec.evidence_ids:
                p.append(f"{pw}: 状态为 UNKNOWN，不应包含缺陷或 Evidence")
        for ref in refs:
            ev = evidence_by_id.get(ref)
            if ev is None:
                p.append(f"{pw}: 引用的 Evidence 不存在：{ref}")
            elif ev.point is not pt:
                p.append(f"{pw}: Evidence {ref} 属于部位 {ev.point}，与 {pt} 不一致")
            referenced.add(ref)

    for ev in item.evidence:
        if ev.evidence_id not in referenced:
            p.append(f"{where}: Evidence {ev.evidence_id} 没有被任何部位引用")
        if ev.source is not EvidenceSource.INTAKE:
            p.append(f"{where}: 种子 Evidence {ev.evidence_id} 的来源必须是 INTAKE")
    return p


# ---------------------------------------------------------- economics 一致性


def _check_item_economics(ie: ItemEconomics, where: str) -> list[str]:
    p: list[str] = []
    for dup in _duplicates(o.kind for o in ie.no_inspection_options):
        p.append(f"{where}: 不检查选项类型重复：{dup}")
    for dup in _duplicates(ie.inspection_points):
        p.append(f"{where}: inspection_points 部位重复：{dup}")
    return p


def _resolve_parameter_sets(ef: EconomicsFile) -> tuple[dict[str, ResolvedEconomics], list[str]]:
    p: list[str] = []
    f = ECONOMICS_FILE

    for item_id, ie in ef.base.items.items():
        p.extend(_check_item_economics(ie, f"{f}: base.items.{item_id}"))

    resolved: dict[str, ResolvedEconomics] = {}
    for name, override in ef.parameter_sets.items():
        prefix = f"{f}: parameter_sets.{name}"
        for unknown in sorted(set(override.items) - set(ef.base.items)):
            p.append(f"{prefix}.items.{unknown}: base 中没有该商品的参数")

        items: dict[str, ItemEconomics] = {}
        for item_id, base_item in ef.base.items.items():
            item_override = override.items.get(item_id)
            if item_override is None:
                items[item_id] = base_item
                continue
            merged = base_item.model_dump(mode="json")
            merged.update(item_override.model_dump(mode="json", exclude_none=True))
            try:
                items[item_id] = ItemEconomics.model_validate(merged)
            except ValidationError as exc:
                p.extend(_format_errors(exc, f"{prefix}.items.{item_id}"))
                continue
            p.extend(_check_item_economics(items[item_id], f"{prefix}.items.{item_id}"))

        budget = override.budget_initial if override.budget_initial is not None else ef.base.budget_initial
        resolved[name] = ResolvedEconomics(
            parameter_set=name,
            budget_initial=budget,
            items=MappingProxyType(items),
        )
    return resolved, p


def _check_economics_against_dataset(
    ef: EconomicsFile, resolved: Mapping[str, ResolvedEconomics], ds: Dataset
) -> list[str]:
    p: list[str] = []
    items_by_id = {x.item_id: x for x in ds.items}

    def check(item_id: str, ie: ItemEconomics, where: str) -> None:
        item = items_by_id.get(item_id)
        if item is None:
            p.append(f"{where}: dataset.json 中没有商品 {item_id}")
            return
        for pt in ie.inspection_points:
            rec = item.points.get(pt)
            if rec is None or rec.status is not PointStatus.UNKNOWN:
                status = rec.status if rec is not None else "缺失"
                p.append(f"{where}: 待检查部位 {pt} 在种子中应为 UNKNOWN，实际为 {status}")

    for item_id, ie in ef.base.items.items():
        check(item_id, ie, f"{ECONOMICS_FILE}: base.items.{item_id}")
    for name, override in ef.parameter_sets.items():
        for item_id in override.items:
            if name in resolved and item_id in resolved[name].items and item_id in ef.base.items:
                check(item_id, resolved[name].items[item_id], f"{ECONOMICS_FILE}: parameter_sets.{name}.items.{item_id}")
    return p
