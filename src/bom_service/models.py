"""领域模型：部件、版本、用量行、替代关系、快照。

状态机（对应领域契约 states）：

    草拟 DRAFT -> 待确认 PENDING（校验通过、提交签署）
    待确认 PENDING -> 已发布 FROZEN（签署，冻结完整依赖快照）
    草拟/待确认 -> 已作废 VOID

紧急更正、分支合并、部件停用、并发发布均通过"新版本"实现：
旧版本一经签署即不可变，任何变更都产生新的版本行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class State(str, Enum):
    DRAFT = "草拟"
    PENDING = "待确认"
    FROZEN = "已发布"
    VOID = "已作废"

    @classmethod
    def from_value(cls, value: str) -> "State":
        for item in cls:
            if item.value == value or item.name == value:
                return item
        raise ValueError(f"未知状态：{value}")


# 发布快照的固定结构版本
SNAPSHOT_FORMAT = 1


class BomError(Exception):
    """业务错误。``code`` 为稳定错误码，便于 API 与测试断言。"""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class Part:
    """物料主数据。"""

    code: str
    name: str
    unit: str
    # 单位换算：{目标单位: 系数}，1 目标单位 = factor 主单位
    conversions: dict[str, float] = field(default_factory=dict)
    active: bool = True
    obsolete_on: date | None = None  # 停用日期：当日起展开不再选用

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "unit": self.unit,
            "conversions": dict(self.conversions),
            "active": self.active,
            "obsolete_on": self.obsolete_on.isoformat() if self.obsolete_on else None,
        }


@dataclass
class Line:
    """BOM 用量行（一条父子关系）。

    生效区间为半开区间 ``[valid_from, valid_to)``，``valid_to=None`` 表示无限。
    """

    child: str
    qty: float                 # 每生产 1 单位父件所需子件数量
    unit: str                  # 用量单位（必须能与子件主单位换算）
    scrap: float = 0.0         # 损耗率，0.05 表示 5%
    valid_from: date = field(default_factory=lambda: date(1900, 1, 1))
    valid_to: date | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "child": self.child,
            "qty": self.qty,
            "unit": self.unit,
            "scrap": self.scrap,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "note": self.note,
        }


@dataclass
class Substitute:
    """替代料关系：在区间内，``alt`` 可替代 ``line`` 上的子件。

    优先级 priority 越小越优先（0 表示首选替代）。
    """

    alt: str
    ratio: float               # 1 单位原子件 = ratio 单位替代件
    unit: str                  # 替代件用量单位
    priority: int = 0
    valid_from: date = field(default_factory=lambda: date(1900, 1, 1))
    valid_to: date | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "alt": self.alt,
            "ratio": self.ratio,
            "unit": self.unit,
            "priority": self.priority,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "note": self.note,
        }


@dataclass
class Revision:
    """总成 BOM 的一个版本。"""

    code: str                  # 总成物料编码
    version: int
    state: State
    valid_from: date
    valid_to: date | None
    reason: str = ""           # 版本产生原因（紧急更正/分支合并/部件停用/并发发布）
    parent_version: int | None = None   # 衍生自哪个版本
    base_version: int | None = None     # 内容基线版本（分支合并时为合并来源）
    branch: str = "main"       # 分支名
    created_at: datetime = field(default_factory=datetime.utcnow)
    signed_by: str | None = None
    lines: list[Line] = field(default_factory=list)
    substitutes: dict[str, list[Substitute]] = field(default_factory=dict)

    @property
    def rev_id(self) -> str:
        return f"{self.code}@{self.branch}:v{self.version}"

    def to_dict(self, include_contents: bool = True) -> dict:
        data: dict[str, Any] = {
            "code": self.code,
            "version": self.version,
            "rev_id": self.rev_id,
            "state": self.state.value,
            "branch": self.branch,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "reason": self.reason,
            "parent_version": self.parent_version,
            "base_version": self.base_version,
            "created_at": self.created_at.isoformat() + "Z",
            "signed_by": self.signed_by,
        }
        if include_contents:
            data["lines"] = [line.to_dict() for line in self.lines]
            data["substitutes"] = {
                child: [s.to_dict() for s in subs]
                for child, subs in self.substitutes.items()
            }
        return data


@dataclass
class Snapshot:
    """签署时冻结的完整依赖快照（不可变）。"""

    code: str
    version: int
    branch: str
    signed_by: str
    signed_at: datetime
    valid_from: date
    valid_to: date | None
    # 冻结内容：{总成版本 rev_id: {"revision": ..., "lines": [...], "substitutes": {...}}}
    # 同时冻结所引用的部件主数据与单位换算
    parts: dict[str, dict]
    revisions: dict[str, dict]
    checksum: str

    def to_dict(self) -> dict:
        return {
            "snapshot_format": SNAPSHOT_FORMAT,
            "code": self.code,
            "version": self.version,
            "branch": self.branch,
            "rev_id": f"{self.code}@{self.branch}:v{self.version}",
            "signed_by": self.signed_by,
            "signed_at": self.signed_at.isoformat() + "Z",
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "parts": self.parts,
            "revisions": self.revisions,
            "checksum": self.checksum,
        }
