"""多层 BOM 发布后端的领域回归测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bom_service.api import build_server  # noqa: E402
from bom_service.explode import explode  # noqa: E402
from bom_service.freeze import verify_checksum  # noqa: E402
from bom_service.models import BomError, State  # noqa: E402
from bom_service.service import BomService  # noqa: E402
from bom_service.store import Store  # noqa: E402


def build_car_world() -> tuple[BomService, str]:
    """构造 整车 CAR -> 发动机 ENG -> 活塞 PIS / 机油 OIL，以及螺栓 BOLT。"""
    svc = BomService(Store(":memory:"))
    svc.register_part("CAR", "整车", "台")
    svc.register_part("ENG", "发动机", "台")
    svc.register_part("PIS", "活塞", "个")
    svc.register_part("PIS2", "改进活塞", "个")
    svc.register_part("OIL", "机油", "g", {"kg": 1000.0})
    svc.register_part("BOLT", "螺栓", "个")
    svc.register_part("WASHER", "垫片", "片")

    # 发动机 v1：4 活塞 + 0.5kg 机油/台（kg 经单位图换算到 g）
    d = date(2026, 1, 1)
    eng = svc.create_revision("ENG", d, reason="初次建档")
    svc.set_lines(
        "ENG", "main", eng.version,
        [
            {"child": "PIS", "qty": 4, "unit": "个", "scrap": 0},
            {"child": "OIL", "qty": 0.5, "unit": "kg", "scrap": 0},
        ],
    )
    svc.set_substitutes(
        "ENG", "main", eng.version, "PIS",
        [{"alt": "PIS2", "ratio": 1, "unit": "个", "priority": 0,
          "valid_from": "2026-09-01"}],
    )
    assert svc.submit_for_signoff("ENG", "main", 1)["state"] == "待确认"
    svc.release("ENG", "main", 1, "工程经理")

    # 整车 v1：1 台发动机 + 4 颗螺栓（5% 损耗）
    car = svc.create_revision("CAR", d, reason="试产车型")
    svc.set_lines(
        "CAR", "main", car.version,
        [{"child": "ENG", "qty": 1, "unit": "台"},
         {"child": "BOLT", "qty": 4, "unit": "个", "scrap": 0.05}],
    )
    svc.release("CAR", "main", 1, "工程经理")
    return svc, "CAR"


class ExplosionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_car_world()

    def _rolled(self, result: dict) -> dict[str, float]:
        return {item["part"]: item["gross_qty"] for item in result["rolled"]}

    def test_multilevel_explosion_with_scrap_and_unit_conversion(self) -> None:
        result = explode(self.svc.store, "CAR", 2, date(2026, 3, 1))
        rolled = self._rolled(result)
        self.assertEqual(rolled["ENG"], 2)
        self.assertEqual(rolled["PIS"], 8)          # 2 台 × 4
        self.assertEqual(rolled["OIL"], 1000)       # 2 × 0.5kg = 1000g
        self.assertAlmostEqual(rolled["BOLT"], 8.4)  # 2 × 4 × 1.05

    def test_every_quantity_carries_provenance(self) -> None:
        result = explode(self.svc.store, "CAR", 1, date(2026, 3, 1))
        bolt = next(item for item in result["rolled"] if item["part"] == "BOLT")
        source = bolt["sources"][0]
        self.assertEqual(source["source_revision"], "CAR@main:v1")
        self.assertEqual(source["line_qty_per_parent"], 4)
        self.assertEqual(source["scrap"], 0.05)
        self.assertEqual(source["conversion_to_base_unit"], 1.0)
        self.assertEqual(source["path"], ["CAR", "BOLT"])
        oil = next(item for item in result["rolled"] if item["part"] == "OIL")
        self.assertEqual(oil["sources"][0]["source_revision"], "ENG@main:v1")
        self.assertEqual(oil["sources"][0]["conversion_to_base_unit"], 1000.0)

    def test_history_uses_root_snapshot_even_after_lower_level_changes(self) -> None:
        # 9 月发动机紧急更正为 6 活塞
        eng2 = self.svc.emergency_correction(
            "ENG", 1, valid_from=date(2026, 9, 1), note="活塞加密"
        )
        self.svc.set_lines(
            "ENG", "main", eng2.version,
            [
                {"child": "PIS", "qty": 6, "unit": "个"},
                {"child": "OIL", "qty": 0.5, "unit": "kg"},
            ],
        )
        self.svc.release("ENG", "main", eng2.version, "工程经理")

        # 直接展开发动机：9 月用 v2
        eng_now = explode(self.svc.store, "ENG", 1, date(2026, 10, 1))
        self.assertEqual(eng_now["root_revision"], "ENG@main:v2")
        self.assertEqual(self._rolled(eng_now)["PIS"], 6)

        # 整车 v1 的快照钉住发动机 v1：9 月展开仍是 4 活塞，不受下层改版影响
        car_sep = explode(self.svc.store, "CAR", 1, date(2026, 10, 1))
        self.assertEqual(car_sep["root_revision"], "CAR@main:v1")
        self.assertEqual(self._rolled(car_sep)["PIS"], 4)

        # 整车出新版本引用新发动机后，展开才变化
        car2 = self.svc.emergency_correction("CAR", 1, valid_from=date(2026, 10, 1))
        self.svc.release("CAR", "main", car2.version, "工程经理")
        car_new = explode(self.svc.store, "CAR", 1, date(2026, 10, 1))
        self.assertEqual(car_new["root_revision"], "CAR@main:v2")
        self.assertEqual(self._rolled(car_new)["PIS"], 6)

    def test_line_effective_interval_is_filtered_by_date(self) -> None:
        # 发动机 v3：活塞行 2027 年起才生效，2026 年内展开不计活塞
        rev = self.svc.emergency_correction("ENG", 1, valid_from=date(2027, 1, 1))
        self.svc.set_lines(
            "ENG", "main", rev.version,
            [{"child": "PIS", "qty": 4, "unit": "个", "valid_from": "2027-06-01"},
             {"child": "OIL", "qty": 0.5, "unit": "kg"}],
        )
        self.svc.release("ENG", "main", rev.version, "工程经理")
        early = explode(self.svc.store, "ENG", 1, date(2027, 3, 1))
        self.assertNotIn("PIS", self._rolled(early))
        late = explode(self.svc.store, "ENG", 1, date(2027, 7, 1))
        self.assertEqual(self._rolled(late)["PIS"], 4)

    def test_obsolete_part_switches_to_active_substitute(self) -> None:
        # 活塞 2026-09-01 停用，替代关系同日生效（见建档）
        self.svc.obsolete_part("PIS", date(2026, 9, 1))
        before = explode(self.svc.store, "ENG", 1, date(2026, 8, 15))
        self.assertEqual(self._rolled(before)["PIS"], 4)
        after = explode(self.svc.store, "ENG", 1, date(2026, 9, 15))
        rolled = self._rolled(after)
        self.assertNotIn("PIS", rolled)
        self.assertEqual(rolled["PIS2"], 4)
        src = next(i for i in after["rolled"] if i["part"] == "PIS2")["sources"][0]
        self.assertEqual(src["substituted_from"], "PIS")
        self.assertIn("停用", src["substitute_reason"])

    def test_obsolete_without_substitute_is_surfaces_error(self) -> None:
        self.svc.obsolete_part("OIL", date(2026, 9, 1))
        with self.assertRaises(BomError) as ctx:
            explode(self.svc.store, "ENG", 1, date(2026, 10, 1))
        self.assertEqual(ctx.exception.code, "NO_ACTUAL_SUPPLY")

    def test_explode_without_published_revision(self) -> None:
        with self.assertRaises(BomError) as ctx:
            explode(self.svc.store, "CAR", 1, date(2025, 1, 1))
        self.assertEqual(ctx.exception.code, "NO_PUBLISHED_REVISION")


class ReleaseValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_car_world()

    def _codes(self, code: str = "CAR", branch: str = "main", version: int = 2) -> set[str]:
        return {i["code"] for i in self.svc.validate(code, branch, version)["issues"]}

    def test_lower_level_still_draft_blocks_parent_release(self) -> None:
        # 场景复现：工程同时改总成与子件，子件只有草稿时父级不得发布
        eng2 = self.svc.emergency_correction("ENG", 1, valid_from=date(2026, 11, 1))
        self.svc.set_lines(
            "ENG", "main", eng2.version,
            [{"child": "PIS", "qty": 5, "unit": "个"},
             {"child": "OIL", "qty": 0.5, "unit": "kg"}],
        )
        car2 = self.svc.create_revision("CAR", date(2026, 11, 1), reason="新车型试产")
        self.svc.set_lines(
            "CAR", "main", car2.version,
            [{"child": "ENG", "qty": 1, "unit": "台"},
             {"child": "BOLT", "qty": 4, "unit": "个"}],
        )
        codes = self._codes(version=car2.version)
        self.assertIn("LOWER_LEVEL_DRAFT", codes)
        with self.assertRaises(BomError) as ctx:
            self.svc.submit_for_signoff("CAR", "main", car2.version)
        self.assertEqual(ctx.exception.code, "RELEASE_BLOCKED")

        # 下层签署后，父级校验通过
        self.svc.release("ENG", "main", eng2.version, "工程经理")
        self.assertTrue(self.svc.validate("CAR", "main", car2.version)["ok"])

    def test_cycle_is_detected_including_substitute_edges(self) -> None:
        svc = BomService(Store(":memory:"))
        for code, unit in [("A", "个"), ("B", "个"), ("C", "个")]:
            svc.register_part(code, code, unit)
        ra = svc.create_revision("A", date(2026, 1, 1))
        svc.set_lines("A", "main", ra.version, [{"child": "B", "qty": 1, "unit": "个"}])
        rb = svc.create_revision("B", date(2026, 1, 1))
        svc.set_lines("B", "main", rb.version, [{"child": "C", "qty": 1, "unit": "个"}])
        rc = svc.create_revision("C", date(2026, 1, 1))
        svc.set_lines("C", "main", rc.version, [{"child": "A", "qty": 1, "unit": "个"}])
        # A/B/C 均为草拟，循环仍须在结构检查阶段被发现
        issues = {i["code"] for i in svc.validate("A", "main", ra.version)["issues"]}
        self.assertIn("CYCLE", issues)

    def test_missing_part_and_bad_scrap(self) -> None:
        rev = self.svc.create_revision("WASHER", date(2026, 11, 1))
        self.svc.set_lines(
            "WASHER", "main", rev.version,
            [{"child": "GHOST", "qty": 1, "unit": "个"},
             {"child": "BOLT", "qty": -1, "unit": "个", "scrap": 2}],
        )
        codes = {i["code"] for i in self.svc.validate("WASHER", "main", rev.version)["issues"]}
        self.assertIn("MISSING_PART", codes)
        self.assertIn("BAD_QTY", codes)
        self.assertIn("BAD_SCRAP", codes)

    def test_unit_not_convertible_blocks_release(self) -> None:
        self.svc.register_part("PAINT", "油漆", "L")
        rev = self.svc.create_revision("CAR", date(2026, 11, 1))
        self.svc.set_lines(
            "CAR", "main", rev.version,
            [{"child": "ENG", "qty": 1, "unit": "台"},
             {"child": "PAINT", "qty": 2, "unit": "桶"}],  # 桶与 L 无换算
        )
        codes = {i["code"] for i in self.svc.validate("CAR", "main", rev.version)["issues"]}
        self.assertIn("UNIT_NOT_CONVERTIBLE", codes)
        # 补上换算后通过
        self.svc.add_unit_conversion("PAINT", "桶", 18.0)
        self.assertTrue(self.svc.validate("CAR", "main", rev.version)["ok"])


class VersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = build_car_world()

    def test_emergency_correction_closes_previous_interval_and_copies_contents(self) -> None:
        rev = self.svc.emergency_correction("CAR", 1, valid_from=date(2026, 6, 1), note="现场换型")
        self.assertEqual(rev.version, 2)
        self.assertEqual(rev.state, State.DRAFT)
        self.assertEqual(rev.parent_version, 1)
        self.assertIn("紧急更正", rev.reason)
        old = self.svc.get_revision("CAR", "main", 1)
        self.assertEqual(old.valid_to, date(2026, 6, 1))
        self.assertEqual(len(rev.lines), 2)
        # 旧版仍冻结不可改
        with self.assertRaises(BomError) as ctx:
            self.svc.set_lines("CAR", "main", 1, [])
        self.assertEqual(ctx.exception.code, "REVISION_FROZEN")

    def test_branch_and_merge_produces_new_main_version(self) -> None:
        trial = self.svc.create_branch("CAR", 1, "trial-x", valid_from=date(2026, 6, 1))
        self.assertEqual(trial.version, 1)
        self.svc.set_lines(
            "CAR", "trial-x", trial.version,
            [{"child": "ENG", "qty": 2, "unit": "台"},
             {"child": "WASHER", "qty": 10, "unit": "片"}],
        )
        self.svc.release("CAR", "trial-x", trial.version, "试制经理")
        merged = self.svc.merge_branch("CAR", "trial-x", trial.version, date(2026, 11, 1))
        self.assertEqual(merged.branch, "main")
        self.assertEqual(merged.version, 2)
        self.assertIn("分支合并", merged.reason)
        self.assertEqual(merged.lines[0].child, "ENG")
        self.assertEqual(merged.lines[0].qty, 2)
        self.svc.release("CAR", "main", merged.version, "工程经理")
        result = explode(self.svc.store, "CAR", 1, date(2026, 12, 1))
        rolled = {i["part"]: i["gross_qty"] for i in result["rolled"]}
        self.assertEqual(rolled["ENG"], 2)
        self.assertEqual(rolled["WASHER"], 10)

    def test_obsolete_part_opens_new_draft_for_affected_assembly(self) -> None:
        out = self.svc.obsolete_part("BOLT", date(2026, 9, 1))
        self.assertFalse(out["part"]["active"])
        affected = out["affected_revisions"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["code"], "CAR")
        self.assertEqual(affected[0]["state"], "草拟")
        self.assertIn("部件停用", affected[0]["reason"])

    def test_concurrent_release_only_one_wins(self) -> None:
        rev = self.svc.create_revision("WASHER", date(2026, 11, 1))
        self.svc.set_lines(
            "WASHER", "main", rev.version, [{"child": "BOLT", "qty": 1, "unit": "个"}]
        )
        outcomes: list[str] = []
        lock = threading.Lock()

        def sign(name: str) -> None:
            try:
                self.svc.release("WASHER", "main", rev.version, name)
                with lock:
                    outcomes.append("ok")
            except BomError as exc:
                with lock:
                    outcomes.append(exc.code)

        threads = [threading.Thread(target=sign, args=(f"签署人{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["VERSION_CONFLICT", "ok"])

    def test_later_version_supersedes_open_previous_same_day_conflicts(self) -> None:
        # 生效日更晚的版本：发布时自动截止旧版（版本接替）
        r1 = self.svc.create_revision("WASHER", date(2026, 11, 1))
        self.svc.set_lines("WASHER", "main", r1.version,
                           [{"child": "BOLT", "qty": 1, "unit": "个"}])
        r2 = self.svc.create_revision("WASHER", date(2026, 12, 1))
        self.svc.set_lines("WASHER", "main", r2.version,
                           [{"child": "BOLT", "qty": 2, "unit": "个"}])
        self.svc.release("WASHER", "main", r1.version, "甲")
        result = self.svc.release("WASHER", "main", r2.version, "乙")
        self.assertEqual(result["superseded"], ["WASHER@main:v1"])
        self.assertEqual(self.svc.get_revision("WASHER", "main", 1).valid_to, date(2026, 12, 1))

        # 同日再起一个版本并发发布：无法区分先后，必须冲突
        r3 = self.svc.create_revision("WASHER", date(2026, 12, 1))
        self.svc.set_lines("WASHER", "main", r3.version,
                           [{"child": "BOLT", "qty": 3, "unit": "个"}])
        with self.assertRaises(BomError) as ctx:
            self.svc.release("WASHER", "main", r3.version, "丙")
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_freezes_full_closure_with_checksum(self) -> None:
        svc, _ = build_car_world()
        snap = svc.snapshot_detail("CAR", "main", 1)
        self.assertTrue(verify_checksum(snap))
        self.assertIn("CAR@main:v1", snap["revisions"])
        self.assertIn("ENG@main:v1", snap["revisions"])
        for code in ("CAR", "ENG", "PIS", "OIL", "BOLT"):
            self.assertIn(code, snap["parts"])
        # 单位换算被一并冻结
        self.assertEqual(snap["parts"]["OIL"]["conversions"], {"kg": 1000.0})


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server: ThreadingHTTPServer = build_server(Store(":memory:"), "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _req(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_end_to_end_http_flow(self) -> None:
        for code, unit in [("CAR", "台"), ("ENG", "台"), ("PIS", "个")]:
            status, _ = self._req("POST", "/api/parts",
                                  {"code": code, "name": code, "unit": unit})
            self.assertEqual(status, 201)
        status, body = self._req("POST", "/api/products/ENG/revisions",
                                 {"valid_from": "2026-01-01"})
        self.assertEqual(status, 201)
        self._req("PUT", "/api/revisions/ENG/lines?v=1",
                  {"lines": [{"child": "PIS", "qty": 4, "unit": "个"}]})
        self._req("POST", "/api/revisions/ENG/release?v=1", {"signed_by": "经理"})

        self._req("POST", "/api/products/CAR/revisions", {"valid_from": "2026-01-01"})
        self._req("PUT", "/api/revisions/CAR/lines?v=1",
                  {"lines": [{"child": "ENG", "qty": 1, "unit": "台"}]})
        status, body = self._req("POST", "/api/revisions/CAR/release?v=1", {"signed_by": "经理"})
        self.assertEqual(status, 200)
        self.assertRegex(body["snapshot_checksum"], r"^[0-9a-f]{64}$")

        status, body = self._req("GET", "/api/explode/CAR?date=2026-03-01&qty=3")
        self.assertEqual(status, 200)
        rolled = {i["part"]: i["gross_qty"] for i in body["rolled"]}
        self.assertEqual(rolled["ENG"], 3)
        self.assertEqual(rolled["PIS"], 12)

        # 阻断场景经 HTTP 返回 422
        self._req("POST", "/api/products/ENG/revisions", {"valid_from": "2026-11-01"})
        self._req("PUT", "/api/revisions/ENG/lines?v=2",
                  {"lines": [{"child": "GHOST", "qty": 1, "unit": "个"}]})
        self._req("POST", "/api/products/CAR/revisions", {"valid_from": "2026-11-01"})
        self._req("PUT", "/api/revisions/CAR/lines?v=2",
                  {"lines": [{"child": "ENG", "qty": 1, "unit": "台"}]})
        status, body = self._req("POST", "/api/revisions/CAR/submit?v=2")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "RELEASE_BLOCKED")


if __name__ == "__main__":
    unittest.main()
