"""多层 BOM 发布后端的端到端测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bom_release import (
    Alternative,
    BomLine,
    BomService,
    ConcurrentPublish,
    Material,
    MergeConflict,
    NotFound,
    Store,
    Unit,
    ValidationFailed,
)


def line(no, child, qty, unit="PCS", scrap=0.0, valid_from="2026-01-01", alternatives=None):
    return BomLine(line_no=no, child_material=child, qty_per=qty, unit=unit,
                   scrap_rate=scrap, valid_from=valid_from,
                   alternatives=alternatives or [])


def seed_basic(service: BomService) -> None:
    """SCREW(叶子,单位G) -> GEARBOX -> ASSY(总成)，另有 HOUSING 叶子。"""
    s = service.store
    s.add_unit(Unit("PCS", "个", None))
    s.add_unit(Unit("G", "克", None))
    s.add_unit(Unit("KG", "千克", "G", 1000.0))
    s.add_material(Material("ASSY", "整车总成", "PCS"))
    s.add_material(Material("GEARBOX", "变速箱总成", "PCS"))
    s.add_material(Material("HOUSING", "箱体", "PCS"))
    s.add_material(Material("SCREW", "螺栓", "G"))

    gb = service.create_draft("GEARBOX", "2026-01-01", created_by="工程")
    service.revise_lines(gb.code, gb.edit_seq, [
        line(1, "SCREW", 2.0, "G", scrap=0.1),
        line(2, "HOUSING", 1.0),
    ])
    service.sign(gb.code, "工程主管", gb.edit_seq)

    assy = service.create_draft("ASSY", "2026-02-01", created_by="工程")
    service.revise_lines(assy.code, assy.edit_seq, [line(1, "GEARBOX", 1.0)])
    service.sign(assy.code, "工程主管", assy.edit_seq)


class CoreFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = BomService(Store())
        seed_basic(self.service)

    # -------------------------------------------------- 场景一：快照冻结
    def test_snapshot_pins_exact_child_version(self) -> None:
        snap = self.service.store.require_snapshot("ASSY--V1")
        self.assertEqual(len(snap.lines), 1)
        pinned = snap.lines[0]
        self.assertEqual(pinned.child_version, "GEARBOX--V1")
        self.assertEqual(pinned.child_revision, 1)
        # 递归快照中的叶子件不带版本
        gb_snap = self.service.store.require_snapshot("GEARBOX--V1")
        self.assertIsNone(gb_snap.lines[0].child_version)
        self.assertEqual(snap.digest, self.service.store.require_snapshot("ASSY--V1").digest)

    def test_parent_cannot_reference_draft_child(self) -> None:
        """父级发布时下层只有草稿 -> 拒绝（题面核心故障）。"""
        s = self.service
        s.store.add_material(Material("NEWPART", "试产新件", "PCS"))
        draft = s.create_draft("NEWPART", "2026-03-01")  # 只有草稿
        s.revise_lines(draft.code, draft.edit_seq, [line(1, "HOUSING", 1.0)])

        assy2 = s.create_draft("ASSY", "2026-03-01", based_on="ASSY--V1")
        s.revise_lines(assy2.code, assy2.edit_seq, [
            line(1, "GEARBOX", 1.0),
            line(2, "NEWPART", 1.0),
        ])
        with self.assertRaises(ValidationFailed) as ctx:
            s.sign(assy2.code, "工程主管", assy2.edit_seq)
        codes = {i.code for i in ctx.exception.issues}
        self.assertIn("draft_reference", codes)

    # -------------------------------------------------- 场景二：循环依赖
    def test_cycle_detected_across_signed_graph(self) -> None:
        s = self.service
        # 已发布 GEARBOX；新增循环：GEARBOX 新版本引用 ASSY，而 ASSY 引用 GEARBOX
        # 直接让 ASSY 的草稿引用自身
        s.store.add_material(Material("LOOP-A", "环A", "PCS"))
        s.store.add_material(Material("LOOP-B", "环B", "PCS"))
        a = s.create_draft("LOOP-A", "2026-03-01")
        s.revise_lines(a.code, a.edit_seq, [line(1, "LOOP-B", 1.0)])
        b = s.create_draft("LOOP-B", "2026-03-01")
        s.revise_lines(b.code, b.edit_seq, [line(1, "LOOP-A", 1.0)])
        issues = s.precheck(a.code)
        self.assertTrue(any(i.code == "cycle" for i in issues))

    # -------------------------------------------------- 场景三：缺失依赖与单位
    def test_missing_dependency_and_unit_dimension(self) -> None:
        s = self.service
        draft = s.create_draft("ASSY", "2026-04-01")
        s.revise_lines(draft.code, draft.edit_seq, [
            line(1, "GHOST", 1.0),                       # 物料不存在
            line(2, "SCREW", 1.0, "PCS"),                # PCS 与 G 量纲不同
        ])
        issues = s.precheck(draft.code)
        codes = {(i.code, i.path) for i in issues}
        self.assertTrue(any(c == "missing_dependency" for c, _ in codes))
        self.assertTrue(any(c == "unit_conversion" for c, _ in codes))

    def test_unit_conversion_applied_in_explosion(self) -> None:
        s = self.service
        # SCREW 以 KG 建用量（基本单位 G）：每箱 0.002KG=2G，损耗10%
        gb2 = s.create_draft("GEARBOX", "2026-06-01", based_on="GEARBOX--V1")
        s.revise_lines(gb2.code, gb2.edit_seq, [
            line(1, "SCREW", 0.002, "KG", scrap=0.1),
            line(2, "HOUSING", 1.0),
        ])
        s.sign(gb2.code, "工程主管", gb2.edit_seq)
        result = s.explode("ASSY", 5.0, "2026-07-01", mode="current")
        screw = result.aggregated["SCREW"]
        # 5 ASSY * 1 GEARBOX * 0.002 KG * 1.1 * 1000 = 11 G
        self.assertAlmostEqual(screw["total_qty"], 11.0, places=6)

    # -------------------------------------------------- 场景四：任意日期展开与来源
    def test_explode_with_provenance(self) -> None:
        result = self.service.explode("ASSY", 2.0, "2026-03-01")
        materials = [i.material for i in result.items]
        self.assertIn("GEARBOX", materials)
        self.assertIn("SCREW", materials)
        self.assertIn("HOUSING", materials)
        screw = next(i for i in result.items if i.material == "SCREW")
        # 2 ASSY * 1 * 2G * 1.1 = 4.4G
        self.assertAlmostEqual(screw.required_qty, 4.4, places=6)
        self.assertEqual(screw.bom_version, "GEARBOX--V1")
        self.assertEqual(screw.line_no, 1)
        self.assertEqual(screw.scrap_rate, 0.1)
        self.assertEqual(screw.path[0], "ASSY")
        self.assertTrue(any("GEARBOX--V1" in p for p in screw.path))
        self.assertTrue(screw.pinned)

    def test_explode_before_any_release_is_unresolved(self) -> None:
        result = self.service.explode("ASSY", 1.0, "2025-01-01")
        self.assertIsNone(result.root_version)
        self.assertTrue(result.unresolved)

    def test_explode_uses_snapshot_not_later_draft(self) -> None:
        """子件后续出现草稿/新版本，历史展开仍走签署时固定版本。"""
        s = self.service
        gb2 = s.create_draft("GEARBOX", "2026-09-01", based_on="GEARBOX--V1")
        s.revise_lines(gb2.code, gb2.edit_seq, [
            line(1, "SCREW", 9.0, "G"),
            line(2, "HOUSING", 1.0),
        ])
        s.sign(gb2.code, "工程主管", gb2.edit_seq)
        # 2026-05：ASSY--V1 快照固定 GEARBOX--V1，仍按 2G*1.1
        may = s.explode("ASSY", 1.0, "2026-05-01")
        self.assertAlmostEqual(may.aggregated["SCREW"]["total_qty"], 2.2, places=6)
        # current 模式在 2026-05 也选不到 9 月版本
        # 2026-10 用 snapshot 模式仍固定 V1（签署时口径）
        oct_snap = s.explode("ASSY", 1.0, "2026-10-01", mode="snapshot")
        self.assertAlmostEqual(oct_snap.aggregated["SCREW"]["total_qty"], 2.2, places=6)
        oct_cur = s.explode("ASSY", 1.0, "2026-10-01", mode="current")
        self.assertAlmostEqual(oct_cur.aggregated["SCREW"]["total_qty"], 9.0, places=6)

    # -------------------------------------------------- 场景五：紧急更正
    def test_emergency_correction_trims_old_interval(self) -> None:
        s = self.service
        corrected = s.emergency_correct(
            "GEARBOX--V1", "质量工程师",
            lines=[line(1, "SCREW", 3.0, "G", scrap=0.0), line(2, "HOUSING", 1.0)],
            valid_from="2026-05-01", reason="螺栓用量勘误",
        )
        old = s.store.require_bom("GEARBOX--V1")
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.valid_to, "2026-05-01")
        self.assertEqual(corrected.status, "signed")
        # 更正前按旧口径 2*1.1，更正后按新口径 3
        before = s.explode("ASSY", 1.0, "2026-04-01", mode="current")
        after = s.explode("ASSY", 1.0, "2026-05-15", mode="current")
        self.assertAlmostEqual(before.aggregated["SCREW"]["total_qty"], 2.2, places=6)
        self.assertAlmostEqual(after.aggregated["SCREW"]["total_qty"], 3.0, places=6)

    def test_emergency_correction_cannot_rewrite_frozen_history(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.emergency_correct(
                "GEARBOX--V1", "质量工程师",
                lines=[line(1, "SCREW", 3.0, "G")],
                valid_from="2025-06-01", reason="试图改写历史",
            )

    # -------------------------------------------------- 场景六：分支合并
    def test_branch_merge_three_way(self) -> None:
        s = self.service
        # 主干在 V1 之后发布 V2：第 2 行 HOUSING 用量损耗 5%
        main2 = s.create_draft("GEARBOX", "2026-05-01", based_on="GEARBOX--V1")
        s.revise_lines(main2.code, main2.edit_seq, [
            line(1, "SCREW", 2.0, "G", scrap=0.1),
            line(2, "HOUSING", 1.0, scrap=0.05),
        ])
        s.sign(main2.code, "工程主管", main2.edit_seq)
        # 分支 hotfix：自 V1 拉出，只改第 1 行用量
        br = s.create_branch("GEARBOX", "hotfix", "GEARBOX--V1", valid_from="2026-05-01")
        s.revise_lines(br.code, br.edit_seq, [
            line(1, "SCREW", 2.5, "G", scrap=0.1),
            line(2, "HOUSING", 1.0),
        ])
        s.sign(br.code, "工程主管", br.edit_seq)
        merged = s.merge_branch("GEARBOX", "hotfix", valid_from="2026-08-01")
        merged_lines = {ln.child_material: ln for ln in merged.lines}
        self.assertEqual(merged_lines["SCREW"].qty_per, 2.5)   # 分支改动
        self.assertEqual(merged_lines["HOUSING"].scrap_rate, 0.05)  # 主干改动

    def test_branch_merge_conflict(self) -> None:
        s = self.service
        main2 = s.create_draft("GEARBOX", "2026-05-01", based_on="GEARBOX--V1")
        s.revise_lines(main2.code, main2.edit_seq, [
            line(1, "SCREW", 4.0, "G"), line(2, "HOUSING", 1.0),
        ])
        s.sign(main2.code, "工程主管", main2.edit_seq)
        br = s.create_branch("GEARBOX", "hotfix", "GEARBOX--V1", valid_from="2026-05-01")
        s.revise_lines(br.code, br.edit_seq, [
            line(1, "SCREW", 8.0, "G"), line(2, "HOUSING", 1.0),
        ])
        s.sign(br.code, "工程主管", br.edit_seq)
        with self.assertRaises(MergeConflict) as ctx:
            s.merge_branch("GEARBOX", "hotfix", valid_from="2026-08-01")
        self.assertTrue(any("第 1 行" in c for c in ctx.exception.conflicts))

    # -------------------------------------------------- 场景七：停用
    def test_discontinue_lists_impacted_parents(self) -> None:
        s = self.service
        report = s.discontinue_material("HOUSING", reason="供应商停产")
        self.assertTrue(s.store.materials["HOUSING"].discontinued)
        impacted = {p["bom_version"] for p in report["impacted_published_parents"]}
        self.assertIn("GEARBOX--V1", impacted)
        self.assertIn("ASSY--V1", impacted)
        # 停用后不能再被新发布引用
        draft = s.create_draft("GEARBOX", "2026-10-01", based_on="GEARBOX--V1")
        with self.assertRaises(ValidationFailed):
            s.sign(draft.code, "工程主管", draft.edit_seq)

    # -------------------------------------------------- 场景八：并发发布
    def test_optimistic_lock_concurrent_publish(self) -> None:
        s = self.service
        draft = s.create_draft("ASSY", "2026-11-01")
        s.revise_lines(draft.code, draft.edit_seq, [line(1, "HOUSING", 1.0)])
        stale_seq = draft.edit_seq
        # 工程师 A 先修订
        s.revise_lines(draft.code, stale_seq, [line(1, "HOUSING", 2.0)])
        # 工程师 B 持旧序号签署 -> 冲突，需基于新版本重试
        with self.assertRaises(ConcurrentPublish):
            s.sign(draft.code, "工程主管", stale_seq)
        # 取得最新序号后可签署
        s.sign(draft.code, "工程主管", draft.edit_seq)
        self.assertEqual(s.store.require_bom(draft.code).status, "signed")

    def test_interval_mutex_on_concurrent_dates(self) -> None:
        """同分支回溯覆盖已冻结区间必须被拒；开放尾部接续则允许。"""
        s = self.service
        # 先发布 V2，使 V1 的区间被裁剪关闭 [2026-01-01, 2026-06-01)
        v2 = s.create_draft("GEARBOX", "2026-06-01", based_on="GEARBOX--V1")
        s.revise_lines(v2.code, v2.edit_seq, [
            line(1, "SCREW", 5.0, "G"), line(2, "HOUSING", 1.0),
        ])
        s.sign(v2.code, "工程主管", v2.edit_seq)
        # 试图在已关闭的历史区间内插入版本（2026-02-15 落在 V1 冻结区间内）
        draft = s.create_draft("GEARBOX", "2026-02-15", valid_to="2026-03-01")
        s.revise_lines(draft.code, draft.edit_seq, [line(1, "SCREW", 1.0, "G")])
        with self.assertRaises(ValidationFailed) as ctx:
            s.sign(draft.code, "工程主管", draft.edit_seq)
        self.assertTrue(ctx.exception.issues)
        # 开放尾部接续合法：相当于正常发版/紧急更正
        v3 = s.create_draft("GEARBOX", "2026-09-01", based_on="GEARBOX--V2")
        s.revise_lines(v3.code, v3.edit_seq, [line(1, "SCREW", 1.0, "G")])
        s.sign(v3.code, "工程主管", v3.edit_seq)
        self.assertEqual(s.store.require_bom("GEARBOX--V2").valid_to, "2026-09-01")

    # -------------------------------------------------- 场景九：替代料
    def test_alternative_explosion(self) -> None:
        s = self.service
        s.store.add_material(Material("BOLT2", "替代螺栓", "G"))
        gb2 = s.create_draft("GEARBOX", "2026-03-01", based_on="GEARBOX--V1")
        s.revise_lines(gb2.code, gb2.edit_seq, [
            BomLine(line_no=1, child_material="SCREW", qty_per=2.0, unit="G",
                    scrap_rate=0.1, valid_from="2026-03-01", alternatives=[
                        Alternative(alt_material="BOLT2", priority=1, ratio=1.5,
                                    valid_from="2026-04-01")]),
            line(2, "HOUSING", 1.0),
        ])
        s.sign(gb2.code, "工程主管", gb2.edit_seq)
        normal = s.explode("ASSY", 1.0, "2026-05-01", mode="current")
        self.assertIn("SCREW", normal.aggregated)
        alt = s.explode("ASSY", 1.0, "2026-05-01", mode="current",
                        prefer_alternative="BOLT2")
        self.assertIn("BOLT2", alt.aggregated)
        self.assertNotIn("SCREW", alt.aggregated)
        item = next(i for i in alt.items if i.material == "BOLT2")
        self.assertEqual(item.via_alternative_of, "SCREW")
        # 1 * 2 * 1.5(ratio) * 1.1 = 3.3
        self.assertAlmostEqual(item.required_qty, 3.3, places=6)
        # 替代关系在替代生效日前不被采用
        early = s.explode("ASSY", 1.0, "2026-03-15", mode="current",
                          prefer_alternative="BOLT2")
        self.assertIn("SCREW", early.aggregated)

    # -------------------------------------------------- 持久化
    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            self.service.store.save(path)
            restored = BomService(Store.load(path))
            self.assertEqual(
                restored.store.require_snapshot("ASSY--V1").digest,
                self.service.store.require_snapshot("ASSY--V1").digest,
            )
            result = restored.explode("ASSY", 3.0, "2026-03-01")
            self.assertAlmostEqual(result.aggregated["SCREW"]["total_qty"], 6.6, places=6)


class PrecheckWiringTest(unittest.TestCase):
    def test_warning_does_not_block_sign(self) -> None:
        service = BomService(Store())
        s = service.store
        s.add_unit(Unit("PCS", "个", None))
        s.add_material(Material("P", "父", "PCS"))
        s.add_material(Material("C", "子", "PCS"))
        v1 = service.create_draft("C", "2026-01-01")
        service.revise_lines(v1.code, v1.edit_seq, [])
        service.sign(v1.code, "工程", v1.edit_seq)
        v2 = service.create_draft("C", "2026-09-01")  # 未签署草稿
        p = service.create_draft("P", "2026-02-01")
        service.revise_lines(p.code, p.edit_seq, [line(1, "C", 1.0)])
        issues = service.precheck(p.code)
        self.assertTrue(any(i.code == "draft_reference" and i.severity == "warning"
                            for i in issues))
        service.sign(p.code, "工程", p.edit_seq)  # 警告不阻断；快照固定 C--V1
        snap = service.store.require_snapshot("P--V1")
        self.assertEqual(snap.lines[0].child_version, "C--V1")

    def test_signed_version_is_immutable(self) -> None:
        service = BomService(Store())
        service.store.add_unit(Unit("PCS", "个", None))
        service.store.add_material(Material("P", "父", "PCS"))
        v = service.create_draft("P", "2026-01-01")
        service.sign(v.code, "工程", v.edit_seq)
        with self.assertRaises(NotFound):
            service.revise_lines(v.code, v.edit_seq, [])


if __name__ == "__main__":
    unittest.main()
