"""SRT: модель реплики, ресегментация по словам, перенос строк, запись/чтение."""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

MAX_CUE_DURATION = 6.0     # сек, максимум на одну реплику
MAX_CUE_CHARS = 84         # ~2 строки по 42 символа
MIN_CUE_DURATION = 1.0     # короче — неудобно читать
PAUSE_SPLIT = 0.6          # пауза между словами, после которой режем
LINE_WIDTH = 42


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Cue:
    idx: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)   # тайминги слов от Whisper — для подсветки и правки


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(s: str) -> float:
    h, m, rest = s.strip().replace(".", ",").split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def wrap_text(text: str, width: int = LINE_WIDTH) -> str:
    """Разбивает текст максимум на 2 сбалансированные строки."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    words = text.split(" ")
    best, best_diff = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(a) - len(b))
        if best_diff is None or diff < best_diff:
            best, best_diff = (a, b), diff
    if best and max(len(best[0]), len(best[1])) <= width + 10:
        return "\n".join(best)
    return "\n".join(textwrap.wrap(text, width))


_SENT_END = re.compile(r"[.!?…]$")
_CLAUSE_END = re.compile(r"[,;:—–-]$")


def words_to_cues(words: list[Word]) -> list[Cue]:
    """Собирает реплики из слов с таймингами: режет по паузам, концу фразы,
    длине и длительности, чтобы субтитры удобно читались."""
    cues: list[Cue] = []
    buf: list[Word] = []

    def flush():
        if not buf:
            return
        text = " ".join(w.text.strip() for w in buf).strip()
        if text:
            cues.append(Cue(len(cues) + 1, buf[0].start, buf[-1].end, text,
                            [Word(w.start, w.end, w.text.strip()) for w in buf]))
        buf.clear()

    for w in words:
        if not w.text.strip():
            continue
        if buf:
            prev = buf[-1]
            cur_text = " ".join(x.text.strip() for x in buf)
            cur_len = len(cur_text)
            dur_if_added = w.end - buf[0].start
            pause = w.start - prev.end
            if (
                pause >= PAUSE_SPLIT
                or dur_if_added > MAX_CUE_DURATION
                or cur_len + 1 + len(w.text.strip()) > MAX_CUE_CHARS
                or (_SENT_END.search(prev.text.strip()) and cur_len >= 20)
                or (_CLAUSE_END.search(prev.text.strip()) and cur_len >= 55)
            ):
                flush()
        buf.append(w)
    flush()
    return _fix_timings(cues)


def _fix_timings(cues: list[Cue]) -> list[Cue]:
    """Растягивает слишком короткие реплики, не залезая на следующую."""
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else c.end + 2.0
        if c.end - c.start < MIN_CUE_DURATION:
            c.end = min(c.start + MIN_CUE_DURATION, nxt - 0.05)
        # небольшой «хвост», чтобы текст не исчезал мгновенно
        c.end = max(c.end, min(c.end + 0.25, nxt - 0.05))
        if c.end <= c.start:
            c.end = c.start + 0.5
    return cues


def write_srt(cues: list[Cue], path: Path) -> None:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{wrap_text(c.text)}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def read_srt(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        ts_line = lines[1] if "-->" in lines[1] else lines[0]
        text_lines = lines[2:] if "-->" in lines[1] else lines[1:]
        a, b = ts_line.split("-->")
        cues.append(Cue(len(cues) + 1, parse_ts(a), parse_ts(b), " ".join(text_lines).strip()))
    return cues
