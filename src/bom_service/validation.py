"""发布前完整性校验。

对应契约不变量"发布完整性校验"，覆盖四类问题：

1. ``CYCLE``：多层依赖图存在循环（含经替代料形成的环）。
2. ``MISSING_PART`` / ``LOWER_LEVEL_DRAFT``：子件主数据缺失，
   或下层总成在签署日期没有已发布版本（即"父级已发布、下层仍引用草稿"）。
3. ``UNIT_NOT_CONVERTIBLE``：用量单位/替代单位无法换算到子件主单位。
4. 数值与生效区间问题：用量非正、损耗越界、区间倒挂、引用已停用部件。

校验沿"签署时各下层部件生效的已发布版本"递归闭包展开，
与冻结快照、历史展开使用同一条版本选择规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Line, Revision, State, Substitute
from .store import Store
from .units import UnitGraph


@dataclass
class Issue:
    severity: str   # "error" | "warning"
    code: str
    message: str
    path: list[str] = field(default_factory=list)
    detail: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def validate_release(
    store: Store,
    root: Revision,
    on_date: date | None = None,
) -> list[Issue]:
    """校验一个待签署版本的完整多层闭包。返回问题列表（空列表即可发布）。"""
    check_date = on_date or root.valid_from
    unit_graph = UnitGraph.from_parts(store.list_parts())
    issues: list[Issue] = []

    _check_own_fields(root, issues)

    # 结构循环：沿全部用量行（忽略生效日）做 DFS，未来生效的环也不允许。
    _detect_cycles(store, root, issues)

    # 闭包校验：沿签署日生效的已发布下层版本逐层检查
    visiting: set[str] = set()

    def walk(rev: Revision, path: list[str]) -> None:
        if rev.code in visiting:  # 双保险，结构环已在前面报告
            return
        visiting.add(rev.code)
        for line in rev.lines:
            edge_path = path + [line.child]
            part = store.get_part(line.child)
            if part is None:
                issues.append(
                    Issue("error", "MISSING_PART", f"子件主数据不存在：{line.child}", edge_path)
                )
                continue
            _check_line(line, part.unit, unit_graph, issues, edge_path)
            for sub in rev.substitutes.get(line.child, []):
                sub_path = edge_path + [f"替代:{sub.alt}"]
                alt_part = store.get_part(sub.alt)
                if alt_part is None:
                    issues.append(
                        Issue("error", "MISSING_PART", f"替代料主数据不存在：{sub.alt}", sub_path)
                    )
                    continue
                _check_substitute(sub, alt_part.unit, unit_graph, issues, sub_path)
            if part.obsolete_on and part.obsolete_on <= check_date:
                issues.append(
                    Issue(
                        "warning",
                        "PART_OBSOLETE",
                        f"部件 {line.child} 已于 {part.obsolete_on.isoformat()} 停用，请确认替代料",
                        edge_path,
                    )
                )
            # 下层总成：签署日必须存在生效的已发布版本，不能引用草拟；
            # 并且不允许存在生效区间覆盖签署日的在途草稿/待确认版本——
            # 否则父级一旦先发布，就会与下层未完成的变更产生需求展开不一致。
            if store.has_revisions(line.child):
                child_rev = store.frozen_revision_at(line.child, check_date)
                inflight = [
                    r
                    for r in store.list_revisions(line.child)
                    if r.state in (State.DRAFT, State.PENDING)
                    and r.valid_from <= check_date
                    and (r.valid_to is None or r.valid_to > check_date)
                ]
                if child_rev is None:
                    latest = store.latest_frozen(line.child)
                    detail: dict | None = None
                    if latest is not None:
                        detail = {
                            "latest_frozen_valid_from": latest.valid_from.isoformat(),
                            "latest_frozen_version": latest.version,
                        }
                    issues.append(
                        Issue(
                            "error",
                            "LOWER_LEVEL_DRAFT",
                            f"下层总成 {line.child} 在 {check_date.isoformat()} 无已发布版本"
                            "（仍为草拟或生效区间不覆盖该日），禁止先发布父级",
                            edge_path,
                            detail,
                        )
                    )
                elif inflight:
                    issues.append(
                        Issue(
                            "error",
                            "LOWER_LEVEL_DRAFT",
                            f"下层总成 {line.child} 存在 {len(inflight)} 个生效区间覆盖"
                            f" {check_date.isoformat()} 的在途版本（草拟/待确认），"
                            "请先完成下层发布再签署父级",
                            edge_path,
                            {"inflight": [r.rev_id for r in inflight]},
                        )
                    )
                if child_rev is not None:
                    walk(child_rev, edge_path)
        visiting.discard(rev.code)

    walk(root, [root.code])
    return issues


def _check_own_fields(rev: Revision, issues: list[Issue]) -> None:
    if rev.valid_to is not None and rev.valid_to <= rev.valid_from:
        issues.append(
            Issue(
                "error",
                "BAD_INTERVAL",
                f"{rev.rev_id} 生效区间倒挂：{rev.valid_from} ~ {rev.valid_to}",
                [rev.code],
            )
        )
    children = [line.child for line in rev.lines]
    if len(children) != len(set(children)):
        dup = sorted({c for c in children if children.count(c) > 1})
        issues.append(
            Issue("error", "DUPLICATE_LINE", f"同一子件出现多条用量行：{'、'.join(dup)}", [rev.code])
        )


def _check_line(
    line: Line,
    child_base_unit: str,
    unit_graph: UnitGraph,
    issues: list[Issue],
    path: list[str],
) -> None:
    if line.qty <= 0:
        issues.append(Issue("error", "BAD_QTY", f"用量必须为正数：{line.child}={line.qty}", path))
    if not 0 <= line.scrap < 1:
        issues.append(
            Issue("error", "BAD_SCRAP", f"损耗率必须在 [0,1) 区间：{line.child}={line.scrap}", path)
        )
    if line.valid_to is not None and line.valid_to <= line.valid_from:
        issues.append(
            Issue(
                "error",
                "BAD_INTERVAL",
                f"用量行 {line.child} 生效区间倒挂：{line.valid_from} ~ {line.valid_to}",
                path,
            )
        )
    try:
        unit_graph.rate(line.unit, child_base_unit)
    except Exception as exc:  # BomError
        issues.append(
            Issue(
                "error",
                "UNIT_NOT_CONVERTIBLE",
                f"用量行单位无法换算到子件主单位：{line.unit}->{child_base_unit}（{exc}）",
                path,
            )
        )


def _check_substitute(
    sub: Substitute,
    alt_base_unit: str,
    unit_graph: UnitGraph,
    issues: list[Issue],
    path: list[str],
) -> None:
    if sub.ratio <= 0:
        issues.append(Issue("error", "BAD_QTY", f"替代比例必须为正数：{sub.alt}={sub.ratio}", path))
    if sub.valid_to is not None and sub.valid_to <= sub.valid_from:
        issues.append(
            Issue(
                "error",
                "BAD_INTERVAL",
                f"替代关系 {sub.alt} 生效区间倒挂：{sub.valid_from} ~ {sub.valid_to}",
                path,
            )
        )
    try:
        unit_graph.rate(sub.unit, alt_base_unit)
    except Exception as exc:  # BomError
        issues.append(
            Issue(
                "error",
                "UNIT_NOT_CONVERTIBLE",
                f"替代料单位无法换算到其主单位：{sub.unit}->{alt_base_unit}（{exc}）",
                path,
            )
        )


def _detect_cycles(store: Store, root: Revision, issues: list[Issue]) -> None:
    """在"总成 -> 任意用量子件（含替代料）"的静态图上检测有向环。"""
    color: dict[str, int] = {}  # 0=未访问 1=在栈中 2=完成
    stack: list[str] = []
    found: set[tuple[str, ...]] = set()

    def edges(code: str) -> list[str]:
        rev = _structural_revision(store, code)
        if rev is None:
            return []
        result = [line.child for line in rev.lines]
        for subs in rev.substitutes.values():
            result.extend(s.alt for s in subs if store.has_revisions(s.alt))
        return result

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in edges(node):
            if color.get(nxt, 0) == 0:
                dfs(nxt)
            elif color.get(nxt) == 1:
                idx = stack.index(nxt)
                cycle = tuple(stack[idx:] + [nxt])
                if cycle not in found:
                    found.add(cycle)
                    issues.append(
                        Issue(
                            "error",
                            "CYCLE",
                            "依赖图存在循环：" + " -> ".join(cycle),
                            list(cycle),
                        )
                    )
        stack.pop()
        color[node] = 2

    def _structural_revision(store: Store, code: str) -> Revision | None:
        if code == root.code:
            return root
        # 结构检查取最新已发布版本；没有则取该部件任意最新版本，以便草拟期也能报环
        frozen = store.latest_frozen(code)
        if frozen is not None:
            return frozen
        revs = store.list_revisions(code)
        return revs[-1] if revs else None

    dfs(root.code)


def errors(issues: list[Issue]) -> list[Issue]:
    return [item for item in issues if item.severity == "error"]
