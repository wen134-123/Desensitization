# -*- coding: utf-8 -*-
"""
授权服务器（只负责激活 / 续费 / 订单，不接收任何文档）

启动：
    LICENSE_PUBLIC_URL=http://127.0.0.1:8790 python -m uvicorn license_server.app:app --host 0.0.0.0 --port 8790

生产部署请放在 HTTPS 反向代理后，并把 LICENSE_PUBLIC_URL 设为公网地址。
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import store

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"

app = FastAPI(title="标书脱敏工作台 · 授权服务", docs_url=None, redoc_url=None)

RATE_LIMITS = {
    "/api/activate": (10, 60),
    "/api/heartbeat": (120, 60),
    "/api/orders": (10, 60),
}
_REQUEST_TIMES = defaultdict(deque)


@app.middleware("http")
async def _rate_limit(request, call_next):
    limit = RATE_LIMITS.get(request.url.path)
    if limit:
        limit_count, window = limit
        client = request.client.host if request.client else "unknown"
        key = (request.url.path, client)
        now = time.time()
        times = _REQUEST_TIMES[key]
        while times and times[0] <= now - window:
            times.popleft()
        if len(times) >= limit_count:
            return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
        times.append(now)
    return await call_next(request)


class ActivateRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    machine_code: str = Field(min_length=8, max_length=64)
    app_version: str = Field(default="", max_length=32)


class HeartbeatRequest(BaseModel):
    license_id: str = Field(min_length=8, max_length=64)
    machine_code: str = Field(min_length=8, max_length=64)
    app_version: str = Field(default="", max_length=32)


class OrderRequest(BaseModel):
    plan: str = Field(default="pro", max_length=16)
    email: str = Field(default="", max_length=128)
    days: int | None = Field(default=None, ge=1, le=3650)
    amount: int | None = Field(default=None, ge=0, le=10_000_000)


def _error(exc: store.StoreError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.get("/api/health")
def health():
    return {"ok": True, "service": "bid-redact-license", "public_url": store.PUBLIC_URL,
            "dev_payments": store.DEV_PAYMENTS}


@app.post("/api/activate")
def activate(req: ActivateRequest):
    try:
        result = store.activate(req.code, req.machine_code, req.app_version)
    except store.StoreError as exc:
        return _error(exc)
    return result


@app.post("/api/heartbeat")
def heartbeat(req: HeartbeatRequest):
    try:
        return store.heartbeat(req.license_id, req.machine_code, req.app_version)
    except store.StoreError as exc:
        return _error(exc)


@app.post("/api/orders")
def create_order(req: OrderRequest):
    try:
        return store.create_order(req.plan, None, None, req.email)
    except store.StoreError as exc:
        return _error(exc)


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    try:
        return store.order_status(order_id)
    except store.StoreError as exc:
        return _error(exc)


@app.post("/api/orders/{order_id}/mock-pay")
def mock_pay(order_id: str):
    """仅开发环境开启：模拟支付成功并返回激活码"""
    if not store.DEV_PAYMENTS:
        raise HTTPException(404, "未开启模拟支付")
    try:
        return store.fulfill_order(order_id, provider="mock")
    except store.StoreError as exc:
        return _error(exc)


@app.post("/api/payments/webhook/{provider}")
async def payment_webhook(provider: str, request: Request,
                          x_signature: str = Header(default="")):
    """
    通用支付回调骨架：使用 HMAC-SHA256(raw_body) 校验。
    真实接入时在 fulfill_order 前补充各支付渠道的订单号映射与金额校验。
    """
    body = await request.body()
    if not store.verify_webhook_signature(body, x_signature):
        raise HTTPException(401, "签名校验失败")
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(400, "回调内容不是合法 JSON") from exc
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        raise HTTPException(400, "缺少 order_id")
    try:
        return store.fulfill_order(
            order_id,
            provider=provider,
            amount=payload.get("amount"),
        )
    except store.StoreError as exc:
        return _error(exc)


if SITE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")
