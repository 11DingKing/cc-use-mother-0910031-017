"""JSON HTTP 接口（标准库 http.server，无外部依赖）。

路由（branch 默认 main，版本用 v 指定）::

    POST   /api/parts                                 建立/更新部件主数据
    GET    /api/parts                                 部件清单
    POST   /api/parts/{code}/conversion               登记单位换算
    POST   /api/parts/{code}/obsolete                 部件停用（自动开受影响新版本）
    POST   /api/products/{code}/revisions            起草新版本
    GET    /api/products/{code}/revisions            版本列表
    GET    /api/revisions/{code}?branch=&v=           版本详情
    PUT    /api/revisions/{code}/lines?branch=&v=     设置用量行
    PUT    /api/revisions/{code}/substitutes/{child}?branch=&v= 设置替代关系
    GET    /api/revisions/{code}/validate?branch=&v= 发布前校验
    POST   /api/revisions/{code}/submit?branch=&v=    提交签署
    POST   /api/revisions/{code}/release?branch=&v=   签署发布（冻结快照）
    POST   /api/revisions/{code}/emergency-correction?v=&branch= 紧急更正
    POST   /api/products/{code}/branches/{name}?v=    从已发布版本拉分支
    POST   /api/branches/{code}/{name}/merge?v=       分支合并回 main
    GET    /api/explode/{code}?date=&qty=&branch=     按日期展开需求
    GET    /api/snapshots                             快照清单
    GET    /api/snapshots/{code}?branch=&v=           快照详情

启动：``python -m bom_service.api --db bom.db --port 8080``
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .explode import explode
from .models import BomError
from .service import BomService
from .store import Store


def _parse_date(value: str | None, field: str = "date") -> date:
    if not value:
        raise BomError("BAD_INPUT", f"缺少日期参数：{field}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BomError("BAD_INPUT", f"日期格式应为 YYYY-MM-DD：{value}") from exc


class _Handler(BaseHTTPRequestHandler):
    service: BomService

    # 屏蔽默认日志中过长的路径，统一走我们自己的格式
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- 基础框架
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: BomError) -> None:
        status = {
            "BAD_INPUT": HTTPStatus.BAD_REQUEST,
            "BAD_QTY": HTTPStatus.BAD_REQUEST,
            "BAD_SCRAP": HTTPStatus.BAD_REQUEST,
            "BAD_INTERVAL": HTTPStatus.BAD_REQUEST,
            "UNIT_BAD_FACTOR": HTTPStatus.BAD_REQUEST,
            "BAD_STATE": HTTPStatus.CONFLICT,
            "NOT_FROZEN": HTTPStatus.CONFLICT,
            "REVISION_FROZEN": HTTPStatus.CONFLICT,
            "VERSION_CONFLICT": HTTPStatus.CONFLICT,
            "BRANCH_EXISTS": HTTPStatus.CONFLICT,
            "NO_PUBLISHED_REVISION": HTTPStatus.CONFLICT,
            "SNAPSHOT_MISSING": HTTPStatus.CONFLICT,
            "NO_ACTUAL_SUPPLY": HTTPStatus.UNPROCESSABLE_ENTITY,
            "RELEASE_BLOCKED": HTTPStatus.UNPROCESSABLE_ENTITY,
        }.get(exc.code, HTTPStatus.NOT_FOUND)
        self._send(status, {"error": exc.to_dict()})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BomError("BAD_INPUT", f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise BomError("BAD_INPUT", "请求体必须是 JSON 对象")
        return data

    def _qs(self) -> parse_qs:
        return parse_qs(urlsplit(self.path).query)

    def _params(self, qs: parse_qs) -> tuple[str, int]:
        branch = qs.get("branch", ["main"])[0]
        try:
            version = int(qs["v"][0])
        except (KeyError, ValueError) as exc:
            raise BomError("BAD_INPUT", "缺少版本参数 v（整数）") from exc
        return branch, version

    # --------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except BomError as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except BomError as exc:
            self._error(exc)

    def do_PUT(self) -> None:  # noqa: N802
        try:
            self._route_put()
        except BomError as exc:
            self._error(exc)

    def _route_get(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        qs = self._qs()
        svc = self.service

        if path == "/api/parts":
            self._send(HTTPStatus.OK, {"parts": [p.to_dict() for p in svc.store.list_parts()]})
            return
        if path == "/api/snapshots":
            self._send(HTTPStatus.OK, {"snapshots": svc.store.list_snapshots()})
            return

        m = re.fullmatch(r"/api/products/([^/]+)/revisions", path)
        if m:
            self._send(
                HTTPStatus.OK,
                {"revisions": svc.list_revisions(m.group(1), qs.get("branch", [None])[0])},
            )
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)", path)
        if m:
            branch, version = self._params(qs)
            self._send(HTTPStatus.OK, svc.revision_detail(m.group(1), branch, version))
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)/validate", path)
        if m:
            branch, version = self._params(qs)
            self._send(HTTPStatus.OK, svc.validate(m.group(1), branch, version))
            return

        m = re.fullmatch(r"/api/snapshots/([^/]+)", path)
        if m:
            branch, version = self._params(qs)
            self._send(HTTPStatus.OK, svc.snapshot_detail(m.group(1), branch, version))
            return

        m = re.fullmatch(r"/api/explode/([^/]+)", path)
        if m:
            on_date = _parse_date(qs.get("date", [None])[0])
            try:
                qty = float(qs.get("qty", ["1"])[0])
            except ValueError as exc:
                raise BomError("BAD_INPUT", "qty 必须是数字") from exc
            branch = qs.get("branch", ["main"])[0]
            self._send(
                HTTPStatus.OK,
                explode(svc.store, m.group(1), qty, on_date, branch),
            )
            return

        self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": path}})

    def _route_post(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        qs = self._qs()
        svc = self.service
        data = self._read_json()

        if path == "/api/parts":
            part = svc.register_part(
                code=data["code"],
                name=data.get("name", data["code"]),
                unit=data["unit"],
                conversions=data.get("conversions"),
            )
            self._send(HTTPStatus.CREATED, {"part": part.to_dict()})
            return

        m = re.fullmatch(r"/api/parts/([^/]+)/conversion", path)
        if m:
            svc.add_unit_conversion(m.group(1), data["target"], float(data["factor"]))
            self._send(HTTPStatus.OK, {"ok": True})
            return

        m = re.fullmatch(r"/api/parts/([^/]+)/obsolete", path)
        if m:
            on_date = _parse_date(data.get("on_date"), "on_date") if data.get("on_date") else None
            self._send(HTTPStatus.OK, svc.obsolete_part(m.group(1), on_date))
            return

        m = re.fullmatch(r"/api/products/([^/]+)/revisions", path)
        if m:
            valid_from = _parse_date(data["valid_from"], "valid_from")
            valid_to = _parse_date(data["valid_to"], "valid_to") if data.get("valid_to") else None
            rev = svc.create_revision(
                code=m.group(1),
                valid_from=valid_from,
                valid_to=valid_to,
                branch=data.get("branch", "main"),
                reason=data.get("reason", "初次建档"),
            )
            self._send(HTTPStatus.CREATED, {"revision": rev.to_dict(include_contents=False)})
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)/submit", path)
        if m:
            branch, version = self._params(qs)
            self._send(HTTPStatus.OK, svc.submit_for_signoff(m.group(1), branch, version))
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)/release", path)
        if m:
            branch, version = self._params(qs)
            signed_by = data.get("signed_by") or qs.get("signed_by", [""])[0]
            self._send(HTTPStatus.OK, svc.release(m.group(1), branch, version, signed_by))
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)/emergency-correction", path)
        if m:
            branch = qs.get("branch", ["main"])[0]
            try:
                version = int(qs["v"][0])
            except (KeyError, ValueError) as exc:
                raise BomError("BAD_INPUT", "缺少版本参数 v（整数）") from exc
            start = _parse_date(data.get("valid_from"), "valid_from") if data.get("valid_from") else None
            rev = svc.emergency_correction(
                m.group(1), version, start, branch, data.get("note", "")
            )
            self._send(HTTPStatus.CREATED, {"revision": rev.to_dict(include_contents=False)})
            return

        m = re.fullmatch(r"/api/products/([^/]+)/branches/([^/]+)", path)
        if m:
            try:
                version = int(qs["v"][0])
            except (KeyError, ValueError) as exc:
                raise BomError("BAD_INPUT", "缺少基线版本参数 v") from exc
            start = _parse_date(data.get("valid_from"), "valid_from") if data.get("valid_from") else None
            rev = svc.create_branch(m.group(1), version, m.group(2), start)
            self._send(HTTPStatus.CREATED, {"revision": rev.to_dict(include_contents=False)})
            return

        m = re.fullmatch(r"/api/branches/([^/]+)/([^/]+)/merge", path)
        if m:
            try:
                version = int(qs["v"][0])
            except (KeyError, ValueError) as exc:
                raise BomError("BAD_INPUT", "缺少分支版本参数 v") from exc
            valid_from = _parse_date(data.get("valid_from"), "valid_from")
            rev = svc.merge_branch(m.group(1), m.group(2), version, valid_from)
            self._send(HTTPStatus.CREATED, {"revision": rev.to_dict(include_contents=False)})
            return

        self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": path}})

    def _route_put(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        qs = self._qs()
        svc = self.service
        data = self._read_json()

        m = re.fullmatch(r"/api/revisions/([^/]+)/lines", path)
        if m:
            branch, version = self._params(qs)
            rev = svc.set_lines(m.group(1), branch, version, data.get("lines", []))
            self._send(HTTPStatus.OK, {"revision": rev.to_dict()})
            return

        m = re.fullmatch(r"/api/revisions/([^/]+)/substitutes/([^/]+)", path)
        if m:
            branch, version = self._params(qs)
            rev = svc.set_substitutes(
                m.group(1), branch, version, m.group(2), data.get("substitutes", [])
            )
            self._send(HTTPStatus.OK, {"revision": rev.to_dict()})
            return

        self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": path}})


def build_server(store: Store, host: str = "127.0.0.1", port: int = 8080,
                 verbose: bool = False) -> ThreadingHTTPServer:
    service = BomService(store)

    handler = type("Handler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="多层 BOM 发布服务")
    parser.add_argument("--db", default="bom.db", help="SQLite 路径，默认 bom.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    store = Store(args.db)
    server = build_server(store, args.host, args.port, args.verbose)
    print(f"BOM 服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
