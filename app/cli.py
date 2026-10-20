"""Разовый прогон: python -m app.cli video.mp4 [--context "..."] [--burn]. Результат — рядом с файлом."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from . import config, runner
from .transcribe import get_model

log = logging.getLogger("subbot")


def main():
    ap = argparse.ArgumentParser(description="RU → EN субтитры для видео")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--context", default="", help="тема, имена, термины")
    ap.add_argument("--burn", action="store_true", help="ещё и MP4 с прожжёнными EN-субтитрами")
    ap.add_argument("--out", type=Path, help="папка для результата (по умолчанию — рядом с файлом)")
    args = ap.parse_args()

    runner.setup_logging()
    runner.check_ready()
    files = [f.resolve() for f in args.files
             if f.suffix.lower() in runner.SUPPORTED_EXT or log.warning("Пропускаю %s", f)]
    failed = 0
    with runner.keep_awake():
        if any(f.suffix.lower() != ".srt" for f in files):
            get_model()
        for src in files:
            ctx, hint = runner.read_context(src, args.context)
            try:
                with runner.one_at_a_time():
                    asyncio.run(runner.process(src, args.out or src.parent, ctx, hint,
                                               args.burn or config.BURN))
                runner.notify(f"Готово: {src.name}")
            except Exception as e:
                failed += 1
                log.exception("[%s] ❌ Ошибка", src.name)
                runner.notify(f"Ошибка: {src.name} — {runner.human_error(e)}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
