"""领域模型：物料、单位、BOM 版本、替代料、发布快照。

生效区间统一为半开区间 ``[valid_from, valid_to)``，``valid_to`` 为 ``None``
表示当前生效（开放区间）。所有日期为 ISO ``YYYY-MM-DD`` 纯日期。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


def parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def effective_on(valid_from: date, valid_to: date | None, day: date) -> bool:
    """判断半开区间 ``[valid_from, valid_to)`` 是否覆盖 ``day``。"""
    return day >= valid_from and (valid_to is None or day < valid_to)


@dataclass(frozen=True)
class Unit:
    """计量单位。factor 表示相对基准单位的换算因子：1 本单位 = factor 基准单位。"""

    code: str
    name: str
    base_code: str | None = None  # 同量纲基准单位；None 表示自身即基准
    factor: float = 1.0

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("单位编码不能为空")
        if self.factor <= 0:
            raise ValueError("换算因子必须为正数")


@dataclass
class Material:
    code: str
    name: str
    base_unit: str
    discontinued: bool = False
    discontinuing_bom_version: str | None = None  # 停用时在制的 BOM 版本


@dataclass
class Alternative:
    """替代料关系：在生效区间内可以 ``alt_material`` 替代所属行的子件。

    ``priority`` 小者优先；``ratio`` 为替代用量系数（相对标准行用量），
    如 1 件标准件可用 1.2 件替代件，则 ratio=1.2。
    """

    alt_material: str
    priority: int = 10
    valid_from: str = "1900-01-01"
    valid_to: str | None = None
    ratio: float = 1.0
    bom_version: str | None = None  # 发布时冻结的替代件 BOM 版本（叶子件为 None）

    def effective_on(self, day: date) -> bool:
        return effective_on(parse_date(self.valid_from), parse_date(self.valid_to), day)


@dataclass
class BomLine:
    line_no: int
    child_material: str
    qty_per: float                       # 每父件用量
    unit: str                            # 用量单位（须与子件同量纲）
    scrap_rate: float = 0.0              # 损耗率 0~1（需求量 = qty * (1+scrap)）
    valid_from: str = "1900-01-01"
    valid_to: str | None = None
    alternatives: list[Alternative] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.qty_per <= 0:
            raise ValueError(f"第 {self.line_no} 行用量必须为正数")
        if not 0 <= self.scrap_rate < 1:
            raise ValueError(f"第 {self.line_no} 行损耗率必须位于 [0,1)")

    def effective_on(self, day: date) -> bool:
        return effective_on(parse_date(self.valid_from), parse_date(self.valid_to), day)

    def gross_qty(self) -> float:
        return self.qty_per * (1.0 + self.scrap_rate)


@dataclass
class BomHeader:
    """BOM 版本头。一个父件物料下可有多个版本，按分支组织。"""

    code: str                       # 版本编码，如 ASSY-A--V1
    material: str                   # 父件物料
    revision: int
    branch: str = "main"
    parent_version: str | None = None   # 派生来源（紧急更正/分支）
    status: str = "draft"           # draft | signed | superseded
    valid_from: str = "1900-01-01"
    valid_to: str | None = None
    created_by: str = "工程"
    created_at: str = ""
    signed_by: str | None = None
    signed_at: str | None = None
    edit_seq: int = 0               # 乐观锁：草稿每次修订 +1
    lines: list[BomLine] = field(default_factory=list)
    change_note: str = ""

    @property
    def vf(self) -> date:
        return parse_date(self.valid_from)  # type: ignore[return-value]

    @property
    def vt(self) -> date | None:
        return parse_date(self.valid_to)

    def effective_on(self, day: date) -> bool:
        return effective_on(self.vf, self.vt, day)

    def lines_on(self, day: date) -> list[BomLine]:
        return [line for line in self.lines if line.effective_on(day)]


@dataclass(frozen=True)
class FrozenLine:
    """快照中的冻结行：子件已解析到精确的已签署版本（或确认叶子）。"""

    line_no: int
    child_material: str
    child_version: str | None       # 已签署 BOM 版本编码；叶子件为 None
    child_revision: int | None
    qty_per: float
    unit: str
    scrap_rate: float
    valid_from: str
    valid_to: str | None
    alternatives: tuple[FrozenAlternative, ...] = ()


@dataclass(frozen=True)
class FrozenAlternative:
    alt_material: str
    alt_version: str | None
    priority: int
    ratio: float
    valid_from: str
    valid_to: str | None


@dataclass(frozen=True)
class Snapshot:
    """签署时冻结的完整依赖快照，一经写入不可变。"""

    bom_version: str
    material: str
    signed_at: str
    root: FrozenLine | None                     # 父件自身作为产品的占位
    lines: tuple[FrozenLine, ...]
    digest: str                                 # 规范化内容哈希
    change_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bom_version": self.bom_version,
            "material": self.material,
            "signed_at": self.signed_at,
            "digest": self.digest,
            "change_note": self.change_note,
            "lines": [_frozen_line_dict(line) for line in self.lines],
        }


def _frozen_line_dict(line: FrozenLine) -> dict[str, Any]:
    return {
        "line_no": line.line_no,
        "child_material": line.child_material,
        "child_version": line.child_version,
        "child_revision": line.child_revision,
        "qty_per": line.qty_per,
        "unit": line.unit,
        "scrap_rate": line.scrap_rate,
        "gross_qty": line.qty_per * (1 + line.scrap_rate),
        "valid_from": line.valid_from,
        "valid_to": line.valid_to,
        "alternatives": [
            {
                "alt_material": a.alt_material,
                "alt_version": a.alt_version,
                "priority": a.priority,
                "ratio": a.ratio,
                "valid_from": a.valid_from,
                "valid_to": a.valid_to,
            }
            for a in line.alternatives
        ],
    }
