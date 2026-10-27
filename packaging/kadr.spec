# PyInstaller: python -m PyInstaller packaging/kadr.spec (из корня проекта)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

HERE = Path(SPECPATH)
sys.path.insert(0, str(HERE.parent))
from app import __version__  # noqa: E402
FF = HERE / "build/ffmpeg/bin"

datas, binaries, hidden = collect_data_files("mlx_whisper"), [], []
# mlx_lm грузит архитектуры моделей через importlib по имени — PyInstaller их сам не найдёт
for pkg in ("mlx", "mlx_lm", "hf_xet"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
binaries += [(str(FF / "ffmpeg"), "bin"), (str(FF / "ffprobe"), "bin")]
datas += [(str(HERE.parent / "app/editor"), "app/editor")]   # страница редактора, jassub, шрифты

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(HERE.parent)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + ["keyring.backends.macOS", "UserNotifications", "ServiceManagement", "WebKit", "Vision",
                            "mlx_whisper.transcribe"],
    excludes=["torch", "tkinter", "matplotlib", "IPython", "dotenv", "faster_whisper",
              "tensorflow", "jax", "flax"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Kadr", console=False,
          target_arch="arm64", argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Kadr")
app = BUNDLE(
    coll,
    name="Kadr.app",
    icon=str(HERE / "build/Kadr.icns"),
    bundle_identifier="local.kadr.app",
    version=__version__,
    info_plist={
        "CFBundleName": "Kadr",
        "CFBundleDisplayName": "Kadr",
        "LSMinimumSystemVersion": "15.0",    # = минимум колёс mlx и сборки ffmpeg
        "NSHumanReadableCopyright": "Whisper (MIT), FFmpeg (LGPL 2.1), libass (ISC)",
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Видео и аудио",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["public.movie", "public.audio", "public.data"],
        }],
    },
)
