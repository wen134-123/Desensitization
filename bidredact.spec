# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS 首发）

用法：
    pyinstaller --noconfirm --clean bidredact.spec

随包资源：
    web/                  内嵌网页
    license_public_key.pem 许可证验签公钥（不含私钥）
    vocab.txt             内置词库
    models/ner/*          ONNX NER 模型（存在才打包，首次启动解压到用户目录）
    jieba 词典            兜底 NER
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "license_public_key.pem"), "."),
    (str(ROOT / "vocab.txt"), "."),
]

ner_dir = ROOT / "models" / "ner"
if ner_dir.is_dir():
    datas.append((str(ner_dir), "models/ner"))

datas += collect_data_files("jieba")
datas += collect_data_files("certifi")

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "fastapi",
    "multipart",
    "multipart.multipart",
    "docx",
    "cryptography",
    "onnxruntime",
    "tokenizers",
    "webview",
    "webview.platforms.cocoa",
]

a = Analysis(
    ["desktop.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BidRedact",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BidRedact",
)

app = BUNDLE(
    coll,
    name="BidRedact.app",
    icon="icon.icns",
    bundle_identifier="com.bidredact.desktop",
    info_plist={
        "CFBundleName": "BidRedact",
        "CFBundleDisplayName": "标书脱敏工作台",
        "CFBundleIdentifier": "com.bidredact.desktop",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "CFBundleIconFile": "icon.icns",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
    },
)
