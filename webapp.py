# -*- coding: utf-8 -*-
"""
标书脱敏工作台 · 本地 Web 服务

设计约束：
  - 只监听 127.0.0.1，不对外网开放
  - 不调用任何外部接口，文档与映射只落在本机 workspace/jobs
  - 静态资源全部本地，不使用 CDN

启动：
    python webapp.py
    python webapp.py --port 8765 --no-browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import licensing
import redactor

APP_VERSION = "1.0.0"
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_DIR = BASE_DIR / "web"
JOBS_DIR = (licensing.user_data_dir() / "workspace" / "jobs") if FROZEN else (BASE_DIR / "workspace" / "jobs")
MAX_UPLOAD = 50 * 1024 * 1024
JOB_TTL = 24 * 3600
ALLOWED_DOCS = {".docx", ".txt", ".md", ".markdown"}
ALLOWED_MAPS = {".json", ".enc", ".maskmap"}

app = FastAPI(title="标书脱敏工作台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ---------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------
def _cleanup_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    now = time.time()
    for child in JOBS_DIR.iterdir():
        try:
            if child.is_dir() and now - child.stat().st_mtime > JOB_TTL:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _new_job() -> tuple[str, Path]:
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    path = JOBS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return job_id, path


def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        raise HTTPException(404, "任务不存在")
    path = JOBS_DIR / job_id
    if not path.is_dir():
        raise HTTPException(404, "任务不存在或已过期")
    return path


def _manifest_path(job_id: str) -> Path:
    return _job_path(job_id) / "job.json"


def _load_manifest(job_id: str) -> dict:
    path = _manifest_path(job_id)
    if not path.exists():
        raise HTTPException(404, "任务不存在或已过期")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(job_id: str, data: dict) -> None:
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _manifest_path(job_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _require_license() -> None:
    """所有文档处理入口统一授权校验；源码开发模式自动放行"""
    status = licensing.get_status()
    if not status.valid:
        raise HTTPException(403, status.message or "软件未激活，请先输入激活码")


def _safe_name(name: str, default: str = "文档") -> str:
    name = Path(name or "").name
    name = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", name).strip("._")
    return name[:120] or default


def _safe_stem(name: str) -> str:
    stem = Path(_safe_name(name)).stem
    stem = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", stem).strip("._") or "文档"
    return stem[:80]


async def _save_upload(upload: UploadFile, dest: Path, allowed: set[str]) -> int:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, f"不支持的文件格式：{suffix or '未知'}")
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过 50MB 限制")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "文件为空")
    return size


def _file_info(job_id: str, kind: str, path: Path, display_name: str) -> dict:
    return {
        "kind": kind,
        "name": display_name,
        "size": path.stat().st_size if path.exists() else 0,
        "url": f"/api/download/{job_id}/{kind}",
    }


def _analysis_payload(job_id: str, manifest: dict, analysis: redactor.Analysis) -> dict:
    cats: dict[str, dict] = {}
    for finding in analysis.findings:
        cat = finding["category"]
        item = cats.setdefault(cat, {
            "key": cat,
            "cn": finding["category_cn"],
            "entities": 0,
            "occurrences": 0,
        })
        item["entities"] += 1
        item["occurrences"] += finding["count"]
    return {
        "job_id": job_id,
        "file": {
            "name": manifest["filename"],
            "size": manifest["size"],
            "kind": manifest["kind"],
        },
        "stats": analysis.stats,
        "occurrences": analysis.occurrences,
        "entities": len(analysis.findings),
        "findings": analysis.findings,
        "categories": sorted(cats.values(), key=lambda c: c["occurrences"], reverse=True),
        "smart": manifest.get("use_smart", False),
        "smart_available": redactor.smart_available(),
        "custom_words": manifest.get("custom_words", ""),
    }


# ---------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------
class RedactRequest(BaseModel):
    job_id: str
    mode: str = Field(default="placeholder")
    strategies: dict[str, str] = Field(default_factory=dict)
    excluded: list[str] = Field(default_factory=list)
    password: str = Field(default="", max_length=128)


class PreviewRequest(BaseModel):
    job_id: str
    mode: str = Field(default="placeholder")
    strategies: dict[str, str] = Field(default_factory=dict)
    excluded: list[str] = Field(default_factory=list)


class ActivateRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    server_url: str = Field(default="", max_length=200)


class ReanalyzeRequest(BaseModel):
    job_id: str
    use_smart: bool = True
    custom_words: str = Field(default="", max_length=100_000)


def _validate_license_server(server_url: str) -> None:
    if not server_url:
        return
    parsed = urlsplit(server_url)
    host = (parsed.hostname or "").lower()
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "授权服务器地址必须以 http:// 或 https:// 开头")
    if parsed.scheme != "https" and host not in local_hosts:
        raise HTTPException(400, "外部授权服务器必须使用 HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(400, "授权服务器地址不能包含凭据、查询参数或片段")


# ---------------------------------------------------------------------
# 页面与健康检查
# ---------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "smart": redactor.smart_available(),
        "ner": redactor.ner_info(),
        "max_mb": MAX_UPLOAD // 1024 // 1024,
        "local_only": True,
        "license": licensing.get_status().to_dict(),
    }


@app.get("/api/license/status")
async def license_status():
    return licensing.get_status().to_dict()


@app.post("/api/license/activate")
async def license_activate(req: ActivateRequest):
    _validate_license_server(req.server_url)
    ok, message = await run_in_threadpool(
        licensing.activate, req.code, req.server_url, app_version=APP_VERSION)
    if not ok:
        raise HTTPException(400, message)
    status = licensing.get_status()
    if not status.valid:
        raise HTTPException(400, status.message or "激活后校验失败")
    return status.to_dict()


@app.on_event("startup")
def _license_startup():
    _cleanup_jobs()
    def _cleanup_loop() -> None:
        while True:
            time.sleep(3600)
            _cleanup_jobs()

    threading.Thread(target=_cleanup_loop, daemon=True).start()
    threading.Thread(
        target=licensing.heartbeat_if_due,
        kwargs={"app_version": APP_VERSION},
        daemon=True,
    ).start()


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; img-src 'self' data:; connect-src 'self'"
    )
    return response


# ---------------------------------------------------------------------
# 分析：识别 + 供人工复核
# ---------------------------------------------------------------------
@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    use_smart: bool = Form(True),
    custom_words: str = Form(""),
):
    _require_license()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_DOCS:
        raise HTTPException(400, "当前支持 .docx / .txt / .md 文件")
    job_id, job_dir = _new_job()
    display_name = _safe_name(file.filename, f"文档{suffix}")
    src = job_dir / display_name
    size = await _save_upload(file, src, ALLOWED_DOCS)
    try:
        analysis = await run_in_threadpool(
            redactor.analyze_document,
            src,
            custom_words=custom_words,
            use_smart=use_smart,
        )
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"识别失败：{exc}") from exc

    manifest = {
        "id": job_id,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "filename": display_name,
        "kind": suffix.lstrip("."),
        "size": size,
        "use_smart": bool(use_smart and redactor.smart_available()),
        "custom_words": custom_words,
        "source": src.name,
        "outputs": {},
    }
    _save_manifest(job_id, manifest)
    return _analysis_payload(job_id, manifest, analysis)


@app.post("/api/reanalyze")
async def reanalyze(req: ReanalyzeRequest):
    _require_license()
    manifest = _load_manifest(req.job_id)
    job_dir = _job_path(req.job_id)
    src = job_dir / manifest["source"]
    if not src.exists():
        raise HTTPException(404, "源文件已丢失，请重新上传")
    use_smart = bool(req.use_smart and redactor.smart_available())
    try:
        analysis = await run_in_threadpool(
            redactor.analyze_document,
            src,
            custom_words=req.custom_words,
            use_smart=use_smart,
        )
    except Exception as exc:
        raise HTTPException(400, f"识别失败：{exc}") from exc

    manifest["use_smart"] = use_smart
    manifest["custom_words"] = req.custom_words
    manifest.pop("outputs", None)
    _save_manifest(req.job_id, manifest)
    return _analysis_payload(req.job_id, manifest, analysis)


# ---------------------------------------------------------------------
# 对比预览：按当前策略生成原文/脱敏文对照，不落盘
# ---------------------------------------------------------------------
@app.post("/api/preview")
async def preview(req: PreviewRequest):
    _require_license()
    manifest = _load_manifest(req.job_id)
    job_dir = _job_path(req.job_id)
    src = job_dir / manifest["source"]
    if not src.exists():
        raise HTTPException(404, "源文件已丢失，请重新上传")
    if req.mode not in {"placeholder", "realistic"}:
        raise HTTPException(400, "未知的替换方式")
    try:
        result = await run_in_threadpool(
            redactor.build_preview_document,
            src,
            mode=req.mode,
            strategies=req.strategies,
            excluded=req.excluded,
            custom_words=manifest.get("custom_words", ""),
            use_smart=manifest.get("use_smart", True),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["job_id"] = req.job_id
    return result


# ---------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------
@app.post("/api/redact")
async def redact(req: RedactRequest):
    _require_license()
    job_dir = _job_path(req.job_id)
    manifest = _load_manifest(req.job_id)
    src = job_dir / manifest["source"]
    if not src.exists():
        raise HTTPException(404, "源文件已丢失，请重新上传")
    if req.mode not in {"placeholder", "realistic"}:
        raise HTTPException(400, "未知的替换方式")

    out_dir = job_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = await run_in_threadpool(
            redactor.redact_document,
            src,
            out_dir,
            mode=req.mode,
            strategies=req.strategies,
            excluded=req.excluded,
            password=req.password or None,
            custom_words=manifest.get("custom_words", ""),
            use_smart=manifest.get("use_smart", True),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    outputs = result["outputs"]
    files = [
        _file_info(req.job_id, "redacted", outputs["redacted"], outputs["redacted"].name),
        _file_info(req.job_id, "mapping", outputs["mapping"], outputs["mapping"].name),
        _file_info(req.job_id, "report", outputs["report"], outputs["report"].name),
    ]
    manifest["outputs"] = {
        "redacted": outputs["redacted"].name,
        "mapping": outputs["mapping"].name,
        "report": outputs["report"].name,
        "mapping_encrypted": result["mapping_encrypted"],
    }
    _save_manifest(req.job_id, manifest)

    reversible = sum(1 for i in result["items"] if i["reversible"])
    export_record = {
        "job_id": req.job_id,
        "filename": manifest["filename"],
        "size": manifest["size"],
        "entities": len(result["items"]),
        "occurrences": result["occurrences"],
        "reversible_entities": reversible,
        "irreversible_entities": len(result["items"]) - reversible,
        "elapsed": result["elapsed"],
        "mapping_encrypted": result["mapping_encrypted"],
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "outputs": manifest.get("outputs", {}),
    }
    _add_export_history(export_record)

    return {
        "job_id": req.job_id,
        "stats": result["stats"],
        "entities": len(result["items"]),
        "reversible_entities": reversible,
        "irreversible_entities": len(result["items"]) - reversible,
        "occurrences": result["occurrences"],
        "warnings": result["warnings"],
        "elapsed": result["elapsed"],
        "mapping_encrypted": result["mapping_encrypted"],
        "files": files,
    }


# ---------------------------------------------------------------------
# 导出历史
# ---------------------------------------------------------------------
EXPORTS_FILE = JOBS_DIR / "export_history.json"


def _add_export_history(record: dict) -> None:
    history = []
    try:
        if EXPORTS_FILE.exists():
            history = json.loads(EXPORTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        history = []
    history = [h for h in history if h.get("job_id") != record["job_id"]]
    history.insert(0, record)
    history = history[:200]
    EXPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXPORTS_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_export_history() -> list[dict]:
    try:
        if EXPORTS_FILE.exists():
            return json.loads(EXPORTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return []


@app.get("/api/exports")
async def list_exports():
    history = _load_export_history()
    items = []
    for record in history:
        job_dir = JOBS_DIR / record["job_id"]
        outputs = record.get("outputs", {})
        redacted_name = outputs.get("redacted", "")
        mapping_name = outputs.get("mapping", "")
        report_name = outputs.get("report", "")
        files = []
        for kind, name in [("redacted", redacted_name), ("mapping", mapping_name), ("report", report_name)]:
            if name:
                path = job_dir / "output" / name
                files.append({
                    "kind": kind,
                    "name": name,
                    "size": path.stat().st_size if path.exists() else 0,
                    "url": f"/api/download/{record['job_id']}/{kind}" if path.exists() else "",
                    "exists": path.exists(),
                })
        items.append({
            "job_id": record["job_id"],
            "filename": record["filename"],
            "size": record["size"],
            "entities": record.get("entities", 0),
            "occurrences": record.get("occurrences", 0),
            "exported_at": record.get("exported_at", ""),
            "mapping_encrypted": record.get("mapping_encrypted", False),
            "files": files,
        })
    return {"exports": items}


@app.delete("/api/exports/{job_id}")
async def delete_export(job_id: str):
    history = _load_export_history()
    history = [h for h in history if h.get("job_id") != job_id]
    EXPORTS_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------
# 还原
# ---------------------------------------------------------------------
@app.post("/api/restore")
async def restore(
    redacted: UploadFile = File(...),
    mapping: UploadFile = File(...),
    password: str = Form(""),
):
    _require_license()
    suffix = Path(redacted.filename or "").suffix.lower()
    map_suffix = Path(mapping.filename or "").suffix.lower()
    if suffix not in ALLOWED_DOCS:
        raise HTTPException(400, "请上传 .docx / .txt / .md 脱敏文件")
    if map_suffix not in ALLOWED_MAPS:
        raise HTTPException(400, "请上传 .enc / .json 映射文件")
    if len(password) > 128:
        raise HTTPException(400, "口令过长")

    job_id, job_dir = _new_job()
    redacted_name = _safe_name(redacted.filename, f"脱敏文件{suffix}")
    src = job_dir / redacted_name
    map_path = job_dir / f"mapping{map_suffix}"
    redacted_size = await _save_upload(redacted, src, ALLOWED_DOCS)
    await _save_upload(mapping, map_path, ALLOWED_MAPS)

    out_name = f"{_safe_stem(redacted.filename)}_还原{suffix}"
    out_path = job_dir / "output" / out_name
    try:
        result = await run_in_threadpool(
            redactor.restore_document, src, map_path, out_path, password or None)
    except ValueError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"还原失败：{exc}") from exc

    manifest = {
        "id": job_id,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "filename": redacted_name,
        "kind": "restore",
        "size": redacted_size,
        "source": src.name,
        "restored": out_path.name,
    }
    _save_manifest(job_id, manifest)
    return {
        "job_id": job_id,
        "replaced": result["replaced"],
        "file": _file_info(job_id, "restored", out_path, out_name),
    }


# ---------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------
@app.get("/api/download/{job_id}/{kind}")
async def download(job_id: str, kind: str):
    job_dir = _job_path(job_id)
    manifest = _load_manifest(job_id)
    if kind == "redacted":
        name = manifest.get("outputs", {}).get("redacted")
        base = job_dir / "output"
    elif kind == "mapping":
        name = manifest.get("outputs", {}).get("mapping")
        base = job_dir / "output"
    elif kind == "report":
        name = manifest.get("outputs", {}).get("report")
        base = job_dir / "output"
    elif kind == "restored":
        name = manifest.get("restored")
        base = job_dir / "output"
    else:
        raise HTTPException(404, "文件不存在")
    if not name:
        raise HTTPException(404, "文件尚未生成")
    path = (base / name).resolve()
    if job_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="标书脱敏工作台（本地 Web）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--dev", action="store_true", help="开发模式：跳过激活校验")
    args = parser.parse_args()
    if args.dev:
        os.environ["REDACT_DEV"] = "1"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    status = licensing.get_status()
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n标书脱敏工作台已启动：{url}")
    print(f"授权状态：{'开发模式（跳过激活）' if status.dev else ('已激活 · ' + status.message if status.activated else status.message)}")
    print("仅监听本机 127.0.0.1，文档不会离开这台电脑。按 Ctrl+C 退出。\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
