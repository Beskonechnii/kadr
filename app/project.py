"""Проект: всё, что нужно редактору и пересборке, — в project.json рядом с результатом.
SRT и прожжённое видео — производные: их можно пересобрать из проекта в любой момент."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config, style
from .srt_utils import Cue, read_srt, write_srt

FILE = "project.json"
PLAYABLE = (".mp4", ".mov", ".m4v")   # что играет <video> в WKWebView (mkv — нет)


def display_size(info: dict) -> tuple[int, int]:
    """Размер кадра как его видят плеер и ffmpeg: у роликов с iPhone в файле 1920×1080 + поворот на 90°."""
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = v.get("width", 0), v.get("height", 0)
    rot = v.get("tags", {}).get("rotate") or next(
        (d["rotation"] for d in v.get("side_data_list", []) if "rotation" in d), 0)
    return (h, w) if abs(int(float(rot))) % 180 == 90 else (w, h)


def probe(path: Path) -> dict:
    out = subprocess.run([config.ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def build(ru: list[Cue], en: list[Cue], info: dict, speech: list | None = None) -> dict:
    w, h = display_size(info)
    fmt = style.fmt_of(w, h)
    # переводчики сохраняют тайминги реплик, но выкидывают пустые и перенумеровывают — связываем по началу
    en_by_start = {round(c.start, 3): c.text for c in en}
    cues = [{"start": c.start, "end": c.end, "ru": c.text, "en": en_by_start.get(round(c.start, 3), ""),
             "words": [[round(x.start, 3), round(x.end, 3), x.text, round(x.prob, 2)] for x in c.words]} for c in ru]
    return {"version": 1, "width": w, "height": h, "format": fmt,
            "duration": float(info.get("format", {}).get("duration", 0)),
            "style": {"preset": style.defaults()[fmt]}, "cues": [c for c in cues if c["en"]], "files": {},
            "speech": speech or []}


def cuts(proj: dict) -> list[list[float]]:
    """Вырезанные куски в исходном времени: по порядку, слитые, в пределах ролика."""
    out: list[list[float]] = []
    for a, b in sorted(proj.get("cuts") or []):
        a, b = max(0.0, float(a)), min(float(proj.get("duration") or b), float(b))
        if b - a <= 0.01:
            continue
        if out and a <= out[-1][1] + 0.01:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def kept(proj: dict) -> list[list[float]]:
    """Что остаётся в ролике, в исходном времени — отрезки для ffmpeg."""
    t, out = 0.0, []
    for a, b in cuts(proj):
        if a > t:
            out.append([t, a])
        t = b
    end = float(proj.get("duration") or 0)
    if end > t + 0.01:
        out.append([t, end])
    return out


def without_cuts(proj: dict) -> dict:
    """Проект во времени собранного ролика: вырезанное выкинуто, остальное сдвинуто.
    Реплика или слово, попавшие в вырезку целиком, пропадают; задетые частично — укорачиваются."""
    cs = cuts(proj)

    def at(t: float) -> float:   # исходное время → время после вырезок; внутри вырезки — её начало
        shift = 0.0
        for a, b in cs:
            if t >= b:
                shift += b - a
            elif t > a:
                return round(a - shift, 3)
            else:
                break
        return round(t - shift, 3)

    def stays(a: float, b: float) -> bool:
        return b - a - sum(max(0.0, min(b, y) - max(a, x)) for x, y in cs) > 0.15

    cues = [{**c, "start": at(c["start"]), "end": at(c["end"]),
             "words": [[at(w[0]), at(w[1]), *w[2:]] for w in c.get("words") or [] if stays(w[0], w[1])]}
            for c in proj["cues"] if stays(c["start"], c["end"])]
    speech = [[at(a), at(b)] for a, b in proj.get("speech") or [] if stays(a, b)]
    return {**proj, "cues": cues, "speech": speech, "cuts": []}


def ass(proj: dict, cut: bool = False) -> str:
    """cut — для сборки ролика без вырезанного; превью играет исходник, ему вырезки не нужны."""
    if cut:
        proj = without_cuts(proj)
    return style.build_ass(proj["cues"], style.resolve(proj["style"], proj["format"]), proj["width"], proj["height"],
                           proj.get("speech"))


def save(pj: Path, proj: dict):
    """pj — сам файл проекта; остальные файлы — по именам рядом с ним."""
    d = pj.parent
    pj.write_text(json.dumps(proj, ensure_ascii=False, indent=1), encoding="utf-8")
    f = proj["files"]
    for lang in ("en", "ru"):
        if f.get(f"{lang}_srt"):
            write_srt([Cue(i, c["start"], c["end"], c[lang]) for i, c in enumerate(proj["cues"], 1)
                       if c[lang].strip()], d / f[f"{lang}_srt"])


def video(d: Path, proj: dict) -> Path | None:
    """Чистое видео (без вшитых субтитров) — для превью и пересборки: копия с дорожками или исходник."""
    f = proj["files"]
    for p in (d / f.get("soft", "-"), Path(f.get("source", "-")), d / Path(f.get("source", "-")).name):
        if p.is_file() and p.suffix.lower() in PLAYABLE:
            return p
    return None


def locate(target: Path) -> Path:
    """Файл проекта: сам .json, project.json в папке project-n или <имя>.project.json рядом с роликом (cli)."""
    if target.is_file():
        return target
    return next(iter(sorted(target.glob("*.project.json"))), target / FILE)


def load(pj: Path) -> dict:
    """Проект из файла; у проектов до 1.2 его нет — собираем из SRT в той же папке (без таймингов слов)."""
    if pj.exists():
        return json.loads(pj.read_text(encoding="utf-8"))
    d = pj.parent
    en = next(iter(sorted(d.glob("*-en.srt")) + sorted(d.glob("*.en.srt"))), None)
    ru = next(iter(sorted(d.glob("*-ru.srt")) + sorted(d.glob("*.ru.srt"))), None)
    soft = next((p for p in sorted(d.iterdir()) if p.suffix.lower() in PLAYABLE
                 and "-en." not in p.name and ".en." not in p.name), None)
    if not (en and soft):
        raise RuntimeError("В папке нет субтитров или видео для редактора")
    proj = build(read_srt(ru) if ru else read_srt(en), read_srt(en), probe(soft))
    if not ru:   # только английские: русский текст неизвестен
        for c in proj["cues"]:
            c["ru"] = ""
    burned = next((p for p in d.iterdir() if p.suffix.lower() in PLAYABLE and ("-en." in p.name or ".en." in p.name)),
                  None)
    proj["files"] = {"soft": soft.name, "en_srt": en.name, "ru_srt": ru.name if ru else None,
                     "burned": burned.name if burned else soft.stem + "-en.mp4"}
    return proj
