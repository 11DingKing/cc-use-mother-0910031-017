"""应用服务：草稿维护、签署发布、紧急更正、分支合并、部件停用、并发控制。

并发策略：
- 进程内全局签署锁保证发布串行化；
- 每个草稿带 ``edit_seq`` 乐观锁，修订与签署都须携带期望序号，
  他人改过即抛 :class:`ConcurrentPublish`，失败者以新版本重试；
- 同物料同分支的生效区间互斥，新版本生效时自动裁剪旧的开放区间，
  绝不允许改写已经冻结的历史区间。
"""
from __future__ import annotations

import copy
import threading
from datetime import date

from .errors import ConcurrentPublish, MergeConflict, NotFound, ValidationFailed
from .explosion import explode
from .models import BomHeader, BomLine, parse_date
from .store import Store
from .validation import pin_snapshot, validate_for_sign

_PUBLISH_LOCK = threading.RLock()


class BomService:
    def __init__(self, store: Store):
        self.store = store

    # ============================================================ 草稿维护
    def create_draft(
        self,
        material: str,
        valid_from: str,
        *,
        branch: str = "main",
        based_on: str | None = None,
        created_by: str = "工程",
        change_note: str = "",
        valid_to: str | None = None,
    ) -> BomHeader:
        self.store.require_material(material)
        revision = self.store.next_revision(material, branch)
        code = f"{material}--V{revision}" if branch == "main" else f"{material}--V{revision}-{branch}"
        lines: list[BomLine] = []
        if based_on:
            base = self.store.require_bom(based_on)
            if base.material != material:
                raise NotFound(f"基线版本 {based_on} 不属于物料 {material}")
            lines = _clone_lines(base.lines)
            vf_day = parse_date(valid_from)
            for line in lines:
                # 新版本生效日更晚时，克隆行的区间从新生效日重新起算
                if parse_date(line.valid_from) < vf_day:
                    line.valid_from = valid_from
                for alt in line.alternatives:
                    if parse_date(alt.valid_from) < vf_day:
                        alt.valid_from = valid_from
        bom = BomHeader(
            code=code, material=material, revision=revision, branch=branch,
            parent_version=based_on, status="draft",
            valid_from=valid_from, valid_to=valid_to,
            created_by=created_by, created_at=date.today().isoformat(),
            lines=lines, change_note=change_note,
        )
        self.store.boms[code] = bom
        return bom

    def revise_lines(self, code: str, expected_seq: int, lines: list[BomLine],
                     *, change_note: str | None = None) -> BomHeader:
        bom = self._draft(code)
        self._check_seq(bom, expected_seq)
        bom.lines = lines
        bom.edit_seq += 1
        if change_note is not None:
            bom.change_note = change_note
        return bom

    def precheck(self, code: str):
        """发布前预检：返回全部问题（含警告），不改变状态。"""
        return validate_for_sign(self.store, self._draft(code))

    # ============================================================ 签署发布
    def sign(self, code: str, signed_by: str, expected_seq: int,
             *, signed_at: str | None = None) -> BomHeader:
        with _PUBLISH_LOCK:
            bom = self._draft(code)
            self._check_seq(bom, expected_seq)
            issues = validate_for_sign(self.store, bom)
            errors = [i for i in issues if i.severity == "error"]
            if errors:
                raise ValidationFailed(issues)
            self._retire_overlapped(bom)
            snapshot = pin_snapshot(self.store, bom, signed_at or date.today().isoformat())
            self.store.snapshots[bom.code] = snapshot
            bom.status = "signed"
            bom.signed_by = signed_by
            bom.signed_at = snapshot.signed_at
            bom.edit_seq += 1
            return bom

    def _retire_overlapped(self, bom: BomHeader) -> None:
        """新版本生效：裁剪同分支与之重叠的旧版本开放区间。"""
        new_start = bom.vf
        for old in self.store.versions_of(bom.material):
            if old.branch != bom.branch or old.code == bom.code or old.status == "draft":
                continue
            if old.effective_on(new_start):
                if old.vf >= new_start:
                    raise ValidationFailed([_overlap_issue(bom, old)])
                old.valid_to = new_start.isoformat()
                old.status = "superseded"
            elif old.vt is None and old.vf < new_start:
                old.valid_to = new_start.isoformat()
                old.status = "superseded"
            elif bom.vt is None and old.vf == new_start:  # pragma: no cover
                raise ValidationFailed([_overlap_issue(bom, old)])

    # ============================================================ 紧急更正
    def emergency_correct(
        self,
        source_version: str,
        signed_by: str,
        *,
        lines: list[BomLine],
        valid_from: str,
        reason: str,
    ) -> BomHeader:
        """基于已发布版本立即派生并签署一个新版本，裁剪被更正版本的尾部。"""
        source = self.store.require_bom(source_version)
        if source.status not in ("signed", "superseded"):
            raise NotFound(f"紧急更正只能基于已发布版本：{source_version}")
        new_start = parse_date(valid_from)
        if new_start < source.vf:
            raise ValidationFailed([_make_issue(
                "missing_dependency", source.code,
                f"紧急更正生效日 {valid_from} 早于被更正版本生效日 {source.valid_from}，不能改写已冻结历史")])
        with _PUBLISH_LOCK:
            draft = self.create_draft(
                source.material, valid_from, branch=source.branch,
                based_on=source.code, created_by=signed_by,
                change_note=f"紧急更正：{reason}",
            )
            self.revise_lines(draft.code, draft.edit_seq, lines, change_note=f"紧急更正：{reason}")
            return self.sign(draft.code, signed_by, draft.edit_seq)

    # ============================================================ 分支与合并
    def create_branch(self, material: str, branch: str, from_version: str,
                      *, valid_from: str, created_by: str = "工程") -> BomHeader:
        if branch == "main":
            raise MergeConflict(["分支名不能为 main"])
        existing = [b for b in self.store.versions_of(material) if b.branch == branch]
        if existing:
            raise MergeConflict([f"物料 {material} 已存在分支 {branch}"])
        base = self.store.require_bom(from_version)
        draft = self.create_draft(
            material, valid_from, branch=branch, based_on=base.code,
            created_by=created_by, change_note=f"自 {base.code} 拉出分支 {branch}",
        )
        return draft

    def merge_branch(self, material: str, branch: str, *, merged_by: str = "工程",
                     valid_from: str | None = None) -> BomHeader:
        """三路合并分支到 main，产出 main 上的新草稿（仍需走预检与签署）。"""
        branch_versions = [b for b in self.store.versions_of(material) if b.branch == branch]
        if not branch_versions:
            raise NotFound(f"物料 {material} 不存在分支 {branch}")
        tip = max(branch_versions, key=lambda b: b.revision)
        base = self._merge_base(tip)
        main_tip = next(
            (b for b in sorted(self.store.versions_of(material), key=lambda x: x.revision, reverse=True)
             if b.branch == "main"),
            None,
        )
        if main_tip is None:
            raise NotFound(f"物料 {material} 的 main 分支尚无版本，无从合并")
        vf = valid_from or date.today().isoformat()
        merged_lines, conflicts = _three_way_lines(base, main_tip, tip)
        if conflicts:
            raise MergeConflict(conflicts)
        draft = self.create_draft(
            material, vf, branch="main", based_on=tip.code, created_by=merged_by,
            change_note=f"合并分支 {branch}（基点 {base.code}，主干 {main_tip.code}）",
        )
        self.revise_lines(draft.code, draft.edit_seq, merged_lines)
        return draft

    def _merge_base(self, tip: BomHeader) -> BomHeader:
        cur = tip
        seen: set[str] = set()
        while cur.parent_version and cur.code not in seen:
            seen.add(cur.code)
            parent = self.store.boms[cur.parent_version]
            if parent.branch == "main" and parent.status in ("signed", "superseded"):
                return parent
            cur = parent
        # 退化：取 main 最早版本
        mains = [b for b in self.store.versions_of(tip.material) if b.branch == "main"]
        if mains:
            return mains[0]
        raise NotFound("找不到合并基点")

    # ============================================================ 部件停用
    def discontinue_material(self, code: str, *, on_date: str | None = None,
                             reason: str = "") -> dict:
        material = self.store.require_material(code)
        day = parse_date(on_date) if on_date else date.today()
        current = self.store.effective_version(code, day, "main")
        material.discontinued = True
        material.discontinuing_bom_version = current.code if current else None
        impacted = self.impacted_parents(code)
        return {
            "material": code,
            "discontinued_on": day.isoformat(),
            "frozen_current_version": material.discontinuing_bom_version,
            "reason": reason,
            "impacted_published_parents": impacted,
        }

    def impacted_parents(self, code: str) -> list[dict]:
        """所有已发布快照中（直接或间接）引用该物料的父级版本。"""
        found: list[dict] = []
        for bom_code, snap in self.store.snapshots.items():
            if bom_code == code or snap.material == code:
                continue
            if _snapshot_references(snap, code, self.store):
                hdr = self.store.boms[bom_code]
                found.append({
                    "bom_version": bom_code,
                    "material": snap.material,
                    "branch": hdr.branch,
                    "status": hdr.status,
                    "valid_from": hdr.valid_from,
                    "valid_to": hdr.valid_to,
                })
        return found

    # ============================================================ 需求展开
    def explode(self, material: str, qty: float, on_date: str, **kwargs):
        return explode(self.store, material, qty, on_date, **kwargs)

    # ============================================================ 内部
    def _draft(self, code: str) -> BomHeader:
        bom = self.store.require_bom(code)
        if bom.status != "draft":
            raise NotFound(f"版本 {code} 状态为 {bom.status}，不可再编辑")
        return bom

    @staticmethod
    def _check_seq(bom: BomHeader, expected_seq: int) -> None:
        if expected_seq != bom.edit_seq:
            raise ConcurrentPublish(
                f"版本 {bom.code} 已被他人修订（期望 edit_seq={expected_seq}，"
                f"当前={bom.edit_seq}），请基于最新内容创建新版本重试")


