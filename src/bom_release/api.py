"""HTTP API（标准库实现，零三方依赖）。

端点：
    GET  /health
    POST /api/units                          注册单位
    POST /api/materials                      注册物料
    GET  /api/materials                      物料清单
    POST /api/boms                           创建草稿 {material, valid_from, branch?, based_on?}
    GET  /api/boms?material=                 版本列表
    GET  /api/boms/{code}                    版本详情
    PUT  /api/boms/{code}/lines              修订行 {expected_seq, lines:[...], change_note?}
    GET  /api/boms/{code}/precheck           发布前校验报告
    POST /api/boms/{code}/sign               签署冻结 {signed_by, expected_seq, signed_at?}
    GET  /api/boms/{code}/snapshot           查看冻结快照
    POST /api/boms/{code}/emergency-correct  紧急更正 {lines, valid_from, reason, signed_by}
    POST /api/materials/{m}/branches         建分支 {branch, from_version, valid_from}
    POST /api/materials/{m}/merge            合并 {branch, valid_from?, merged_by?}
    POST /api/materials/{code}/discontinue   停用 {on_date?, reason?}
    GET  /api/materials/{code}/impacted      受影响的已发布父级
    GET  /api/explode?material=&qty=&date=&mode=snapshot&branch=&prefer_alternative=
    POST /api/store/save  {"path": "..."}
    POST /api/store/load  {"path": "..."}

错误码：404 不存在；409 并发/合并冲突；422 发布校验失败（附全部问题）；400 其它请求错误。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import (
    BomError,
    ConcurrentPublish,
    MergeConflict,
    NotFound,
    ValidationFailed,
)
from .models import Alternative, BomHeader, BomLine, Material, Unit
from .service import BomService
from .store import Store


def create_service(path: str | None = None) -> BomService:
    store = Store.load(path) if path else Store()
    return BomService(store)


def _bom_dict(b: BomHeader, *, with_lines: bool = True) -> dict:
    data = {
        "code": b.code, "material": b.material, "revision": b.revision,
        "branch": b.branch, "parent_version": b.parent_version, "status": b.status,
        "valid_from": b.valid_from, "valid_to": b.valid_to,
        "created_by": b.created_by, "created_at": b.created_at,
        "signed_by": b.signed_by, "signed_at": b.signed_at,
        "edit_seq": b.edit_seq, "change_note": b.change_note,
    }
    if with_lines:
        data["lines"] = [
            {
                "line_no": ln.line_no, "child_material": ln.child_material,
                "qty_per": ln.qty_per, "unit": ln.unit, "scrap_rate": ln.scrap_rate,
                "valid_from": ln.valid_from, "valid_to": ln.valid_to,
                "alternatives": [vars(a) for a in ln.alternatives],
            }
            for ln in b.lines
        ]
    return data


def _lines_from_payload(payload: list[dict]) -> list[BomLine]:
    result = []
    for item in payload:
        result.append(BomLine(
            line_no=item["line_no"],
            child_material=item["child_material"],
            qty_per=float(item["qty_per"]),
            unit=item["unit"],
            scrap_rate=float(item.get("scrap_rate", 0.0)),
            valid_from=item.get("valid_from", "1900-01-01"),
            valid_to=item.get("valid_to"),
            alternatives=[Alternative(**a) for a in item.get("alternatives", [])],
        ))
    return result


class BomHandler(BaseHTTPRequestHandler):
    service: BomService  # 由 make_server 注入到类属性

    server_version = "BomRelease/1.0"

    # ------------------------------------------------------------ 基础
    def _send(self, status: int, body: dict | list) -> None:
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BomError(f"请求体不是合法 JSON：{exc}") from exc

    def _handle_errors(self, fn):
        try:
            fn()
        except ValidationFailed as exc:
            self._send(422, {"error": "validation_failed",
                             "issues": [i.to_dict() for i in exc.issues]})
        except ConcurrentPublish as exc:
            self._send(409, {"error": "concurrent_publish", "message": str(exc)})
        except MergeConflict as exc:
            self._send(409, {"error": "merge_conflict", "conflicts": exc.conflicts})
        except NotFound as exc:
            self._send(404, {"error": "not_found", "message": str(exc)})
        except (BomError, ValueError, KeyError, TypeError) as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self._handle_errors(lambda: self._route_get())

    def do_POST(self) -> None:  # noqa: N802
        self._handle_errors(lambda: self._route_post())

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_errors(lambda: self._route_put())

    def _route_get(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}

        if parts == ["health"]:
            self._send(200, {"status": "ok"})
        elif parts == ["api", "materials"]:
            self._send(200, [vars(m) for m in self.service.store.materials.values()])
        elif parts == ["api", "boms"]:
            items = self.service.store.versions_of(q["material"]) if "material" in q \
                else list(self.service.store.boms.values())
            self._send(200, [_bom_dict(b, with_lines=False) for b in items])
        elif len(parts) == 4 and parts[:2] == ["api", "boms"] and parts[3] == "precheck":
            issues = self.service.precheck(parts[2])
            self._send(200, {"ok": not any(i.severity == "error" for i in issues),
                             "issues": [i.to_dict() for i in issues]})
        elif len(parts) == 4 and parts[:2] == ["api", "boms"] and parts[3] == "snapshot":
            snap = self.service.store.require_snapshot(parts[2])
            self._send(200, snap.to_dict())
        elif len(parts) == 4 and parts[:2] == ["api", "boms"]:
            self._send(200, _bom_dict(self.service.store.require_bom(parts[2])))
        elif (len(parts) == 4 and parts[:2] == ["api", "materials"]
              and parts[3] == "impacted"):
            self._send(200, self.service.impacted_parents(parts[2]))
        elif parts == ["api", "explode"]:
            for key in ("material", "qty", "date"):
                if key not in q:
                    raise BomError(f"缺少查询参数：{key}")
            result = self.service.explode(
                q["material"], float(q["qty"]), q["date"],
                mode=q.get("mode", "snapshot"),
                branch=q.get("branch"),
                prefer_alternative=q.get("prefer_alternative"),
            )
            self._send(200, result.to_dict())
        else:
            self._send(404, {"error": "not_found", "message": url.path})

    def _route_post(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        body = self._body()
        svc = self.service

        if parts == ["api", "units"]:
            svc.store.add_unit(Unit(**body))
            self._send(201, body)
        elif parts == ["api", "materials"]:
            m = Material(code=body["code"], name=body["name"],
                         base_unit=body["base_unit"],
                         discontinued=body.get("discontinued", False))
            svc.store.add_material(m)
            self._send(201, vars(m))
        elif parts == ["api", "boms"]:
            bom = svc.create_draft(
                body["material"], body["valid_from"],
                branch=body.get("branch", "main"),
                based_on=body.get("based_on"),
                created_by=body.get("created_by", "工程"),
                change_note=body.get("change_note", ""),
                valid_to=body.get("valid_to"),
            )
            self._send(201, _bom_dict(bom))
        elif len(parts) == 4 and parts[:2] == ["api", "boms"] and parts[3] == "sign":
            bom = svc.sign(parts[2], body["signed_by"], int(body["expected_seq"]),
                           signed_at=body.get("signed_at"))
            self._send(200, _bom_dict(bom))
        elif len(parts) == 4 and parts[:2] == ["api", "boms"] and parts[3] == "emergency-correct":
            bom = svc.emergency_correct(
                parts[2], body.get("signed_by", "工程"),
                lines=_lines_from_payload(body["lines"]),
                valid_from=body["valid_from"], reason=body.get("reason", ""),
            )
            self._send(201, _bom_dict(bom))
        elif len(parts) == 4 and parts[:2] == ["api", "materials"] and parts[3] == "branches":
            bom = svc.create_branch(
                parts[2], body["branch"], body["from_version"],
                valid_from=body["valid_from"], created_by=body.get("created_by", "工程"))
            self._send(201, _bom_dict(bom))
        elif len(parts) == 4 and parts[:2] == ["api", "materials"] and parts[3] == "merge":
            bom = svc.merge_branch(
                parts[2], body["branch"],
                merged_by=body.get("merged_by", "工程"),
                valid_from=body.get("valid_from"))
            self._send(201, _bom_dict(bom))
        elif len(parts) == 4 and parts[:2] == ["api", "materials"] and parts[3] == "discontinue":
            self._send(200, svc.discontinue_material(
                parts[2], on_date=body.get("on_date"), reason=body.get("reason", "")))
        elif parts == ["api", "store", "save"]:
            svc.store.save(body["path"])
            self._send(200, {"saved": body["path"]})
        elif parts == ["api", "store", "load"]:
            self.service = create_service(body["path"])
            type(self).service = self.service
            self._send(200, {"loaded": body["path"]})
        else:
            self._send(404, {"error": "not_found", "message": url.path})

    def _route_put(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        body = self._body()
        if len(parts) == 4 and parts[:2] == ["api", "boms"] and parts[3] == "lines":
            bom = self.service.revise_lines(
                parts[2], int(body["expected_seq"]),
                _lines_from_payload(body["lines"]),
                change_note=body.get("change_note"))
            self._send(200, _bom_dict(bom))
        else:
            self._send(404, {"error": "not_found", "message": url.path})


def make_server(host: str = "127.0.0.1", port: int = 8080,
                data_path: str | None = None) -> ThreadingHTTPServer:
    service = create_service(data_path)

    class _Handler(BomHandler):
        pass

    _Handler.service = service
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd
