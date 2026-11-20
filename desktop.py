# -*- coding: utf-8 -*-
"""
桌面壳：pywebview 内嵌本地网页

  - FastAPI 只监听 127.0.0.1 的随机端口，不对外开放
  - 文档处理全部在本机完成，授权服务器只负责激活/续费
  - 不做账号体系，激活码即身份

启动：
    python desktop.py
    python desktop.py --dev        # 开发模式，跳过激活
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import urllib.request

import uvicorn

import licensing
from webapp import APP_VERSION, app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base_url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=0.6) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="标书脱敏工作台（桌面版）")
    parser.add_argument("--dev", action="store_true", help="开发模式：跳过激活校验")
    parser.add_argument("--port", type=int, default=0, help="固定端口（默认随机）")
    args = parser.parse_args()
    if args.dev:
        os.environ["REDACT_DEV"] = "1"

    port = args.port or _free_port()
    threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": port, "log_level": "warning"},
        daemon=True,
    ).start()

    url = f"http://127.0.0.1:{port}"
    if not _wait_ready(url):
        raise SystemExit("本地服务启动失败，请检查依赖是否完整")

    threading.Thread(
        target=licensing.heartbeat_if_due,
        kwargs={"app_version": APP_VERSION},
        daemon=True,
    ).start()

    try:
        import webview
    except ImportError as exc:
        raise SystemExit("缺少 pywebview，请先执行：pip install pywebview") from exc

    status = licensing.get_status()
    if not status.valid:
        print(f"授权状态：{status.message}")

    webview.create_window(
        "标书脱敏工作台", url,
        width=1280, height=900, min_size=(1024, 720),
    )
    webview.start()


if __name__ == "__main__":
    main()
