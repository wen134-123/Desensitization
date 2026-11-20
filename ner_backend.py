# -*- coding: utf-8 -*-
"""
可插拔本地 NER 后端

  - onnx : onnxruntime + tokenizers，模型随安装包内置，CPU 可跑
  - jieba: 内置词性标注兜底

模型目录约定（models/ner/）：
    model.onnx      导出的 NER 模型
    tokenizer.json  HuggingFace fast tokenizer（含 offset 映射）
    labels.json     标签顺序，如 ["O", "B-COMPANY", "I-COMPANY", ...]

模型查找顺序：
    1. 用户目录（首次从安装包解压后永久离线使用）
    2. 安装包内置资源（PyInstaller _MEIPASS 或源码目录）

环境变量：
    REDACT_NER_BACKEND=onnx|jieba|auto   强制指定后端（默认 auto）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

MODEL_SUBDIR = Path("models") / "ner"
MODEL_FILES = ("model.onnx", "tokenizer.json", "labels.json")
MAX_TOKENS = 510
CHUNK_CHARS = 320
LOGGER = logging.getLogger(__name__)

# 通用标签映射：同时兼容 MSRA（NR/NT/NS）、BIO（PER/ORG/LOC）与 HanLP（PERSON/ORGANIZATION/LOCATION）
LABEL_MAP = {
    "PER": "PERSON", "PERSON": "PERSON", "NR": "PERSON",
    "ORG": "COMPANY", "COMPANY": "COMPANY", "NT": "COMPANY",
    "ORGANIZATION": "COMPANY",
    "LOC": "ADDRESS", "ADDRESS": "ADDRESS", "NS": "ADDRESS",
    "LOCATION": "ADDRESS",
}


# =====================================================================
# 路径
# =====================================================================
def _resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _bundled_model_dir() -> Path:
    return _resource_dir() / MODEL_SUBDIR


def _user_model_dir() -> Path:
    try:
        from licensing import user_data_dir

        return user_data_dir() / MODEL_SUBDIR
    except Exception:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "BidRedact"
        elif os.name == "nt":
            base = Path(os.environ.get("APPDATA", Path.home())) / "BidRedact"
        else:
            base = Path.home() / ".local" / "share" / "bidredact"
        return base / MODEL_SUBDIR


def _has_model(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in MODEL_FILES)


def ensure_model() -> Optional[Path]:
    """返回可用的模型目录；内置模型首次使用时复制到用户目录，之后永久离线。"""
    user_dir = _user_model_dir()
    if _has_model(user_dir):
        return user_dir
    bundled = _bundled_model_dir()
    if not _has_model(bundled):
        return None
    try:
        user_dir.parent.mkdir(parents=True, exist_ok=True)
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
        shutil.copytree(bundled, user_dir)
        return user_dir
    except OSError:
        return bundled


# =====================================================================
# 后端实现
# =====================================================================
class JiebaNer:
    name = "jieba"

    @staticmethod
    def available() -> bool:
        import importlib.util

        return importlib.util.find_spec("jieba") is not None

    def detect(self, text: str) -> list[dict]:
        try:
            import jieba
            import jieba.posseg as pseg
        except Exception:
            return []
        jieba.setLogLevel(20)
        hits: list[dict] = []
        cursor = 0
        for word, flag in pseg.cut(text):
            idx = text.find(word, cursor)
            if idx < 0:
                continue
            cursor = idx + len(word)
            if flag == "nr":
                hits.append({"start": idx, "end": cursor, "category": "PERSON", "text": word})
            elif flag == "nt":
                hits.append({"start": idx, "end": cursor, "category": "COMPANY", "text": word})
            elif flag == "ns":
                hits.append({"start": idx, "end": cursor, "category": "ADDRESS", "text": word})
        return hits


class OnnxNer:
    name = "onnx"

    def __init__(self, model_dir: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self.model_dir = model_dir
        self.session = ort.InferenceSession(
            str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.labels = json.loads((model_dir / "labels.json").read_text(encoding="utf-8"))

    @staticmethod
    def available() -> bool:
        import importlib.util

        if importlib.util.find_spec("onnxruntime") is None:
            return False
        if importlib.util.find_spec("tokenizers") is None:
            return False
        return _has_model(_user_model_dir()) or _has_model(_bundled_model_dir())

    def detect(self, text: str) -> list[dict]:
        hits: list[dict] = []
        for chunk, base in _chunks(text):
            hits.extend(self._detect_chunk(chunk, base))
        return hits

    def _detect_chunk(self, chunk: str, base: int) -> list[dict]:
        np = self._np
        encoding = self.tokenizer.encode(chunk)
        ids = list(encoding.ids)
        if not ids:
            return []
        offsets = list(encoding.offsets)
        feeds = {"input_ids": np.array([ids], dtype=np.int64)}
        if "attention_mask" in self.input_names:
            mask = list(getattr(encoding, "attention_mask", [1] * len(ids)))
            feeds["attention_mask"] = np.array([mask], dtype=np.int64)
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros((1, len(ids)), dtype=np.int64)

        outputs = self.session.run(None, feeds)
        logits = outputs[0]
        if logits.ndim == 3:
            logits = logits[0]

        # 模型可能在内部去掉了 [CLS]/[SEP]，导致 logits 比 token 少 2 个
        if logits.shape[0] == len(ids) - 2:
            offsets = offsets[1:-1]
        elif logits.shape[0] != len(ids):
            return []

        gold = [self.labels[int(idx)] if 0 <= int(idx) < len(self.labels) else "O"
                for idx in logits.argmax(axis=-1)]
        return _decode(gold, offsets, chunk, base)


class _Unavailable:
    name = "none"

    def detect(self, _text: str) -> list[dict]:
        return []


# =====================================================================
# 内部工具
# =====================================================================
def _chunks(text: str) -> list[tuple[str, int]]:
    """按句子边界切块，避免长段落被 tokenizer 截断丢失实体。"""
    parts: list[tuple[str, int]] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + CHUNK_CHARS, length)
        if end < length:
            cut = max(text.rfind(p, start, end) for p in "。！？；\n")
            if cut > start + CHUNK_CHARS // 3:
                end = cut + 1
        parts.append((text[start:end], start))
        start = end
    return parts or [(text, 0)]


def _decode(labels: list[str], offsets: list[tuple[int, int]],
            chunk: str, base: int) -> list[dict]:
    """解码实体标签，同时兼容 BIO 和 BMESO 两种标注体系。

    BIO:   B-X I-X I-X → 一个实体
    BMESO: B-X M-X E-X → 一个实体；S-X → 单字实体
    """
    hits: list[dict] = []
    current: Optional[dict] = None

    def close() -> None:
        nonlocal current
        if current and current["end"] > current["start"]:
            current["text"] = chunk[current["start"] - base:current["end"] - base]
            hits.append(current)
        current = None

    for label, (start, end) in zip(labels, offsets):
        if end <= start:
            continue
        prefix, _, kind = label.partition("-")
        category = LABEL_MAP.get(kind.upper() or prefix.upper())
        tag = prefix.upper()

        if category and tag in ("B", "S"):
            close()
            current = {"start": start + base, "end": end + base, "category": category}
            if tag == "S":
                close()
        elif category and tag in ("I", "M") and current and current["category"] == category:
            # 中间部分，延续实体
            current["end"] = end + base
        elif category and tag == "E" and current and current["category"] == category:
            # 结束部分，延续后关闭
            current["end"] = end + base
            close()
        elif category and tag in ("I", "M", "E") and not current:
            # 没有开头但出现了中间/结束标记，容错处理
            current = {"start": start + base, "end": end + base, "category": category}
            if tag == "E":
                close()
        else:
            close()
    close()
    return _merge_adjacent(hits)


def _merge_adjacent(hits: list[dict]) -> list[dict]:
    """合并直接相邻的同类型实体。

    NER 模型有时会把一个长实体拆成多段（如公司名尾部被拆开），
    这里把首尾相接、类别相同的实体合并。
    """
    if not hits:
        return hits
    merged = [hits[0]]
    for hit in hits[1:]:
        prev = merged[-1]
        if prev["category"] == hit["category"] and prev["end"] == hit["start"]:
            prev["end"] = hit["end"]
            prev["text"] = prev.get("text", "") + hit.get("text", "")
        else:
            merged.append(hit)
    return merged


# =====================================================================
# 对外接口
# =====================================================================
_BACKEND = None
_BACKEND_LOADED = False


def _preferred() -> str:
    return (os.environ.get("REDACT_NER_BACKEND") or "auto").strip().lower()


def _load_backend():
    global _BACKEND, _BACKEND_LOADED
    if _BACKEND_LOADED:
        return _BACKEND
    _BACKEND_LOADED = True
    preferred = _preferred()

    if preferred != "jieba" and OnnxNer.available():
        model_dir = ensure_model()
        if model_dir:
            try:
                _BACKEND = OnnxNer(model_dir)
                return _BACKEND
            except Exception:
                _BACKEND = None
    if preferred != "onnx" and JiebaNer.available():
        _BACKEND = JiebaNer()
    else:
        _BACKEND = _Unavailable()
    return _BACKEND


def available() -> bool:
    return _load_backend().name != "none"


def active_name() -> str:
    return _load_backend().name


def info() -> dict:
    backends: list[str] = []
    if OnnxNer.available():
        backends.append("onnx")
    if JiebaNer.available():
        backends.append("jieba")
    model_dir = ensure_model()
    return {
        "active": active_name(),
        "available": backends,
        "model": str(model_dir) if model_dir else "",
    }


def detect(text: str) -> list[dict]:
    backend = _load_backend()
    if backend.name == "none" or not text:
        return []
    try:
        return backend.detect(text)
    except Exception:
        LOGGER.exception("本地智能识别失败，本次已降级为仅规则结果")
        return []
