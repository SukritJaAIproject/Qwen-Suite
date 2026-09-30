from __future__ import annotations

import argparse
import json
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    target = args.target.resolve()
    if args.offline:
        if not (target / "config.json").exists():
            raise FileNotFoundError(f"Bundled reranker model is incomplete: {target}")
    else:
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id="Qwen/Qwen3-Reranker-0.6B",
            local_dir=str(target),
            ignore_patterns=["*.msgpack", "*.h5", "*.ot"],
        )
    required = ["config.json", "tokenizer_config.json", "tokenizer.json"]
    missing = [name for name in required if not (target / name).exists()]
    if missing:
        raise RuntimeError(f"Reranker model is missing: {', '.join(missing)}")
    if not any((target / name).exists() for name in ("model.safetensors", "pytorch_model.bin")):
        raise RuntimeError(f"Reranker model weights are missing: {target}")
    print(json.dumps({"model": "Qwen/Qwen3-Reranker-0.6B", "path": str(target)}, indent=2))


if __name__ == "__main__":
    main()
