"""按任意日期展开需求，并说明每项用量来源。

版本选择规则：

- 根总成取在展开日生效（``valid_from <= 日期 < valid_to``）的已发布版本；
  已签署版本的下层闭包**固定使用冻结快照中钉住的版本**，
  下层后来如何改版或出草稿都不影响本次展开（快照冻结不变量）。
- 用量行与替代关系按展开日过滤各自的生效区间。
- 原子件在展开日已停用时，自动选用当日生效、优先级最高的替代料。

输出同时给出：

- ``tree``：逐层需求树，每个节点带完整用量来源（路径、出处版本、
  用量行、损耗、单位换算、替代决策）；
- ``rolled``：按物料汇总的毛需求，附每一笔贡献的来源说明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import BomError
from .store import Store
from .units import UnitGraph


@dataclass
class DemandNode:
    part: str
    qty: float                       # 毛需求（已含损耗），单位 unit
    unit: str                        # 该物料主单位
    parent_qty: float                # 父件需求量
    depth: int
    path: list[str]
    source_revision: str | None      # 用量行所在版本 rev_id（根节点为 None）
    line_qty: float | None = None    # 用量行的每父件定额
    scrap: float | None = None
    conversion_rate: float | None = None  # 用量单位 -> 主单位
    substituted_from: str | None = None
    substitute_reason: str | None = None
    line_interval: tuple[str, str | None] | None = None
    children: list["DemandNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "part": self.part,
            "qty": round(self.qty, 9),
            "unit": self.unit,
            "path": self.path,
            "depth": self.depth,
            "source_revision": self.source_revision,
        }
        if self.source_revision is not None:
            out["provenance"] = {
                "parent_qty": round(self.parent_qty, 9),
                "line_qty_per_parent": self.line_qty,
                "scrap": self.scrap,
                "conversion_to_base_unit": self.conversion_rate,
                "formula": (
                    f"{round(self.parent_qty, 9)} × {self.line_qty} × (1+{self.scrap})"
                    f" × {self.conversion_rate:g}"
                ),
                "line_valid_from": self.line_interval[0] if self.line_interval else None,
                "line_valid_to": self.line_interval[1] if self.line_interval else None,
                "substituted_from": self.substituted_from,
                "substitute_reason": self.substitute_reason,
            }
        out["children"] = [c.to_dict() for c in self.children]
        return out


def _active(valid_from: str, valid_to: str | None, on_date: date) -> bool:
    if date.fromisoformat(valid_from) > on_date:
        return False
    if valid_to and date.fromisoformat(valid_to) <= on_date:
        return False
    return True


def _unit_graph_from_snapshot(parts: dict[str, dict]) -> UnitGraph:
    graph = UnitGraph()
    for part in parts.values():
        # 1 目标单位 = factor 主单位
        for target, factor in part["conversions"].items():
            graph.add_conversion(target, part["unit"], factor)
    return graph


def explode(
    store: Store,
    code: str,
    qty: float,
    on_date: date,
    branch: str = "main",
) -> dict:
    if qty <= 0:
        raise BomError("BAD_QTY", f"展开数量必须为正数：{qty}")
    root_rev = store.frozen_revision_at(code, on_date, branch)
    if root_rev is None:
        raise BomError(
            "NO_PUBLISHED_REVISION",
            f"{code} 在 {on_date.isoformat()} 没有生效的已发布版本，无法展开",
            {"code": code, "date": on_date.isoformat(), "branch": branch},
        )
    snap = store.get_snapshot_payload(code, root_rev.branch, root_rev.version)
    if snap is None:
        raise BomError(
            "SNAPSHOT_MISSING",
            f"{root_rev.rev_id} 缺少冻结快照，不能展开",
        )

    parts: dict[str, dict] = snap["parts"]
    revisions: dict[str, dict] = snap["revisions"]
    # 每个部件在快照中钉住唯一版本
    rev_by_part: dict[str, dict] = {}
    for rev_data in revisions.values():
        rev_by_part[rev_data["code"]] = rev_data
    graph = _unit_graph_from_snapshot(parts)

    def resolve_part(part_code: str) -> dict:
        part = parts.get(part_code)
        if part is None:  # 快照外不应发生（冻结时已闭包），兜底
            live = store.get_part(part_code)
            if live is None:
                raise BomError("MISSING_PART", f"展开时缺少部件主数据：{part_code}")
            return live.to_dict()
        # 结构信息（主单位、换算）以快照为准；停用状态是带日期的主数据事实，
        # 按当前主数据实时叠加，使历史日期展开能按当日是否停用选择替代料。
        live = store.get_part(part_code)
        if live is not None:
            part = dict(part)
            part["active"] = live.active
            part["obsolete_on"] = live.obsolete_on.isoformat() if live.obsolete_on else None
        return part

    def pick_line(line: dict, rev_data: dict, on_date: date) -> tuple[str, str | None, tuple[float, str] | None]:
        """返回 (实际用料编码, 决策原因, (替代比例, 替代单位) 或 None)。"""
        child = line["child"]
        part = resolve_part(child)
        obsolete = bool(part.get("obsolete_on")) and date.fromisoformat(part["obsolete_on"]) <= on_date
        # active=False 且已到停用日（或停用日未知）才算失效；未来停用不影响当日展开
        inactive = (not part.get("active", True)) and (
            part.get("obsolete_on") is None
            or date.fromisoformat(part["obsolete_on"]) <= on_date
        )
        if not (obsolete or inactive):
            return child, None, None
        reason_base = (
            f"原子件 {child} 已于 {part.get('obsolete_on')} 停用"
            if obsolete
            else f"原子件 {child} 已失效"
        )
        candidates = []
        for sub in rev_data.get("substitutes", {}).get(child, []):
            if not _active(sub["valid_from"], sub["valid_to"], on_date):
                continue
            alt = resolve_part(sub["alt"])
            alt_obsolete = bool(alt.get("obsolete_on")) and date.fromisoformat(alt["obsolete_on"]) <= on_date
            candidates.append((alt_obsolete, sub["priority"], sub))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if not candidates:
            raise BomError(
                "NO_ACTUAL_SUPPLY",
                f"{reason_base}，且当日无生效替代料",
                {"path_child": child, "date": on_date.isoformat()},
            )
        _, _, sub = candidates[0]
        return (
            sub["alt"],
            f"{reason_base}，按优先级 {sub['priority']} 改用替代料",
            (sub["ratio"], sub["unit"]),
        )

    contributions: list[dict] = []

    def expand_part(part_code: str, demand_qty: float, depth: int, path: list[str],
                    source_rev: str | None, provenance: dict | None) -> DemandNode:
        part = resolve_part(part_code)
        node = DemandNode(
            part=part_code,
            qty=demand_qty,
            unit=part["unit"],
            parent_qty=provenance["parent_qty"] if provenance else 0.0,
            depth=depth,
            path=path,
            source_revision=source_rev,
        )
        if provenance:
            node.line_qty = provenance["line_qty"]
            node.scrap = provenance["scrap"]
            node.conversion_rate = provenance["conversion_rate"]
            node.substituted_from = provenance.get("substituted_from")
            node.substitute_reason = provenance.get("substitute_reason")
            node.line_interval = provenance["line_interval"]
            contributions.append(
                {
                    "part": part_code,
                    "unit": part["unit"],
                    "qty": demand_qty,
                    "path": list(path),
                    "source_revision": source_rev,
                    "line_child": provenance["line_child"],
                    "line_qty_per_parent": provenance["line_qty"],
                    "scrap": provenance["scrap"],
                    "conversion_to_base_unit": provenance["conversion_rate"],
                    "line_valid_from": provenance["line_interval"][0],
                    "line_valid_to": provenance["line_interval"][1],
                    "substituted_from": provenance.get("substituted_from"),
                    "substitute_reason": provenance.get("substitute_reason"),
                }
            )

        rev_data = rev_by_part.get(part_code)
        if rev_data is not None:
            for line in rev_data["lines"]:
                if not _active(line["valid_from"], line["valid_to"], on_date):
                    continue
                actual, reason, sub_spec = pick_line(line, rev_data, on_date)
                # 用量行先按父件需求与损耗折算到行单位
                gross_in_line_unit = demand_qty * line["qty"] * (1.0 + line["scrap"])
                rev_id = (
                    rev_data["rev_id"]
                    if "rev_id" in rev_data
                    else f"{rev_data['code']}@{rev_data['branch']}:v{rev_data['version']}"
                )
                if sub_spec is None:
                    rate = graph.rate(line["unit"], resolve_part(actual)["unit"])
                    child_qty = gross_in_line_unit * rate
                    prov = {
                        "parent_qty": demand_qty,
                        "line_qty": line["qty"],
                        "scrap": line["scrap"],
                        "conversion_rate": rate,
                        "line_interval": (line["valid_from"], line["valid_to"]),
                        "line_child": line["child"],
                    }
                else:
                    # 替代：1 单位原子件（行单位）= ratio 替代件（sub.unit）
                    ratio, sub_unit = sub_spec
                    alt = resolve_part(actual)
                    rate = ratio * graph.rate(sub_unit, alt["unit"])
                    child_qty = gross_in_line_unit * rate
                    prov = {
                        "parent_qty": demand_qty,
                        "line_qty": line["qty"],
                        "scrap": line["scrap"],
                        "conversion_rate": rate,
                        "line_interval": (line["valid_from"], line["valid_to"]),
                        "line_child": line["child"],
                        "substituted_from": line["child"],
                        "substitute_reason": reason,
                    }
                node.children.append(
                    expand_part(actual, child_qty, depth + 1, path + [actual], rev_id, prov)
                )
        return node

    root_part = resolve_part(code)
    tree = expand_part(code, qty, 0, [code], None, None)

    # 汇总：同一物料同单位的毛需求累加
    rolled: dict[tuple[str, str], float] = {}
    for item in contributions:
        rolled[(item["part"], item["unit"])] = rolled.get((item["part"], item["unit"]), 0.0) + item["qty"]
    rolled_list = [
        {
            "part": part_code,
            "unit": unit,
            "gross_qty": round(total, 9),
            "is_leaf": part_code not in rev_by_part,
            "sources": [c for c in contributions if c["part"] == part_code and c["unit"] == unit],
        }
        for (part_code, unit), total in sorted(rolled.items())
    ]

    return {
        "root": code,
        "date": on_date.isoformat(),
        "root_revision": f"{root_rev.code}@{root_rev.branch}:v{root_rev.version}",
        "snapshot_checksum": snap["checksum"],
        "root_unit": root_part["unit"],
        "requested_qty": qty,
        "tree": tree.to_dict(),
        "rolled": rolled_list,
    }
