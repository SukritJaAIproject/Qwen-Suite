from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont


PALETTES = {
    "premium": ((99, 72, 218), (25, 33, 70), (247, 183, 67), (246, 247, 255)),
    "education": ((38, 126, 211), (16, 52, 96), (255, 111, 145), (244, 249, 255)),
    "cinematic": ((188, 82, 55), (28, 24, 35), (238, 184, 84), (250, 244, 235)),
    "corporate": ((19, 128, 122), (15, 40, 64), (91, 174, 204), (244, 250, 251)),
    "playful": ((227, 72, 135), (75, 41, 118), (255, 183, 64), (255, 247, 252)),
    "minimal": ((67, 91, 119), (23, 35, 48), (123, 164, 191), (248, 250, 252)),
}
DIMENSIONS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
# Written by Resolve-OllamaGpu.ps1 whenever the Ollama server starts (added 2026-09-29).
GPU_PROFILE_PATH = Path(
    os.environ.get("QWEN_GPU_PROFILE", str(Path(__file__).resolve().parents[1] / "data" / "gpu-profile.json"))
)


def gpu_vram_class() -> str:
    """'large' when the server runs on a >=20 GB eGPU, 'small', or 'none' (CPU)."""
    try:
        data = json.loads(GPU_PROFILE_PATH.read_text(encoding="utf-8-sig"))
        return str(data.get("vram_class") or "none") if isinstance(data, dict) else "none"
    except (OSError, ValueError):
        return "none"


