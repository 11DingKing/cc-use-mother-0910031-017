"""免安装启动入口：python3 tools/serve.py --port 8080 [--data store.json]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bom_release.api import make_server  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="多层 BOM 发布后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None, help="JSON 持久化文件（可选）")
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, args.data)
    print(f"多层 BOM 发布服务已启动：http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
