# -*- coding: utf-8 -*-
"""授权服务器管理 CLI（不接触任何文档）

示例：
    python -m license_server.admin init
    python -m license_server.admin create --plan pro --days 365 --count 10 --customer 某公司
    python -m license_server.admin list
    python -m license_server.admin extend --license <license_id> --days 365
    python -m license_server.admin revoke --code BRD-XXXXX-XXXXX-XXXXX-XXXXX
    python -m license_server.admin reset-machine --code BRD-...
    python -m license_server.admin order --plan pro --email a@b.com
    python -m license_server.admin fulfill --order <order_id>
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from license_server import store  # noqa: E402


def _ensure_keys() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    store.PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not store.PRIVATE_KEY_PATH.exists():
        private = ed25519.Ed25519PrivateKey.generate()
        store.PRIVATE_KEY_PATH.write_bytes(private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)
        store.PUBLIC_KEY_PATH.write_bytes(public_pem)
        print(f"已生成签名密钥：{store.PRIVATE_KEY_PATH}")
    elif not store.PUBLIC_KEY_PATH.exists():
        from cryptography.hazmat.primitives import serialization
        private = serialization.load_pem_private_key(
            store.PRIVATE_KEY_PATH.read_bytes(), password=None)
        store.PUBLIC_KEY_PATH.write_bytes(private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))
        print(f"已补全公钥：{store.PUBLIC_KEY_PATH}")

    client_public = ROOT / "license_public_key.pem"
    if store.PUBLIC_KEY_PATH.read_bytes() != (client_public.read_bytes() if client_public.exists() else b""):
        shutil.copyfile(store.PUBLIC_KEY_PATH, client_public)
        print(f"已同步客户端公钥：{client_public}")


def main() -> int:
    parser = argparse.ArgumentParser(description="标书脱敏工作台授权管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库与签名密钥")
    sub.add_parser("stats", help="查看授权统计")
    sub.add_parser("list", help="列出激活码").add_argument("--status", default="")

    create = sub.add_parser("create", help="批量生成激活码")
    create.add_argument("--plan", default="pro", choices=sorted(store.PLANS))
    create.add_argument("--days", type=int, default=365)
    create.add_argument("--count", type=int, default=1)
    create.add_argument("--customer", default="")
    create.add_argument("--note", default="")

    revoke = sub.add_parser("revoke", help="停用激活码")
    revoke.add_argument("--code", required=True)

    extend = sub.add_parser("extend", help="给许可证续期")
    extend.add_argument("--license", required=True, dest="license_id")
    extend.add_argument("--days", type=int, default=365)

    reset = sub.add_parser("reset-machine", help="换机：解绑机器码")
    reset.add_argument("--code", required=True)

    order = sub.add_parser("order", help="创建订单")
    order.add_argument("--plan", default="pro", choices=sorted(store.PLANS))
    order.add_argument("--email", default="")
    order.add_argument("--days", type=int, default=None)
    order.add_argument("--amount", type=int, default=None)

    fulfill = sub.add_parser("fulfill", help="标记订单已支付并发放激活码")
    fulfill.add_argument("--order", required=True, dest="order_id")

    args = parser.parse_args()

    if args.cmd == "init":
        _ensure_keys()
        store.init_db()
        print(f"数据库：{store.DB_PATH}")
        print("授权服务已就绪。")
        return 0

    _ensure_keys()
    store.init_db()

    try:
        if args.cmd == "stats":
            data = store.stats()
            for key, value in data.items():
                print(f"{key}: {value}")
        elif args.cmd == "list":
            for row in store.list_keys(args.status):
                print(f"{row['code_hint']}  {row['plan']:<6} {row['status']:<8} "
                      f"{row['created_at']}  machine={row['machine_code'] or '-'}  "
                      f"license={row['license_id'] or '-'}")
        elif args.cmd == "create":
            codes = store.create_keys(args.plan, args.days, args.count,
                                      customer=args.customer, note=args.note)
            print(f"已生成 {len(codes)} 个激活码（{args.plan} / {args.days} 天）：")
            for code in codes:
                print(code)
        elif args.cmd == "revoke":
            store.revoke(args.code)
            print("已停用。")
        elif args.cmd == "extend":
            expiry = store.extend(args.license_id, args.days)
            print(f"新到期时间：{expiry}")
        elif args.cmd == "reset-machine":
            store.reset_machine(args.code)
            print("已解绑，用户可用同一激活码在新电脑重新激活。")
        elif args.cmd == "order":
            data = store.create_order(args.plan, args.days, args.amount, args.email)
            print(f"订单：{data['order_id']}  金额：{data['amount'] / 100:.2f} 元")
        elif args.cmd == "fulfill":
            data = store.fulfill_order(args.order_id, provider="manual")
            print(f"订单已支付，激活码：{data['activation_code']}")
    except store.StoreError as exc:
        print(f"错误：{exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