def _snapshot_references(snap, code: str, store: Store) -> bool:
    for fl in snap.lines:
        if fl.child_material == code:
            return True
        if fl.child_version:
            child_snap = store.snapshots.get(fl.child_version)
            if child_snap and child_snap is not snap and _snapshot_references(child_snap, code, store):
                return True
    return False


def _clone_lines(lines: list[BomLine]) -> list[BomLine]:
    cloned = []
    for line in lines:
        cloned.append(BomLine(
            line_no=line.line_no, child_material=line.child_material,
            qty_per=line.qty_per, unit=line.unit, scrap_rate=line.scrap_rate,
            valid_from=line.valid_from, valid_to=line.valid_to,
            alternatives=[copy.deepcopy(a) for a in line.alternatives],
        ))
    return cloned


# --------------------------------------------------------------- 三路合并
def _line_key(line: BomLine) -> tuple:
    return (
        line.child_material, line.qty_per, line.unit, line.scrap_rate,
        line.valid_from, line.valid_to,
        tuple(sorted(
            (a.alt_material, a.priority, a.ratio, a.valid_from, a.valid_to)
            for a in line.alternatives
        )),
    )


def _line_map(bom: BomHeader) -> dict[int, tuple[BomLine, tuple]]:
    return {line.line_no: (line, _line_key(line)) for line in bom.lines}


