"""Распознавание речи локально: mlx-whisper (Apple Silicon, GPU) или faster-whisper (Intel, CPU)."""
from __future__ import annotations

import logging
import os
import re
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import config, media
from .srt_utils import Cue, Word, words_to_cues

log = logging.getLogger(__name__)

_model = None
_name = _path = None   # какая модель загружена и откуда
_model_lock = threading.Lock()
_devnull = open(os.devnull, "w")
# MLX привязывает GPU-stream к потоку, где создана модель: вся работа с ней — в одном потоке
_mlx_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")

# титры из обучающих данных Whisper: на тишине, музыке и шуме игры он «дописывает» их сам.
# Первые никто не произносит; вторые говорят и вживую — их режем, только если модель сама не уверена, что это речь
JUNK = re.compile(r"субтитр\w*\s+(сделал|создавал|подогнал|делал|подготовил|редактировал)|редактор субтитров"
                  r"|корректор\s+[А-ЯЁ]\.|dimatorzok|продолжение следует", re.I)
MAYBE_JUNK = re.compile(r"спасибо за просмотр|подписывайтесь на (наш )?канал|ставьте лайк", re.I)
# пометки вместо речи: «СМЕХ», «[музыка]», «(аплодисменты)» — в субтитрах перевода они лишние
SOUND = re.compile(r"^\W*(смех|музыка|аплодисменты|музыкальная заставка|непонятно)\W*$|^\s*[\[(*].*[\])*]\s*$", re.I)


def junk(text: str, no_speech: float) -> bool:
    w = re.findall(r"\w+", text.lower())
    loop = len(w) >= 4 and len(set(w)) / len(w) <= 0.3   # «a! a! a! a!» — модель зациклилась на музыке
    return bool(loop or JUNK.search(text) or SOUND.search(text) or (no_speech >= 0.5 and MAYBE_JUNK.search(text)))


def missed(spans: list[list[float]], words: list[Word], gap: float = 1.0, least: float = 1.0) -> list[tuple[float, float]]:
    """Где голос есть, а слов нет: Whisper иногда молча пропускает целое 30-секундное окно (на стриме
    из 44 с речи осталось 20). Соседние куски ближе gap — один; меньше least секунд голоса — не берём."""
    out: list[list[float]] = []
    for a, b in spans:
        if any(w.start < b + 0.3 and w.end > a - 0.3 for w in words):
            continue
        if out and a - out[-1][1] < gap:
            out[-1][1:] = [b, out[-1][2] + b - a]
        else:
            out.append([a, b, b - a])
    return [(max(0.0, a - 0.3), b + 0.3) for a, b, voiced in out if voiced >= least]


def local_path(repo: str, patterns: list[str] | None = None) -> str:
    """Путь к скачанной модели. По имени репозитория mlx-whisper и mlx_lm при каждой загрузке ходят
    в сеть проверять обновления: модель грузится к каждому видео, и сбой Hugging Face ронял задачу.
    patterns — те же, с какими модель качали, иначе снимок без README считается неполным."""
    from huggingface_hub import snapshot_download

    if Path(repo).exists():
        return repo
    try:
        return snapshot_download(repo, local_files_only=True, allow_patterns=patterns)
    except Exception:
        return snapshot_download(repo, allow_patterns=patterns)


def get_model():
    """Грузит модель один раз; вызывать при старте, чтобы первая задача не ждала загрузки."""
    if config.WHISPER_BACKEND == "mlx":
        return _mlx_thread.submit(_load).result()
    return _load()


def unload():
    """Освободить память под переводчик (ONE_MODEL). Вызывать в потоке MLX."""
    with _model_lock:
        _unload()


def _unload():
    global _model
    if _model is not None and config.WHISPER_BACKEND == "mlx":
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        _model = ModelHolder.model = None
        mx.clear_cache()


def release():
    """Выгрузить и Whisper, и переводчик: простой или смена нагрузки."""
    from . import translate_local

    if config.WHISPER_BACKEND == "mlx":
        _mlx_thread.submit(lambda: (unload(), translate_local.unload())).result()


