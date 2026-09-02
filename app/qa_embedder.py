# -*- coding: utf-8 -*-
"""问答库本地向量检索（RAG）支持。

设计要点：
- 嵌入模型：Xenova/bge-small-zh-v1.5 的 ONNX int8 量化版（23MB），
  纯 CPU 推理，单条文本约 30~60ms，无外部 API 依赖、零成本。
- 索引文本 = 问题 + 回答（节选问答的问题可能只有「在吗」这种低信息量
  内容，答案才是语义主体，所以对整对做嵌入）。
- 向量存 SQLite BLOB（float32），万条以内暴力余弦完全够用，
  不引入向量数据库。
- 任何失败（模型缺失/加载失败/推理异常）都降级为 available()=False，
  调用方回退到全量注入，RAG 故障不影响 AI 回复主流程。
"""
import os
import threading
import time
from typing import List, Optional, Sequence

from loguru import logger

import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "bge-small-zh-v1.5")
MODEL_FILE = os.path.join(MODEL_DIR, "model_quantized.onnx")
TOKENIZER_FILE = os.path.join(MODEL_DIR, "tokenizer.json")
# bge-small-zh-v1.5 的向量维度
EMBED_DIM = 512
# 单次推理的文本批上限，避免长对话一次性推理卡顿
MAX_BATCH = 32
# 单条文本截断的 token 数（BGE 官方 max_seq_length=512，问答拼接一般远小于此）
MAX_TOKENS = 256

_lock = threading.Lock()
_session = None          # onnxruntime.InferenceSession
_tokenizer = None        # tokenizers.Tokenizer
_load_failed = False     # 加载失败标记（避免每次调用都重试拖慢主流程）


def _try_load():
    """加载模型（进程内只尝试一次，失败则标记并不再重试）。"""
    global _session, _tokenizer, _load_failed
    if _session is not None or _load_failed:
        return
    with _lock:
        if _session is not None or _load_failed:
            return
        if not (os.path.isfile(MODEL_FILE) and os.path.isfile(TOKENIZER_FILE)):
            _load_failed = True
            logger.info(f"[QA-RAG] 嵌入模型文件缺失（{MODEL_DIR}），问答库走全量注入模式")
            return
        try:
            t0 = time.time()
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2   # 低配容器友好，避免抢占主服务线程
            _session = ort.InferenceSession(
                MODEL_FILE, sess_options=opts, providers=["CPUExecutionProvider"])
            _tokenizer = Tokenizer.from_file(TOKENIZER_FILE)
            # 编码器初始化（触发惰性初始化）
            _run_encode(["启动预热"])
            logger.info(f"[QA-RAG] 嵌入模型加载成功（{time.time() - t0:.1f}s），维度={EMBED_DIM}")
        except Exception as exc:
            _load_failed = True
            _session = None
            _tokenizer = None
            logger.warning(f"[QA-RAG] 嵌入模型加载失败，问答库走全量注入模式: {exc}")


def _run_encode(texts: Sequence[str]) -> np.ndarray:
    """分词+推理+mean pooling+归一化，返回 [n, EMBED_DIM] float32。"""
    import numpy as np
    encoded = [_tokenizer.encode(t) for t in texts]
    maxlen = min(max((len(e.ids) for e in encoded), default=1), MAX_TOKENS)
    pad = lambda rows: np.array([r + [0] * (maxlen - len(r)) for r in rows], dtype=np.int64)
    input_ids = pad([e.ids[:MAX_TOKENS] for e in encoded])
    attention = pad([[1] * min(len(e.ids), MAX_TOKENS) for e in encoded])
    token_types = np.zeros_like(input_ids)
    out = _session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention,
        "token_type_ids": token_types,
    })
    hidden = out[0]  # [batch, seq, dim]
    mask = attention[:, :, None]
    emb = (hidden * mask).sum(1) / np.clip(mask.sum(1), 1e-9, None)
    emb = emb / np.clip(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9, None)
    return emb.astype(np.float32)


def available() -> bool:
    """模型是否可用（可用才启用向量检索，否则调用方走全量注入）。"""
    _try_load()
    return _session is not None


def embed(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """批量转向量。失败返回 None（调用方降级）。"""
    if not texts:
        return []
    _try_load()
    if _session is None:
        return None
    try:
        result: List[List[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = [str(t)[:2000] for t in texts[start:start + MAX_BATCH]]
            result.extend(_run_encode(batch).tolist())
        return result
    except Exception as exc:
        logger.warning(f"[QA-RAG] 向量推理失败: {exc}")
        return None


def embed_one(text: str) -> Optional[List[float]]:
    """单条转向量。失败返回 None。"""
    vectors = embed([text])
    return vectors[0] if vectors else None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """两个已归一化向量的余弦相似度。"""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom < 1e-12:
        return 0.0
    return float(np.dot(va, vb) / denom)
