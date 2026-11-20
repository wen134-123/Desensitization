# -*- coding: utf-8 -*-
"""授权服务器的数据与签发逻辑（SQLite + Ed25519）"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import sqlite3
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import licensing  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("LICENSE_DB", SERVER_DIR / "data" / "licenses.db"))
PRIVATE_KEY_PATH = Path(os.environ.get("LICENSE_PRIVATE_KEY", SERVER_DIR / "keys" / "private_key.pem"))
PUBLIC_KEY_PATH = Path(os.environ.get("LICENSE_PUBLIC_KEY", SERVER_DIR / "keys" / "public_key.pem"))
PUBLIC_URL = os.environ.get("LICENSE_PUBLIC_URL", "http://127.0.0.1:8790").rstrip("/")
ADMIN_TOKEN = os.environ.get("LICENSE_ADMIN_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
DEV_PAYMENTS = os.environ.get("LICENSE_DEV_PAYMENTS") == "1"

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^BRD-[A-Z0-9]{5}(?:-[A-Z0-9]{5}){3}$")

PLANS = {
    "pro": {"name": "专业版", "days": 365, "amount": 29900},
    "team": {"name": "团队版", "days": 365, "amount": 89900},
}


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS activation_keys (
            id TEXT PRIMARY KEY,
            code_hash TEXT UNIQUE NOT NULL,
            code_hint TEXT NOT NULL,
            plan TEXT NOT NULL,
            days INTEGER NOT NULL,
            customer TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unused',
            created_at TEXT NOT NULL,
            activated_at TEXT,
            machine_code TEXT,
            license_id TEXT,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            plan TEXT NOT NULL,
            customer TEXT DEFAULT '',
            machine_code TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            provider TEXT DEFAULT '',
            amount INTEGER DEFAULT 0,
            plan TEXT NOT NULL,
            days INTEGER NOT NULL,
            email TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            paid_at TEXT,
            key_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_keys_hash ON activation_keys(code_hash);
        CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(key_id);
        """)


# ---------------------------------------------------------------------
# 激活码
# ---------------------------------------------------------------------
def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _code_hash(code: str) -> str:
    return hashlib.sha256(f"{licensing.APP_ID}|{normalize_code(code)}".encode("utf-8")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(20))
    return "BRD-" + "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def create_keys(plan: str, days: int, count: int = 1,
                customer: str = "", note: str = "") -> list[str]:
    init_db()
    plan = (plan or "pro").lower()
    if plan not in PLANS:
        raise StoreError("未知套餐")
    if count < 1 or count > 500:
        raise StoreError("单次生成数量需在 1-500 之间")
    codes = []
    now = licensing.iso(licensing.utcnow())
    with _connect() as conn:
        for _ in range(count):
            code = _new_code()
            conn.execute(
                "INSERT INTO activation_keys (id, code_hash, code_hint, plan, days, customer,"
                " status, created_at, note) VALUES (?,?,?,?,?,?, 'unused', ?,?)",
                (uuid.uuid4().hex, _code_hash(code), code[-5:], plan, int(days),
                 customer, now, note))
            codes.append(code)
    return codes


def _load_private_key() -> bytes:
    if not PRIVATE_KEY_PATH.exists():
        raise StoreError("授权服务器私钥缺失，请先运行 init", 500)
    mode = stat.S_IMODE(PRIVATE_KEY_PATH.stat().st_mode)
    if mode & 0o077:
        raise StoreError("授权服务器私钥权限过宽，请设置为 0600", 500)
    return PRIVATE_KEY_PATH.read_bytes()


def _license_payload(row: sqlite3.Row, license_id: str) -> dict:
    return {
        "version": licensing.LICENSE_VERSION,
        "app": licensing.APP_ID,
        "license_id": license_id,
        "plan": row["plan"],
        "customer": row["customer"] or "",
        "machine": row["machine_code"],
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "server": PUBLIC_URL,
        "features": ["redact", "restore", "ner", "report"],
    }


