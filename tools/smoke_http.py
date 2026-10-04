"""HTTP 端到端冒烟：复现“父已发布/子件草稿”故障并验证发布门、快照、日期展开。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

B = "http://127.0.0.1:8091"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> None:
    for u in ({"code": "PCS", "name": "个"}, {"code": "G", "name": "克"},
              {"code": "KG", "name": "千克", "base_code": "G", "factor": 1000}):
        call("POST", "/api/units", u)
    for m, n, u in (("ASSY", "整车总成", "PCS"), ("SUB", "子系统", "PCS"), ("RAW", "原料", "G")):
        call("POST", "/api/materials", {"code": m, "name": n, "base_unit": u})

    # SUB 先建草稿不签署
    call("POST", "/api/boms", {"material": "SUB", "valid_from": "2026-03-01"})
    call("PUT", "/api/boms/SUB--V1/lines", {"expected_seq": 0, "lines": [
        {"line_no": 1, "child_material": "RAW", "qty_per": 100, "unit": "G",
         "scrap_rate": 0.05}]})
    # ASSY 引用 SUB
    call("POST", "/api/boms", {"material": "ASSY", "valid_from": "2026-03-01"})
    call("PUT", "/api/boms/ASSY--V1/lines", {"expected_seq": 0, "lines": [
        {"line_no": 1, "child_material": "SUB", "qty_per": 2, "unit": "PCS"}]})

    st, pre = call("GET", "/api/boms/ASSY--V1/precheck")
    print("ASSY 预检 ok =", pre["ok"])
    for i in pre["issues"]:
        print("  -", i["code"], i["severity"], i["message"])
    st, resp = call("POST", "/api/boms/ASSY--V1/sign",
                    {"signed_by": "主管", "expected_seq": 1})
    print("SUB 未发布时签署 ASSY -> HTTP", st, resp.get("error"))

    st, _ = call("POST", "/api/boms/SUB--V1/sign", {"signed_by": "主管", "expected_seq": 1})
    print("签署 SUB -> HTTP", st)
    st, assy = call("POST", "/api/boms/ASSY--V1/sign", {"signed_by": "主管", "expected_seq": 1})
    print("签署 ASSY -> HTTP", st, assy["status"])

    st, snap = call("GET", "/api/boms/ASSY--V1/snapshot")
    fl = snap["lines"][0]
    print("快照固定子件版本:", fl["child_version"], "摘要:", snap["digest"][:12])

    st, ex = call("GET", "/api/explode?material=ASSY&qty=3&date=2026-05-01")
    print("展开 ASSY x3 @2026-05-01（root:", ex["root_version"], ")")
    for item in ex["items"]:
        print(f"  L{item['level']} {item['material']}: {item['required_qty']} {item['base_unit']}"
              f"  用量={item['qty_per']} {item['line_unit']} 换算系数={item['conversion_factor']}"
              f" 损耗={item['scrap_rate']} 父需求={item['parent_demand']}"
              f" 来源={item['bom_version']}#L{item['line_no']} pinned={item['pinned']}")
        print("       来源链:", " -> ".join(item["path"]))
    print("  汇总:", ex["aggregated"])

    # 持久化
    st, r = call("POST", "/api/store/save", {"path": "/tmp/bom_smoke.json"})
    print("持久化 ->", st, r)


if __name__ == "__main__":
    main()
