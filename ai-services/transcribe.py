from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def transcribe(path: Path, language: str = "") -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    from faster_whisper import WhisperModel

    root = Path(__file__).resolve().parents[1]
    model_path = Path(
        os.environ.get(
            "QWEN_WHISPER_MODEL",
            str(root / "model-cache" / "faster-whisper-base"),
        )
    )
    if not model_path.is_dir():
        raise FileNotFoundError(f"Whisper model is missing: {model_path}")
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=8)
    segments, _info = model.transcribe(
        str(path),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Thai/English speech-to-text for Qwen/OpenClaw")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--language", default="", help="Optional ISO language hint, for example th or en")
    args = parser.parse_args()
    try:
        sys.stdout.write(transcribe(args.audio.resolve(), args.language))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
