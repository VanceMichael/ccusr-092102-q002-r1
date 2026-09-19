"""服务启动入口。

用法::

    python3 -m gold_attribution                 # 监听 0.0.0.0:8080
    GOLD_LEDGER_DB=/data/ledger.db python3 -m gold_attribution --port 8080

健康检查：``GET /health``；业务接口前缀 ``/api/``（需携带 X-Actor / X-Team 头）。
"""

from __future__ import annotations

import argparse
import os

from .http_app import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="黄金归因账本服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--db",
        default=os.environ.get("GOLD_LEDGER_DB", "gold_ledger.db"),
        help="SQLite 文件路径（多线程服务不能使用 :memory:）",
    )
    args = parser.parse_args()
    server = build_server(args.db, host=args.host, port=args.port)
    print(f"黄金归因账本已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
