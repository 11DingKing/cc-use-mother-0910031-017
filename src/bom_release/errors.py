"""错误类型。校验问题统一收集为 ValidationIssue，支持发布前整体报告。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    code: str            # cycle | missing_dependency | draft_reference | discontinued | unit_conversion
    severity: str        # error | warning
    path: str            # 如 ASSY-A--V2 / line 3 -> SUB-B
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "path": self.path, "message": self.message}


class BomError(Exception):
    """业务规则错误基类。"""


class ValidationFailed(BomError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("发布校验未通过：" + "；".join(i.message for i in issues if i.severity == "error"))


class ConcurrentPublish(BomError):
    """编辑序号冲突（乐观锁）或生效区间互斥冲突。"""


class NotFound(BomError):
    """引用的物料或版本不存在。"""


class MergeConflict(BomError):
    """分支合并出现双方修改同一行的冲突。"""

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__("分支合并冲突：" + "；".join(conflicts))
