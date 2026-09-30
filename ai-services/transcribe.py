"""Local Thai/English speech-to-text for OpenClaw voice notes and Qwen Dictate (dictate.ps1).

Model choice (2026-09-30): faster-whisper-small when it is installed (clearly better on Thai: 25 %
vs 38 % character errors for base on a Thai TTS test clip), otherwise faster-whisper-base. Small
is ~3x slower on this CPU-only box and OpenClaw stops the transcriber after 300 s
(openclaw.json tools.media.models[0].timeoutSeconds), so a clip that small would not finish well
inside the budget - a long one, or one arriving while the local LLM keeps the same cores busy -
is done with base instead. QWEN_WHISPER_MODEL=<folder|small|base> still forces one model;
QWEN_WHISPER_BUDGET_SECONDS changes the budget (default 150 s, half the OpenClaw timeout).
The previous version is in backups\\claude-20260930-batch3\\transcribe.py.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMALL = ROOT / "model-cache" / "faster-whisper-small"
BASE = ROOT / "model-cache" / "faster-whisper-base"
SAMPLE_RATE = 16000
SMALL_RTF_IDLE = 0.45   # seconds of work per second of audio for small on an idle CPU (measured 0.33-0.43)
BUSY_FACTOR = 8.0       # slowdown at 100 % CPU, deliberately pessimistic (base measured 2-3.5x under the LLM)
DEFAULT_BUDGET = 150.0  # half of OpenClaw's 300 s media timeout


def _installed(folder: Path) -> bool:
    return (folder / "model.bin").is_file() and (folder / "config.json").is_file()


def _budget() -> float:
    try:
        return max(10.0, float(os.environ.get("QWEN_WHISPER_BUDGET_SECONDS", DEFAULT_BUDGET)))
    except ValueError:
        return DEFAULT_BUDGET


def cpu_busy() -> float:
    """System-wide CPU load 0..1 over 0.3 s (this process sleeps meanwhile); 0.5 if unknown."""
    try:
        import psutil

        return max(0.0, min(1.0, psutil.cpu_percent(interval=0.3) / 100.0))
    except Exception:
        return 0.5


def choose_model(audio_seconds: float | None = None, busy: float | None = None, forced: str = "") -> tuple[Path, str]:
    """Return (model folder, reason)."""
    forced = (forced or os.environ.get("QWEN_WHISPER_MODEL", "")).strip()
    if forced:
        return {"small": SMALL, "base": BASE}.get(forced.lower(), Path(forced)), "forced"
    if not _installed(SMALL):
        return BASE, "small not installed"
    if not _installed(BASE) or audio_seconds is None:
        return SMALL, "preferred"
    if busy is None:
        busy = cpu_busy()
    estimate = audio_seconds * SMALL_RTF_IDLE * (1.0 + BUSY_FACTOR * busy)
    if estimate > _budget():
        return BASE, f"small would need ~{estimate:.0f} s (> {_budget():.0f} s budget; audio {audio_seconds:.0f} s, cpu {busy:.0%})"
    return SMALL, f"preferred (~{estimate:.0f} s estimated)"


def transcribe(path: Path, language: str = "", model: str = "", explain: bool = False) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    from faster_whisper import WhisperModel, decode_audio

    audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
    model_path, reason = choose_model(len(audio) / SAMPLE_RATE, None, model)
    if not model_path.is_dir():
        raise FileNotFoundError(f"Whisper model is missing: {model_path}")
    if explain:
        sys.stderr.write(f"whisper model: {model_path.name} ({reason})\n")
    whisper = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=8)
    segments, _info = whisper.transcribe(
        audio,
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
    parser.add_argument("--model", default="", help="small, base or a model folder (default: automatic)")
    parser.add_argument("--explain", action="store_true", help="Print the chosen model and why to stderr")
    args = parser.parse_args()
    try:
        sys.stdout.write(transcribe(args.audio.resolve(), args.language, args.model, args.explain))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
