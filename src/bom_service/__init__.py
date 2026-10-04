"""多层物料清单发布后端。

领域模块：

- ``models``：领域模型、状态与错误码
- ``units``：单位换算图
- ``store``：SQLite 仓储与事务
- ``validation``：发布前完整性校验（循环、缺失依赖、单位换算）
- ``freeze``：签署后冻结完整依赖快照
- ``explode``：按任意日期展开需求并附用量来源
- ``service``：用例编排（紧急更正、分支合并、部件停用、并发发布）
- ``api``：JSON HTTP 接口
"""
from __future__ import annotations

from .models import BomError, Part, Revision, Line, Substitute, Snapshot
from .service import BomService
from .store import Store

__all__ = [
    "BomError",
    "BomService",
    "Line",
    "Part",
    "Revision",
    "Snapshot",
    "Store",
    "Substitute",
]
