"""Бенч качества: распознавание (WER), нарезка реплик, перевод (chrF, судья Claude), время и память.

  .venv/bin/python tools/bench.py                 # всё, что есть в test/
  .venv/bin/python tools/bench.py --judge         # + оценка перевода Claude (1–5 за реплику, ~1 цент)
  .venv/bin/python tools/bench.py --translator claude test/bench.ru.srt
  .venv/bin/python tools/bench.py --edits         # сколько правок в редакторе на минуту по dev/done

Эталоны: test/<имя>.ru.srt — проверенная расшифровка (+ test/<имя>.mp4 для распознавания,
test/<имя>.en.srt — проверенный перевод, test/<имя>.txt — контекст). Результаты прогона —
в test/.bench/, строка истории — в test/bench-history.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import resource
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, media, runner  # noqa: E402
from app.srt_utils import CPS_MAX, MAX_CUE_CHARS, Cue, read_srt  # noqa: E402
from app.train import align, norm, wer  # noqa: E402  WER — общий с окном «Обучение»

TEST, OUT = ROOT / "test", ROOT / "test" / ".bench"
MEDIA = (".mp4", ".mov", ".m4v", ".mkv", ".m4a", ".mp3", ".wav")


# ---------- метрики ----------
def chrf(ref: str, hyp: str, n: int = 6, beta: float = 2.0) -> float:
    """chrF: F-мера по символьным n-граммам (1..6), без пробелов, как в sacrebleu."""
    ref, hyp = re.sub(r"\s+", "", ref.lower()), re.sub(r"\s+", "", hyp.lower())
    p = r = 0.0
    for k in range(1, n + 1):
        rg, hg = Counter(ref[i:i + k] for i in range(len(ref) - k + 1)), Counter(hyp[i:i + k] for i in range(len(hyp) - k + 1))
        common = sum((rg & hg).values())
        p += common / max(sum(hg.values()), 1)
        r += common / max(sum(rg.values()), 1)
    p, r = p / n, r / n
    return 0.0 if not p + r else (1 + beta**2) * p * r / (beta**2 * p + r)


def seg_stats(cues: list[Cue]) -> dict:
    """Насколько реплики читаются: доля длинных, быстрых, коротких."""
    n = max(len(cues), 1)
    cps = [len(c.text) / max(c.end - c.start, 0.1) for c in cues]
    return {"cues": len(cues),
            "long%": round(100 * sum(len(c.text) > MAX_CUE_CHARS for c in cues) / n),
            "fast%": round(100 * sum(x > CPS_MAX for x in cps) / n),
            "short%": round(100 * sum(c.end - c.start < 1.0 for c in cues) / n)}


def peak_gb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 1)


# ---------- этапы ----------
async def bench_asr(src: Path, ref: list[Cue], hint: str | None) -> dict:
    from app.transcribe import transcribe

    OUT.mkdir(exist_ok=True)
    wav = await media.extract_audio(src, OUT / f"{src.stem}.wav")
    t = time.monotonic()
    cues = await asyncio.to_thread(transcribe, wav, None, hint)
    sec = round(time.monotonic() - t, 1)
    wav.unlink()
    from app.srt_utils import write_srt
    write_srt(cues, OUT / f"{src.stem}.ru.srt")
    ref_w, hyp_w = norm(" ".join(c.text for c in ref)), norm(" ".join(c.text for c in cues))
    ops = align(ref_w, hyp_w)
    errs = Counter(f"{a or '∅'} → {b or '∅'}" for op, a, b in ops if op != "ok")
    return {"wer%": round(100 * wer(ops, len(ref_w)), 1), "asr_s": sec, **seg_stats(cues),
            "errors": errs.most_common(12), "cues_out": cues}


async def bench_tl(name: str, ref_ru: list[Cue], ref_en: list[Cue] | None, ctx: str) -> dict:
    from app.srt_utils import write_srt
    from app.translate import translate_cues

    t = time.monotonic()
    en = await translate_cues(ref_ru, ctx)
    sec = round(time.monotonic() - t, 1)
    OUT.mkdir(exist_ok=True)
    write_srt(en, OUT / f"{name}.en.srt")
    r = {"tl_s": sec, "en_fast%": seg_stats(en)["fast%"], "en_cues": en}
    if confs := [c.conf for c in en if c.conf is not None]:   # только локальный переводчик
        r["en_conf"] = round(sum(confs) / len(confs), 2)
    if ref_en:
        r["chrf"] = round(100 * chrf(" ".join(c.text for c in ref_en), " ".join(c.text for c in en)), 1)
    return r


async def judge(ru: list[Cue], en: list[Cue]) -> dict:
    """Claude ставит 1–5 каждой реплике: смысл, естественность, имена. Дешёво: одна реплика ≈ 30 токенов."""
    from anthropic import AsyncAnthropic

    by_start = {round(c.start, 3): c.text for c in en}
    pairs = [(c.idx, c.text, by_start.get(round(c.start, 3), "")) for c in ru]
    body = "\n".join(f"{i}\nRU: {r}\nEN: {e}" for i, r, e in pairs)
    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, default_headers=(
        {"anthropic-workspace-id": config.ANTHROPIC_WORKSPACE_ID} if config.ANTHROPIC_WORKSPACE_ID else None))
    resp = await client.messages.create(
        model=config.CLAUDE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": (
            "Rate each English subtitle as a translation of the Russian (spoken video, subtitles). "
            "5 = a native speaker would say exactly this, meaning intact; 4 = fine, minor wording; "
            "3 = understandable but stiff or a calque; 2 = meaning distorted or a name/term wrong; "
            "1 = wrong or missing. Answer ONLY with JSON: "
            '{"scores":[{"id":1,"s":5,"why":""},...]} — "why" only for s<=3, max 8 words.\n\n' + body)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    scores = json.loads(text[text.index("{"):text.rindex("}") + 1])["scores"]
    s = [x["s"] for x in scores]
    bad = [(x["id"], x["s"], x.get("why", "")) for x in scores if x["s"] <= 3]
    r = {"judge": round(sum(s) / max(len(s), 1), 2), "judge_bad": bad}
    # ловит ли уверенность переводчика плохие реплики: ранговая корреляция с баллом судьи (около 0 — не ловит)
    conf, by_id = {round(c.start, 3): c.conf for c in en}, {x["id"]: x["s"] for x in scores}
    pairs = [(conf[round(c.start, 3)], by_id[c.idx]) for c in ru
             if conf.get(round(c.start, 3)) is not None and c.idx in by_id]
    if len(pairs) > 5 and len({s for _, s in pairs}) > 1:
        from scipy.stats import spearmanr

        r["conf~judge"] = round(float(spearmanr(*zip(*pairs)).statistic), 2)
    return r


def edits_report():
    rows = []
    for pj in sorted(config.DONE.glob("*/project.json")):
        p = json.loads(pj.read_text(encoding="utf-8"))
        if p.get("edits"):
            rows.append((pj.parent.name, p["edits"], round(p["edits"] / max(p.get("duration", 60) / 60, 0.1), 1)))
    if not rows:
        return print("Правок пока не записано (считаются с этой версии редактора).")
    print("| проект | правок | на минуту |\n|---|---|---|")
    for r in rows:
        print("| {} | {} | {} |".format(*r))
    print(f"\nВ среднем {round(sum(r[2] for r in rows) / len(rows), 1)} правок/мин по {len(rows)} проектам")


# ---------- главное ----------
async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("refs", nargs="*", type=Path, help="test/<имя>.ru.srt (по умолчанию — все)")
    ap.add_argument("--asr", action="store_true", help="только распознавание")
    ap.add_argument("--tl", action="store_true", help="только перевод")
    ap.add_argument("--judge", action="store_true", help="оценка перевода Claude")
    ap.add_argument("--translator", choices=["local", "claude"])
    ap.add_argument("--edits", action="store_true", help="правки в редакторе по dev/done")
    a = ap.parse_args()
    if a.edits:
        return edits_report()
    if a.translator:
        config.TRANSLATOR = a.translator
    if (a.judge or config.TRANSLATOR == "claude") and not config.ANTHROPIC_API_KEY:
        sys.exit("Нужен ANTHROPIC_API_KEY в .env")
    runner.setup_logging()
    do_asr, do_tl = not a.tl, not a.asr
    refs = a.refs or sorted(TEST.glob("*.ru.srt"))
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    results = []
    for ref_path in refs:
        name = ref_path.name.removesuffix(".ru.srt")
        ref_ru = read_srt(ref_path)
        ctx, hint = runner.read_context(ref_path)
        src = next((p for p in TEST.iterdir() if p.stem == name and p.suffix.lower() in MEDIA), None)
        en_path = TEST / f"{name}.en.srt"
        row = {"name": name}
        if do_asr and src:
            print(f"🎧 {name}: распознаю…", flush=True)
            row.update(await bench_asr(src, ref_ru, hint))
        if do_tl:
            print(f"🌍 {name}: перевожу ({config.TRANSLATOR})…", flush=True)
            row.update(await bench_tl(name, ref_ru, read_srt(en_path) if en_path.exists() else None, ctx))
            if a.judge:
                try:
                    row.update(await judge(ref_ru, row["en_cues"]))
                except Exception as e:   # судья — надстройка: без него таблица всё равно нужна
                    print(f"⚠️ Судья не сработал: {runner.human_error(e)}", flush=True)
        results.append(row)

    cols = ["name", "wer%", "asr_s", "cues", "long%", "fast%", "short%", "chrf", "judge", "en_conf", "conf~judge",
            "en_fast%", "tl_s"]
    cols = [c for c in cols if any(c in r for r in results)]
    print(f"\n{date.today()} {rev} · {config.EFFORT} · whisper {config.WHISPER_MODEL.split('/')[-1]} · "
          f"перевод {config.TRANSLATOR}{'' if config.TRANSLATOR == 'claude' else ' ' + config.LOCAL_LLM.split('/')[-1]}"
          f" · пик памяти {peak_gb()} GB\n")
    print("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
    for r in results:
        print("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    for r in results:
        if r.get("errors"):
            print(f"\n{r['name']} — частые ошибки распознавания (эталон → услышано):")
            print("  " + "; ".join(f"{k} ×{v}" if v > 1 else k for k, v in r["errors"]))
        if r.get("judge_bad"):
            print(f"\n{r['name']} — слабые переводы (№, балл, почему):")
            for i, s, why in r["judge_bad"]:
                print(f"  {i}: {s} — {why}")
    print(f"\nРезультаты прогона — в {OUT.relative_to(ROOT)}/")
    with open(TEST / "bench-history.jsonl", "a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"date": str(date.today()), "rev": rev, "effort": config.EFFORT,
                                "translator": config.TRANSLATOR, "peak_gb": peak_gb(),
                                **{k: v for k, v in r.items() if k not in ("errors", "cues_out", "en_cues", "judge_bad")}},
                               ensure_ascii=False) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