def _load():
    global _model, _name, _path
    with _model_lock:
        if _name != config.WHISPER_MODEL:   # нагрузку переключили — модель другая
            _unload()
        if _model is None:
            if config.ONE_MODEL and config.WHISPER_BACKEND == "mlx":
                from . import translate_local

                translate_local.unload()
            log.info("Загружаю Whisper %s (%s)…", config.WHISPER_MODEL, config.WHISPER_BACKEND)
            if config.WHISPER_BACKEND == "mlx":
                import mlx.core as mx
                from mlx_whisper.transcribe import ModelHolder

                _path = local_path(config.WHISPER_MODEL)
                _model = ModelHolder.get_model(_path, mx.float16)
                mx.set_cache_limit(config.MLX_CACHE)
            else:
                from faster_whisper import WhisperModel

                _model = WhisperModel(
                    config.WHISPER_MODEL,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                    cpu_threads=config.WHISPER_THREADS,
                    download_root=config.MODELS_DIR,
                )
            _name = config.WHISPER_MODEL
            log.info("Whisper загружен")
    return _model


def transcribe(audio: Path, on_progress: Callable[[float], None] | None = None,
               hint: str | None = None) -> list[Cue]:
    """Возвращает русские реплики с таймингами. on_progress(0..1) вызывается из потока."""
    get_model()
    if config.WHISPER_BACKEND == "mlx":
        words = _mlx_thread.submit(_transcribe_mlx, audio, on_progress, hint).result()
    else:
        words = _transcribe_faster(audio, on_progress, hint)
    return words_to_cues(words)


def _transcribe_mlx(audio, on_progress, hint) -> list[Word]:
    import importlib

    config.job_qos()

    import tqdm

    # не `import … as`: функция transcribe из __init__ перекрывает одноимённый подмодуль
    mt = importlib.import_module("mlx_whisper.transcribe")

    # mlx-whisper отдаёт прогресс только в tqdm — подменяем его на колбэк
    class Bar(tqdm.tqdm):
        def update(self, n=1):
            super().update(n)
            if on_progress and self.total:
                on_progress(min(self.n / self.total, 1.0))

    mt.tqdm = types.SimpleNamespace(tqdm=lambda **kw: Bar(**kw, file=_devnull))
    # wav уже 16 kHz mono s16le (media.extract_audio); массивом — чтобы mlx не искал ffmpeg в PATH
    import wave

    import numpy as np

    with wave.open(str(audio)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    kw = dict(
        path_or_hf_repo=_path,               # тот же путь, что в _load: ModelHolder не грузит заново
        language=config.SOURCE_LANG,
        task="transcribe",
        word_timestamps=True,
        condition_on_previous_text=False,    # меньше «зацикливаний»
        initial_prompt=hint or None,         # имена/термины помогают распознаванию
        hallucination_silence_threshold=2.0,  # VAD нет — режем «галлюцинации» на тишине так
    )
    words = _words(mt.transcribe(pcm, verbose=False, **kw)["segments"])   # False = включить tqdm
    # второй проход: куски с голосом, где слов нет, — каждый отдельно, со своим окном
    for a, b in missed(media.speech_spans(audio), words):
        found = _words(mt.transcribe(pcm[int(a * 16000):int(b * 16000)], verbose=None, **kw)["segments"], a)
        log.info("Второй проход %.1f–%.1f с: %s", a, b, " ".join(w.text.strip() for w in found) or "—")
        words += found
    return sorted(words, key=lambda w: w.start)


def _words(segments, offset: float = 0.0) -> list[Word]:
    words: list[Word] = []
    for seg in segments:
        if junk(seg["text"], seg.get("no_speech_prob", 0)):
            log.info("Выкинул галлюцинацию: %s", seg["text"].strip())
        elif seg.get("words"):
            words.extend(Word(w["start"] + offset, w["end"] + offset, w["word"], w.get("probability", 1.0))
                         for w in seg["words"])
        elif seg["text"].strip():
            words.append(Word(seg["start"] + offset, seg["end"] + offset, seg["text"]))
    return words


def _transcribe_faster(audio, on_progress, hint) -> list[Word]:
    segments, info = _model.transcribe(
        str(audio),
        language=config.SOURCE_LANG,
        task="transcribe",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        initial_prompt=hint or None,
    )
    duration = info.duration or 1.0
    words: list[Word] = []
    for seg in segments:
        if junk(seg.text, seg.no_speech_prob):
            continue
        if seg.words:
            words.extend(Word(w.start, w.end, w.word, w.probability) for w in seg.words)
        else:
            words.append(Word(seg.start, seg.end, seg.text))
        if on_progress:
            on_progress(min(seg.end / duration, 1.0))
    return words
