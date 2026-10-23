"""Обёртки над ffmpeg/ffprobe."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


async def _run(*args: str) -> str:
    # QoS потока дочерний процесс не наследует — в «Экономе» зажимаем ffmpeg явно
    clamp = ["/usr/sbin/taskpolicy", "-c", "utility"] if config.EFFORTS[config.EFFORT]["bg"] else []
    proc = await asyncio.create_subprocess_exec(
        *clamp, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        tail = err.decode(errors="replace")[-1500:]
        raise RuntimeError(f"{args[0]} завершился с ошибкой:\n{tail}")
    return out.decode(errors="replace")


async def probe(path: Path) -> dict:
    out = await _run(config.ffprobe(), "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(path))
    return json.loads(out)


async def has_video(path: Path) -> bool:
    info = await probe(path)
    return any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1
               for s in info.get("streams", []))


async def extract_audio(src: Path, dst: Path) -> Path:
    """16 kHz mono WAV — то, что ест Whisper."""
    await _run(config.ffmpeg(), "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(dst))
    return dst


def speech_spans(wav: Path) -> list[list[float]]:
    """Где в записи звучит голос: [[начало, конец], …]. Whisper тянет слова через паузы, а перевод
    раскладывается по этим отрезкам — иначе английское слово загорается в тишину."""
    import numpy as np
    x = np.frombuffer(wav.read_bytes()[44:], np.int16).astype(np.float32)   # 16 kHz mono после extract_audio
    fr = x[:len(x) // 320 * 320].reshape(-1, 320)                           # кадры по 20 мс
    spec = np.abs(np.fft.rfft(fr * np.hanning(320), axis=1)) ** 2
    db = 10 * np.log10(spec[:, 4:71].sum(1) + 1)    # 200–3500 Гц: голос, без гула и шипения
    lo, hi = np.percentile(db, [10, 95])
    on = db > lo + 0.4 * (hi - lo)                  # порог от уровня самой записи: тихий микрофон, музыка фоном
    spans: list[list[float]] = []
    for i in np.flatnonzero(on):
        t = float(i) * 0.02
        if spans and t - spans[-1][1] < 0.2:        # провалы короче 200 мс — между слогами, не пауза
            spans[-1][1] = t + 0.02
        else:
            spans.append([t, t + 0.02])
    return [[round(float(a), 2), round(float(b), 2)] for a, b in spans if b - a >= 0.1]


# то, что QuickTime/iPhone играют из MP4 без перекодирования; остальное — в MKV
MP4_VIDEO = {"h264", "hevc"}
MP4_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3"}


async def mux_soft_subs(video: Path, en_srt: Path, ru_srt: Path, dst: Path) -> Path:
    """Исходное видео/аудио без перекодирования + две отключаемые дорожки субтитров.
    dst без расширения: MP4 (QuickTime, iPhone), а если кодеки там не играют — MKV (IINA, VLC)."""
    streams = (await probe(video))["streams"]
    v = next((s["codec_name"] for s in streams if s.get("codec_type") == "video"), None)
    a = {s["codec_name"] for s in streams if s.get("codec_type") == "audio"}
    mp4 = v in MP4_VIDEO and a <= MP4_AUDIO
    out = dst.with_name(dst.name + (".mp4" if mp4 else ".mkv"))
    extra = ["-c:s", "mov_text", "-movflags", "+faststart"] if mp4 else ["-c:s", "srt"]
    if mp4 and v == "hevc":
        extra += ["-tag:v", "hvc1"]   # иначе QuickTime не откроет HEVC в MP4
    await _run(
        config.ffmpeg(), "-y", "-i", str(video), "-i", str(en_srt), "-i", str(ru_srt),
        "-map", "0:v:0", "-map", "0:a?", "-map", "1:0", "-map", "2:0", "-c", "copy", *extra,
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
        "-metadata:s:s:1", "language=rus", "-metadata:s:s:1", "title=Русский",
        "-disposition:s:0", "default", "-disposition:s:1", "0",
        str(out),
    )
    return out


def _escape_filter_path(p: Path) -> str:
    # экранирование пути для фильтра subtitles=
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


async def has_filter(name: str) -> bool:
    out = await _run(config.ffmpeg(), "-hide_banner", "-filters")
    return any(line.split()[1:2] == [name] for line in out.splitlines())


async def has_encoder(name: str) -> bool:
    out = await _run(config.ffmpeg(), "-hide_banner", "-encoders")
    return any(line.split()[1:2] == [name] for line in out.splitlines())


async def burn_subs(video: Path, ass: Path, dst: Path, keep: list[list[float]] | None = None) -> Path:
    """MP4 с субтитрами, вшитыми в картинку. На Mac кодирует аппаратно через VideoToolbox.
    ASS собран из пресета (app/style.py), шрифты — свои из fontsdir: тот же рендер, что в превью редактора.
    keep — отрезки исходника, которые остаются (монтаж по тексту): склеиваются в один ролик, ASS уже в его времени."""
    if not await has_filter("subtitles"):
        raise RuntimeError(f"{config.ffmpeg()} собран без libass — прожиг невозможен (нужен ffmpeg-full)")
    from .style import FONTS_DIR

    vf = f"subtitles='{_escape_filter_path(ass)}':fontsdir='{_escape_filter_path(FONTS_DIR)}'"
    if keep:
        audio = any(s.get("codec_type") == "audio" for s in (await probe(video))["streams"])
        parts, cat = [], ""
        for k, (a, b) in enumerate(keep):
            parts.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{k}]")
            cat += f"[v{k}]"
            if audio:
                parts.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{k}]")
                cat += f"[a{k}]"
        parts.append(f"{cat}concat=n={len(keep)}:v=1:a={int(audio)}[cv]" + ("[ca]" if audio else ""))
        parts.append(f"[cv]{vf}[vo]")
        maps = ["-filter_complex", ";".join(parts), "-map", "[vo]", *(["-map", "[ca]"] if audio else [])]
    else:
        # только картинка и звук: у копии с дорожками субтитров их не нужно тащить в прожиг
        maps = ["-map", "0:v:0", "-map", "0:a:0?", "-vf", vf]
    x264 = ["-c:v", "libx264", "-preset", config.BURN_PRESET, "-crf", config.BURN_CRF]
    vt = ["-c:v", "h264_videotoolbox", "-q:v", "65"]
    for codec in ([vt, x264] if await has_encoder("h264_videotoolbox") else [x264]):
        try:
            await _run(
                config.ffmpeg(), "-y", "-i", str(video), *maps, *codec,
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(dst),
            )
            return dst
        except RuntimeError:
            if codec is x264:
                raise
            log.warning("h264_videotoolbox не сработал, перекодирую через libx264")
    return dst