def _sign_license_row(row: sqlite3.Row) -> dict:
    return licensing.sign_license(_license_payload(row, row["id"]), _load_private_key())


# ---------------------------------------------------------------------
# 激活 / 心跳
# ---------------------------------------------------------------------
def activate(code: str, machine_code: str, app_version: str = "") -> dict:
    init_db()
    if not CODE_RE.match((code or "").strip().upper()):
        raise StoreError("激活码格式不正确", 400)
    if not machine_code or len(machine_code) < 12:
        raise StoreError("机器码无效", 400)

    with _connect() as conn:
        key = conn.execute("SELECT * FROM activation_keys WHERE code_hash = ?",
                           (_code_hash(code),)).fetchone()
        if not key:
            raise StoreError("激活码不存在，请核对后重试", 404)
        if key["status"] == "revoked":
            raise StoreError("该激活码已被停用，请联系客服", 403)

        if key["status"] == "active":
            if key["machine_code"] != machine_code:
                raise StoreError("激活码已绑定其他电脑，请联系客服换机", 409)
            lic = conn.execute("SELECT * FROM licenses WHERE id = ?",
                               (key["license_id"],)).fetchone()
            if not lic:
                raise StoreError("许可证数据异常，请联系客服", 500)
            _touch_license(conn, lic["id"])
            return {"license": _sign_license_row(lic),
                    "license_id": lic["id"],
                    "expires_at": lic["expires_at"],
                    "plan": lic["plan"],
                    "reactivated": True}

        now = licensing.utcnow()
        expires = now + timedelta(days=int(key["days"]))
        license_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO licenses (id, key_id, plan, customer, machine_code, issued_at,"
            " expires_at, status, last_seen_at) VALUES (?,?,?,?,?,?,?, 'active', ?)",
            (license_id, key["id"], key["plan"], key["customer"], machine_code,
             licensing.iso(now), licensing.iso(expires), licensing.iso(now)))
        conn.execute(
            "UPDATE activation_keys SET status='active', activated_at=?, machine_code=?,"
            " license_id=? WHERE id=?",
            (licensing.iso(now), machine_code, license_id, key["id"]))
        lic = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
        return {"license": _sign_license_row(lic),
                "license_id": license_id,
                "expires_at": lic["expires_at"],
                "plan": lic["plan"],
                "reactivated": False}


def _touch_license(conn: sqlite3.Connection, license_id: str) -> None:
    conn.execute("UPDATE licenses SET last_seen_at = ? WHERE id = ?",
                 (licensing.iso(licensing.utcnow()), license_id))


def heartbeat(license_id: str, machine_code: str, app_version: str = "") -> dict:
    init_db()
    with _connect() as conn:
        lic = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
        if not lic:
            raise StoreError("许可证不存在", 404)
        if lic["status"] != "active":
            raise StoreError("许可证已被停用", 403)
        if lic["machine_code"] != machine_code:
            raise StoreError("机器码不匹配", 409)
        _touch_license(conn, license_id)
        return {"license": _sign_license_row(lic),
                "license_id": license_id,
                "expires_at": lic["expires_at"],
                "plan": lic["plan"]}


# ---------------------------------------------------------------------
# 订单 / 支付（骨架）
# ---------------------------------------------------------------------
def create_order(plan: str, days: Optional[int], amount: Optional[int],
                 email: str = "", provider: str = "") -> dict:
    init_db()
    plan = (plan or "pro").lower()
    if plan not in PLANS:
        raise StoreError("未知套餐")
    info = PLANS[plan]
    order_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            "INSERT INTO orders (id, provider, amount, plan, days, email, status, created_at)"
            " VALUES (?,?,?,?,?,?, 'pending', ?)",
            (order_id, provider, int(amount if amount is not None else info["amount"]),
             plan, int(days or info["days"]), email, licensing.iso(licensing.utcnow())))
    return {"order_id": order_id, "plan": plan, "amount": int(amount if amount is not None else info["amount"]),
            "days": int(days or info["days"]), "status": "pending"}


