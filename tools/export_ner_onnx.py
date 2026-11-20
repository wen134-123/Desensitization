# -*- coding: utf-8 -*-
"""
把 HanLP NER 模型（如 MSRA_NER_ELECTRA_SMALL_ZH）导出为 ONNX，供 ner_backend 使用。

HanLP 模型目录通常包含：config.json、model.pt、vocabs.json，
例如从 https://file.hankcs.com/hanlp/ner/msra_ner_electra_small_20220215_205503.zip 解压得到。

仅导出时需要 torch + hanlp + transformers + onnx + onnxscript；
安装包与运行时只需要 onnxruntime + tokenizers。

用法：
    pip install torch hanlp transformers onnx onnxscript
    python tools/export_ner_onnx.py \
        --model /path/to/msra_ner_electra_small_20220215_205503 \
        --out models/ner

导出结果：
    model.onnx      单文件模型（外部权重已合并）
    tokenizer.json  fast tokenizer（已关闭 padding，含 offset）
    labels.json     BMESO 标签顺序
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 HanLP NER 模型为 ONNX")
    parser.add_argument("--model", required=True, help="HanLP 模型目录")
    parser.add_argument("--out", default="models/ner", help="输出目录（默认 models/ner）")
    parser.add_argument("--opset", type=int, default=18)
    parser.add_argument("--max-len", type=int, default=510, help="分词器最大长度")
    args = parser.parse_args()

    import numpy as np
    import onnx
    import torch
    import torch.nn as nn
    import hanlp

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ner = hanlp.load(args.model)
    base = ner.model
    base.eval()

    class NerWrapper(nn.Module):
        """标准 BERT 输入 → 实体 logits。

        内部流程：ELECTRA 编码 → 去掉 [CLS]/[SEP] → RelativeTransformer → 分类头。
        """

        def __init__(self, model):
            super().__init__()
            self.electra = model.encoder.transformer
            self.secondary_encoder = model.secondary_encoder
            self.classifier = model.classifier

        def forward(self, input_ids, attention_mask, token_type_ids):
            outputs = self.electra(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
            )
            hidden = getattr(outputs, "last_hidden_state", None)
            if hidden is None:
                hidden = outputs[0]
            hidden = hidden[:, 1:-1, :]
            mask = attention_mask[:, 1:-1].float()
            hidden = self.secondary_encoder(hidden, mask=mask)
            return self.classifier(hidden)

    wrapper = NerWrapper(base).eval()

    # 优先使用 HanLP 已下载的本地分词器，避免再次联网
    tokenizer = getattr(base.encoder, "transformer_tokenizer", None)
    if tokenizer is None:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(base.encoder.config.transformer, use_fast=True)

    sample = tokenizer(
        "示例文本：北京城建集团有限责任公司。",
        return_tensors="pt",
        padding="max_length",
        max_length=64,
        truncation=True,
    )
    input_ids = sample["input_ids"]
    attention_mask = sample["attention_mask"]
    token_type_ids = sample.get("token_type_ids", torch.zeros_like(input_ids))

    onnx_path = out_dir / "model.onnx"
    print(f"正在导出 ONNX：{onnx_path}")
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (input_ids, attention_mask, token_type_ids),
            str(onnx_path),
            input_names=["input_ids", "attention_mask", "token_type_ids"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "sequence"},
                "attention_mask": {0: "batch", 1: "sequence"},
                "token_type_ids": {0: "batch", 1: "sequence"},
                "logits": {0: "batch", 1: "sequence"},
            },
            opset_version=args.opset,
            do_constant_folding=True,
        )

    # 新版 PyTorch 可能把权重写成外部文件，这里合并成单文件
    model_proto = onnx.load(str(onnx_path))
    onnx.save(model_proto, str(onnx_path), save_as_external_data=False)
    external = Path(str(onnx_path) + ".data")
    if external.exists():
        external.unlink()

    tokenizer.save_pretrained(str(out_dir))
    tokenizer.backend_tokenizer.no_padding()
    tokenizer.backend_tokenizer.enable_truncation(max_length=args.max_len)
    tokenizer.backend_tokenizer.save(str(out_dir / "tokenizer.json"))
    for name in ("config.json", "pytorch_model.bin", "tokenizer_config.json",
                 "special_tokens_map.json", "added_tokens.json", "generation_config.json"):
        (out_dir / name).unlink(missing_ok=True)

    labels = ner.vocabs["tag"]["idx_to_token"]
    (out_dir / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"模型：{onnx_path}（{onnx_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"分词器：{out_dir / 'tokenizer.json'}")
    print(f"标签：{out_dir / 'labels.json'}（{len(labels)} 个）")

    # 简单校验：ONNX 与 PyTorch 输出应基本一致
    import onnxruntime as ort

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    feeds = {
        "input_ids": input_ids.numpy(),
        "attention_mask": attention_mask.numpy(),
        "token_type_ids": token_type_ids.numpy(),
    }
    with torch.no_grad():
        expected = wrapper(input_ids, attention_mask, token_type_ids).numpy()
    actual = session.run(None, feeds)[0]
    print(f"校验最大误差：{np.abs(expected - actual).max():.6f}")


if __name__ == "__main__":
    main()
