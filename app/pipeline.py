"""Сквозной пайплайн: файл → ru.srt → en.srt (+ видео с отключаемыми дорожками)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import config, media
from .srt_utils import apply_fixes, write_srt
from .transcribe import transcribe
from .translate import translate_cues

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


@dataclass
class JobResult:
    ru_srt: Path
    en_srt: Path
    subs_video: Path | None = None
    is_video: bool = False
    cues: int = 0
    ru_cues: list = field(default_factory=list)
    en_cues: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    speech: list = field(default_factory=list)   # где звучит голос — для подсветки перевода


async def run(job_dir: Path, src: Path, user_context: str, progress: Progress,
              hint: str | None = None) -> JobResult:
    """Пишет результаты в job_dir; result.extra["timings"] — секунды по этапам."""
    loop = asyncio.get_running_loop()
    stem = src.stem or "video"
    timings: dict[str, float] = {}
    t = time.monotonic()

    def lap(name: str):
        nonlocal t
        now = time.monotonic()
        timings[name] = round(now - t, 1)
        t = now

    await progress("🎧 Извлекаю звук…")
    is_video = await media.has_video(src)
    wav = await media.extract_audio(src, job_dir / "audio.wav")
    lap("audio")

    await progress("📝 Распознаю русскую речь… 0%")
    last = {"p": -1}

    def on_tr_progress(p: float):
        pct = int(p * 100)
        if pct - last["p"] >= 5:
            last["p"] = pct
            asyncio.run_coroutine_threadsafe(progress(f"📝 Распознаю русскую речь… {pct}%"), loop)

    hint = (hint or user_context or "")[:200] or None
    ru_cues = apply_fixes(await asyncio.to_thread(transcribe, wav, on_tr_progress, hint), config.fixes())
    if not ru_cues:
        raise RuntimeError("Речь не найдена в файле.")
    ru_srt = job_dir / f"{stem}.ru.srt"
    write_srt(ru_cues, ru_srt)
    lap("transcribe")

    await progress(f"🌍 Перевожу на английский… ({len(ru_cues)} реплик)")

    async def on_tl_progress(p: float):
        await progress(f"🌍 Перевожу на английский… {int(p * 100)}%")

    en_cues = apply_fixes(await translate_cues(ru_cues, user_context, on_tl_progress), config.fixes())
    en_srt = job_dir / f"{stem}.en.srt"
    write_srt(en_cues, en_srt)
    lap("translate")

    result = JobResult(ru_srt=ru_srt, en_srt=en_srt, is_video=is_video, cues=len(en_cues),
                       ru_cues=ru_cues, en_cues=en_cues, extra={"timings": timings})
    if is_video:
        await progress("🎬 Собираю видео с дорожками субтитров…")
        try:
            result.subs_video = await media.mux_soft_subs(src, en_srt, ru_srt, job_dir / f"{stem}.subs")
        except Exception:
            log.exception("Не удалось собрать видео с дорожками")
        lap("subs_video")
    try:
        result.speech = await asyncio.to_thread(media.speech_spans, wav)
    except Exception:
        log.exception("Не удалось найти паузы в речи")
    try:
        wav.unlink()
    except OSError:
        pass
    return result
