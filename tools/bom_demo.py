"""端到端业务场景演示：新车型试产时的多层 BOM 发布。

运行：``python3 tools/bom_demo.py``

场景：
1. 工程部门同时修改总成（整车 v2）、子件（发动机 v2 草拟）与替代料关系；
2. 采购尝试先发布父级 -> 被"下层仍引用草稿"规则阻断；
3. 下层先签署，父级再签署 -> 冻结完整依赖快照；
4. 按任意日期展开需求，逐项说明用量来源；
5. 部件停用 -> 自动切替代料；紧急更正 -> 新版本，历史展开不受影响。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bom_service.explode import explode
from bom_service.models import BomError
from bom_service.service import BomService
from bom_service.store import Store


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = BomService(Store(":memory:"))

    # 部件主数据（机油主单位 g，登记 1kg = 1000g）
    for code, name, unit, conv in [
        ("CAR", "整车", "台", {}),
        ("ENG", "发动机", "台", {}),
        ("PIS", "活塞", "个", {}),
        ("PIS2", "改进活塞", "个", {}),
        ("OIL", "机油", "g", {"kg": 1000.0}),
        ("BOLT", "螺栓", "个", {}),
    ]:
        svc.register_part(code, name, unit, conv)

    # 发动机 v1：4 活塞 + 0.5kg 机油
    eng1 = svc.create_revision("ENG", date(2026, 1, 1), reason="量产基线")
    svc.set_lines("ENG", "main", eng1.version, [
        {"child": "PIS", "qty": 4, "unit": "个"},
        {"child": "OIL", "qty": 0.5, "unit": "kg"},
    ])
    svc.set_substitutes("ENG", "main", eng1.version, "PIS", [
        {"alt": "PIS2", "ratio": 1, "unit": "个", "priority": 0,
         "valid_from": "2026-09-01"},
    ])
    svc.submit_for_signoff("ENG", "main", eng1.version)
    svc.release("ENG", "main", eng1.version, "工程经理-张")

    # 整车 v1
    car1 = svc.create_revision("CAR", date(2026, 1, 1), reason="量产车型")
    svc.set_lines("CAR", "main", car1.version, [
        {"child": "ENG", "qty": 1, "unit": "台"},
        {"child": "BOLT", "qty": 4, "unit": "个", "scrap": 0.05},
    ])
    svc.release("CAR", "main", car1.version, "工程经理-张")

    # 新车型试产：工程同时改子件（发动机 v2 草拟）和总成（整车 v2 草拟）
    eng2 = svc.create_revision("ENG", date(2026, 11, 1), reason="新机型：6 活塞")
    svc.set_lines("ENG", "main", eng2.version, [
        {"child": "PIS", "qty": 6, "unit": "个"},
        {"child": "OIL", "qty": 0.5, "unit": "kg"},
    ])
    car2 = svc.create_revision("CAR", date(2026, 11, 1), reason="新车型试产")
    svc.set_lines("CAR", "main", car2.version, [
        {"child": "ENG", "qty": 1, "unit": "台"},
        {"child": "BOLT", "qty": 4, "unit": "个", "scrap": 0.05},
    ])

    # 采购尝试先发布父级 —— 必须被阻断
    print("\n===== 采购先发布父级 CAR v2（下层 ENG v2 仍为草拟）=====")
    try:
        svc.release("CAR", "main", car2.version, "采购计划员")
    except BomError as exc:
        print(f"已阻断 [{exc.code}]：{exc.message}")
        for item in exc.details or []:
            if item["code"] == "LOWER_LEVEL_DRAFT":
                print("  ->", item["message"], item.get("detail") or "")

    # 正确顺序：下层先签署，再签父级，冻结完整快照
    svc.release("ENG", "main", eng2.version, "工程经理-李")
    result = svc.release("CAR", "main", car2.version, "工程经理-李")
    show("父级签署成功并冻结快照", {"rev_id": result["rev_id"], "checksum": result["snapshot_checksum"]})

    # 按日期展开
    before = explode(svc.store, "CAR", 10, date(2026, 6, 1))
    show("2026-06-01 展开 10 台（旧版：4 活塞/台）",
         {"root_revision": before["root_revision"],
          "rolled": [{"part": x["part"], "qty": x["gross_qty"], "unit": x["unit"]}
                     for x in before["rolled"]]})

    after = explode(svc.store, "CAR", 10, date(2026, 11, 1))
    show("2026-11-01 展开 10 台（新版：6 活塞/台，螺栓含 5% 损耗）",
         {"root_revision": after["root_revision"],
          "rolled": [{"part": x["part"], "qty": x["gross_qty"], "unit": x["unit"]}
                     for x in after["rolled"]]})
    bolt_src = next(x for x in after["rolled"] if x["part"] == "BOLT")["sources"][0]
    show("螺栓用量来源说明", bolt_src)

    # 部件停用 -> 自动替代
    svc.obsolete_part("PIS", date(2026, 9, 1))
    sub = explode(svc.store, "ENG", 1, date(2026, 9, 15))
    pis2 = next(x for x in sub["rolled"] if x["part"] == "PIS2")
    show("活塞停用后 2026-09-15 展开", {
        "rolled": [{"part": x["part"], "qty": x["gross_qty"]} for x in sub["rolled"]],
        "替代来源": pis2["sources"][0],
    })


if __name__ == "__main__":
    main()
