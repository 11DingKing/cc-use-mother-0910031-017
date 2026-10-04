"""多层 BOM 发布后端：生效区间版本化、发布校验、快照冻结、历史日期展开。"""
from __future__ import annotations

from .errors import (
    BomError,
    ConcurrentPublish,
    MergeConflict,
    NotFound,
    ValidationFailed,
    ValidationIssue,
)
from .explosion import ExplosionResult, UsageSource, explode
from .models import (
    Alternative,
    BomHeader,
    BomLine,
    FrozenAlternative,
    FrozenLine,
    Material,
    Snapshot,
    Unit,
)
from .service import BomService
from .store import Store
from .units import UnitRegistry

__all__ = [
    "BomService", "Store", "UnitRegistry",
    "Unit", "Material", "BomHeader", "BomLine", "Alternative",
    "FrozenLine", "FrozenAlternative", "Snapshot",
    "UsageSource", "ExplosionResult", "explode",
    "ValidationIssue", "BomError", "ValidationFailed",
    "ConcurrentPublish", "MergeConflict", "NotFound",
]
