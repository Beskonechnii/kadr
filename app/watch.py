"""Следит за inbox/: каждый новый файл → done/<имя>/ (или failed/<имя>/ с error.txt). По одному."""
from __future__ import annotations

import asyncio
import logging
import queue
import re
import shutil
import time
import traceback
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import config, runner
from .transcribe import get_model, release

log = logging.getLogger("kadr")

jobs: queue.Queue[Path] = queue.Queue()
queued: set[Path] = set()
learning: set[Path] = set()   # сырые видео для базы знаний: без прожига, результат — в KNOWLEDGE


def want(p: Path, anywhere: bool = False) -> bool:
    return ((anywhere or p.parent == config.INBOX) and not p.name.startswith(".")
            and p.suffix.lower() in runner.SUPPORTED_EXT)


def enqueue(p: Path, anywhere: bool = False, learn: bool = False):
    """anywhere=True — файл открыт явно (иконка, Finder): исходник остаётся на месте."""
    if want(p, anywhere) and p not in queued:
        queued.add(p)
        if learn:
            learning.add(p)
        jobs.put(p)


class Handler(FileSystemEventHandler):
    def on_created(self, e):
        if not e.is_directory:
            enqueue(Path(e.src_path))

    def on_moved(self, e):
        if not e.is_directory:
            enqueue(Path(e.dest_path))


def wait_stable(p: Path, quiet: float = 3.0) -> bool:
    """Ждёт, пока файл докопируется: размер не меняется quiet секунд."""
    last, since = -1, time.monotonic()
    while True:
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            return False
        if size != last:
            last, since = size, time.monotonic()
        elif size > 0 and time.monotonic() - since >= quiet:
            return True
        time.sleep(0.5)


def next_project(dirs=(config.DONE, config.FAILED)) -> int:
    """Сквозной номер project-n: следующий после самого большого в «Готово» и «Ошибках»."""
    nums = [int(m[1]) for d in dirs if d.exists() for p in d.iterdir()
            if (m := re.fullmatch(r"project-(\d+)", p.name))]
    return max(nums, default=0) + 1


def handle(src: Path):
    if not wait_stable(src):
        return
    ctx_file = runner.context_file(src)
    # из inbox забираем исходник в результат; открытый явно — не трогаем
    take = src.parent == config.INBOX
    ctx, hint = runner.read_context(src)
    learn = src in learning
    n = next_project((config.KNOWLEDGE,)) if learn else next_project()
    out = (config.KNOWLEDGE if learn else config.DONE) / f"project-{n}"
    log.info("[%s] ▶ Начинаю%s", src.name, " (с контекстом)" if ctx else "")
    try:
        with runner.keep_awake(), runner.one_at_a_time():
            asyncio.run(runner.process(src, out, ctx, hint, config.BURN and not learn, number=n))
    except Exception as e:
        log.exception("[%s] ❌ Ошибка", src.name)
        shutil.rmtree(out, ignore_errors=True)
        fail = config.FAILED / f"project-{n}"
        fail.mkdir(parents=True)
        for f in (src, ctx_file) if take else ():
            if f.exists():
                shutil.move(f, fail / f.name)
        (fail / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        runner.notify(f"Ошибка: {src.name} — {runner.human_error(e)}")
        return
    for f in (src, ctx_file) if take else ():
        if f.exists():
            shutil.move(f, out / f.name)
    log.info("[%s] 📁 %s", src.name, out)
    runner.notify(f"Готово: project-{n} ({src.name})" + (" — поправьте в редакторе для базы знаний" if learn else ""))


def main():
    runner.setup_logging()
    runner.check_ready()
    if config.KEEP_MODELS is None:
        get_model()
    try:
        serve()
    except KeyboardInterrupt:
        pass


def serve(hold=lambda: None):
    """Следит за inbox и обрабатывает очередь по одному; hold() — почему брать задачу пока нельзя (None — можно)."""
    for d in (config.INBOX, config.DONE, config.FAILED):
        d.mkdir(parents=True, exist_ok=True)
    obs = Observer()
    obs.schedule(Handler(), str(config.INBOX), recursive=False)
    obs.start()
    for p in sorted(config.INBOX.iterdir(), key=lambda p: p.stat().st_mtime):
        enqueue(p)
    log.info("Слежу за %s — кидай туда видео (Ctrl+C — выход)", config.INBOX)
    try:
        while True:
            try:
                src = jobs.get(timeout=config.KEEP_MODELS)
            except queue.Empty:
                release()   # простой: память — системе, следующее видео подождёт загрузку моделей
                src = jobs.get()
            said = None
            while why := hold():
                if why != said:
                    log.info("⏸ %s", said := why)
                time.sleep(2)
            try:
                handle(src)
            finally:
                queued.discard(src)
                learning.discard(src)
    finally:
        obs.stop()


if __name__ == "__main__":
    main()
