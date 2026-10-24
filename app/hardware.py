"""Потянет ли этот Mac: при запуске — не пускаем, если нет; перед каждым видео — ждём, если Mac занят."""
from __future__ import annotations

import ctypes
import platform
import shutil
from pathlib import Path

from . import config

MIN_RAM = min(p["ram"] for p in config.EFFORTS.values())
WORK_GB = 3   # wav, копия с дорожками и прожиг длинного ролика во временной папке

_libc = ctypes.CDLL(None)


def pressure() -> int:
    """Давление памяти macOS: 1 — норма, 2 — предупреждение, 4 — критично."""
    v, n = ctypes.c_int(0), ctypes.c_size_t(4)
    _libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(v), ctypes.byref(n), None, 0)
    return v.value


def thermal() -> int:
    """0 — норма … 3 — критично: macOS уже сама душит процессор."""
    from Foundation import NSProcessInfo

    return NSProcessInfo.processInfo().thermalState()


def free_gb() -> float:
    p = Path(config.MODELS_DIR)
    while not p.exists():   # первый запуск: папки ещё нет, место считаем по диску, где она будет
        p = p.parent
    return shutil.disk_usage(p).free / 2**30


def repo_dir(repo: str) -> Path:
    return Path(config.MODELS_DIR) / "hub" / ("models--" + repo.replace("/", "--"))


def cached(repo: str) -> bool:
    return any(repo_dir(repo).glob("snapshots/*/*"))


def need_gb(effort: str, translator: str | None = None) -> float:
    """Сколько места нужно профилю: недокачанные модели + рабочая папка."""
    p = config.EFFORTS[effort]
    repos = [p["whisper"]] + ([p["llm"]] if (translator or config.TRANSLATOR) == "local" else [])
    return sum(config.MODEL_GB.get(r, 6) for r in repos if not cached(r)) + WORK_GB


def allowed(effort: str) -> bool:
    return config.EFFORTS[effort]["ram"] <= config.RAM_GB


def blocker() -> str | None:
    """Почему Kadr не может работать на этом Mac вообще; None — может.
    Профиль, на который не хватает места, сам понижается до «Эконома», если тот влезает."""
    if config.WHISPER_BACKEND == "mlx" and platform.machine() != "arm64":
        return "Нужен Mac на Apple Silicon (M1 или новее): распознавание и перевод работают на его GPU."
    if config.RAM_GB < MIN_RAM:
        return f"Нужно минимум {MIN_RAM} ГБ оперативной памяти, на этом Mac — {config.RAM_GB} ГБ."
    if free_gb() < need_gb(config.EFFORT) and config.EFFORT != "eco" and free_gb() >= need_gb("eco"):
        config.set_effort("eco")
    if free_gb() < (n := need_gb(config.EFFORT)):
        return (f"Не хватает места на диске: моделям и обработке нужно {n:.0f} ГБ, свободно {free_gb():.1f} ГБ.\n"
                "Освободите место и откройте Kadr снова.")
    return None


def hold() -> str | None:
    """Почему сейчас не стоит брать видео; None — можно. Проверяется перед каждой задачей."""
    if pressure() >= 4:
        from .transcribe import release

        release()   # сначала отдаём свои простаивающие модели — может, этого хватит
        if pressure() >= 4:
            return "Жду: памяти Mac не хватает — закройте тяжёлые программы"
    if thermal() >= 3:
        return "Жду, пока Mac остынет"
    if free_gb() < WORK_GB:
        return f"Жду: на диске меньше {WORK_GB} ГБ — освободите место"
    return None


def model_files(repo: str) -> set[Path]:
    """Файлы модели: snapshots — симлинки, сами веса — в blobs, своих или общих hub/blobs (xet)."""
    d = repo_dir(repo)
    return {f.resolve() for f in d.glob("snapshots/*/*")} | {f for f in d.rglob("*") if f.is_file() and not f.is_symlink()}


def unused_models() -> list[tuple[str, float]]:
    """Скачанные модели, которые текущий профиль и переводчик не используют: (repo, GB)."""
    p = config.EFFORTS[config.EFFORT]
    used = {p["whisper"]} | ({p["llm"]} if config.TRANSLATOR == "local" else set())
    return [(r, sum(f.stat().st_size for f in model_files(r) if f.exists()) / 2**30)
            for r in config.MODEL_GB if r not in used and repo_dir(r).exists()]


def remove_model(repo: str):
    for f in model_files(repo):
        f.unlink(missing_ok=True)
    shutil.rmtree(repo_dir(repo), ignore_errors=True)
