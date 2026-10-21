"""Пресеты внешнего вида и сборка ASS — один и тот же ASS рисуют и превью (libass в wasm), и прожиг (ffmpeg)."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import config
from .srt_utils import wrap_text

FONTS_DIR = Path(__file__).parent / "editor" / "fonts"
# подпись в окне → (семейство в файле шрифта, жирный, файл). Семейства — ровно как в name-таблице TTF,
# иначе libass молча подставит другой шрифт и превью разойдётся с прожигом
FONTS = {
    "Montserrat Bold": ("Montserrat", True, "Montserrat-Bold.ttf"),
    "Montserrat ExtraBold": ("Montserrat ExtraBold", False, "Montserrat-ExtraBold.ttf"),
    "Montserrat Black": ("Montserrat Black", False, "Montserrat-Black.ttf"),
    "Inter Regular": ("Inter", False, "Inter-Regular.ttf"),
    "Inter Medium": ("Inter Medium", False, "Inter-Medium.ttf"),
    "Inter SemiBold": ("Inter SemiBold", False, "Inter-SemiBold.ttf"),
    "Inter Bold": ("Inter", True, "Inter-Bold.ttf"),
    "Rubik Bold": ("Rubik", True, "Rubik-Bold.ttf"),
    "Oswald Bold": ("Oswald", True, "Oswald-Bold.ttf"),
    "Unbounded Bold": ("Unbounded", True, "Unbounded-Bold.ttf"),
}
FORMATS = ("9:16", "1:1", "16:9")

# size — % короткой стороны кадра (одинаково читается и на 9:16, и на 16:9); outline/shadow — % размера;
# y — низ текста, % высоты от верха, своя для каждого формата; words — сколько слов на экране (0 — вся реплика);
# highlight — цвет текущего слова; karaoke — слово заливается по мере речи
BASE = {"font": "Montserrat Bold", "size": 5.2, "color": "#FFFFFF", "outline": 9, "outline_color": "#000000",
        "shadow": 0, "box": False, "box_color": "#000000", "box_opacity": 60, "upper": False, "words": 0,
        "highlight": None, "karaoke": False, "bilingual": False, "x": 50,
        "y": {"9:16": 74, "1:1": 88, "16:9": 90}}
BUILTIN = {
    "classic": {"name": "Классика"},
    "box": {"name": "Плашка", "font": "Inter SemiBold", "box": True, "outline": 0, "box_opacity": 65},
    "reels": {"name": "Reels", "font": "Montserrat Black", "size": 8, "upper": True, "words": 3,
              "highlight": "#FFD400", "outline": 12, "y": {"9:16": 62, "1:1": 80, "16:9": 85}},
    "minimal": {"name": "Минимал", "font": "Inter Medium", "size": 4.6, "outline": 0, "shadow": 6},
    "karaoke": {"name": "Караоке", "font": "Rubik Bold", "size": 5.6, "karaoke": True, "highlight": "#FFD400"},
    "bilingual": {"name": "Двуязычный", "font": "Inter Bold", "size": 4.8, "bilingual": True},
}
DEFAULTS = {"9:16": "reels", "1:1": "classic", "16:9": "classic"}
USER_FILE = config.DATA / "presets.json"


def fmt_of(w: int, h: int) -> str:
    r = w / h if h else 1
    return "9:16" if r < 0.7 else "16:9" if r > 1.3 else "1:1"


def _user() -> dict:
    try:
        return json.loads(USER_FILE.read_text())
    except (OSError, ValueError):
        return {}


def presets() -> dict:
    """id → полный пресет: встроенные + свои (свои лежат в presets.json рядом с настройками)."""
    out = {}
    for pid, p in {**BUILTIN, **_user().get("custom", {})}.items():
        out[pid] = {**copy.deepcopy(BASE), **copy.deepcopy(p)}
    return out


def defaults() -> dict:
    return {**DEFAULTS, **_user().get("defaults", {})}


def save_user(custom: dict | None = None, default: tuple[str, str] | None = None):
    data = _user()
    if custom:
        data.setdefault("custom", {}).update(custom)
    if default:
        data.setdefault("defaults", {})[default[0]] = default[1]
    config.DATA.mkdir(parents=True, exist_ok=True)
    USER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def resolve(style: dict, fmt: str) -> dict:
    """Стиль проекта = пресет + правки поверх; y — уже для формата кадра."""
    s = {**presets().get(style.get("preset"), presets()["classic"]), **style}
    if isinstance(s["y"], dict):
        s["y"] = s["y"].get(fmt, BASE["y"][fmt])
    return s


def _color(hex_: str, opacity: float = 100) -> str:
    """#RRGGBB → &HAABBGGRR (в ASS альфа обратная: 00 — непрозрачно)."""
    h = hex_.lstrip("#")
    return f"&H{round(255 * (1 - opacity / 100)):02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _tag(hex_: str) -> str:
    """Цвет для тега \\c внутри строки: &HBBGGRR& без альфы."""
    h = hex_.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ts(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _syllables(w: str) -> int:
    # говорят слогами, а не буквами: «strengths» короче «idea»
    return max(1, len(re.findall(r"[aeiouyаеёиоуыэюя]+", w.lower())))


def _timed_words(cue: dict, speech: list | None = None) -> list[tuple[float, float, str]]:
    """Слова перевода с примерными таймингами: своих у английского нет, раскладываем по слогам
    только на отрезки, где в реплике звучит голос, — паузы пропускаем."""
    words = cue["en"].split()
    if not words:
        return []
    s, e = cue["start"], cue["end"]
    spans = [(max(a, s), min(b, e)) for a, b in speech or [] if b > s and a < e]
    if sum(b - a for a, b in spans) < 0.3:   # голоса не нашли (старый проект, шум) — по словам Whisper
        ru = cue.get("words") or []
        a = max(ru[0][0], s) if ru else s
        spans = [(a, min(max(ru[-1][1] if ru else e, a + 0.3), e))]
    lens = [b - a for a, b in spans]
    total, syl = sum(lens), [_syllables(w) for w in words]

    def at(x, end=False):   # x секунд речи от начала реплики → время в видео; начало слова на стыке — уже после паузы
        for (a, _), d in zip(spans, lens):
            if x < d or end and x <= d:
                return a + x
            x -= d
        return spans[-1][1]

    out, acc = [], 0
    for w, n in zip(words, syl):
        x = total * acc / sum(syl)
        acc += n
        out.append((at(x), at(total * acc / sum(syl), True), w))
    return out


def build_ass(cues: list[dict], style: dict, w: int, h: int, speech: list | None = None) -> str:
    s = style
    short = min(w, h)
    size = round(short * s["size"] / 100)
    font, bold, _ = FONTS.get(s["font"], FONTS["Montserrat Bold"])
    upper = (lambda t: t.upper()) if s["upper"] else (lambda t: t)
    # сколько знаков влезает в строку: средняя ширина знака ~0,56 кегля, у капса ~0,68
    fit = int(0.9 * w / (size * (0.68 if s["upper"] else 0.56)))
    width = max(8, min(42, fit))
    if s["box"]:   # BorderStyle 3: OutlineColour — цвет плашки, Outline — её поля
        border, outline, oc = 3, round(size * 0.28), _color(s["box_color"], s["box_opacity"])
    else:
        border, outline, oc = 1, round(size * s["outline"] / 100, 1), _color(s["outline_color"])
    primary, secondary = _color(s["color"]), _color(s["color"])
    if s["karaoke"]:   # в караоке Secondary — ещё не спетое, Primary — залитое
        primary = _color(s["highlight"] or "#FFD400")
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},{secondary},{oc},&H80000000,{-1 if bold else 0},0,0,0,"
        f"100,100,0,0,{border},{outline},{round(size * s['shadow'] / 100, 1)},2,"
        f"{round(w * 0.05)},{round(w * 0.05)},0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pos = f"{{\\an2\\pos({round(w * s['x'] / 100)},{round(h * s['y'] / 100)})}}"
    hl = _tag(s["highlight"]) if s["highlight"] else None
    lines = []

    def event(a, b, text):
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Default,,0,0,0,,{pos}{text}")

    def second(cue):   # двуязычный: русский мельче и приглушённее под английским
        ru = _esc(wrap_text(cue.get("ru") or "", round(width / 0.72))).replace("\n", "\\N")   # мельче — влезает больше
        return (f"\\N{{\\fs{round(size * 0.72)}\\c{_tag('#D9D9D9')}}}{ru}"
                if s["bilingual"] and cue.get("ru") else "")

    for cue in cues:
        if not cue["en"].strip():
            continue
        words = _timed_words(cue, speech)
        groups = [words[i:i + s["words"]] for i in range(0, len(words), s["words"])] if s["words"] else [words]
        for gi, g in enumerate(groups):
            a = g[0][0] if s["words"] else cue["start"]
            # группа держится до следующей, последняя — до конца реплики
            b = groups[gi + 1][0][0] if gi + 1 < len(groups) else cue["end"]
            if s["karaoke"]:
                kt = [f"{{\\kf{max(1, round((we - ws) * 100))}}}{_esc(upper(t))}" for ws, we, t in g]
                lead = max(0, round((g[0][0] - a) * 100))
                event(a, b, (f"{{\\k{lead}}}" if lead else "") + _wrap_tagged(" ".join(kt), width) + second(cue))
            elif hl:
                # одно событие на каждое слово: подсвечено текущее — так выглядят субтитры в CapCut
                plain = [upper(t) for _, _, t in g]
                for k, (ws, we, _) in enumerate(g):
                    nxt = g[k + 1][0] if k + 1 < len(g) else b
                    txt = " ".join(f"{{\\c{hl}}}{_esc(t)}{{\\c{_tag(s['color'])}}}" if j == k else _esc(t)
                                   for j, t in enumerate(plain))
                    event(a if k == 0 else ws, nxt, _wrap_tagged(txt, width) + second(cue))
            else:
                txt = " ".join(upper(t) for _, _, t in g)
                event(a, b, _esc(wrap_text(txt, width)).replace("\n", "\\N") + second(cue))
    return head + "\n".join(lines) + "\n"


def _wrap_tagged(txt: str, width: int) -> str:
    """Перенос строки по видимым знакам: теги {\\c…} в ширину не считаются."""
    plain = re.sub(r"\{[^}]*\}", "", txt)
    wrapped = wrap_text(plain, width)
    if "\n" not in wrapped:
        return txt
    cut = len(wrapped.split("\n")[0].split(" "))   # сколько слов в первой строке
    parts = txt.split(" ")
    return " ".join(parts[:cut]) + "\\N" + " ".join(parts[cut:])