# Narration pace, MEASURED on this pipeline rather than assumed. A real 12-second render on
# 2026-09-23 (job video-20260923-214450-db3d656, en-US voice) produced 23 words of narration
# that took 15.31 s including the pauses between scenes = 1.50 words/second. The first
# version of this budget assumed 2.2 and that render still had to be sped up 1.37x.
# Budgeting at 1.4 leaves a small margin under the measured rate.
# Note: Thai text has no spaces between words, so word counts here only really bound
# English narration; the narration_tempo_ok QA check still catches Thai overflow.
WORDS_PER_SECOND = 1.4
MIN_WORDS_PER_SCENE = 3
# Words a trimmed line should not end on - trimming used to produce "Sun is clean and."
DANGLING_WORDS = {
    "a", "an", "the", "and", "or", "but", "so", "to", "of", "for", "with", "in", "on", "at",
    "by", "from", "as", "that", "which", "because", "is", "are", "was", "were", "be", "can",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class VideoBuild:
    def __init__(self, job_path: Path) -> None:
        self.job_path = job_path.resolve()
        self.job = json.loads(self.job_path.read_text(encoding="utf-8"))
        self.work = Path(self.job["work_dir"]).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.cards = self.work / "cards"
        self.cards.mkdir(exist_ok=True)
        self.previews = self.work / "previews"
        self.previews.mkdir(exist_ok=True)
        self.ffmpeg = self.find_program("ffmpeg")
        self.ffprobe = self.find_program("ffprobe")
        self.command_log = self.work / "commands.log"
        self.warnings: list[str] = list(self.job.get("warnings") or [])

    @staticmethod
    def find_program(name: str) -> str:
        direct = shutil.which(name)
        if direct:
            return direct
        link = Path(r"C:\Users\EGAT\AppData\Local\Microsoft\WinGet\Links") / f"{name}.exe"
        if link.is_file():
            return str(link)
        package_root = Path(r"C:\Users\EGAT\AppData\Local\Microsoft\WinGet\Packages")
        matches = list(package_root.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe")) if package_root.exists() else []
        if matches:
            return str(matches[0])
        raise FileNotFoundError(f"{name} is not installed or discoverable")

    def refresh(self) -> None:
        current = json.loads(self.job_path.read_text(encoding="utf-8"))
        current.update(self.job)
        self.job = current

    def update(self, phase: str, percent: float, message: str, **extra: Any) -> None:
        try:
            current = json.loads(self.job_path.read_text(encoding="utf-8"))
        except Exception:
            current = dict(self.job)
        coordinator_fields = {
            key: current.get(key)
            for key in ("task_id", "attempt", "auto_retry", "max_attempts")
            if key in current
        }
        current.update(self.job)
        current.update(coordinator_fields)
        current.update(extra)
        current["phase"] = phase
        current["progress_percent"] = round(max(0.0, min(100.0, percent)), 1)
        current["message"] = message
        current["updated_at"] = now_iso()
        current["warnings"] = self.warnings
        self.job = current
        atomic_json(self.job_path, current)
        print(f"PROGRESS {current['progress_percent']:.1f}% [{phase}] {message}", flush=True)

    def run(self, argv: list[str], label: str, timeout: int = 7200) -> subprocess.CompletedProcess[str]:
        started = time.time()
        with self.command_log.open("a", encoding="utf-8") as log:
            log.write(f"\n[{now_iso()}] {label}\n{subprocess.list2cmdline(argv)}\n")
        completed = subprocess.run(
            argv,
            cwd=str(self.work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        with self.command_log.open("a", encoding="utf-8") as log:
            log.write(completed.stdout[-20_000:])
            log.write(completed.stderr[-40_000:])
            log.write(f"\nexit={completed.returncode} elapsed={time.time()-started:.1f}s\n")
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout)[-3000:]
            raise RuntimeError(f"{label} failed with exit code {completed.returncode}: {tail}")
        return completed

    @property
    def duration(self) -> int:
        return int(self.job["duration_seconds"])

    @property
    def dimensions(self) -> tuple[int, int]:
        return DIMENSIONS[str(self.job["aspect_ratio"])]

    def detect_language(self) -> str:
        requested = str(self.job.get("language", "auto"))
        if requested != "auto":
            return requested
        return "thai" if re.search(r"[ก-๙]", str(self.job["prompt"])) else "english"

    def storyboard(self) -> dict[str, Any]:
        target = self.work / "storyboard.json"
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(data.get("scenes"), list) and len(data["scenes"]) >= 4:
                    return data
            except Exception:
                pass

        language = self.detect_language()
        thai_alphabet_mode = self.is_thai_alphabet_brief()
        # At least 6 scenes forced a 12-second video into 2-second scenes whose minimum
        # narration could not fit. Short videos get 4 (the floor scene_count_ok accepts);
        # 30 s and longer keep the previous behaviour exactly.
        scene_count = max(4 if self.duration < 30 else 6, min(60, round(self.duration / 14)))
        # A complete Thai-consonant lesson needs an intro, all 44 consonants,
        # and a recap. Do not silently drop the final letters just because the
        # generic scene cap was tuned for ordinary explainer videos.
        if thai_alphabet_mode and self.duration >= 360:
            scene_count = 46
        response_schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "scenes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "body": {"type": "string"},
                            "narration": {"type": "string"},
                            "emoji": {"type": "string"},
                            "keyword": {"type": "string"},
                        },
                        "required": ["heading", "body", "narration", "emoji", "keyword"],
                    },
                },
            },
            "required": ["title", "subtitle", "scenes"],
        }
        same_language = "Thai" if language == "thai" else "English"
        # "must fit the requested duration at a natural speaking pace" was too vague to
        # act on: the 2026-09-21/22 jobs asked for 8-12 second videos and came back with
        # 28-34 seconds of narration, which the mixer then compressed 3-4x into noise.
        # Give the model the arithmetic instead of the adjective.
        word_budget = self.narration_word_budget(scene_count)
        prompt = (
            f"Create a concise studio-quality storyboard in {same_language} for a {self.duration}-second video. "
            f"Return exactly {scene_count} scenes. Each heading is under 8 words, body under 24 words, "
            f"keyword under 3 words, and emoji is one relevant single emoji.\n"
            f"NARRATION LENGTH IS A HARD LIMIT: at most {word_budget} words per scene, "
            f"{word_budget * scene_count} words in total for the whole video. That is what fits "
            f"{self.duration} seconds at a natural speaking pace ({WORDS_PER_SECOND} words per second); "
            "narration longer than this is unusable because it has to be sped up until it cannot be understood. "
            "Write fewer, shorter sentences rather than going over.\n"
            "Build a clear beginning, useful middle, recap, and strong ending. "
            "Do not mention that you are an AI.\n\nUSER BRIEF:\n"
            + str(self.job["prompt"])
        )
        # Alphabet lessons are structured data, not an open-ended writing task.
        # Build the canonical 44-letter sequence deterministically so a stalled
        # or undersized model can never omit, duplicate, or reorder a letter.
        data: dict[str, Any] | None = self.thai_alphabet_storyboard() if thai_alphabet_mode and scene_count == 46 else None
        try:
            if data is not None:
                raise StopIteration
            # On a 24 GB eGPU the general model is resident and fast, and loading granite4.2:8b
            # next to it would evict it; write the storyboard with it instead. No num_ctx,
            # num_thread or shift in either branch: every caller loads a model with the
            # same runner options (the server's 32768 context, llama-server's own thread
            # count, the default shift), so this reuses a model someone else loaded instead
            # of making Ollama reload it (see README "runner options").
            large_gpu = gpu_vram_class() == "large"
            # 1.6: Granite 4.2 8B replaced qwen3:4b as the small model (qwen3:4b is no longer installed).
            storyboard_model = "qwen3.8:27b" if large_gpu else "granite4.2:8b"
            if storyboard_model == "granite4.2:8b" and language == "thai":
                # Measured 2026-10-06 (i7-10750H): about half of Granite's Thai narration lines were
                # ungrammatical, and 10 scenes took 548 s. The template is what Thai jobs got anyway.
                raise RuntimeError("granite4.2:8b writes unreliable Thai; using the instant local storyboard fallback")
            options: dict[str, Any] = {
                "temperature": 0.35,
                "num_predict": max(2200, min(7200, scene_count * 120)),
            }
            if large_gpu:
                keep_alive, timeout = "2h", (10, 900)
            else:
                # The whole JSON must arrive within the read timeout (stream=False). Granite writes about
                # 3 tokens/s on this CPU, roughly 150 tokens a scene: 4 scenes -> 190 s, 10 -> 430 s.
                keep_alive, timeout = "15m", (10, min(600, 30 + 40 * scene_count))
                running = requests.get("http://127.0.0.1:11434/api/ps", timeout=5).json().get("models", [])
                running_names = {str(item.get("name") or item.get("model") or "") for item in running}
                if running_names and storyboard_model not in running_names:
                    raise RuntimeError(
                        "Ollama is busy with another large model; using the instant local storyboard fallback"
                    )
            response = requests.post(
                "http://127.0.0.1:11434/api/chat",
                json={
                    "model": storyboard_model,
                    "messages": [
                        {"role": "system", "content": "You are a senior educational video producer. Return valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "format": response_schema,
                    "stream": False,
                    "think": False,
                    "keep_alive": keep_alive,
                    "options": options,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            content = str((response.json().get("message") or {}).get("content") or "").strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S)
            data = json.loads(content)
        except StopIteration:
            pass
        except Exception as exc:
            self.warnings.append(f"Local storyboard model fallback used: {type(exc).__name__}: {exc}")

        if not data or not isinstance(data.get("scenes"), list):
            data = self.fallback_storyboard(scene_count, language)
        scenes = [item for item in data.get("scenes", []) if isinstance(item, dict)]
        if not scenes:
            data = self.fallback_storyboard(scene_count, language)
            scenes = data["scenes"]
        while len(scenes) < scene_count:
            source = dict(scenes[len(scenes) % len(scenes)])
            source["heading"] = f"{source.get('heading', 'Key idea')} · {len(scenes)+1}"
            scenes.append(source)
        data["scenes"] = scenes[:scene_count]
        data["title"] = str(data.get("title") or self.job["title"])[:120]
        data["subtitle"] = str(data.get("subtitle") or self.job["prompt"])[:180]
        data["language"] = language
        atomic_json(target, data)
        return data

    def is_thai_alphabet_brief(self) -> bool:
        brief = str(self.job.get("prompt") or "")
        lowered = brief.casefold()
        return bool(
            re.search(r"ก\s*(?:-|–|—|ถึง|to)\s*ฮ", brief, flags=re.I)
            or ("thai" in lowered and any(term in lowered for term in ("alphabet", "consonant", "letters")))
            or ("พยัญชนะไทย" in brief)
        )

    def thai_alphabet_storyboard(self) -> dict[str, Any]:
        """Canonical English-language lesson covering all 44 Thai consonants."""
        units = [
            ("ก", "ko kai", "chicken", "k", "🐔"),
            ("ข", "kho khai", "egg", "kh", "🥚"),
            ("ฃ", "kho khuat", "bottle (obsolete letter)", "kh", "🍾"),
            ("ค", "kho khwai", "buffalo", "kh", "🐃"),
            ("ฅ", "kho khon", "person (obsolete letter)", "kh", "👤"),
            ("ฆ", "kho rakhang", "bell", "kh", "🔔"),
            ("ง", "ngo ngu", "snake", "ng", "🐍"),
            ("จ", "cho chan", "plate", "ch", "🍽️"),
            ("ฉ", "cho ching", "cymbals", "ch", "🎶"),
            ("ช", "cho chang", "elephant", "ch", "🐘"),
            ("ซ", "so so", "chain", "s", "⛓️"),
            ("ฌ", "cho choe", "tree", "ch", "🌳"),
            ("ญ", "yo ying", "woman", "y", "👩"),
            ("ฎ", "do chada", "Thai headdress", "d", "👑"),
            ("ฏ", "to patak", "goad", "t", "⭐"),
            ("ฐ", "tho than", "pedestal", "th", "🏆"),
            ("ฑ", "tho montho", "Montho", "th", "📖"),
            ("ฒ", "tho phuthao", "elder", "th", "👴"),
            ("ณ", "no nen", "novice monk", "n", "🙏"),
            ("ด", "do dek", "child", "d", "👦"),
            ("ต", "to tao", "turtle", "t", "🐢"),
            ("ถ", "tho thung", "bag", "th", "👜"),
            ("ท", "tho thahan", "soldier", "th", "⭐"),
            ("ธ", "tho thong", "flag", "th", "🚩"),
            ("น", "no nu", "mouse", "n", "🐭"),
            ("บ", "bo baimai", "leaf", "b", "🍃"),
            ("ป", "po pla", "fish", "p", "🐟"),
            ("ผ", "pho phueng", "bee", "ph", "🐝"),
            ("ฝ", "fo fa", "lid", "f", "🥘"),
            ("พ", "pho phan", "tray", "ph", "🍽️"),
            ("ฟ", "fo fan", "tooth", "f", "🦷"),
            ("ภ", "pho samphao", "sailing junk", "ph", "⛵"),
            ("ม", "mo ma", "horse", "m", "🐴"),
            ("ย", "yo yak", "giant", "y", "👹"),
            ("ร", "ro ruea", "boat", "r", "🚤"),
            ("ล", "lo ling", "monkey", "l", "🐒"),
            ("ว", "wo waen", "ring", "w", "💍"),
            ("ศ", "so sala", "pavilion", "s", "🏡"),
            ("ษ", "so ruesi", "hermit", "s", "🧙"),
            ("ส", "so suea", "tiger", "s", "🐯"),
            ("ห", "ho hip", "chest", "h", "📦"),
            ("ฬ", "lo chula", "Chula kite", "l", "🪁"),
            ("อ", "o ang", "basin", "silent or vowel carrier", "🥣"),
            ("ฮ", "ho nokhuk", "owl", "h", "🦉"),
        ]
        scenes: list[dict[str, str]] = [
            {
                "heading": "Thai Consonants · ก–ฮ",
                "body": "Meet all 44 Thai consonants in their traditional order.",
                "narration": "Welcome! Let us learn all forty-four Thai consonants, from ko kai to ho nok huk, with clear English guidance.",
                "emoji": "🇹🇭",
                "keyword": "ก–ฮ",
            }
        ]
        for letter, name, meaning, sound, icon in units:
            body = f"{letter} is {name}. Picture: {meaning}. Initial sound: {sound}."
            narration = f"This is {letter}, called {name}. Its memory word means {meaning}. The initial sound is {sound}. Say {name}."
            scenes.append(
                {
                    "heading": f"{letter} · {name}",
                    "body": body,
                    "narration": narration,
                    "emoji": icon,
                    "keyword": letter,
                }
            )
        scenes.append(
            {
                "heading": "Great work! Review ก–ฮ",
                "body": "You completed all 44 consonants. Review in small groups and practise every day.",
                "narration": "Excellent work! You reached ho nok huk. Review the letters in small groups, repeat their names, and practise again tomorrow.",
                "emoji": "🏆",
                "keyword": "Review",
            }
        )
        return {
            "title": "Thai Consonants ก–ฮ",
            "subtitle": "All 44 Thai consonants explained in English",
            "scenes": scenes,
            "language": "english",
        }

    def narration_word_budget(self, scene_count: int) -> int:
        """Words of narration one scene may carry so the whole video needs no speed-up."""
        total = max(1.0, float(self.duration)) * WORDS_PER_SECOND
        per_scene = int(total // max(1, int(scene_count)))
        return max(MIN_WORDS_PER_SCENE, min(per_scene, 45))

    @staticmethod
    def trim_to_words(text: str, limit: int) -> str:
        """Hard cap on a narration line - the model does not always respect the prompt.

        Cuts at a clause boundary when one falls in the back half of the window, and never
        leaves the line ending on a dangling word ("... clean and.").
        """
        words = str(text or "").split()
        if len(words) <= limit:
            return " ".join(words)
        cut = words[:limit]
        for index in range(len(cut) - 1, max(0, len(cut) // 2) - 1, -1):
            if cut[index].endswith((",", ";", ":")):
                cut = cut[: index + 1]
                break
        while len(cut) > 1 and cut[-1].lower().strip(",;:.!?") in DANGLING_WORDS:
            cut = cut[:-1]
        trimmed = " ".join(cut).rstrip(",;:-")
        return trimmed if trimmed.endswith((".", "!", "?", "ๆ")) else trimmed + "."

    def fallback_storyboard(self, count: int, language: str) -> dict[str, Any]:
        brief = re.sub(r"\s+", " ", str(self.job["prompt"])).strip()
        parts = [part.strip() for part in re.split(r"[\n.!?]+", brief) if part.strip()]
        if not parts:
            parts = [brief or "Qwen Video"]
        if language == "thai":
            labels = ["เริ่มต้น", "แนวคิดสำคัญ", "ตัวอย่าง", "วิธีนำไปใช้", "ทบทวน", "สรุป"]
            closing = "สรุปสาระสำคัญและนำแนวคิดนี้ไปใช้ได้ทันที"
        else:
            labels = ["Welcome", "Key idea", "Example", "How to use it", "Quick review", "Conclusion"]
            closing = "Review the key idea and put it into practice today."
        icons = ["🎬", "💡", "📚", "🛠️", "✅", "🏁"]
        budget = self.narration_word_budget(count)
        scenes = []
        for index in range(count):
            part = parts[index % len(parts)]
            heading = labels[index % len(labels)] if index not in {0, count - 1} else labels[0 if index == 0 else -1]
            body = part[:180]
            narration = self.trim_to_words(part if index < count - 1 else closing, budget)
            scenes.append({"heading": heading, "body": body, "narration": narration, "emoji": icons[index % len(icons)], "keyword": heading})
        return {"title": str(self.job["title"]), "subtitle": brief[:180], "scenes": scenes, "language": language}

    @staticmethod
    def font_path(bold: bool, non_latin: bool = False, emoji: bool = False) -> Path:
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        names = ["seguiemj.ttf"] if emoji else (["tahomabd.ttf", "leelawui.ttf", "arialbd.ttf"] if non_latin and bold else ["tahoma.ttf", "leelawui.ttf", "arial.ttf"] if non_latin else ["segoeuib.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"])
        for name in names:
            candidate = fonts / name
            if candidate.is_file():
                return candidate
        raise FileNotFoundError("Required Windows font is missing")

    @classmethod
    def font(cls, size: int, bold: bool = False, text: str = "", emoji: bool = False) -> ImageFont.FreeTypeFont:
        non_latin = bool(re.search(r"[^\x00-\x7f]", text)) and not emoji
        return ImageFont.truetype(str(cls.font_path(bold, non_latin, emoji)), max(12, int(size)))

    @staticmethod
    def fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
        words = re.sub(r"\s+", " ", text).strip().split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or draw.textbbox((0, 0), candidate, font=font)[2] <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(" ".join(lines)) < len(text.strip()):
            lines[-1] = lines[-1].rstrip(" .") + "…"
        return lines

    @staticmethod
    def gradient(size: tuple[int, int], start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
        width, height = size
        strip = Image.new("RGB", (1, height))
        pixels = strip.load()
        for y in range(height):
            ratio = y / max(1, height - 1)
            pixels[0, y] = tuple(round(start[i] * (1 - ratio) + end[i] * ratio) for i in range(3))
        return strip.resize((width, height))

    def render_card(self, scene: dict[str, Any], index: int, total: int, title: str) -> Path:
        width, height = self.dimensions
        accent, ink, warm, paper = PALETTES.get(str(self.job["style"]), PALETTES["premium"])
        pale = tuple(min(255, round(channel * 0.14 + 255 * 0.86)) for channel in accent)
        image = self.gradient((width, height), paper, pale)
        draw = ImageDraw.Draw(image, "RGBA")
        unit = min(width, height) / 1080
        margin = int(48 * unit)
        radius = int(34 * unit)

        for seed in range(13):
            x = int((seed * 211 + 73) % width)
            y = int((seed * 317 + 140) % height)
            r = int((18 + (seed % 4) * 12) * unit)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(*accent, 18 + (seed % 3) * 8))

        header_h = int(112 * unit)
        draw.rounded_rectangle((margin, margin, width - margin, margin + header_h), radius=radius, fill=(255, 255, 255, 232), outline=(*accent, 55), width=max(1, int(2 * unit)))
        brand_font = self.font(int(31 * unit), bold=True, text="QWEN VIDEO STUDIO")
        draw.text((margin + int(36 * unit), margin + int(35 * unit)), "QWEN VIDEO STUDIO", font=brand_font, fill=ink)
        pill_text = f"SCENE {index + 1:02d} / {total:02d}"
        pill_font = self.font(int(25 * unit), bold=True, text=pill_text)
        pill_box = draw.textbbox((0, 0), pill_text, font=pill_font)
        pill_w = pill_box[2] - pill_box[0] + int(54 * unit)
        draw.rounded_rectangle((width - margin - pill_w, margin + int(23 * unit), width - margin - int(18 * unit), margin + header_h - int(23 * unit)), radius=int(28 * unit), fill=(*accent, 25))
        draw.text((width - margin - pill_w + int(25 * unit), margin + int(39 * unit)), pill_text, font=pill_font, fill=accent)

        panel_top = margin + header_h + int(56 * unit)
        panel_bottom = height - margin - int(130 * unit)
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle((margin + int(10 * unit), panel_top + int(20 * unit), width - margin + int(10 * unit), panel_bottom + int(20 * unit)), radius=int(48 * unit), fill=(15, 23, 42, 40))
        shadow = shadow.filter(ImageFilter.GaussianBlur(int(22 * unit)))
        image = Image.alpha_composite(image.convert("RGBA"), shadow)
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rounded_rectangle((margin, panel_top, width - margin, panel_bottom), radius=int(48 * unit), fill=(255, 255, 255, 244), outline=(255, 255, 255, 255), width=max(1, int(2 * unit)))

        landscape = width > height
        icon_size = int((270 if not landscape else 245) * unit)
        icon_x = width // 2 if not landscape else margin + int(270 * unit)
        icon_y = panel_top + int((215 if not landscape else 250) * unit)
        draw.ellipse((icon_x - icon_size // 2, icon_y - icon_size // 2, icon_x + icon_size // 2, icon_y + icon_size // 2), fill=(*accent, 25), outline=(*accent, 42), width=max(2, int(3 * unit)))
        emoji = str(scene.get("emoji") or "✨")[:8]
        try:
            emoji_font = self.font(int(170 * unit), text=emoji, emoji=True)
            box = draw.textbbox((0, 0), emoji, font=emoji_font, embedded_color=True)
            draw.text((icon_x - (box[2] - box[0]) / 2, icon_y - (box[3] - box[1]) / 2 - box[1]), emoji, font=emoji_font, embedded_color=True)
        except Exception:
            keyword = str(scene.get("keyword") or scene.get("heading") or "Q")
            badge = keyword[:2].upper()
            badge_font = self.font(int(108 * unit), bold=True, text=badge)
            box = draw.textbbox((0, 0), badge, font=badge_font)
            draw.text((icon_x - (box[2] - box[0]) / 2, icon_y - (box[3] - box[1]) / 2 - box[1]), badge, font=badge_font, fill=accent)

        content_left = margin + int(76 * unit) if not landscape else margin + int(560 * unit)
        content_right = width - margin - int(76 * unit)
        text_top = icon_y + icon_size // 2 + int(76 * unit) if not landscape else panel_top + int(155 * unit)
        heading = str(scene.get("heading") or "Key idea").strip()
        body = str(scene.get("body") or "").strip()
        heading_size = int((72 if not landscape else 68) * unit)
        heading_font = self.font(heading_size, bold=True, text=heading)
        lines = self.fit_lines(draw, heading, heading_font, content_right - content_left, 3)
        y = text_top
        line_gap = int(10 * unit)
        for line in lines:
            box = draw.textbbox((0, 0), line, font=heading_font)
            x = (width - (box[2] - box[0])) / 2 if not landscape else content_left
            draw.text((x, y), line, font=heading_font, fill=ink)
            y += (box[3] - box[1]) + line_gap
        y += int(32 * unit)
        body_font = self.font(int(38 * unit), text=body)
        body_lines = self.fit_lines(draw, body, body_font, content_right - content_left, 6 if not landscape else 5)
        for line in body_lines:
            box = draw.textbbox((0, 0), line, font=body_font)
            x = (width - (box[2] - box[0])) / 2 if not landscape else content_left
            draw.text((x, y), line, font=body_font, fill=(*ink, 215))
            y += (box[3] - box[1]) + int(14 * unit)

        keyword = str(scene.get("keyword") or title).strip()[:36]
        key_font = self.font(int(28 * unit), bold=True, text=keyword)
        key_box = draw.textbbox((0, 0), keyword, font=key_font)
        key_w = min(content_right - content_left, key_box[2] - key_box[0] + int(64 * unit))
        key_x = (width - key_w) / 2 if not landscape else content_left
        key_y = panel_bottom - int(105 * unit)
        draw.rounded_rectangle((key_x, key_y, key_x + key_w, key_y + int(62 * unit)), radius=int(28 * unit), fill=(*warm, 36), outline=(*warm, 75), width=max(1, int(2 * unit)))
        draw.text((key_x + int(32 * unit), key_y + int(15 * unit)), keyword, font=key_font, fill=ink)

        progress_y = height - margin - int(66 * unit)
        bar_left, bar_right = margin + int(20 * unit), width - margin - int(20 * unit)
        draw.rounded_rectangle((bar_left, progress_y, bar_right, progress_y + int(16 * unit)), radius=int(8 * unit), fill=(255, 255, 255, 175))
        draw.rounded_rectangle((bar_left, progress_y, bar_left + (bar_right - bar_left) * (index + 1) / total, progress_y + int(16 * unit)), radius=int(8 * unit), fill=accent)
        title_font = self.font(int(24 * unit), bold=True, text=title)
        title_display = textwrap.shorten(title, width=48, placeholder="…")
        draw.text((bar_left, progress_y - int(42 * unit)), title_display, font=title_font, fill=(*ink, 175))

        target = self.cards / f"{index:03d}.png"
        image.convert("RGB").save(target, quality=95)
        return target

    def render_cards(self, storyboard: dict[str, Any]) -> list[Path]:
        scenes = storyboard["scenes"]
        paths: list[Path] = []
        for index, scene in enumerate(scenes):
            target = self.cards / f"{index:03d}.png"
            if not target.is_file() or target.stat().st_size < 20_000:
                self.render_card(scene, index, len(scenes), str(storyboard["title"]))
            paths.append(target)
            if index % max(1, len(scenes) // 5) == 0:
                self.update("designing", 15 + 20 * (index + 1) / len(scenes), f"Designing premium scene {index + 1}/{len(scenes)}")
        return paths

    @staticmethod
    def srt_time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def write_timeline(self, storyboard: dict[str, Any], cards: list[Path]) -> tuple[Path, Path, str]:
        manifest = self.work / "timeline.ffconcat"
        captions = self.work / "captions.srt"
        scene_duration = self.duration / len(cards)
        manifest_lines = ["ffconcat version 1.0"]
        caption_lines: list[str] = []
        narration_parts: list[str] = []
        cursor = 0.0
        for index, (card, scene) in enumerate(zip(cards, storyboard["scenes"]), start=1):
            safe = card.as_posix().replace("'", "'\\''")
            manifest_lines.extend([f"file '{safe}'", f"duration {scene_duration:.6f}"])
            narration = re.sub(r"\s+", " ", str(scene.get("narration") or scene.get("body") or "")).strip()
            # Enforce the budget the storyboard prompt asked for. Whatever the model
            # returns, the narration that reaches TTS fits the video, so the mixer never
            # has to speed it up past the point of intelligibility.
            narration = self.trim_to_words(narration, self.narration_word_budget(len(cards)))
            narration_parts.append(narration)
            caption_lines.extend([
                str(index),
                f"{self.srt_time(cursor)} --> {self.srt_time(min(self.duration, cursor + scene_duration - 0.08))}",
                narration,
                "",
            ])
            cursor += scene_duration
        safe_last = cards[-1].as_posix().replace("'", "'\\''")
        manifest_lines.append(f"file '{safe_last}'")
        manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        captions.write_text("\n".join(caption_lines), encoding="utf-8")
        narration_text = "\n\n".join(part for part in narration_parts if part)
        (self.work / "narration.txt").write_text(narration_text, encoding="utf-8")
        return manifest, captions, narration_text

    def synthesize(self, text: str, language: str) -> Path:
        output = self.work / "narration.mp3"
        if output.is_file() and output.stat().st_size > 10_000:
            return output
        voice = str(self.job.get("voice") or "auto")
        if voice == "auto":
            voice = "th-TH-PremwadeeNeural" if language == "thai" else "en-US-AriaNeural"
        python_candidates = [
            Path(sys.executable),
            Path(r"C:\Users\EGAT\anaconda3\python.exe"),
            Path(r"C:\Program Files\Python311\python.exe"),
        ]
        errors: list[str] = []
        for python in python_candidates:
            if not python.is_file():
                continue
            try:
                self.run([str(python), "-m", "edge_tts", "--text", text[:28_000], "--voice", voice, "--write-media", str(output)], "Edge TTS", timeout=1200)
                if output.is_file() and output.stat().st_size > 10_000:
                    return output
            except Exception as exc:
                errors.append(str(exc))
                output.unlink(missing_ok=True)

        sapi_text = self.work / "sapi-text.txt"
        sapi_wav = self.work / "narration.wav"
        sapi_script = self.work / "sapi-tts.ps1"
        sapi_text.write_text(text[:28_000], encoding="utf-8")
        sapi_script.write_text(
            "param([string]$TextPath,[string]$OutputPath)\n"
            "Add-Type -AssemblyName System.Speech\n"
            "$speaker=New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$speaker.Rate=0\n"
            "$speaker.SetOutputToWaveFile($OutputPath)\n"
            "$speaker.Speak([IO.File]::ReadAllText($TextPath,[Text.Encoding]::UTF8))\n"
            "$speaker.Dispose()\n",
            encoding="utf-8-sig",
        )
        try:
            self.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(sapi_script), "-TextPath", str(sapi_text), "-OutputPath", str(sapi_wav)], "Windows offline TTS", timeout=1200)
            self.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(sapi_wav), "-c:a", "libmp3lame", "-b:a", "128k", str(output)], "Encode narration", timeout=300)
            self.warnings.append("Edge TTS was unavailable; Windows offline voice was used.")
            return output
        except Exception as exc:
            errors.append(str(exc))
            raise RuntimeError("All TTS engines failed: " + " | ".join(errors[-3:])) from exc

    def probe(self, path: Path) -> dict[str, Any]:
        result = self.run([self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], f"Probe {path.name}", timeout=120)
        return json.loads(result.stdout)

    def audio_duration(self, path: Path) -> float:
        try:
            return float(self.probe(path)["format"]["duration"])
        except Exception:
            return 0.0

    @staticmethod
    def atempo_chain(speed: float) -> str:
        speed = max(0.5, speed)
        factors: list[float] = []
        while speed > 2.0:
            factors.append(2.0)
            speed /= 2.0
        while speed < 0.5:
            factors.append(0.5)
            speed /= 0.5
        factors.append(speed)
        return ",".join(f"atempo={factor:.5f}" for factor in factors)

    def music(self) -> Path:
        output = self.work / "original_background.wav"
        if output.is_file() and output.stat().st_size > 100_000:
            return output
        frequencies = {"premium": (196, 247, 294), "education": (220, 277, 330), "cinematic": (146, 196, 220), "corporate": (174, 220, 261), "playful": (262, 330, 392), "minimal": (164, 220, 247)}
        f1, f2, f3 = frequencies.get(str(self.job["style"]), frequencies["premium"])
        filters = f"[0:a]volume=0.035[a0];[1:a]volume=0.025[a1];[2:a]volume=0.020[a2];[a0][a1][a2]amix=inputs=3:normalize=0,afade=t=in:st=0:d=2,afade=t=out:st={max(0,self.duration-3)}:d=3[a]"
        self.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={f1}:sample_rate=48000:duration={self.duration}",
            "-f", "lavfi", "-i", f"sine=frequency={f2}:sample_rate=48000:duration={self.duration}",
            "-f", "lavfi", "-i", f"sine=frequency={f3}:sample_rate=48000:duration={self.duration}",
            "-filter_complex", filters, "-map", "[a]", "-c:a", "pcm_s16le", str(output),
        ], "Create original background music", timeout=900)
        return output

    def render_visual(self, manifest: Path) -> Path:
        output = self.work / "visual.mp4"
        if output.is_file():
            try:
                if abs(float(self.probe(output)["format"]["duration"]) - self.duration) <= 1.0:
                    return output
            except Exception:
                output.unlink(missing_ok=True)
        width, height = self.dimensions
        extra_x = 100 if width >= height else 70
        extra_y = 70 if width >= height else 125
        visual_filter = (
            f"scale={width+extra_x}:{height+extra_y}:flags=lanczos,"
            f"crop={width}:{height}:x='(in_w-out_w)/2+(in_w-out_w)*0.28*sin(t*0.18)':"
            f"y='(in_h-out_h)/2+(in_h-out_h)*0.25*cos(t*0.15)',fps=30,format=yuv420p"
        )
        self.run([
            self.ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-vf", visual_filter, "-t", str(self.duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-an", "-movflags", "+faststart", str(output),
        ], "Render premium visual track", timeout=max(1800, self.duration * 8))
        return output

    def final_mux(self, visual: Path, narration: Path, music: Path, captions: Path) -> Path:
        target = Path(self.job["output_path"]).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        building = target.with_name(target.stem + ".building.mp4")
        narration_seconds = self.audio_duration(narration)
        speed = narration_seconds / max(1.0, self.duration * 0.93) if narration_seconds > self.duration * 0.95 else 1.0
        # Recorded so validate() can reject an unintelligible render. Measured on the
        # 2026-09-21/22 jobs: 27.9 s of narration squeezed into an 8 s video = 3.75x.
        # Nothing was checking this, so those jobs were recorded as "completed".
        self.narration_speed = speed
        if speed > 1.25:
            self.warnings.append(
                "Narration was compressed %.2fx to fit the requested duration; extend the duration instead." % speed
            )
        tempo = self.atempo_chain(speed)
        filters = (
            f"[1:a]{tempo},volume=1.18,highpass=f=70,acompressor=threshold=0.10:ratio=2:attack=20:release=180,apad,asplit=2[voice_mix][voice_sc];"
            "[2:a]volume=0.24[music];[music][voice_sc]sidechaincompress=threshold=0.018:ratio=9:attack=18:release=360[ducked];"
            "[voice_mix][ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,alimiter=limit=0.94[aout]"
        )
        self.run([
            self.ffmpeg, "-hide_banner", "-y", "-i", str(visual), "-i", str(narration), "-i", str(music), "-i", str(captions),
            "-filter_complex", filters, "-map", "0:v:0", "-map", "[aout]", "-map", "3:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Captions",
            "-metadata", "title=" + str(self.job["title"]), "-metadata", "comment=Created by Qwen Video Studio",
            "-t", str(self.duration), "-movflags", "+faststart", str(building),
        ], "Mix narration, music, captions, and video", timeout=max(1200, self.duration * 5))
        os.replace(building, target)
        return target

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def measure_loudness(self, output: Path) -> dict[str, Any]:
        """Mean/peak level of the final mix.

        An AAC stream with nothing in it satisfies audio_codec_ok, which is how a
        narration-less render reached "completed" on 2026-09-21. Silence measures
        around -91 dB; a real mix on this pipeline measures about -24 dB mean.
        """
        try:
            result = self.run(
                [
                    self.ffmpeg, "-hide_banner", "-nostats", "-i", str(output),
                    "-map", "0:a:0", "-af", "volumedetect", "-f", "null",
                    "NUL" if os.name == "nt" else "/dev/null",
                ],
                "Measure final audio loudness",
                timeout=max(300, int(self.duration) * 2),
            )
        except Exception as exc:  # no audio stream at all, or ffmpeg refused the map
            return {"mean_db": None, "max_db": None, "error": str(exc)[:300]}
        text = result.stderr or ""

        def grab(name: str) -> float | None:
            match = re.search(name + r":\s*(-?\d+(?:\.\d+)?) dB", text)
            return float(match.group(1)) if match else None

        return {"mean_db": grab("mean_volume"), "max_db": grab("max_volume")}

    def narration_asset_seconds(self) -> float:
        """How much synthesized narration exists for this job, in seconds."""
        for name in ("narration.mp3", "narration.wav"):
            candidate = self.work / name
            if candidate.exists():
                seconds = self.audio_duration(candidate)
                if seconds > 0:
                    return seconds
        return 0.0

    def minimum_output_bytes(self, width: int, height: int) -> int:
        """A floor, not a quality signal.

        The previous threshold was max(100_000, duration * 8_000) - lowered on
        2026-09-21 so that a 402 KB file would pass. Bitrate cannot tell a good render
        from a bad one here (the good 10-minute master and the 8-second stub both sit
        near 290 kbps), so this only catches a render that produced almost nothing.
        The checks that actually discriminate are audio_audible_ok and
        narration_coverage_ok below.
        """
        pixels = max(1, int(width) * int(height))
        per_second = max(6_000, pixels // 120)
        return int(max(80_000, self.duration * per_second))

    def validate(self, output: Path, scene_count: int) -> dict[str, Any]:
        data = self.probe(output)
        streams = data.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), {})
        audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
        subtitle = next((item for item in streams if item.get("codec_type") == "subtitle"), {})
        seconds = float(data.get("format", {}).get("duration") or 0)
        width, height = self.dimensions
        loudness = self.measure_loudness(output)
        mean_db = loudness.get("mean_db")
        narration_seconds = self.narration_asset_seconds()
        narration_speed = float(getattr(self, "narration_speed", 1.0) or 1.0)
        checks = {
            "duration_ok": abs(seconds - self.duration) <= 1.0,
            "resolution_ok": video.get("width") == width and video.get("height") == height,
            "video_codec_ok": video.get("codec_name") == "h264",
            "audio_codec_ok": audio.get("codec_name") == "aac",
            "subtitle_ok": subtitle.get("codec_name") == "mov_text",
            "scene_count_ok": scene_count >= 4,
            "file_size_ok": output.stat().st_size > self.minimum_output_bytes(width, height),
            # Added 2026-09-22: the checks that tell a real render from a broken one.
            "audio_audible_ok": mean_db is not None and mean_db > -50.0,
            "narration_coverage_ok": narration_seconds >= max(3.0, self.duration * 0.25),
            "narration_tempo_ok": narration_speed <= 1.25,
        }
        sample_seconds = sorted({min(max(0.5, self.duration * ratio), max(0.5, self.duration - 0.5)) for ratio in (0.08, 0.5, 0.92)})
        preview_paths: list[str] = []
        for index, second in enumerate(sample_seconds, start=1):
            preview = self.previews / f"preview_{index}_{int(second):04d}s.jpg"
            self.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{second:.3f}", "-i", str(output), "-frames:v", "1", "-q:v", "2", str(preview)], f"Create QA preview {index}", timeout=120)
            preview_paths.append(str(preview))
        self.run([self.ffmpeg, "-v", "error", "-sseof", "-5", "-i", str(output), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "NUL" if os.name == "nt" else "/dev/null"], "Decode final media tail", timeout=300)
        report = {
            "status": "completed" if all(checks.values()) else "validation_failed",
            "output": str(output),
            "size_bytes": output.stat().st_size,
            "sha256": self.sha256(output),
            "duration_seconds": seconds,
            "scene_count": scene_count,
            "checks": checks,
            "loudness": loudness,
            "narration_seconds": round(narration_seconds, 2),
            "narration_speed": round(narration_speed, 3),
            "used_storyboard_fallback": any("storyboard model fallback" in str(item).lower() for item in self.warnings),
            "previews": preview_paths,
            "warnings": self.warnings,
            "probe": data,
            "completed_at": now_iso(),
        }
        report_path = self.work / "BUILD-REPORT.json"
        atomic_json(report_path, report)
        if report["status"] != "completed":
            raise RuntimeError("Final video failed QA: " + json.dumps(checks))
        return report

    def remember(self, report: dict[str, Any]) -> None:
        memory = Path(self.job["project_root"]) / "MEMORY.md"
        entry = (
            f"\n\n## {datetime.now().date().isoformat()} - Qwen Video Studio\n\n"
            f"- Status: completed and quality-checked.\n"
            f"- Title: {self.job['title']}\n"
            f"- File: `{report['output']}`\n"
            f"- Format: {self.job['aspect_ratio']}, {self.dimensions[0]}x{self.dimensions[1]}, 30 fps, H.264/AAC, selectable subtitles.\n"
            f"- Duration: {report['duration_seconds']:.2f} seconds; scenes: {report['scene_count']}.\n"
            f"- SHA-256: `{report['sha256']}`\n"
            f"- Video job: `{self.job['id']}`; report: `{self.work / 'BUILD-REPORT.json'}`\n"
        )
        with memory.open("a", encoding="utf-8") as handle:
            handle.write(entry)

    def build(self) -> None:
        self.job["status"] = "running"
        self.job["started_at"] = self.job.get("started_at") or now_iso()
        self.update("thinking", 3, "Thinking: Qwen is creating the storyboard")
        storyboard = self.storyboard()
        self.update("storyboard", 14, f"Storyboard ready with {len(storyboard['scenes'])} scenes")
        cards = self.render_cards(storyboard)
        manifest, captions, narration_text = self.write_timeline(storyboard, cards)
        self.update("voice", 38, "Generating narration and captions")
        narration = self.synthesize(narration_text, str(storyboard.get("language") or self.detect_language()))
        self.update("music", 48, "Creating original background music")
        music = self.music()
        self.update("rendering", 55, "Rendering the visual track; valid checkpoints will be reused after interruption")
        visual = self.render_visual(manifest)
        self.update("mixing", 82, "Mixing narration, music, captions, and final video")
        output = self.final_mux(visual, narration, music, captions)
        self.update("quality_check", 93, "Checking duration, resolution, codecs, audio, subtitles, and ending frames")
        report = self.validate(output, len(cards))
        self.remember(report)
        self.job["status"] = "completed"
        self.job["finished_at"] = now_iso()
        self.job["error"] = None
        self.job["report_path"] = str(self.work / "BUILD-REPORT.json")
        self.job["previews"] = report["previews"]
        self.job["sha256"] = report["sha256"]
        self.job["size_bytes"] = report["size_bytes"]
        self.update("completed", 100, f"Completed: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen Video Studio resumable worker")
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    build: VideoBuild | None = None
    try:
        build = VideoBuild(args.job)
        build.build()
        return 0
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if build is not None:
            build.job["status"] = "failed"
            build.job["error"] = message
            build.job["finished_at"] = now_iso()
            build.update("failed", float(build.job.get("progress_percent") or 0), message)
        print(message, file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
