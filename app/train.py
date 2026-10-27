"""Обучение по эталонам: звук ролика + выверенная расшифровка (ручные субтитры). Kadr распознаёт эталон
заново текущими моделями, словарём и заменами и считает WER — видно, помогли ли правки; расхождения
с эталоном — готовые замены. Хранится только 16 kHz wav (~2 MB на минуту), не видео."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

from . import config, media, runner
from .srt_utils import apply_fixes, read_srt, word_fixes, write_srt

REFS = config.DATA / "refs"
SUBS = (".srt", ".vtt")


def norm(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return re.sub(r"[^\w\s]|_", " ", text).split()


def align(ref: list[str], hyp: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Левенштейн по словам с обратным ходом: [('ok'|'sub'|'del'|'ins', ref, hyp), …]."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    ops, i, j = [], n, m
    while i or j:
        if i and j and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            ops.append(("ok" if ref[i - 1] == hyp[j - 1] else "sub", ref[i - 1], hyp[j - 1])); i, j = i - 1, j - 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            ops.append(("del", ref[i - 1], None)); i -= 1
        else:
            ops.append(("ins", None, hyp[j - 1])); j -= 1
    return ops[::-1]


def wer(ops, ref_len: int, partial: bool = False) -> float:
    """partial — эталон не весь текст ролика (вшитые субтитры: монтажёр подписал не каждую фразу):
    лишние слова распознавания там чаще правда, чем ошибка, — считаем только искажённое и пропущенное."""
    return sum(op != "ok" and not (partial and op == "ins") for op, *_ in ops) / max(ref_len, 1)


def stem(p: Path) -> str:
    """Имя без языка и расширения: «влог.ru.vtt» и «влог.mp4» — одна пара."""
    return p.name.split(".")[0]


def refs() -> list[Path]:
    return sorted(d for d in REFS.iterdir() if (d / "ref.ru.srt").exists()) if REFS.exists() else []


def meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": []}


def add(video: Path, subs: Path, partial: bool = False, name: str | None = None) -> Path:
    """Новый эталон: звук и расшифровка. Тот же ролик ещё раз — заменяет старый."""
    cues = read_srt(subs)
    if not cues:
        raise RuntimeError(f"Не прочитал субтитры {subs.name}")
    d = REFS / (name or stem(video))
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    asyncio.run(media.extract_audio(video, d / "audio.wav"))
    write_srt(cues, d / "ref.ru.srt")
    (d / "meta.json").write_text(json.dumps({"source": str(video), "added": str(date.today()),
                                             "minutes": round(cues[-1].end / 60, 1), "partial": partial, "runs": []},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def check(d: Path) -> dict:
    """Распознать эталон как обычное видео (словарь — подсказкой, замены — после) и сравнить с расшифровкой."""
    from .transcribe import transcribe

    ref = read_srt(d / "ref.ru.srt")
    _, hint = runner.read_context(d / "ref.ru.srt")   # свой контекст — ref.txt рядом, плюс общий словарь
    with runner.one_at_a_time():
        cues = apply_fixes(transcribe(d / "audio.wav", None, hint), config.fixes())
    write_srt(cues, d / "hyp.ru.srt")
    ref_w, hyp_w = norm(" ".join(c.text for c in ref)), norm(" ".join(c.text for c in cues))
    ops = align(ref_w, hyp_w)
    m = meta(d)
    part = m.get("partial", False)
    run = {"date": str(date.today()), "model": config.WHISPER_MODEL.split("/")[-1],
           "wer": round(100 * wer(ops, len(ref_w), part), 1),
           "errors": Counter(f"{a or '∅'} → {b or '∅'}" for op, a, b in ops
                             if op != "ok" and not (part and op == "ins")).most_common(8)}
    m["runs"].append(run)
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return run


def suggestions(known: dict[str, str]) -> list[tuple[str, str, int]]:
    """Замены из расхождений распознанного с эталонами: (было, стало, сколько раз), частые — первыми."""
    known = {k.lower() for k in known}
    n = Counter()
    for d in refs():
        if (d / "hyp.ru.srt").exists():
            hyp, ref = (" ".join(c.text for c in read_srt(d / f)) for f in ("hyp.ru.srt", "ref.ru.srt"))
            n.update(p for p in word_fixes(hyp, ref) if p[0].lower() not in known)
    return [(a, b, k) for (a, b), k in n.most_common()]


def report() -> str:
    """Таблица эталонов: WER первой и последней проверки и частые ошибки последней."""
    rows = []
    for d in refs():
        m, runs = meta(d), meta(d)["runs"]
        if not runs:
            rows.append(f"{d.name[:24]:<24} {m.get('minutes', 0):>5}  ещё не проверен")
            continue
        first, last = runs[0]["wer"], runs[-1]["wer"]
        trend = f"{first:>5}% → {last:>5}%" if len(runs) > 1 else f"{last:>5}%"
        errs = "; ".join(f"{e} ×{k}" if k > 1 else e for e, k in runs[-1]["errors"][:4])
        rows.append(f"{d.name[:24]:<24} {m.get('minutes', 0):>5}  {trend}" + (" ≈ частичный" if m.get("partial") else "")
                    + f"\n    {errs}")
    if not rows:
        return "Эталонов пока нет."
    return f"{'Эталон':<24} {'мин':>5}  ошибок в словах (WER)\n" + "\n".join(rows)
