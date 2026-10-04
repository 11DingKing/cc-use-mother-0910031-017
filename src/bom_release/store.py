"""仓储：内存数据 + JSON 文件持久化。

数据分四类：单位、物料、BOM 版本、签署快照。快照一经写入不可修改。
"""
from __future__ import annotations

import json
from pathlib import Path

from .errors import NotFound
from .models import (
    Alternative,
    BomHeader,
    BomLine,
    FrozenAlternative,
    FrozenLine,
    Material,
    Snapshot,
)
from .units import UnitRegistry, Unit


class Store:
    def __init__(self) -> None:
        self.units = UnitRegistry()
        self.materials: dict[str, Material] = {}
        self.boms: dict[str, BomHeader] = {}
        self.snapshots: dict[str, Snapshot] = {}
        # (物料, 分支) -> 下一修订号
        self._revision_seq: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ 单位
    def add_unit(self, unit: Unit) -> None:
        self.units.add(unit)

    # ---------------------------------------------------------------- 物料
    def add_material(self, material: Material) -> None:
        self.materials[material.code] = material

    def require_material(self, code: str) -> Material:
        if code not in self.materials:
            raise NotFound(f"物料不存在：{code}")
        return self.materials[code]

    def versions_of(self, material: str) -> list[BomHeader]:
        return sorted(
            (b for b in self.boms.values() if b.material == material),
            key=lambda b: (b.revision, b.code),
        )

    def require_bom(self, code: str) -> BomHeader:
        if code not in self.boms:
            raise NotFound(f"BOM 版本不存在：{code}")
        return self.boms[code]

    def next_revision(self, material: str, branch: str) -> int:
        key = (material, branch)
        rev = self._revision_seq.get(key, 0) + 1
        self._revision_seq[key] = rev
        return rev

    def signed_versions(self, material: str) -> list[BomHeader]:
        return [
            b for b in self.versions_of(material)
            if b.status in ("signed", "superseded")
        ]

    def effective_version(self, material: str, day, branch: str | None = None) -> BomHeader | None:
        """查找物料在指定日期生效的已签署版本。

        优先指定分支；默认 main。生效区间半开覆盖 day 时，取生效起始日最晚者
        （同一天多个版本按修订号取大）。
        """
        candidates = [
            b for b in self.signed_versions(material)
            if b.effective_on(day) and (branch is None or b.branch == branch)
        ]
        if not candidates and branch is not None:
            return None
        if not candidates:
            candidates = [
                b for b in self.signed_versions(material)
                if b.effective_on(day) and b.branch == "main"
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda b: (b.valid_from, b.revision))

    def require_snapshot(self, bom_version: str) -> Snapshot:
        if bom_version not in self.snapshots:
            raise NotFound(f"签署快照不存在：{bom_version}")
        return self.snapshots[bom_version]

    # -------------------------------------------------------------- 持久化
    def save(self, path: str | Path) -> None:
        data = {
            "units": [vars(u) for u in self.units._units.values()],  # noqa: SLF001
            "materials": [vars(m) for m in self.materials.values()],
            "revision_seq": [
                {"material": k[0], "branch": k[1], "next": v}
                for k, v in self._revision_seq.items()
            ],
            "boms": [_bom_dict(b) for b in self.boms.values()],
            "snapshots": [s.to_dict() for s in self.snapshots.values()],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        for item in data.get("units", []):
            store.add_unit(Unit(**item))
        for item in data.get("materials", []):
            store.add_material(Material(**item))
        for item in data.get("revision_seq", []):
            store._revision_seq[(item["material"], item["branch"])] = item["next"]  # noqa: SLF001
        for item in data.get("boms", []):
            store.boms[item["code"]] = _bom_from_dict(item)
        for item in data.get("snapshots", []):
            store.snapshots[item["bom_version"]] = _snapshot_from_dict(item)
        return store


def _bom_dict(b: BomHeader) -> dict:
    return {
        **vars(b),
        "lines": [
            {
                **vars(line),
                "alternatives": [vars(a) for a in line.alternatives],
            }
            for line in b.lines
        ],
    }


def _bom_from_dict(d: dict) -> BomHeader:
    lines = [
        BomLine(
            **{k: v for k, v in line.items() if k != "alternatives"},
            alternatives=[Alternative(**a) for a in line.get("alternatives", [])],
        )
        for line in d.get("lines", [])
    ]
    return BomHeader(**{k: v for k, v in d.items() if k != "lines"}, lines=lines)


def _snapshot_from_dict(d: dict) -> Snapshot:
    lines = tuple(
        FrozenLine(
            **{k: v for k, v in line.items()
               if k not in ("alternatives", "gross_qty")},
            alternatives=tuple(FrozenAlternative(**a) for a in line.get("alternatives", [])),
        )
        for line in d.get("lines", [])
    )
    return Snapshot(
        bom_version=d["bom_version"],
        material=d["material"],
        signed_at=d["signed_at"],
        root=None,
        lines=lines,
        digest=d["digest"],
        change_note=d.get("change_note", ""),
    )
