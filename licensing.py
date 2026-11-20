# -*- coding: utf-8 -*-
"""
本地授权核心（离线签名许可证）

设计原则：
  - 文档永不离开本机；授权服务器只负责激活 / 续费，不接触任何文档
  - 许可证使用 Ed25519 签名，客户端只内置公钥，可完全离线校验
  - 激活码即身份，不做用户注册登录
  - 许可证到期后保留 30 天宽限期；期间每 7 天尝试一次心跳
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    HAS_CRYPTO = False
    InvalidSignature = Exception  # type: ignore


APP_ID = "bid-redact"
APP_NAME = "标书脱敏工作台"
LICENSE_VERSION = 1
GRACE_DAYS = 30
HEARTBEAT_INTERVAL = 7 * 86400
DEFAULT_SERVER = os.environ.get("REDACT_LICENSE_SERVER", "").strip()


# =====================================================================
# 路径
# =====================================================================
def resource_path(name: str) -> Path:
    """源码运行与 PyInstaller 打包后都能找到随包资源"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def user_data_dir() -> Path:
    override = os.environ.get("REDACT_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "BidRedact"
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA", Path.home())) / "BidRedact"
    else:
        path = Path.home() / ".local" / "share" / "bidredact"
    path.mkdir(parents=True, exist_ok=True)
    return path


def license_file() -> Path:
    return user_data_dir() / "license.json"


def state_file() -> Path:
    return user_data_dir() / "license_state.json"


def find_public_key() -> Optional[Path]:
    for candidate in (resource_path("license_public_key.pem"), resource_path("keys/public_key.pem")):
        if candidate.exists():
            return candidate
    return None


def find_server_url(license_payload: Optional[dict] = None) -> str:
    if DEFAULT_SERVER:
        return DEFAULT_SERVER.rstrip("/")
    if license_payload and license_payload.get("server"):
        return str(license_payload["server"]).rstrip("/")
    config = resource_path("license_config.json")
    if config.exists():
        try:
            return str(json.loads(config.read_text(encoding="utf-8")).get("server_url", "")).rstrip("/")
        except (OSError, ValueError):
            return ""
    return ""


# =====================================================================
# 时间 / 机器码
# =====================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _raw_machine_id() -> str:
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                text=True, timeout=4, stderr=subprocess.DEVNULL)
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if match:
                return match.group(1)
        elif os.name == "nt":
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.strip():
                return out.strip()
        else:
            machine_id = Path("/etc/machine-id")
            if machine_id.exists():
                return machine_id.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return f"{platform.node()}|{uuid.getnode()}"


_MACHINE_CODE: Optional[str] = None


def machine_code() -> str:
    global _MACHINE_CODE
    if _MACHINE_CODE:
        return _MACHINE_CODE
    digest = hashlib.sha256(f"{APP_ID}|{_raw_machine_id()}".encode("utf-8")).hexdigest()[:20].upper()
    _MACHINE_CODE = "-".join(digest[i:i + 4] for i in range(0, 20, 4))
    return _MACHINE_CODE


# =====================================================================
# 许可证签名 / 校验
# =====================================================================
def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_license(payload: dict, private_key_pem: bytes) -> dict:
    if not HAS_CRYPTO:
        raise RuntimeError("缺少 cryptography，无法签发许可证")
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(canonical_bytes(payload))
    return {"payload": payload, "signature": base64.b64encode(signature).decode("ascii")}


def verify_license(data: dict, machine: Optional[str] = None) -> tuple[bool, str, Optional[dict]]:
    """只校验签名与机器绑定，不校验有效期。返回 (ok, message, payload)"""
    if not HAS_CRYPTO:
        return False, "缺少 cryptography，无法校验许可证", None
    if not isinstance(data, dict) or "payload" not in data or "signature" not in data:
        return False, "许可证格式不正确", None
    pub_path = find_public_key()
    if not pub_path:
        return False, "授权公钥缺失，请重新安装软件", None
    payload = data["payload"]
    if not isinstance(payload, dict):
        return False, "许可证内容损坏", None
    try:
        public_key: Ed25519PublicKey = serialization.load_pem_public_key(pub_path.read_bytes())
        signature = base64.b64decode(data["signature"])
        public_key.verify(signature, canonical_bytes(payload))
    except InvalidSignature:
        return False, "许可证签名无效，可能已被篡改", None
    except Exception:
        return False, "许可证无法解析", None

    if payload.get("app") != APP_ID:
        return False, "许可证不属于本软件", None
    if payload.get("version") != LICENSE_VERSION:
        return False, "许可证版本不兼容，请升级软件", None
    bound = str(payload.get("machine", ""))
    if machine and bound != machine:
        return False, "许可证已绑定其他电脑，请在本机重新激活", None
    return True, "", payload


