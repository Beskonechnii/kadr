"""Обработка одного файла + общее окружение для cli и watch: логи, контекст, уведомления, caffeinate."""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config, media, pipeline, project
from .srt_utils import read_srt, write_srt
from .translate import translate_cues

log = logging.getLogger("kadr")

MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".ts", ".mts", ".3gp",
             ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}
SUPPORTED_EXT = MEDIA_EXT | {".srt"}


def setup_logging():
    config.LOGS.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(config.LOGS / "kadr.log", maxBytes=2_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    ch.addFilter(lambda r: r.name == "kadr" or r.levelno >= logging.WARNING)
    root.addHandler(fh)
    root.addHandler(ch)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "huggingface_hub", "urllib3", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def check_ready():
    if config.TRANSLATOR == "claude" and not config.ANTHROPIC_API_KEY:
        notify("Не задан ключ Anthropic API")
        raise SystemExit(f"Не задан ANTHROPIC_API_KEY в {config.ROOT / '.env'}")
    log.info("ffmpeg: %s", config.ffmpeg())


def base_name(src: Path) -> str:
    name = src.stem
    return name.removesuffix(".ru") if src.suffix.lower() == ".srt" else name


def context_file(src: Path) -> Path:
    return src.with_name(base_name(src) + ".txt")


def read_context(src: Path, extra: str = "") -> tuple[str, str | None]:
    """(user_context для перевода, hint для распознавания). Свой контекст файла важнее общего."""
    own = " ".join(p for p in (extra.strip(), _read(context_file(src))) if p)
    common = _read(config.GLOBAL_CONTEXT)
    user_context = "\n".join(p for p in (own, common) if p)
    return user_context, (" ".join(p for p in (own, common) if p)[:200] or None)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except (OSError, UnicodeDecodeError):
        log.warning("Не прочитал контекст %s", p)
        return ""


def notify(text: str, title: str = config.APP_NAME):
    # текст через argv — без экранирования кавычек в AppleScript
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 1 of argv) with title (item 2 of argv)",
                    "-e", "end run", text, title], capture_output=True)


def human_error(e: Exception) -> str:
    """Короткое объяснение для уведомления; полный текст — в error.txt и логе."""
    import anthropic

    msg = str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "Неверный ключ Anthropic API — меню → «Ключ Anthropic API…»"
    if "credit balance" in msg:
        return "На балансе Anthropic закончились деньги: console.anthropic.com → Billing"
    if "anthropic-workspace-id" in msg:
        return "Ключ без workspace — укажите Workspace ID в меню"
    if isinstance(e, anthropic.APIConnectionError):
        return "Нет связи с Anthropic — проверьте интернет и закиньте файл ещё раз"
    return msg[:150]


@contextmanager
def keep_awake():
    p = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    try:
        yield
    finally:
        p.terminate()


@contextmanager
def one_at_a_time():
    """Межпроцессный замок: watch и Quick Action не грузят GPU/память одновременно."""
    work = config.WORK_TMP
    work.mkdir(parents=True, exist_ok=True)
    with open(work / "lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Жду, пока закончится другая задача…")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


@dataclass
class Result:
    files: list[Path]
    cues: int = 0
    timings: dict = field(default_factory=dict)


def output_name(fname: str, name: str, n: int) -> str:
    """<имя>.en.mp4 → video-n-en.mp4 и т. д.: в project-n понятные имена вместо имени исходника."""
    return {f"{name}.en.mp4": f"video-{n}-en.mp4",
            f"{name}.subs.mp4": f"video-{n}.mp4",
            f"{name}.subs.mkv": f"video-{n}.mkv",
            f"{name}.en.srt": f"subs-{n}-en.srt",
            f"{name}.ru.srt": f"subs-{n}-ru.srt"}.get(fname, fname)


async def process(src: Path, out_dir: Path, user_context: str, hint: str | None,
                  burn: bool, number: int | None = None) -> Result:
    """Готовит субтитры во временной папке и переносит результат в out_dir.
    number — номер project-n: файлы переименовываются в video-n-en.mp4, subs-n-en.srt и т. д."""
    name = base_name(src)

    async def progress(text: str):
        log.info("[%s] %s", src.name, text)

    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{name}.", dir=config.WORK_TMP))
    proj = None   # проект — только для видео
    try:
        if src.suffix.lower() == ".srt":
            cues = read_srt(src)
            if not cues:
                raise RuntimeError("Не смог прочитать .srt")
            t = time.monotonic()
            await progress(f"🌍 Перевожу на английский… ({len(cues)} реплик)")
            en = await translate_cues(cues, user_context,
                                      lambda p: progress(f"🌍 Перевожу… {int(p * 100)}%"))
            write_srt(en, work / f"{name}.en.srt")
            res = Result([], len(en), {"translate": round(time.monotonic() - t, 1)})
        else:
            r = await pipeline.run(work, src, user_context, progress, hint)
            res = Result([], r.cues, r.extra["timings"])
            if r.is_video:
                proj = project.build(r.ru_cues, r.en_cues, await media.probe(src), r.speech)
            if burn and r.is_video:
                t = time.monotonic()
                await progress("🔥 Прожигаю EN-субтитры в видео…")
                ass = work / f"{name}.en.ass"
                ass.write_text(project.ass(proj), encoding="utf-8")
                await media.burn_subs(src, ass, work / f"{name}.en.mp4")
                ass.unlink()
                res.timings["burn"] = round(time.monotonic() - t, 1)
        rename = (lambda f: output_name(f, name, number)) if number else (lambda f: f)
        for f in sorted(work.iterdir()):
            dst = out_dir / rename(f.name)
            shutil.move(f, dst)
            res.files.append(dst)
        if proj:
            soft = next((rename(f"{name}.subs{e}") for e in (".mp4", ".mkv") if (out_dir / rename(f"{name}.subs{e}")).exists()), None)
            proj["files"] = {"soft": soft, "source": str(src.resolve()), "burned": rename(f"{name}.en.mp4"),
                             "en_srt": rename(f"{name}.en.srt"), "ru_srt": rename(f"{name}.ru.srt")}
            project.save(out_dir / (project.FILE if number else f"{name}.{project.FILE}"), proj)
        await progress(f"✅ Готово: {res.cues} реплик, этапы (сек): {res.timings}")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)
