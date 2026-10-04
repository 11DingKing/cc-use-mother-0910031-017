"""按任意日期展开多层需求，并为每项用量给出完整来源链。

两种展开模式：

- ``snapshot``（默认）：取根件在该日期生效的版本，沿其签署快照中固定的
  精确版本逐层展开——任意日期查询结果与签署时刻一致、可复现；
- ``current``：在该日期对每层重新选取生效的已发布版本（仍只会选到冻结版本，
  草稿永不参与），用于“按今天最新发布口径重算”。

每个展开节点记录：来源 BOM 版本、行号、单位用量、损耗率、单位换算系数、
是否经替代料引入、以及从根到该节点的来源路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .errors import BomError
from .models import parse_date
from .store import Store


@dataclass
class UsageSource:
    level: int
    bom_version: str                 # 提供该用量行的（冻结）BOM 版本
    bom_revision: int | None
    line_no: int
    parent_material: str
    material: str
    qty_per: float
    scrap_rate: float
    line_unit: str
    base_unit: str
    conversion_factor: float        # 行单位 -> 物料基本单位
    parent_demand: float            # 展开到该行时父件需求量（父件基本单位口径）
    required_qty: float             # 本层毛需求 = parent_demand * qty_per * (1+scrap)，已换算基本单位
    via_alternative_of: str | None  # 经哪一个标准子件的替代关系引入
    alternative_ratio: float | None
    effective_lines_on: str         # 本次展开所用日期
    path: tuple[str, ...]           # 物料来源链
    pinned: bool                    # 是否来自签署快照固定版本


@dataclass
class ExplosionResult:
    root_material: str
    root_qty: float
    on_date: str
    mode: str
    root_version: str | None
    items: list[UsageSource] = field(default_factory=list)
    aggregated: dict[str, dict] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_material": self.root_material,
            "root_qty": self.root_qty,
            "on_date": self.on_date,
            "mode": self.mode,
            "root_version": self.root_version,
            "items": [vars(i) for i in self.items],
            "aggregated": self.aggregated,
            "unresolved": self.unresolved,
        }


def explode(
    store: Store,
    root_material: str,
    root_qty: float,
    on_date: str | date,
    *,
    mode: str = "snapshot",
    branch: str | None = None,
    prefer_alternative: str | None = None,
) -> ExplosionResult:
    """展开需求。

    prefer_alternative: 可选物料编码；当某行存在该替代料且在当日有效时，
    用替代料展开（按 ratio 调整用量），否则走标准子件。
    """
    if mode not in ("snapshot", "current"):
        raise BomError("mode 只能是 snapshot 或 current")
    day = parse_date(on_date)
    store.require_material(root_material)
    if root_qty <= 0:
        raise BomError("展开数量必须为正数")

    root_hdr = store.effective_version(root_material, day, branch)
    result = ExplosionResult(
        root_material=root_material,
        root_qty=root_qty,
        on_date=day.isoformat(),
        mode=mode,
        root_version=root_hdr.code if root_hdr else None,
    )
    if root_hdr is None:
        result.unresolved.append({
            "material": root_material, "reason": "该日期没有已发布且生效的 BOM 版本",
        })
        return result

    _walk(store, result, root_hdr, root_qty, day, mode, 0,
          (root_material,), prefer_alternative)
    _aggregate(result)
    return result


def _lines_for(store: Store, hdr, day: date, mode: str):
    """返回 (版本头, 快照)。snapshot 模式用版本自身冻结快照；
    current 模式切到当日生效版本及其快照。"""
    if mode == "current":
        current = store.effective_version(hdr.material, day, hdr.branch) or \
                  store.effective_version(hdr.material, day, "main")
        if current is not None and current.code != hdr.code:
            hdr = current
    snap = store.snapshots.get(hdr.code)
    return hdr, snap


def _walk(store, result, hdr, parent_demand, day, mode, level, path, prefer_alt):
    hdr, snap = _lines_for(store, hdr, day, mode)
    if snap is None:  # 理论不可达：已发布版本必有快照
        result.unresolved.append({"material": hdr.material, "reason": f"版本 {hdr.code} 缺少冻结快照"})
        return
    for fl in snap.lines:
        if not _frozen_line_effective(fl, day):
            continue
        child = store.materials.get(fl.child_material)
        base_unit = child.base_unit if child else fl.unit
        try:
            factor = store.units.factor(fl.unit, base_unit)
        except Exception:  # noqa: BLE001
            factor = 1.0
            result.unresolved.append({
                "material": fl.child_material,
                "reason": f"单位 {fl.unit} -> {base_unit} 无法换算，按系数 1 处理",
            })

        chosen_alt = None
        if prefer_alt:
            chosen_alt = next(
                (a for a in fl.alternatives
                 if a.alt_material == prefer_alt and _alt_effective(a, day)),
                None,
            )

        if chosen_alt is None:
            required = parent_demand * fl.qty_per * (1.0 + fl.scrap_rate) * factor
            item = UsageSource(
                level=level + 1,
                bom_version=hdr.code,
                bom_revision=hdr.revision,
                line_no=fl.line_no,
                parent_material=hdr.material,
                material=fl.child_material,
                qty_per=fl.qty_per,
                scrap_rate=fl.scrap_rate,
                line_unit=fl.unit,
                base_unit=base_unit,
                conversion_factor=factor,
                parent_demand=round(parent_demand, 6),
                required_qty=round(required, 6),
                via_alternative_of=None,
                alternative_ratio=None,
                effective_lines_on=day.isoformat(),
                path=path + (f"{fl.child_material}@{hdr.code}/L{fl.line_no}",),
                pinned=(mode == "snapshot"),
            )
            result.items.append(item)
            _descend(store, result, fl.child_material, fl.child_version, required,
                     day, mode, level + 1, path + (fl.child_material,), prefer_alt)
        else:
            alt_material = chosen_alt.alt_material
            amat = store.materials.get(alt_material)
            alt_base = amat.base_unit if amat else fl.unit
            try:
                alt_factor = store.units.factor(fl.unit, alt_base)
            except Exception:  # noqa: BLE001
                alt_factor = 1.0
            required = parent_demand * fl.qty_per * chosen_alt.ratio * (1.0 + fl.scrap_rate) * alt_factor
            item = UsageSource(
                level=level + 1,
                bom_version=hdr.code,
                bom_revision=hdr.revision,
                line_no=fl.line_no,
                parent_material=hdr.material,
                material=alt_material,
                qty_per=round(fl.qty_per * chosen_alt.ratio, 6),
                scrap_rate=fl.scrap_rate,
                line_unit=fl.unit,
                base_unit=alt_base,
                conversion_factor=alt_factor,
                parent_demand=round(parent_demand, 6),
                required_qty=round(required, 6),
                via_alternative_of=fl.child_material,
                alternative_ratio=chosen_alt.ratio,
                effective_lines_on=day.isoformat(),
                path=path + (f"{alt_material}@{hdr.code}/L{fl.line_no}替代",),
                pinned=True,
            )
            result.items.append(item)
            _descend(store, result, alt_material, chosen_alt.alt_version, required,
                     day, mode, level + 1, path + (alt_material,), prefer_alt)


def _descend(store, result, material, pinned_version_code, required, day, mode, level, path, prefer_alt):
    if not store.versions_of(material):
        return  # 外购叶子件，不再向下展开
    if mode == "snapshot":
        if not pinned_version_code:
            result.unresolved.append({
                "material": material,
                "reason": "快照中该子件未固定版本（发布时为叶子，之后却出现了 BOM）",
            })
            return
        hdr = store.boms.get(pinned_version_code)
        if hdr is None:
            result.unresolved.append({"material": material, "reason": f"固定版本 {pinned_version_code} 已丢失"})
            return
    else:
        hdr = store.effective_version(material, day, None)
        if hdr is None:
            result.unresolved.append({"material": material, "reason": "该日期无生效已发布版本"})
            return
    _walk(store, result, hdr, required, day, mode, level, path, prefer_alt)


def _frozen_line_effective(fl, day: date) -> bool:
    vf = parse_date(fl.valid_from)
    vt = parse_date(fl.valid_to)
    return day >= vf and (vt is None or day < vt)


def _alt_effective(alt, day: date) -> bool:
    vf = parse_date(alt.valid_from)
    vt = parse_date(alt.valid_to)
    return day >= vf and (vt is None or day < vt)


def _aggregate(result: ExplosionResult) -> None:
    for item in result.items:
        bucket = result.aggregated.setdefault(item.material, {
            "material": item.material,
            "base_unit": item.base_unit,
            "total_qty": 0.0,
            "sources": 0,
        })
        bucket["total_qty"] = round(bucket["total_qty"] + item.required_qty, 6)
        bucket["sources"] += 1
