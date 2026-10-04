"""单位注册与量纲换算。

每个单位指向同量纲基准单位 ``base_code`` 并给出因子 ``factor``
（1 本单位 = factor 基准单位）。换算：
    数量(基准) = 数量(A) * factor_A
    数量(B)   = 数量(基准) / factor_B
"""
from __future__ import annotations

from .errors import NotFound
from .models import Unit


class UnitRegistry:
    def __init__(self) -> None:
        self._units: dict[str, Unit] = {}

    def add(self, unit: Unit) -> None:
        if unit.base_code is not None and unit.base_code not in self._units and unit.base_code != unit.code:
            # 基准单位允许稍后注册，定义期不强制顺序
            pass
        self._units[unit.code] = unit

    def get(self, code: str) -> Unit:
        if code not in self._units:
            raise NotFound(f"单位不存在：{code}")
        return self._units[code]

    def _resolve_base(self, code: str) -> tuple[str, float]:
        """沿 base_code 链解析到量纲基准，返回 (基准编码, 累乘因子)。"""
        factor = 1.0
        seen: set[str] = set()
        cur = code
        while True:
            unit = self.get(cur)
            if cur in seen:
                raise ValueError(f"单位换算链存在循环：{code}")
            seen.add(cur)
            if unit.base_code in (None, "", cur):
                return cur, factor
            factor *= unit.factor
            cur = unit.base_code

    def dimension(self, code: str) -> str:
        return self._resolve_base(code)[0]

    def compatible(self, a: str, b: str) -> bool:
        try:
            return self._resolve_base(a)[0] == self._resolve_base(b)[0]
        except (NotFound, ValueError):
            return False

    def factor(self, source: str, target: str) -> float:
        """返回把 source 单位数量换算为 target 单位数量的乘数。"""
        base_s, fac_s = self._resolve_base(source)
        base_t, fac_t = self._resolve_base(target)
        if base_s != base_t:
            raise ValueError(f"单位量纲不一致，无法换算：{source} -> {target}")
        return fac_s / fac_t
