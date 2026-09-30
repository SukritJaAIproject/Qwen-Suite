from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path


def _chunks(text: str, limit: int = 360) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?。！？])\s*|(?<=\n)", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            head, sentence = sentence[:limit], sentence[limit:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def synthesize(text: str, output: Path) -> None:
    import numpy as np
    import torch
    from transformers import AutoTokenizer, VitsModel

    if len(text) > 2000:
        raise ValueError("TTS text exceeds the 2,000-character safety limit.")
    pieces = _chunks(text)
    if not pieces:
        raise ValueError("TTS text is empty.")
    root = Path(__file__).resolve().parents[1]
    model_path = Path(os.environ.get("QWEN_TTS_MODEL", root / "model-cache" / "mms-tts-tha"))
    if not (model_path / "model.safetensors").is_file():
        raise FileNotFoundError(f"Thai TTS model is missing: {model_path}")

    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = VitsModel.from_pretrained(model_path, local_files_only=True, use_safetensors=True)
    model.eval()
    torch.manual_seed(7)
    sample_rate = int(model.config.sampling_rate)
    silence = np.zeros(int(sample_rate * 0.14), dtype=np.float32)
    audio: list[np.ndarray] = []
    with torch.inference_mode():
        for index, piece in enumerate(pieces):
            inputs = tokenizer(piece, return_tensors="pt")
            waveform = model(**inputs).waveform[0].cpu().float().numpy()
            audio.append(waveform)
            if index + 1 < len(pieces):
                audio.append(silence)
    merged = np.concatenate(audio)
    peak = float(np.max(np.abs(merged))) if merged.size else 0.0
    if peak > 1.0:
        merged = merged / peak
    pcm = (np.clip(merged, -1.0, 1.0) * 32767.0).astype("<i2")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    def write_wav(path: Path) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm.tobytes())

    if output.suffix.casefold() not in {".opus", ".ogg", ".mp3"}:
        write_wav(output)
        return
    temporary = output.with_name(output.name + ".source.wav")
    write_wav(temporary)
    configured_ffmpeg = os.environ.get("QWEN_FFMPEG", "").strip()
    ffmpeg = configured_ffmpeg or shutil.which("ffmpeg") or ""
    if not ffmpeg or not Path(ffmpeg).is_file():
        temporary.unlink(missing_ok=True)
        raise FileNotFoundError("FFmpeg is required to create Opus/MP3 speech output.")
    codec = ["-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "1"]
    if output.suffix.casefold() == ".mp3":
        codec = ["-c:a", "libmp3lame", "-b:a", "96k", "-ar", "44100", "-ac", "1"]
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(temporary), *codec, str(output)],
            check=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Thai text-to-speech for Qwen/OpenClaw")
    parser.add_argument("output", type=Path)
    parser.add_argument("text")
    args = parser.parse_args()
    try:
        synthesize(args.text, args.output)
        print(str(args.output.resolve()))
        return 0
    except Exception as exc:
        import sys

        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