def _three_way_lines(base: BomHeader, main_tip: BomHeader, branch_tip: BomHeader):
    base_m, main_m, br_m = _line_map(base), _line_map(main_tip), _line_map(branch_tip)
    conflicts: list[str] = []
    result: list[BomLine] = []
    all_nos = sorted(set(base_m) | set(main_m) | set(br_m))
    for no in all_nos:
        b = base_m.get(no)
        m = main_m.get(no)
        r = br_m.get(no)
        b_sig = b[1] if b else None
        m_sig = m[1] if m else None
        r_sig = r[1] if r else None
        if m_sig == r_sig:
            chosen = m or r
        elif b_sig == m_sig:
            chosen = r                       # 仅分支修改
        elif b_sig == r_sig:
            chosen = m                       # 仅主干修改
        else:
            conflicts.append(
                f"第 {no} 行双方都已修改：主干={m_sig[0] if m else '删除'}，"
                f"分支={r_sig[0] if r else '删除'}")
            continue
        if chosen:
            result.append(_clone_lines([chosen[0]])[0])
    return result, conflicts


def _overlap_issue(bom: BomHeader, old: BomHeader):
    from .errors import ValidationIssue
    return ValidationIssue(
        "missing_dependency", "error", bom.code,
        f"生效区间与已冻结版本 {old.code}（{old.valid_from}~{old.valid_to or '开放'}）冲突，"
        "不能改写已发布历史，请改用更晚的生效日或新版本接续")


def _make_issue(code: str, path: str, message: str):
    from .errors import ValidationIssue
    return ValidationIssue(code, "error", path, message)
