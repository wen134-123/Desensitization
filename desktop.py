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
import base64
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import webview  # 顶层显式 import，PyInstaller 必须在 Analysis 阶段看到

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


class DesktopApi:
    """暴露给前端 JS 的 pywebview API：文件保存（系统原生对话框）。

    所有方法必须顶层引用 webview，PyInstaller 才能把 create_file_dialog 等符号带进去。
    """

    def __init__(self) -> None:
        self.port = 0

    def _pick_path(self, filename: str) -> str | None:
        """弹系统保存对话框，返回用户选定路径或 None（取消）。"""
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
            )
        except Exception as exc:
            print(f"[desktop] create_file_dialog 失败: {exc}", file=sys.stderr)
            return None
        if not paths:
            return None
        return paths[0]

    def save_file(self, data_b64: str, filename: str) -> str | None:
        path = self._pick_path(filename)
        if not path:
            return None
        try:
            Path(path).write_bytes(base64.b64decode(data_b64))
            return path
        except Exception as exc:
            print(f"[desktop] save_file 写入失败: {exc}", file=sys.stderr)
            return None

    def save_file_from_url(self, url: str, filename: str) -> str | None:
        path = self._pick_path(filename)
        if not path:
            return None
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            Path(path).write_bytes(data)
            return path
        except Exception as exc:
            print(f"[desktop] save_file_from_url 失败: {exc}", file=sys.stderr)
            return None


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

    status = licensing.get_status()
    if not status.valid:
        print(f"授权状态：{status.message}")

    api = DesktopApi()
    api.port = port

    webview.create_window(
        "标书脱敏工作台", url,
        width=1280, height=900, min_size=(1024, 720),
        js_api=api,
    )
    webview.start()


if __name__ == "__main__":
    main()
