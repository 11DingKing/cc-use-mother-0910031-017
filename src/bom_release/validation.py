"""发布前校验与快照冻结解析。

校验覆盖契约中四项不变量中的前两项：
- 多层依赖图：循环依赖检测；
- 发布完整性校验：缺失依赖、子件仅存在草稿、停用件、单位量纲换算。

快照冻结在 ``pin_snapshot`` 中完成：每个子件都被解析到 *签署时刻已发布*
的精确版本，从根上杜绝“父级已发布、下层仍引用草稿”。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from .errors import ValidationIssue
from .models import (
    BomHeader,
    FrozenAlternative,
    FrozenLine,
    Snapshot,
    parse_date,
)
from .store import Store

LEAF = object()  # 已注册但没有任何 BOM 版本的物料 = 外购叶子件


def validate_for_sign(store: Store, bom: BomHeader) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(_check_lines(store, bom))
    issues.extend(_check_cycles(store, bom))
    issues.extend(_check_intervals(store, bom))
    return issues


# ------------------------------------------------------------------ 单行检查
def _resolve_pinned(store: Store, bom: BomHeader, code: str, resolve_day):
    """返回 (将冻结的版本头, 该物料全部版本)；无任何版本的注册物料返回 (LEAF, [])。"""
    versions = store.versions_of(code)
    if not versions:
        return LEAF, []
    pinned = store.effective_version(code, resolve_day, bom.branch)
    if pinned is None and bom.branch != "main":
        pinned = store.effective_version(code, resolve_day, "main")
    return pinned, versions


def _dependency_issues(store: Store, bom: BomHeader, code: str, path: str,
                       resolve_day) -> list[ValidationIssue]:
    """子件/替代料通用的存在性、草稿引用、停用、缺失生效版本检查。"""
    issues: list[ValidationIssue] = []
    child = store.materials.get(code)
    if child is None:
        issues.append(ValidationIssue("missing_dependency", "error", path,
                                      f"物料 {code} 不存在"))
        return issues
    if child.discontinued:
        issues.append(ValidationIssue("discontinued", "error", path,
                                      f"物料 {child.code} 已停用，不能发布新的引用"))
    pinned, versions = _resolve_pinned(store, bom, code, resolve_day)
    if pinned is LEAF:
        return issues
    drafts = [v for v in versions if v.status == "draft"]
    signed = [v for v in versions if v.status in ("signed", "superseded")]
    if pinned is None and drafts and not signed:
        issues.append(ValidationIssue("draft_reference", "error", path,
            f"物料 {child.code} 只有草稿版本（{', '.join(v.code for v in drafts)}），"
            "父级不能先于下层发布"))
    elif pinned is None:
        issues.append(ValidationIssue("missing_dependency", "error", path,
            f"物料 {child.code} 在 {resolve_day.isoformat()} 没有已发布且生效的版本"))
    elif drafts:
        issues.append(ValidationIssue("draft_reference", "warning", path,
            f"物料 {child.code} 存在未发布草稿 "
            f"{', '.join(v.code for v in drafts)}，本次快照将固定为 {pinned.code}"))
    return issues


def _check_lines(store: Store, bom: BomHeader) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    parent_day = bom.vf
    seen_lines: set[int] = set()
    for line in bom.lines:
        path = f"{bom.code} / 第{line.line_no}行 -> {line.child_material}"
        if line.line_no in seen_lines:
            issues.append(ValidationIssue("missing_dependency", "error", path, f"行号 {line.line_no} 重复"))
        seen_lines.add(line.line_no)

        resolve_day = max(parent_day, parse_date(line.valid_from))
        issues.extend(_dependency_issues(store, bom, line.child_material, path, resolve_day))
        child = store.materials.get(line.child_material)

        # 单位量纲换算
        try:
            store.units.get(line.unit)
            if child is not None and not store.units.compatible(line.unit, child.base_unit):
                issues.append(ValidationIssue("unit_conversion", "error", path,
                    f"用量单位 {line.unit} 与子件基本单位 {child.base_unit} 量纲不同，无法换算"))
        except Exception as exc:  # noqa: BLE001 - 单位缺失也作为校验问题
            issues.append(ValidationIssue("unit_conversion", "error", path, f"用量单位异常：{exc}"))

        # 行生效区间是父版本区间内的进一步收窄；
        # 行 valid_from 早于父版本生效日视为“随版本生效”，以父版本生效日为准
        vt = parse_date(line.valid_to)
        if vt is not None and vt <= parse_date(line.valid_from):
            issues.append(ValidationIssue("missing_dependency", "error", path, "行生效区间为空"))
        if bom.vt is not None and vt is not None and vt > bom.vt:
            issues.append(ValidationIssue("missing_dependency", "error", path,
                                          "行失效日晚于父版本失效日"))

        # 替代料
        for alt in line.alternatives:
            apath = f"{path} 替代料 {alt.alt_material}"
            alt_day = max(parent_day, parse_date(alt.valid_from))
            issues.extend(_dependency_issues(store, bom, alt.alt_material, apath, alt_day))
            amat = store.materials.get(alt.alt_material)
            if amat is None:
                continue
            if not store.units.compatible(line.unit, amat.base_unit):
                issues.append(ValidationIssue("unit_conversion", "error", apath,
                    f"替代料基本单位 {amat.base_unit} 与行单位 {line.unit} 量纲不同"))
            if alt.ratio <= 0:
                issues.append(ValidationIssue("missing_dependency", "error", apath, "替代系数必须为正数"))
            if amat.code == line.child_material:
                issues.append(ValidationIssue("cycle", "error", apath, "替代料不能指向自身"))
    return issues


# ------------------------------------------------------------------ 循环检测
def _check_cycles(store: Store, bom: BomHeader) -> list[ValidationIssue]:
    """以物料为节点做 DFS：回到当前路径栈中任意祖先物料即为循环。

    边的来源：
    - 正在校验的版本：其草稿行（含替代料）；
    - 其它物料：签署时将冻结的已发布版本快照；
    - 尚无已发布版本但存在草稿的物料：沿最新草稿前向探测，
      以便在“工程同时改总成和子件”的试产场景提前发现草稿间互引。
    """
    issues: list[ValidationIssue] = []

    def edges_for(material: str) -> list[tuple[str, str]]:
        """返回 (子件物料, 边描述) 列表。"""
        if material == bom.material:
            return _draft_edges(bom)
        versions = store.versions_of(material)
        if versions:
            day = bom.vf
            pinned = store.effective_version(material, day, bom.branch)
            if pinned is None and bom.branch != "main":
                pinned = store.effective_version(material, day, "main")
            if pinned is not None:
                snap = store.snapshots.get(pinned.code)
                if snap is not None:
                    out: list[tuple[str, str]] = []
                    for fl in snap.lines:
                        out.append((fl.child_material, f"{fl.child_material}@{pinned.code}"))
                        out.extend(
                            (a.alt_material, f"{a.alt_material}@{pinned.code}[替代]")
                            for a in fl.alternatives
                        )
                    return out
            drafts = [v for v in versions if v.status == "draft"]
            if drafts:
                latest = max(drafts, key=lambda v: v.revision)
                return _draft_edges(latest)
        return []

    stack: list[str] = []
    trail: list[str] = []
    done: set[str] = set()  # 已完整展开且未发现环的子树（静态图下可安全剪枝）

    def dfs(material: str) -> None:
        for child, desc in edges_for(material):
            if child not in store.materials:
                continue  # 缺失依赖在单行检查中报告
            if child in stack:
                cycle_path = " -> ".join(trail + [desc, child])
                issues.append(ValidationIssue(
                    "cycle", "error", bom.code, f"检测到循环依赖：{cycle_path}"))
                continue
            if child in done:
                continue
            stack.append(child)
            trail.append(desc)
            dfs(child)
            trail.pop()
            stack.pop()
            done.add(child)

    stack.append(bom.material)
    dfs(bom.material)
    stack.pop()
    uniq: dict[str, ValidationIssue] = {}
    for i in issues:
        uniq.setdefault(i.message, i)
    return list(uniq.values())


def _draft_edges(h: BomHeader) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in h.lines:
        out.append((line.child_material, f"{line.child_material}(第{line.line_no}行)"))
        out.extend(
            (a.alt_material, f"{a.alt_material}(第{line.line_no}行[替代])")
            for a in line.alternatives
        )
    return out


# ------------------------------------------------------------------ 区间检查
def _check_intervals(store: Store, bom: BomHeader) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if bom.vt is not None and bom.vt <= bom.vf:
        issues.append(ValidationIssue("missing_dependency", "error", bom.code, "版本生效区间为空"))
    same_branch = [
        b for b in store.versions_of(bom.material)
        if b.branch == bom.branch and b.code != bom.code and b.status in ("signed", "superseded")
    ]
    new_start, new_end = bom.vf, bom.vt
    for old in same_branch:
        if _overlap(new_start, new_end, old.vf, old.vt):
            # 仅允许“新版本在开放区间旧版本之后接续”，由发布流程负责裁剪旧版本
            allowed = old.vt is None and new_end is None and old.vf < new_start
            if not allowed:
                issues.append(ValidationIssue("missing_dependency", "error", bom.code,
                    f"生效区间与已发布版本 {old.code}（{old.valid_from}~"
                    f"{old.valid_to or '开放'}）重叠，不能改写已发布历史"))
    return issues


def _overlap(start1: date, end1: date | None, start2: date, end2: date | None) -> bool:
    return start1 < (end2 or date.max) and start2 < (end1 or date.max)


# ------------------------------------------------------------------ 快照冻结
def pin_snapshot(store: Store, bom: BomHeader, signed_at: str) -> Snapshot:
    """把整棵依赖树固定为精确版本。调用前必须已通过 validate_for_sign。"""
    frozen: list[FrozenLine] = []
    for line in bom.lines:
        resolve_day = max(bom.vf, parse_date(line.valid_from))
        child_version = store.effective_version(line.child_material, resolve_day, bom.branch)
        if child_version is None and bom.branch != "main":
            child_version = store.effective_version(line.child_material, resolve_day, "main")
        if store.versions_of(line.child_material) and child_version is None:  # pragma: no cover
            raise RuntimeError(f"快照冻结失败：{line.child_material} 无生效已发布版本")
        alts: list[FrozenAlternative] = []
        for alt in line.alternatives:
            alt_day = max(bom.vf, parse_date(alt.valid_from))
            alt_ver = store.effective_version(alt.alt_material, alt_day, bom.branch)
            if alt_ver is None and bom.branch != "main":
                alt_ver = store.effective_version(alt.alt_material, alt_day, "main")
            if not store.versions_of(alt.alt_material):
                alt_ver = None  # 外购叶子替代料
            alts.append(FrozenAlternative(
                alt_material=alt.alt_material,
                alt_version=alt_ver.code if alt_ver else None,
                priority=alt.priority,
                ratio=alt.ratio,
                valid_from=alt.valid_from,
                valid_to=alt.valid_to,
            ))
        frozen.append(FrozenLine(
            line_no=line.line_no,
            child_material=line.child_material,
            child_version=child_version.code if child_version and store.versions_of(line.child_material) else None,
            child_revision=child_version.revision if child_version and store.versions_of(line.child_material) else None,
            qty_per=line.qty_per,
            unit=line.unit,
            scrap_rate=line.scrap_rate,
            valid_from=line.valid_from,
            valid_to=line.valid_to,
            alternatives=tuple(alts),
        ))
    digest = hashlib.sha256(
        json.dumps(
            {"bom": bom.code, "lines": [_canonical_line(f) for f in frozen]},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return Snapshot(
        bom_version=bom.code,
        material=bom.material,
        signed_at=signed_at,
        root=None,
        lines=tuple(frozen),
        digest=digest,
        change_note=bom.change_note,
    )


def _canonical_line(f: FrozenLine) -> dict:
    return {
        "line_no": f.line_no,
        "child_material": f.child_material,
        "child_version": f.child_version,
        "qty_per": f.qty_per,
        "unit": f.unit,
        "scrap_rate": f.scrap_rate,
        "valid_from": f.valid_from,
        "valid_to": f.valid_to,
        "alternatives": [
            {"alt_material": a.alt_material, "alt_version": a.alt_version,
             "priority": a.priority, "ratio": a.ratio,
             "valid_from": a.valid_from, "valid_to": a.valid_to}
            for a in f.alternatives
        ],
    }
