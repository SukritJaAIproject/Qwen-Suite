from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer


ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = os.environ.get("QWEN_RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B")
MODEL_PATH = Path(
    os.environ.get("QWEN_RERANKER_MODEL_PATH", str(ROOT / "model-cache" / "Qwen3-Reranker-0.6B"))
)
MAX_LENGTH = max(512, min(int(os.environ.get("QWEN_RERANKER_MAX_LENGTH", "2048")), 8192))
BATCH_SIZE = max(1, min(int(os.environ.get("QWEN_RERANKER_BATCH_SIZE", "1")), 8))
DEFAULT_INSTRUCTION = "Given a user query, retrieve relevant passages from project memory and documents that answer the query."


class RerankRequest(BaseModel):
    model: str = MODEL_ID
    query: str = Field(min_length=1, max_length=50_000)
    documents: list[str] = Field(min_length=1, max_length=64)
    top_n: int | None = None
    instruction: str | None = None


app = FastAPI(title="Qwen3 Local Reranker", version="1.0.0")
_load_lock = threading.Lock()
_inference_lock = threading.Lock()
_tokenizer = None
_model = None
_prefix_tokens: list[int] = []
_suffix_tokens: list[int] = []
_yes_id = 0
_no_id = 0
_loaded_at = 0.0


def _load() -> None:
    global _tokenizer, _model, _prefix_tokens, _suffix_tokens, _yes_id, _no_id, _loaded_at
    if _model is not None:
        return
    with _load_lock:
        if _model is not None:
            return
        source = str(MODEL_PATH) if MODEL_PATH.exists() else MODEL_ID
        local_only = MODEL_PATH.exists()
        tokenizer = AutoTokenizer.from_pretrained(source, padding_side="left", local_files_only=local_only)
        model = AutoModelForCausalLM.from_pretrained(
            source,
            local_files_only=local_only,
            dtype=torch.float32,
            low_cpu_mem_usage=True,
        ).eval().to("cpu")
        prefix = (
            '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the '
            'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        _prefix_tokens = tokenizer.encode(prefix, add_special_tokens=False)
        _suffix_tokens = tokenizer.encode(suffix, add_special_tokens=False)
        _yes_id = tokenizer.convert_tokens_to_ids("yes")
        _no_id = tokenizer.convert_tokens_to_ids("no")
        _tokenizer = tokenizer
        _model = model
        _loaded_at = time.time()


def _format(instruction: str, query: str, document: str) -> str:
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"


def _score_batch(texts: list[str]) -> list[float]:
    assert _tokenizer is not None and _model is not None
    usable_length = MAX_LENGTH - len(_prefix_tokens) - len(_suffix_tokens)
    encoded = _tokenizer(
        texts,
        padding=False,
        truncation=True,
        max_length=usable_length,
        return_attention_mask=False,
    )
    for index, token_ids in enumerate(encoded["input_ids"]):
        encoded["input_ids"][index] = _prefix_tokens + token_ids + _suffix_tokens
    inputs = _tokenizer.pad(encoded, padding=True, return_tensors="pt", max_length=MAX_LENGTH)
    with torch.inference_mode():
        logits = _model(**inputs).logits[:, -1, :]
        yes = logits[:, _yes_id]
        no = logits[:, _no_id]
        scores = torch.softmax(torch.stack([no, yes], dim=1), dim=1)[:, 1]
    return [float(value) for value in scores.tolist()]


def rerank(query: str, documents: list[str], instruction: str) -> list[float]:
    _load()
    pairs = [_format(instruction, query, str(document)[:50_000]) for document in documents]
    scores: list[float] = []
    with _inference_lock:
        for offset in range(0, len(pairs), BATCH_SIZE):
            scores.extend(_score_batch(pairs[offset : offset + BATCH_SIZE]))
    return scores


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL_ID,
        "model_path": str(MODEL_PATH),
        "model_present": MODEL_PATH.exists(),
        "loaded": _model is not None,
        "max_length": MAX_LENGTH,
        "batch_size": BATCH_SIZE,
        "device": "cpu",
    }


@app.post("/v1/rerank")
def rerank_endpoint(body: RerankRequest) -> dict[str, Any]:
    try:
        instruction = (body.instruction or DEFAULT_INSTRUCTION).strip()
        scores = rerank(body.query, body.documents, instruction)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Qwen3 reranking failed: {exc}") from exc
    results = [
        {"index": index, "relevance_score": score}
        for index, score in enumerate(scores)
    ]
    results.sort(key=lambda item: item["relevance_score"], reverse=True)
    top_n = len(results) if body.top_n is None else max(1, min(int(body.top_n), len(results)))
    return {"id": f"qwen-rerank-{int(time.time())}", "model": MODEL_ID, "results": results[:top_n]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8771, log_level="info")

