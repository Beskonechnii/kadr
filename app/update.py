"""Обновление без Apple Developer: новая версия берётся из GitHub Releases (Kadr.zip),
распаковывается рядом с настройками и подменяет .app после выхода. Скачанное Python'ом
без карантина — Gatekeeper не спросит «Всё равно открыть», как при загрузке браузером."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__, config

log = logging.getLogger(__name__)

REPO = "Beskonechnii/kadr"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
EVERY = 6 * 3600   # как часто спрашивать GitHub, пока приложение запущено
DIR = config.DATA / "update"

available: tuple[str, str] | None = None   # (версия, url zip) — когда есть новее


def _v(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.lstrip("v").split("."))


def check() -> tuple[str, str] | None:
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "Kadr"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    ver = data["tag_name"].lstrip("v")
    url = next((a["browser_download_url"] for a in data.get("assets", []) if a["name"] == "Kadr.zip"), None)
    return (ver, url) if url and _v(ver) > _v(__version__) else None


def watch_releases():
    """Фоновый поток: проверка при запуске и дальше раз в EVERY секунд."""
    global available
    while True:
        try:
            available = check()
            if available:
                log.info("Доступна версия %s", available[0])
        except Exception as e:
            log.warning("Не удалось проверить обновления: %s", e)
        time.sleep(EVERY)


def download(url: str, on_progress: Callable[[int], None]) -> Path:
    """Скачивает и распаковывает Kadr.zip → путь к новому Kadr.app."""
    shutil.rmtree(DIR, ignore_errors=True)
    DIR.mkdir(parents=True)
    zip_path = DIR / "Kadr.zip"
    last = -1

    def hook(blocks, size, total):
        nonlocal last
        pct = min(blocks * size * 100 // total, 100) if total > 0 else 0
        if pct != last:
            last = pct
            on_progress(pct)

    urllib.request.urlretrieve(url, zip_path, hook)
    subprocess.run(["ditto", "-x", "-k", str(zip_path), str(DIR)], check=True)
    zip_path.unlink()
    app = next(DIR.rglob("Kadr.app"), None)
    if not app or not (app / "Contents/MacOS").is_dir():
        raise RuntimeError("В архиве нет Kadr.app")
    return app


def swap_after_exit(new_app: Path, app_path: Path, pid: int = os.getpid()):
    """Отдельный процесс ждёт выхода приложения, подменяет .app и запускает новое.
    Не вышло подменить (нет прав на папку) — показывает скачанное в Finder, пусть перетащат сами."""
    script = f"""
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
if rm -rf "{app_path}" && mv "{new_app}" "{app_path}"; then
  open "{app_path}"
else
  open -R "{new_app}"
  osascript -e 'display notification "Перетащите новый Kadr в «Программы»" with title "Kadr"'
fi
"""
    subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