# =====================================================================
# 状态
# =====================================================================
@dataclass
class LicenseStatus:
    activated: bool
    valid: bool
    dev: bool
    plan: str = ""
    customer: str = ""
    license_id: str = ""
    expires_at: str = ""
    days_left: int = 0
    in_grace: bool = False
    grace_days_left: int = 0
    machine_code: str = ""
    server_url: str = ""
    message: str = ""
    backends: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def dev_mode() -> bool:
    """源码运行默认开发模式；PyInstaller 打包后强制启用授权"""
    forced = os.environ.get("REDACT_DEV")
    if forced == "1":
        return True
    if forced == "0":
        return False
    return not getattr(sys, "frozen", False)


def read_license() -> Optional[dict]:
    path = license_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_license(data: dict) -> None:
    path = license_file()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_state() -> dict:
    path = state_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    path = state_file()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_status() -> LicenseStatus:
    machine = machine_code()
    status = LicenseStatus(
        activated=False, valid=False, dev=dev_mode(),
        machine_code=machine,
        backends=_backend_info(),
    )
    data = read_license()
    if not data:
        status.message = "软件尚未激活，请输入激活码"
        status.valid = status.dev
        return status

    ok, message, payload = verify_license(data, machine)
    if not ok:
        status.message = message
        status.valid = status.dev
        return status

    status.activated = True
    status.plan = str(payload.get("plan", ""))
    status.customer = str(payload.get("customer", ""))
    status.license_id = str(payload.get("license_id", ""))
    status.expires_at = str(payload.get("expires_at", ""))
    status.server_url = find_server_url(payload)

    try:
        expires = parse_time(status.expires_at)
    except ValueError:
        status.message = "许可证到期时间无效"
        status.valid = status.dev
        return status

    now = utcnow()
    if now <= expires:
        status.valid = True
        status.days_left = max(0, (expires - now).days)
        status.message = f"订阅有效，剩余 {status.days_left} 天"
        return status

    grace_end = expires + timedelta(days=GRACE_DAYS)
    if now <= grace_end:
        status.valid = True
        status.in_grace = True
        status.days_left = 0
        status.grace_days_left = max(0, (grace_end - now).days)
        status.message = f"订阅已到期，宽限期剩余 {status.grace_days_left} 天，请尽快续费"
        return status

    status.valid = status.dev
    status.days_left = (expires - now).days
    status.message = "订阅已过期，请续费后再使用"
    return status


def _backend_info() -> dict:
    try:
        import ner_backend

        return ner_backend.info()
    except Exception:
        return {"active": "unknown", "available": []}


# =====================================================================
# HTTP
# =====================================================================
def _post_json(url: str, body: dict, timeout: int = 8) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"detail": f"授权服务器返回 {exc.code}"}
        return exc.code, detail
    except Exception as exc:
        return 0, {"detail": f"无法连接授权服务器：{exc}"}


def activate(code: str, server_url: str = "", app_version: str = "") -> tuple[bool, str]:
    code = (code or "").strip().upper()
    if not code:
        return False, "请输入激活码"
    server = (server_url or find_server_url()).rstrip("/")
    if not server:
        return False, "未配置授权服务器地址，请检查安装包或联系客服"
    status_code, data = _post_json(f"{server}/api/activate", {
        "code": code,
        "machine_code": machine_code(),
        "app_version": app_version,
    })
    if status_code != 200:
        return False, str(data.get("detail") or "激活失败，请检查激活码")
    license_data = data.get("license")
    ok, message, _payload = verify_license(license_data, machine_code())
    if not ok:
        return False, f"服务器返回的许可证无效：{message}"
    write_license(license_data)
    _write_state({"last_heartbeat": iso(utcnow()), "last_error": ""})
    return True, "激活成功"


def heartbeat_if_due(app_version: str = "") -> Optional[LicenseStatus]:
    """每 7 天尝试一次心跳；失败不影响离线使用"""
    status = get_status()
    if not status.activated:
        return None
    state = _read_state()
    last = state.get("last_heartbeat")
    if last:
        try:
            if (utcnow() - parse_time(last)).total_seconds() < HEARTBEAT_INTERVAL:
                return None
        except ValueError:
            pass
    server = status.server_url or find_server_url()
    if not server:
        return None

    status_code, data = _post_json(f"{server}/api/heartbeat", {
        "license_id": status.license_id,
        "machine_code": status.machine_code,
        "app_version": app_version,
    }, timeout=5)
    if status_code == 200 and data.get("license"):
        license_data = data["license"]
        ok, message, _payload = verify_license(license_data, status.machine_code)
        if ok:
            write_license(license_data)
            state["last_heartbeat"] = iso(utcnow())
            state["last_error"] = ""
            _write_state(state)
            return get_status()
        state["last_error"] = message
    else:
        state["last_error"] = str(data.get("detail") or "心跳失败")
    state["last_heartbeat"] = iso(utcnow())
    _write_state(state)
    return None
