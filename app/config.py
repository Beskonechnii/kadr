import ctypes
import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Kadr"
OLD_NAME = "Субтитры"   # имя до 1.1: от него зависят папки, запись в Keychain и Quick Action у тех, кто ставил 1.0
# собрано PyInstaller-ом в .app: настройки — settings.json + Keychain, а не .env
FROZEN = getattr(sys, "frozen", False)


def _migrate_dirs():
    lib = Path.home() / "Library"
    for base in (lib / "Application Support", Path.home() / "Movies", lib / "Logs"):
        if (base / OLD_NAME).is_dir() and not (base / APP_NAME).exists():
            (base / OLD_NAME).rename(base / APP_NAME)
    # Quick Action из 1.0 открывает приложение по старому bundle id — без правки он запустит старое .app или ничего
    wf = lib / "Services/Сделать субтитры.workflow/Contents/document.wflow"
    try:
        wf.write_bytes(wf.read_bytes().replace(b"local.subbot.subtitles", b"local.kadr.app"))
    except OSError:
        pass


if FROZEN:
    _migrate_dirs()
    DATA = Path.home() / "Library/Application Support" / APP_NAME
    HOME = Path.home() / "Movies" / APP_NAME
    INBOX, DONE, FAILED = (HOME / d for d in ("Входящие", "Готово", "Ошибки"))
    LOGS = Path.home() / "Library/Logs" / APP_NAME
else:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    # рабочие папки dev-версии — в dev/, чтобы в корне лежали только приложение и гайд
    DATA = HOME = ROOT / "dev"
    INBOX, DONE, FAILED, LOGS = (DATA / d for d in ("inbox", "done", "failed", "logs"))

WORK_TMP = DATA / ".work"
SETTINGS = DATA / "settings.json"   # то, что меняется из меню приложения; .env (dev) важнее
try:
    for k, v in json.loads(SETTINGS.read_text()).items():
        os.environ.setdefault(k, str(v))
except (OSError, ValueError):
    pass
GLOBAL_CONTEXT = HOME / "context.txt"
FIXES = DATA / "fixes.json"   # замены «запомнить» из редактора: {"было": "стало"}


def fixes() -> dict[str, str]:
    try:
        return json.loads(FIXES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

# кэш моделей HF; ставим до импорта huggingface_hub
os.environ.setdefault("HF_HOME", str(DATA / "models"))
MODELS_DIR = os.environ["HF_HOME"]

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# ключ без привязки к workspace требует явный id (заголовок anthropic-workspace-id)
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
# local — LLM на этом Mac через MLX (бесплатно); claude — Claude API (лучше, нужен ключ и баланс)
TRANSLATOR = os.getenv("TRANSLATOR", "local")
TRANSLATE_PARALLEL = int(os.getenv("TRANSLATE_PARALLEL", "3"))


def load_key():
    """Ключ из Связки ключей — только когда нужен Claude. .app подписан ad-hoc: после каждого обновления
    macOS считает его новым приложением и спрашивает пароль на доступ к записи, даже при локальном переводе."""
    global ANTHROPIC_API_KEY
    if ANTHROPIC_API_KEY or not FROZEN:
        return
    try:
        import keyring.backends.macOS

        kc = keyring.backends.macOS.Keyring()
        key = kc.get_password(APP_NAME, "anthropic")
        if key is None and (key := kc.get_password(OLD_NAME, "anthropic")):
            # старую не удаляем: из нового .app это не вышло, а ошибка срывала загрузку ключа
            kc.set_password(APP_NAME, "anthropic", key)
        ANTHROPIC_API_KEY = key or ""
    except Exception:
        pass


if TRANSLATOR == "claude":
    load_key()

SOURCE_LANG = os.getenv("SOURCE_LANG", "ru")
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "mlx")          # mlx | faster
RAM_GB = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                            text=True).stdout or 0) // 2**30
# Нагрузка: выбирается в окне вручную, по умолчанию — по объёму памяти.
# keep — сколько секунд держать модели в памяти после видео (None — всегда, и грузить при старте);
# both — Whisper и переводчик в памяти одновременно; bg — обработка уступает работе пользователя
EFFORTS = {
    "eco": {"whisper": "mlx-community/whisper-large-v3-turbo", "llm": "mlx-community/gemma-4-e4b-it-4bit",
            "keep": 0, "both": False, "bg": True},
    "balance": {"whisper": "mlx-community/whisper-large-v3-mlx", "llm": "mlx-community/Qwen3.5-9B-MLX-4bit",
                "keep": 180, "both": False, "bg": False},
    "max": {"whisper": "mlx-community/whisper-large-v3-mlx", "llm": "mlx-community/Qwen3.5-9B-MLX-4bit",
            "keep": None, "both": True, "bg": False},
}


