"""Вшитые русские субтитры готового ролика → .ru.srt: эталон для «Обучения» без ручной расшифровки.
Кадры 4 раза в секунду, текст — macOS Vision. Титр отличаем от надписей в кадре не по месту (в роликах он
стоит где угодно), а по поведению: кириллица, держится секунды, а не весь ролик.

    .venv/bin/python tools/ocr_subs.py <видео…> --out dev/ocr
    .venv/bin/python tools/ocr_subs.py <видео…> --ref     # сразу в эталоны «Обучения» (частичные)
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import Vision
from Foundation import NSURL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, train  # noqa: E402
from app.srt_utils import Cue, write_srt  # noqa: E402

FPS = 4
MAX_HOLD = 7.0     # дольше на экране — надпись в кадре, а не титр
MIN_CUES = 8       # меньше — в ролике не субтитры речи, а пара надписей
CYR = re.compile(r"[а-яё]", re.I)


def read(img: Path, req) -> list[str]:
    """Строки кадра сверху вниз; только кириллица и уверенные: латиница в этих роликах — этикетки."""
    h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(img)), None)
    h.performRequests_error_([req], None)
    lines = []
    for o in req.results() or []:
        c = o.topCandidates_(1)[0]
        s = str(c.string()).strip()
        # «ИЕ», «-05В» — обрывки этикеток на коробке: меньше трёх букв или цифры вперемешку с буквами
        if c.confidence() >= 0.5 and len(CYR.findall(s)) >= 3 and not re.search(r"\d", s):
            lines.append((-o.boundingBox().origin.y, s))
    return [t for _, t in sorted(lines)]


def same(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 or b.startswith(a)   # слова дописываются


def frames_lines(video: Path) -> list[list[str]]:
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)   # точный
    req.setRecognitionLanguages_(["ru-RU"])
    req.setUsesLanguageCorrection_(True)
    with tempfile.TemporaryDirectory() as t:
        # 4K Vision читает вчетверо дольше, а титр и в 1920 по длинной стороне крупный
        subprocess.run([config.ffmpeg(), "-nostdin", "-v", "error", "-i", str(video),
                        "-vf", f"fps={FPS},scale='if(gt(ih,iw),-2,min(iw,1920))':'if(gt(ih,iw),min(ih,1920),-2)'",
                        "-q:v", "2", f"{t}/%05d.jpg"], check=True)
        return [read(p, req) for p in sorted(Path(t).glob("*.jpg"))]


def steady(frames: list[list[str]]) -> set[str]:
    """Строки, похожие на них — дольше MAX_HOLD за ролик (в коротком — дольше 15 % ролика, но не меньше 3 с:
    на 17 с ролика пачка в руке видна 7 с, а титр — 2). OCR читает этикетку каждый раз чуть иначе
    («сова любит кофе ЭФИОПИЯ ПАПА», «любит кофе ЭФИОПИЯ»), поэтому считаем похожие и вложенные, а не точные."""
    def near(a: str, b: str) -> bool:
        return (len(a) >= 5 and a in b) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8

    low = [[s.lower() for s in lines] for lines in frames]
    hold = max(3.0, min(MAX_HOLD, 0.15 * len(frames) / FPS))
    return {u for u in {s for lines in low for s in lines}
            if sum(any(near(u, s) for s in lines) for lines in low) / FPS > hold}


def to_cues(frames: list[list[str]]) -> list[Cue]:
    """Подряд идущие кадры с тем же текстом — одна реплика; текст — самый частый вариант (OCR дрожит).
    Строка, которая за ролик набирает больше MAX_HOLD секунд, — надпись на пачке или вывеска, даже если
    рядом с ней меняются титры: её выкидываем до склейки."""
    labels = steady(frames)
    texts = [" ".join(s for s in lines if s.lower() not in labels) for lines in frames]
    runs: list[list] = []   # [первый кадр, последний кадр, варианты]
    for k, t in enumerate(texts):
        if not t:
            continue
        # пропуск до двух кадров — титр мигнул или OCR не прочёл кадр, а не новая реплика
        if runs and runs[-1][1] >= k - 3 and same(runs[-1][2][-1], t):
            runs[-1][1] = k
            runs[-1][2].append(t)
        else:
            runs.append([k, k, [t]])
    cues = []
    for a, b, var in runs:
        if (b - a + 1) / FPS > MAX_HOLD:   # надпись в кадре; титр в один кадр оставляем — монтаж бывает быстрый
            continue
        longest = max(len(v) for v in var)   # слова дописываются — берём полный вариант
        n = Counter(v for v in var if len(v) >= longest * 0.9)
        text = max(n, key=lambda v: (n[v], len(v)))   # поровну — длинный: последнее слово не обрезано
        text = re.sub(r"\s+", " ", re.sub(r"\*[^*]*\*|\[[^]]*\]", "", text)).strip()   # *непонятные звуки* не произносят
        if text:
            cues.append(Cue(len(cues) + 1, a / FPS, (b + 1) / FPS, text))
    return cues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=config.DATA / "ocr")
    ap.add_argument("--ref", action="store_true", help="добавить в эталоны «Обучения» как частичные")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for v in args.videos:
        cache = args.out / f"{v.stem}.frames.json"   # OCR — минуты на ролик; фильтры потом крутятся без него
        if cache.exists():
            frames = json.loads(cache.read_text(encoding="utf-8"))
        else:
            frames = frames_lines(v)
            cache.write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")
        cues = to_cues(frames)
        dst = args.out / f"{v.stem}.ru.srt"
        write_srt(cues, dst)
        print(f"{v.name}: {len(cues)} реплик → {dst}", flush=True)
        if args.ref and len(cues) >= MIN_CUES:
            # частичный: монтажёр подписывает не каждую фразу — лишние слова распознавания не ошибка
            print(f"  эталон {train.add(v, dst, partial=True).name}", flush=True)


if __name__ == "__main__":
    main()
