"""Перевод реплик RU → EN через Claude API с сохранением разбиения и таймингов."""
from __future__ import annotations

import asyncio
import logging

from anthropic import AsyncAnthropic

from . import config
from .srt_utils import Cue, prompt_line

log = logging.getLogger(__name__)

CHUNK = 80     # реплик за один запрос
OVERLAP = 6    # реплик контекста из предыдущего куска

TOOL = {
    "name": "submit_translation",
    "description": "Return the English translation for every subtitle cue, one item per input id.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    },
}

SYSTEM = """You are a professional subtitle translator from Russian to English.

Rules:
- Translate each cue into natural, idiomatic spoken English, the way a native speaker would actually say it. Avoid word-for-word calques.
- Keep exactly one output item per input id. Never merge, split, drop or reorder cues, even if a sentence continues across cues: translate each part so the pieces read naturally in sequence.
- Subtitles must be readable: keep each cue about as long as the Russian or shorter (aim for ≤ 84 characters). Drop filler words (ну, вот, как бы, типа, это самое) unless they carry meaning.
- A cue marked "[max N chars]" is on screen too briefly: its English must fit in N characters — rephrase shorter, drop what is non-essential.
- Preserve tone and register: slang stays casual, profanity stays profanity (equivalent strength), formal speech stays formal.
- Names: transliterate properly (e.g. Сергей → Sergey). Keep brand names in their usual English form.
- Colloquial place names and nicknames become the standard English name: Питер → St. Petersburg, Мск → Moscow, Подмосковье → the Moscow region, Нижний → Nizhny Novgorod.
- The text comes from speech recognition and may contain errors; infer the intended meaning from context.
- If a cue is pure noise or unintelligible, return an empty string for it.
- Output only via the submit_translation tool."""


def _client() -> AsyncAnthropic:
    headers = {"anthropic-workspace-id": config.ANTHROPIC_WORKSPACE_ID} if config.ANTHROPIC_WORKSPACE_ID else None
    return AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4, timeout=300,
                          default_headers=headers)


async def _summarize(client: AsyncAnthropic, cues: list[Cue]) -> str:
    """Короткое резюме всего видео — даёт переводчику контекст для каждого куска."""
    full = " ".join(c.text for c in cues)[:60_000]
    resp = await client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                "Below is an auto-generated Russian transcript of a video. In English, write 4-6 "
                "sentences: what the video is about, who speaks (roles, names in correct English "
                "spelling), the tone/register, and a short glossary of recurring names and terms "
                "with their English renderings.\n\n" + full
            ),
        }],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def _translate_chunk(client: AsyncAnthropic, chunk: list[Cue], prev: list[Cue],
                           context: str) -> dict[int, str]:
    parts = []
    if context:
        parts.append(f"<video_context>\n{context}\n</video_context>")
    if prev:
        parts.append("<previous_cues_for_context_only>\n" +
                     "\n".join(f"{c.idx}: {c.text}" for c in prev) +
                     "\n</previous_cues_for_context_only>")
    parts.append("<cues_to_translate>\n" +
                 "\n".join(prompt_line(c) for c in chunk) +
                 "\n</cues_to_translate>")
    parts.append(f"Translate all {len(chunk)} cues (ids {chunk[0].idx}–{chunk[-1].idx}).")

    want = {c.idx for c in chunk}
    result: dict[int, str] = {}
    for attempt in range(3):
        resp = await client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=[TOOL],
            # принудительный tool_choice ("tool"/"any") на Sonnet 5.5+ даёт 400 — auto + указание в SYSTEM
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                for item in block.input.get("translations", []):
                    try:
                        i = int(item["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if i in want:
                        result[i] = str(item.get("text", "")).strip()
        missing = want - result.keys()
        if not missing:
            return result
        log.warning("Не хватает %d реплик (попытка %d), повторяю", len(missing), attempt + 1)
        chunk = [c for c in chunk if c.idx in missing]
        parts[-2] = ("<cues_to_translate>\n" + "\n".join(prompt_line(c) for c in chunk) +
                     "\n</cues_to_translate>")
        parts[-1] = f"Translate all {len(chunk)} cues."
    # если так и не перевелось — оставим оригинал, чтобы не терять тайминг
    for c in chunk:
        result.setdefault(c.idx, c.text)
    return result


async def translate_cues(cues: list[Cue], user_context: str = "",
                         on_progress=None) -> list[Cue]:
    if config.TRANSLATOR == "local":
        from .translate_local import translate_cues_local

        return await translate_cues_local(cues, user_context, on_progress)
    if not cues:
        return []
    client = _client()
    summary = await _summarize(client, cues)
    context = summary
    if user_context:
        context = f"Notes from the video owner: {user_context}\n\n{summary}"
    log.info("Контекст видео: %s", context)

    chunks = [cues[i:i + CHUNK] for i in range(0, len(cues), CHUNK)]
    sem = asyncio.Semaphore(config.TRANSLATE_PARALLEL)
    done = 0
    translated: dict[int, str] = {}

    async def run(k: int, chunk: list[Cue]):
        nonlocal done
        prev = cues[max(0, k * CHUNK - OVERLAP):k * CHUNK]
        async with sem:
            translated.update(await _translate_chunk(client, chunk, prev, context))
        done += 1
        if on_progress:
            await on_progress(done / len(chunks))

    await asyncio.gather(*(run(k, ch) for k, ch in enumerate(chunks)))
    out = [Cue(c.idx, c.start, c.end, translated.get(c.idx, c.text)) for c in cues]
    # пустые (шум) выкидываем
    out = [c for c in out if c.text.strip()]
    for n, c in enumerate(out, 1):
        c.idx = n
    return out


async def translate_one(cues: list[Cue], i: int, user_context: str = "", limit: int | None = None) -> str:
    """Перевести заново одну реплику из редактора; предыдущие — контекстом, как при переводе целиком.
    limit — сколько знаков успевают прочитать за время реплики («Сократить» у быстрых реплик)."""
    prev = cues[max(0, i - OVERLAP):i]
    if limit:
        user_context += (f"\n\nLine {cues[i].idx} is on screen too briefly to read: its English must be at most "
                         f"{limit} characters. Rephrase it shorter and drop anything non-essential.")
    if config.TRANSLATOR == "local":
        from .translate_local import _translate_chunk as local
        from .transcribe import _mlx_thread

        got = await asyncio.wrap_future(_mlx_thread.submit(local, [cues[i]], prev, user_context))
    else:
        got = await _translate_chunk(_client(), [cues[i]], prev, user_context)
    return got.get(cues[i].idx, cues[i].text)
