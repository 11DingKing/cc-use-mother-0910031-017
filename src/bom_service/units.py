"""单位换算。

每个部件声明主单位及到其他单位的换算系数，系数语义为
``1 个目标单位 = factor 个主单位``（主单位 g、目标单位 kg 时 factor=1000）。
换算关系视为无向图：``a -> b`` 的系数为 f，则 ``b -> a`` 为 1/f，
并允许沿图做多跳推导。发布前校验"用量单位必须能与子件主单位互通"。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from .models import BomError, Part


class UnitGraph:
    """全局单位换算图（跨部件共享的单位，如 g/kg/套）。"""

    def __init__(self) -> None:
        # {单位: [(邻居, 有向系数 this->neighbor)]}
        self._edges: dict[str, list[tuple[str, float]]] = defaultdict(list)

    def add_conversion(self, a: str, b: str, factor: float) -> None:
        """登记 1 个 a = factor 个 b。"""
        if factor <= 0:
            raise BomError("UNIT_BAD_FACTOR", f"换算系数必须为正数：{a}->{b}={factor}")
        self._edges[a].append((b, factor))
        self._edges[b].append((a, 1.0 / factor))

    def rate(self, src: str, dst: str) -> float:
        """返回 1 个 src 折合多少个 dst；不可达时抛 UNIT_NOT_CONVERTIBLE。"""
        if src == dst:
            return 1.0
        seen = {src: 1.0}
        queue = deque([src])
        while queue:
            cur = queue.popleft()
            for nxt, edge_factor in self._edges.get(cur, ()):
                candidate = seen[cur] * edge_factor
                if nxt not in seen:
                    seen[nxt] = candidate
                    if nxt == dst:
                        return candidate
                    queue.append(nxt)
        raise BomError(
            "UNIT_NOT_CONVERTIBLE",
            f"单位无法换算：{src} -> {dst}",
            {"from": src, "to": dst},
        )

    def is_convertible(self, src: str, dst: str) -> bool:
        try:
            self.rate(src, dst)
            return True
        except BomError:
            return False

    @classmethod
    def from_parts(cls, parts: Iterable[Part]) -> "UnitGraph":
        graph = cls()
        for part in parts:
            # conversions 语义：1 目标单位 = factor 主单位
            for target, factor in part.conversions.items():
                graph.add_conversion(target, part.unit, factor)
        return graph
