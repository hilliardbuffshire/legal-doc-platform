"""
경량 한국어 임베딩 — onnxruntime + tokenizers
multilingual-e5-small ONNX 모델 사용 (130MB, Torch 불필요)
"""
import os, json, numpy as np
from pathlib import Path
from huggingface_hub import hf_hub_download

MODEL_ID   = "Xenova/multilingual-e5-small"
ONNX_FILE  = "onnx/model.onnx"
TOKEN_FILE = "tokenizer.json"
TOKEN_CFG  = "tokenizer_config.json"

def _get_cache(data_dir: Path) -> Path:
    p = data_dir / "embed_model"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _download(cache: Path):
    for filename in [ONNX_FILE, TOKEN_FILE, TOKEN_CFG]:
        dest = cache / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            print(f"[임베딩 모델] 다운로드 중: {filename} ...")
            src = hf_hub_download(
                repo_id=MODEL_ID,
                filename=filename,
                local_dir=str(cache),
            )


class Embedder:
    def __init__(self, data_dir: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        cache = _get_cache(data_dir)
        _download(cache)

        self._sess = ort.InferenceSession(
            str(cache / ONNX_FILE),
            providers=["CPUExecutionProvider"],
        )
        self._tok = Tokenizer.from_file(str(cache / TOKEN_FILE))
        self._tok.enable_padding(pad_token="[PAD]", pad_id=0)
        self._tok.enable_truncation(max_length=512)

    def embed(self, text: str) -> list[float]:
        # multilingual-e5-small은 "query: " / "passage: " 접두어를 붙여야 최적 품질
        enc = self._tok.encode("query: " + text[:1024])
        ids  = np.array([enc.ids],       dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        tids = np.zeros_like(ids)

        out = self._sess.run(None, {
            "input_ids":      ids,
            "attention_mask": mask,
            "token_type_ids": tids,
        })[0]   # (1, seq, hidden)

        # mean pooling (마스크 적용)
        m   = mask[..., None].astype(np.float32)
        vec = (out * m).sum(1) / m.sum(1)
        vec = vec[0]
        vec /= np.linalg.norm(vec) + 1e-10
        return vec.tolist()