def fulfill_order(order_id: str, provider: str = "",
                  amount: int | float | None = None) -> dict:
    init_db()
    with _connect() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise StoreError("订单不存在", 404)
        if order["status"] == "paid":
            raise StoreError("订单已支付，激活码已发放", 409)
        if amount is not None and int(amount) != int(order["amount"]):
            raise StoreError("支付金额与订单金额不一致", 400)
        if order["provider"] and provider and order["provider"] != provider:
            raise StoreError("支付渠道与订单渠道不一致", 400)
    code = create_keys(order["plan"], order["days"], 1, note=f"order:{order_id}")[0]
    with _connect() as conn:
        key = conn.execute("SELECT id FROM activation_keys WHERE code_hash = ?",
                           (_code_hash(code),)).fetchone()
        conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, key_id=?, provider=?"
            " WHERE id=?",
            (licensing.iso(licensing.utcnow()), key["id"], provider or order["provider"], order_id))
    return {"order_id": order_id, "status": "paid", "activation_code": code,
            "plan": order["plan"], "days": order["days"]}


def order_status(order_id: str, reveal_code: bool = False) -> dict:
    init_db()
    with _connect() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise StoreError("订单不存在", 404)
        result = {"order_id": order_id, "status": order["status"], "plan": order["plan"],
                  "days": order["days"], "amount": order["amount"],
                  "created_at": order["created_at"], "paid_at": order["paid_at"]}
        if reveal_code and order["key_id"]:
            result["activation_code"] = "(已发放，出于安全不在查询中重复显示)"
        return result


# ---------------------------------------------------------------------
# 管理操作
# ---------------------------------------------------------------------
def list_keys(status: str = "") -> list[dict]:
    init_db()
    sql = "SELECT * FROM activation_keys"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{k: row[k] for k in row.keys()} for row in rows]


def list_licenses() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM licenses ORDER BY issued_at DESC").fetchall()
    return [{k: row[k] for k in row.keys()} for row in rows]


def revoke(code: str) -> None:
    init_db()
    with _connect() as conn:
        key = conn.execute("SELECT * FROM activation_keys WHERE code_hash = ?",
                           (_code_hash(code),)).fetchone()
        if not key:
            raise StoreError("激活码不存在", 404)
        conn.execute("UPDATE activation_keys SET status='revoked' WHERE id=?", (key["id"],))
        if key["license_id"]:
            conn.execute("UPDATE licenses SET status='revoked' WHERE id=?", (key["license_id"],))


def extend(license_id: str, days: int) -> str:
    init_db()
    with _connect() as conn:
        lic = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
        if not lic:
            raise StoreError("许可证不存在", 404)
        current = licensing.parse_time(lic["expires_at"])
        base = max(current, licensing.utcnow())
        new_expiry = base + timedelta(days=int(days))
        conn.execute("UPDATE licenses SET expires_at=? WHERE id=?",
                     (licensing.iso(new_expiry), license_id))
        return licensing.iso(new_expiry)


def reset_machine(code: str) -> None:
    """换机：解绑机器码并删除旧许可证，用户可用同一激活码重新激活"""
    init_db()
    with _connect() as conn:
        key = conn.execute("SELECT * FROM activation_keys WHERE code_hash = ?",
                           (_code_hash(code),)).fetchone()
        if not key:
            raise StoreError("激活码不存在", 404)
        if key["license_id"]:
            conn.execute("DELETE FROM licenses WHERE id=?", (key["license_id"],))
        conn.execute(
            "UPDATE activation_keys SET status='unused', machine_code=NULL,"
            " license_id=NULL, activated_at=NULL WHERE id=?", (key["id"],))


def stats() -> dict:
    init_db()
    with _connect() as conn:
        key_count = conn.execute("SELECT COUNT(*) FROM activation_keys").fetchone()[0]
        active_count = conn.execute("SELECT COUNT(*) FROM licenses WHERE status='active'").fetchone()[0]
        order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        paid_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
    return {"keys": key_count, "active_licenses": active_count,
            "orders": order_count, "paid_orders": paid_count}


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())
