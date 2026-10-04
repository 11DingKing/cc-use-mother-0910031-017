"""用例编排：部件主数据、版本起草、四类新版本场景、签署发布。

版本产生场景（均不修改历史，全部生成新版本）：

- ``emergency_correction``：紧急更正——从已发布版本立即拉出新草稿，
  生效区间可回填（自更正日起替换旧版）。
- ``create_branch`` / ``merge_branch``：分支试制与合并——
  分支上独立出版本，合并时以分支内容在 main 上生成新版本。
- ``obsolete_part``：部件停用——标记停用日期，可自动重开受影响总成的新版本。
- 并发发布：``release`` 全程在仓储锁内复核状态，两个并发签署只有一个成功；
  冲突时后来方需基于新基线拉版本（``VERSION_CONFLICT``）。
"""
from __future__ import annotations

from datetime import date

from .freeze import build_snapshot
from .models import BomError, Line, Part, Revision, State
from .store import Store
from .validation import errors as error_issues
from .validation import validate_release


class BomService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------- 部件主数据
    def register_part(
        self,
        code: str,
        name: str,
        unit: str,
        conversions: dict[str, float] | None = None,
    ) -> Part:
        if not code or not code.strip():
            raise BomError("BAD_INPUT", "部件编码不能为空")
        if not unit:
            raise BomError("BAD_INPUT", f"部件 {code} 主单位不能为空")
        part = Part(code=code, name=name, unit=unit, conversions=dict(conversions or {}))
        return self.store.upsert_part(part)

    def add_unit_conversion(self, code: str, target: str, factor: float) -> None:
        part = self.store.get_part(code)
        if part is None:
            raise BomError("MISSING_PART", f"部件不存在：{code}")
        if factor <= 0:
            raise BomError("UNIT_BAD_FACTOR", "换算系数必须为正数")
        part.conversions[target] = factor
        self.store.upsert_part(part)

    def obsolete_part(self, code: str, on_date: date | None = None) -> dict:
        """停用部件，并为引用它的已发布总成自动开新版本（草稿）。"""
        part = self.store.get_part(code)
        if part is None:
            raise BomError("MISSING_PART", f"部件不存在：{code}")
        stop = on_date or date.today()
        part.active = False
        part.obsolete_on = stop
        self.store.upsert_part(part)

        affected: list[dict] = []
        for rev in self.store.list_revisions():
            if rev.state is not State.FROZEN:
                continue
            if not any(line.child == code for line in rev.lines):
                continue
            if rev.valid_to is not None:
                continue
            new_rev = self._spawn_draft(
                rev,
                reason=f"部件停用：{code} 自 {stop.isoformat()} 起停用",
                valid_from=stop,
            )
            affected.append(new_rev.to_dict(include_contents=False))
        return {"part": part.to_dict(), "affected_revisions": affected}

    # ----------------------------------------------------------------- 版本
    def create_revision(
        self,
        code: str,
        valid_from: date,
        valid_to: date | None = None,
        branch: str = "main",
        reason: str = "初次建档",
    ) -> Revision:
        if self.store.get_part(code) is None:
            raise BomError("MISSING_PART", f"总成部件不存在，请先建档：{code}")
        version = self.store.next_version(code, branch)
        rev = Revision(
            code=code,
            version=version,
            state=State.DRAFT,
            valid_from=valid_from,
            valid_to=valid_to,
            reason=reason,
            branch=branch,
        )
        self.store.insert_revision(rev)
        return rev

    @staticmethod
    def _line_from(data: dict) -> Line:
        try:
            return Line(
                child=data["child"],
                qty=float(data["qty"]),
                unit=data["unit"],
                scrap=float(data.get("scrap", 0.0)),
                valid_from=date.fromisoformat(data.get("valid_from", "1900-01-01")),
                valid_to=date.fromisoformat(data["valid_to"]) if data.get("valid_to") else None,
                note=data.get("note", ""),
            )
        except KeyError as exc:
            raise BomError("BAD_INPUT", f"用量行缺少字段：{exc}") from exc
        except (TypeError, ValueError) as exc:
            raise BomError("BAD_INPUT", f"用量行格式错误：{exc}") from exc

    def set_lines(self, code: str, branch: str, version: int, lines: list[dict]) -> Revision:
        rev = self._require_revision(code, branch, version)
        rev.lines = [self._line_from(item) for item in lines]
        self.store.save_contents(rev)
        return rev

    def set_substitutes(
        self, code: str, branch: str, version: int, line_child: str, substitutes: list[dict]
    ) -> Revision:
        rev = self._require_revision(code, branch, version)
        if not any(line.child == line_child for line in rev.lines):
            raise BomError("LINE_NOT_FOUND", f"用量行不存在：{rev.rev_id} -> {line_child}")
        subs = []
        for data in substitutes:
            try:
                from .models import Substitute

                subs.append(
                    Substitute(
                        alt=data["alt"],
                        ratio=float(data["ratio"]),
                        unit=data["unit"],
                        priority=int(data.get("priority", 0)),
                        valid_from=date.fromisoformat(data.get("valid_from", "1900-01-01")),
                        valid_to=date.fromisoformat(data["valid_to"]) if data.get("valid_to") else None,
                        note=data.get("note", ""),
                    )
                )
            except KeyError as exc:
                raise BomError("BAD_INPUT", f"替代关系缺少字段：{exc}") from exc
            except (TypeError, ValueError) as exc:
                raise BomError("BAD_INPUT", f"替代关系格式错误：{exc}") from exc
        rev.substitutes[line_child] = subs
        self.store.save_contents(rev)
        return rev

    def get_revision(self, code: str, branch: str, version: int) -> Revision:
        return self._require_revision(code, branch, version)

    def _require_revision(self, code: str, branch: str, version: int) -> Revision:
        rev = self.store.get_revision(code, branch, version)
        if rev is None:
            raise BomError("REVISION_NOT_FOUND", f"版本不存在：{code}@{branch}:v{version}")
        return rev

    # ------------------------------------------------------- 新版本派生场景
    def emergency_correction(
        self,
        code: str,
        version: int,
        valid_from: date | None = None,
        branch: str = "main",
        note: str = "",
    ) -> Revision:
        """紧急更正：复制已发布版本内容为新草稿，并把旧版本生效截止到更正日前。"""
        base = self._require_revision(code, branch, version)
        if base.state is not State.FROZEN:
            raise BomError("NOT_FROZEN", f"仅已发布版本可发起紧急更正：{base.rev_id}")
        start = valid_from or date.today()
        new_rev = self._spawn_draft(
            base,
            reason=f"紧急更正：{note or '现场问题整改'}",
            valid_from=start,
            close_previous=True,
        )
        return new_rev

    def create_branch(
        self,
        code: str,
        version: int,
        branch: str,
        valid_from: date | None = None,
    ) -> Revision:
        """从 main 的已发布版本拉试制分支，复制内容为分支上的草稿。"""
        if branch == "main":
            raise BomError("BAD_INPUT", "分支名不能为 main")
        base = self._require_revision(code, "main", version)
        if base.state is not State.FROZEN:
            raise BomError("NOT_FROZEN", f"只能从已发布版本拉分支：{base.rev_id}")
        existing = self.store.list_revisions(code, branch)
        if existing:
            raise BomError("BRANCH_EXISTS", f"分支已存在：{code}@{branch}")
        new_version = self.store.next_version(code, branch)
        rev = Revision(
            code=code,
            version=new_version,
            state=State.DRAFT,
            valid_from=valid_from or base.valid_from,
            valid_to=None,
            reason=f"试制分支，派生自 main:v{version}",
            parent_version=version,
            base_version=version,
            branch=branch,
            lines=[Line(**vars(line)) for line in base.lines],
            substitutes={
                child: [type(s)(**vars(s)) for s in subs]
                for child, subs in base.substitutes.items()
            },
        )
        self.store.insert_revision(rev, base_version=version)
        return rev

    def merge_branch(self, code: str, branch: str, version: int, valid_from: date) -> Revision:
        """把分支版本合并回 main：以分支内容在 main 上开新版本草稿。"""
        source = self._require_revision(code, branch, version)
        if source.state not in (State.FROZEN, State.PENDING, State.DRAFT):
            raise BomError("BAD_STATE", f"分支版本状态不可合并：{source.rev_id}")
        new_version = self.store.next_version(code, "main")
        rev = Revision(
            code=code,
            version=new_version,
            state=State.DRAFT,
            valid_from=valid_from,
            valid_to=None,
            reason=f"分支合并：{branch}:v{version} 合入 main",
            parent_version=version,
            base_version=version,
            branch="main",
            lines=[Line(**vars(line)) for line in source.lines],
            substitutes={
                child: [type(s)(**vars(s)) for s in subs]
                for child, subs in source.substitutes.items()
            },
        )
        self.store.insert_revision(rev, base_version=version)
        # 旧版区间在合并版本实际签署时才由发布流程截止，起草阶段不改动历史
        return rev

    def _spawn_draft(
        self,
        base: Revision,
        reason: str,
        valid_from: date,
        close_previous: bool = False,
    ) -> Revision:
        new_version = self.store.next_version(base.code, base.branch)
        rev = Revision(
            code=base.code,
            version=new_version,
            state=State.DRAFT,
            valid_from=valid_from,
            valid_to=None,
            reason=reason,
            parent_version=base.version,
            base_version=base.version,
            branch=base.branch,
            lines=[Line(**vars(line)) for line in base.lines],
            substitutes={
                child: [type(s)(**vars(s)) for s in subs]
                for child, subs in base.substitutes.items()
            },
        )
        self.store.insert_revision(rev, base_version=base.version)
        if close_previous:
            self.store.set_valid_to(base.code, base.branch, base.version, valid_from)
        return rev

    # ------------------------------------------------------------- 校验/发布
    def validate(self, code: str, branch: str, version: int) -> dict:
        rev = self._require_revision(code, branch, version)
        issues = validate_release(self.store, rev)
        return {
            "rev_id": rev.rev_id,
            "ok": not error_issues(issues),
            "issues": [item.to_dict() for item in issues],
        }

    def submit_for_signoff(self, code: str, branch: str, version: int) -> dict:
        rev = self._require_revision(code, branch, version)
        if rev.state not in (State.DRAFT, State.PENDING):
            raise BomError("BAD_STATE", f"当前状态 {rev.state.value} 不能提交签署")
        issues = validate_release(self.store, rev)
        if error_issues(issues):
            raise BomError(
                "RELEASE_BLOCKED",
                f"{rev.rev_id} 存在 {len(error_issues(issues))} 项阻断性问题",
                [item.to_dict() for item in issues],
            )
        if rev.state is State.DRAFT:
            self.store.mark_pending(rev)
        return {
            "rev_id": rev.rev_id,
            "state": State.PENDING.value,
            "warnings": [i.to_dict() for i in issues if i.severity == "warning"],
        }

    def release(self, code: str, branch: str, version: int, signed_by: str) -> dict:
        """签署发布：锁内复核状态 -> 重新校验 -> 冻结快照 -> 置为已发布。"""
        if not signed_by or not signed_by.strip():
            raise BomError("BAD_INPUT", "签署人不能为空")
        with self.store.lock:
            rev = self.store.get_revision(code, branch, version)
            if rev is None:
                raise BomError("REVISION_NOT_FOUND", f"版本不存在：{code}@{branch}:v{version}")
            if rev.state is State.FROZEN:
                raise BomError(
                    "VERSION_CONFLICT",
                    f"{rev.rev_id} 已被并发签署发布",
                    {"signed_by": rev.signed_by},
                )
            if rev.state is State.VOID:
                raise BomError("BAD_STATE", f"{rev.rev_id} 已作废")
            issues = validate_release(self.store, rev)
            if error_issues(issues):
                raise BomError(
                    "RELEASE_BLOCKED",
                    f"{rev.rev_id} 存在 {len(error_issues(issues))} 项阻断性问题",
                    [item.to_dict() for item in issues],
                )
            superseded = self._resolve_overlap(rev)
            snap, payload = build_snapshot(self.store, rev, signed_by)
            self.store.put_snapshot(snap, payload)
            self.store.mark_frozen(rev, signed_by)
            self.store.commit()
        result = {
            "rev_id": rev.rev_id,
            "state": State.FROZEN.value,
            "signed_by": signed_by,
            "snapshot_checksum": snap.checksum,
        }
        if superseded:
            result["superseded"] = superseded
        return result

    def _resolve_overlap(self, rev: Revision) -> list[str]:
        """处理同总成、同分支上生效区间重叠的已发布版本。

        - 旧版为开放式（valid_to 为空）且新生效日晚于旧版生效日：
          自动将旧版截止到新生效日（版本接替）；
        - 生效日相同（并发发布同日版本）或旧版已有明确截止日仍重叠：
          抛 VERSION_CONFLICT，要求先串行化。
        """
        superseded: list[str] = []
        for other in self.store.list_revisions(rev.code, rev.branch):
            if other.version == rev.version or other.state is not State.FROZEN:
                continue
            overlaps = (other.valid_to is None or other.valid_to > rev.valid_from) and (
                rev.valid_to is None or rev.valid_to > other.valid_from
            )
            if not overlaps:
                continue
            if other.valid_to is None and rev.valid_from > other.valid_from:
                self.store.set_valid_to(rev.code, rev.branch, other.version, rev.valid_from)
                superseded.append(other.rev_id)
            else:
                raise BomError(
                    "VERSION_CONFLICT",
                    f"与已发布版本 {other.rev_id} 生效区间冲突（同日并发发布或区间交叉），"
                    "请基于最新版本重新起草",
                    {"conflict_with": other.rev_id},
                )
        return superseded

    # --------------------------------------------------------------- 查询
    def list_revisions(self, code: str | None = None, branch: str | None = None) -> list[dict]:
        return [r.to_dict(include_contents=False) for r in self.store.list_revisions(code, branch)]

    def revision_detail(self, code: str, branch: str, version: int) -> dict:
        return self._require_revision(code, branch, version).to_dict()

    def snapshot_detail(self, code: str, branch: str, version: int) -> dict:
        payload = self.store.get_snapshot_payload(code, branch, version)
        if payload is None:
            raise BomError("SNAPSHOT_NOT_FOUND", f"快照不存在：{code}@{branch}:v{version}")
        return payload
