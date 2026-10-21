"""Перевод реплик RU → EN локальной LLM через MLX — без ключей и оплаты."""
from __future__ import annotations

import asyncio
import logging
import re
import threading

from . import config
from .srt_utils import Cue
from .transcribe import _mlx_thread, local_path  # общий поток: модели MLX привязаны к потоку, где созданы

log = logging.getLogger(__name__)

CHUNK = 40     # маленькой модели легче держать нумерацию на коротких кусках
OVERLAP = 6

SYSTEM = """You translate Russian video subtitles into English.

Rules:
- Natural, idiomatic spoken English, the way a native speaker would say it. No word-for-word calques.
- Exactly one output line per input line, same number, format "N: translation". Never merge, split, skip or reorder lines, even if a sentence continues on the next line.
- Keep each line about as long as the Russian or shorter. Drop filler words (ну, вот, как бы, типа) unless they carry meaning.
- Keep tone: slang stays casual, profanity stays profanity, formal stays formal.
- Names: proper English transliteration (Сергей → Sergey). Brands in their usual English form.
- The text comes from speech recognition and may contain errors; infer the intended meaning.
- If a line is pure noise, output "N:" with nothing after it.
- Output only the numbered lines, nothing else."""

_LINE = re.compile(r"^\s*(\d+)\s*[:.)]\s?(.*)$")

_model = None
_name = None
_lock = threading.Lock()


def unload():
    """Освободить память под Whisper (ONE_MODEL). Вызывать в потоке MLX."""
    with _lock:
        _unload()


def _unload():
    global _model
    if _model is not None:
        import mlx.core as mx

        _model = None
        mx.clear_cache()


def _load():
    global _model, _name
    with _lock:
        if _name != config.LOCAL_LLM:   # нагрузку переключили — модель другая
            _unload()
        if _model is None:
            if config.ONE_MODEL:
                from . import transcribe

                transcribe.unload()
            import mlx.core as mx
            from mlx_lm import load

            log.info("Загружаю переводчик %s…", config.LOCAL_LLM)
            from mlx_lm.utils import DEFAULT_ALLOW_PATTERNS

            _model, _name = load(local_path(config.LOCAL_LLM, DEFAULT_ALLOW_PATTERNS)), config.LOCAL_LLM
            mx.set_cache_limit(config.MLX_CACHE)
            log.info("Переводчик загружен")
    return _model


def warm():
    """Загрузить модель заранее (в потоке MLX, как и Whisper)."""
    _mlx_thread.submit(_load).result()


def _generate(prompt: str, max_tokens: int) -> str:
    from mlx_lm import generate

    model, tok = _load()
    text = tok.apply_chat_template(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        add_generation_prompt=True, tokenize=False, enable_thinking=False)
    return generate(model, tok, text, max_tokens=max_tokens)


def _parse(out: str, want: set[int]) -> dict[int, str]:
    res = {}
    for line in out.splitlines():
        m = _LINE.match(line)
        if m and int(m[1]) in want:
            res[int(m[1])] = m[2].strip()
    return res


def _translate_chunk(chunk: list[Cue], prev: list[Cue], context: str) -> dict[int, str]:
    config.job_qos()
    head = []
    if context:
        head.append(f"About the video (names, terms):\n{context}")
    if prev:
        head.append("Previous lines, for context only — do not translate:\n" +
                    "\n".join(f"{c.idx}: {c.text}" for c in prev))
    result: dict[int, str] = {}
    todo = chunk
    for attempt in range(3):
        body = "\n".join(f"{c.idx}: {c.text}" for c in todo)
        prompt = "\n\n".join(head + [f"Translate these {len(todo)} lines:\n{body}"])
        out = _generate(prompt, max_tokens=80 * len(todo) + 200)
        result.update(_parse(out, {c.idx for c in todo}))
        todo = [c for c in chunk if c.idx not in result]
        if not todo:
            return result
        log.warning("Не хватает %d реплик (попытка %d), повторяю", len(todo), attempt + 1)
    # не перевелось — оставим оригинал, чтобы не терять тайминг
    for c in todo:
        result[c.idx] = c.text
    return result


async def translate_cues_local(cues: list[Cue], user_context: str = "",
                               on_progress=None) -> list[Cue]:
    if not cues:
        return []
    chunks = [cues[i:i + CHUNK] for i in range(0, len(cues), CHUNK)]
    translated: dict[int, str] = {}
    for k, chunk in enumerate(chunks):
        prev = cues[max(0, k * CHUNK - OVERLAP):k * CHUNK]
        translated.update(await asyncio.wrap_future(
            _mlx_thread.submit(_translate_chunk, chunk, prev, user_context)))
        if on_progress:
            await on_progress((k + 1) / len(chunks))
    out = [Cue(c.idx, c.start, c.end, translated.get(c.idx, c.text)) for c in cues]
    out = [c for c in out if c.text.strip()]
    for n, c in enumerate(out, 1):
        c.idx = n
    return out