def set_effort(name: str):
    global EFFORT, WHISPER_MODEL, LOCAL_LLM, KEEP_MODELS, ONE_MODEL
    p = EFFORTS[name]
    EFFORT, KEEP_MODELS = name, p["keep"]
    # явные модели из .env важнее профиля
    WHISPER_MODEL = os.getenv("WHISPER_MODEL") or (p["whisper"] if WHISPER_BACKEND == "mlx" else "large-v3-turbo")
    LOCAL_LLM = os.getenv("LOCAL_LLM") or p["llm"]
    # на 8 GB Whisper и переводчик вместе не помещаются при любом профиле
    ONE_MODEL = not p["both"] or RAM_GB < 16


_libc = ctypes.CDLL(None)


def job_qos():
    """Вызывать в рабочем потоке перед тяжёлым этапом. bg → QoS utility: поток уступает приложениям
    пользователя, но не заперт на E-ядрах, как PRIO_DARWIN_BG (тот замедлял обработку втрое).
    nice не подходит: вернуть приоритет обратно без root нельзя, а нагрузку переключают на лету."""
    _libc.pthread_set_qos_class_self_np(0x11 if EFFORTS[EFFORT]["bg"] else 0x15, 0)   # UTILITY | DEFAULT


set_effort(os.getenv("EFFORT") if os.getenv("EFFORT") in EFFORTS else "balance" if RAM_GB >= 16 else "eco")

# освобождённые буферы MLX держит у себя для повторного использования; без лимита они копятся
MLX_CACHE = int(float(os.getenv("MLX_CACHE_GB", "1")) * 2**30)

# только для faster-whisper (Intel)
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_THREADS = int(os.getenv("WHISPER_THREADS", "0"))

BURN = os.getenv("BURN", "1").strip() == "1"   # .en.mp4 с субтитрами в кадре — главный результат
BURN_CRF = os.getenv("BURN_CRF", "20")
BURN_PRESET = os.getenv("BURN_PRESET", "veryfast")


def _runs(exe: str) -> bool:
    try:
        return subprocess.run([exe, "-version"], capture_output=True, timeout=20).returncode == 0
    except OSError:
        return False


@lru_cache
def ffmpeg_dir() -> Path:
    """ffmpeg-full (с libass) предпочтительнее; launchd/Finder не видят PATH Homebrew,
    поэтому ищем по абсолютным путям."""
    bundled = Path(getattr(sys, "_MEIPASS", ROOT)) / "bin"
    for d in (os.getenv("FFMPEG_DIR"), bundled, "/opt/homebrew/opt/ffmpeg-full/bin", "/opt/homebrew/bin",
              "/usr/local/opt/ffmpeg-full/bin", "/usr/local/bin"):
        if d and _runs(f"{d}/ffmpeg"):
            return Path(d)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg не найден: brew install ffmpeg")
    return Path(found).parent


def ffmpeg() -> str:
    return str(ffmpeg_dir() / "ffmpeg")


def ffprobe() -> str:
    return str(ffmpeg_dir() / "ffprobe")
