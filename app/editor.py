"""Окно редактора субтитров: WKWebView со страницей из app/editor, файлы — через схему kadr://.
ASS собирает только Python (app/style.py): превью и прожиг рисуют один и тот же файл."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import objc
from AppKit import NSMakeRect, NSWindow
from Foundation import NSData, NSHTTPURLResponse, NSObject, NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKContentWorld, WKUserScript, WKWebView, WKWebViewConfiguration

from . import config, media, project, style, train
from .srt_utils import Cue

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "editor"
CHUNK = 4 * 2**20   # больше за раз не отдаём: <video> сам дозапросит следующий кусок
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/ttf", ".ttf")

# console.* и ошибки страницы → в наш лог: у WKWebView нет консоли, которую видно без Safari
CONSOLE = """
for (const k of ['log', 'warn', 'error']) {
  const orig = console[k].bind(console);
  console[k] = (...a) => { orig(...a); webkit.messageHandlers.console.postMessage(k + ': ' + a.join(' ')) };
}
addEventListener('error', e => console.error(e.message, e.filename + ':' + e.lineno));
addEventListener('unhandledrejection', e => console.error('promise:', e.reason && (e.reason.stack || e.reason)));
"""


class Scheme(NSObject, protocols=[objc.protocolNamed("WKURLSchemeHandler")]):
    """kadr://app/… — файлы редактора, kadr://project/… — папка проекта, kadr://video/… — чистое видео
    (может лежать вне папки, если файл открывали «на месте»). Своя схема, а не file://: модульные воркеры
    и fetch wasm с file:// WebKit не пускает. Range обязателен — без 206-ответов <video> не перематывается."""

    def initWithRoots_(self, roots):
        self = objc.super(Scheme, self).init()
        self.roots = roots
        return self

    def webView_startURLSchemeTask_(self, web, task):
        url = task.request().URL()
        root = self.roots.get(url.host())
        if isinstance(root, Path) and root.is_file():
            path = root
        else:
            path = (root / url.path().lstrip("/")).resolve() if root else None
            if not path or root.resolve() not in path.parents or not path.is_file():
                return self._send(task, url, 404, {}, b"")
        size = path.stat().st_size
        headers = {"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                   "Accept-Ranges": "bytes", "Cache-Control": "no-store",
                   "Access-Control-Allow-Origin": "*"}   # app и project — разные хосты, для fetch это CORS
        rng = task.request().valueForHTTPHeaderField_("Range")
        with open(path, "rb") as f:
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a or 0)
                end = min(int(b) if b else size - 1, start + CHUNK - 1, size - 1)
                f.seek(start)
                body = f.read(end - start + 1)
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                return self._send(task, url, 206, headers, body)
            return self._send(task, url, 200, headers, f.read())

    def webView_stopURLSchemeTask_(self, web, task):
        pass

    @staticmethod
    def _send(task, url, code, headers, body):
        headers["Content-Length"] = str(len(body))
        task.didReceiveResponse_(NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(
            url, code, "HTTP/1.1", headers))
        task.didReceiveData_(NSData.dataWithBytes_length_(body, len(body)))
        task.didFinish()


class Session:
    """Открытый проект и команды страницы: cmd_<имя>(**данные) → ответ, который уйдёт в JS как JSON."""
    SLOW = {"retranslate", "to_refs"}   # ждут модель или сеть — в отдельном потоке, окно не замирает

    def __init__(self, pj: Path):
        self.pj = pj
        self.proj = project.load(pj)
        self.web = None
        v = project.video(pj.parent, self.proj)
        if not self.proj.get("speech") and v:   # проект до 1.3: паузы в речи ищем по звуку видео
            try:
                with tempfile.TemporaryDirectory() as t:
                    asyncio.run(media.extract_audio(v, Path(t) / "a.wav"))
                    self.proj["speech"] = media.speech_spans(Path(t) / "a.wav")
            except Exception:
                log.exception("Редактор: не нашёл паузы в речи")

    def _take(self, proj):
        if proj is not None:
            self.proj = proj

    def cmd_init(self):
        return {"project": self.proj, "presets": style.presets(), "defaults": style.defaults(),
                "fonts": {k: v[2] for k, v in style.FONTS.items()},   # подпись → файл шрифта
                "video": project.video(self.pj.parent, self.proj) is not None, "title": self.pj.parent.name,
                "ass": project.ass(self.proj)}

    def cmd_ass(self, proj=None):
        self._take(proj)
        return project.ass(self.proj)

    def cmd_save(self, proj=None):
        self._take(proj)
        project.save(self.pj, self.proj)
        return True

    def cmd_save_preset(self, name, values):
        pid = "my-" + "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
        style.save_user(custom={pid: {**values, "name": name}})
        return {"id": pid, "presets": style.presets(), "defaults": style.defaults()}

    def cmd_set_default(self, fmt, preset):
        style.save_user(default=(fmt, preset))
        return style.defaults()

    def cmd_retranslate(self, i, proj=None, limit=None):
        """Перевод одной реплики заново — после правки русского текста или разрезания; limit — «Сократить»."""
        from .translate import translate_one

        self._take(proj)
        cues = [Cue(n + 1, c["start"], c["end"], c.get("ru") or c["en"]) for n, c in enumerate(self.proj["cues"])]
        return asyncio.run(translate_one(cues, i, self.proj.get("context", ""), limit))

    def cmd_remember_fix(self, find, repl):
        """«Запомнить» замену: дальше её делает сам конвейер после распознавания и перевода."""
        config.FIXES.write_text(json.dumps({**config.fixes(), find: repl}, ensure_ascii=False, indent=1), encoding="utf-8")
        return True

    def cmd_to_refs(self, proj=None, sure=False):
        """Поправленный ролик — в эталоны: «Обучение модели» проверит на нём распознавание.
        None — русский не правили: переспросить."""
        self.cmd_save(proj)
        # невычитанный текст — это сам Whisper: проверка покажет ~0% ошибок и ничему не научит
        if not sure and all(train.norm(" ".join(w[2] for w in c.get("words") or [])) == train.norm(c.get("ru") or "")
                            for c in self.proj["cues"]):
            return None
        d, f = self.pj.parent, self.proj["files"]
        v = project.video(d, self.proj)
        if not (v and f.get("ru_srt")):
            raise RuntimeError("Нет видео или русских субтитров")
        name = train.stem(Path(f.get("source") or v))
        train.add(v, d / f["ru_srt"], name=name)
        return name

    def cmd_reveal(self):
        f = self.pj.parent / (self.proj["files"].get("burned") or "")
        subprocess.run(["open", "-R", str(f)] if f.is_file() else ["open", str(self.pj.parent)])
        return True

    def cmd_rebuild(self, proj=None):
        """Пересобрать прожжённое видео из проекта; ход — в window.onRebuild(state, text)."""
        self.cmd_save(proj)
        threading.Thread(target=self._rebuild, daemon=True).start()
        return True

    def _rebuild(self):
        d, proj = self.pj.parent, self.proj
        f = proj["files"]
        try:
            src = project.video(d, proj) or d / (f.get("soft") or "-")
            if not src.is_file():
                raise RuntimeError("Не нашёл исходное видео")
            dst = d / (f.get("burned") or f"{self.pj.stem}-en.mp4")
            ass, tmp = d / ".kadr-rebuild.ass", d / f".kadr-rebuild{dst.suffix}"
            ass.write_text(project.ass(proj, cut=True), encoding="utf-8")
            try:
                # сперва главный результат — -en с субтитрами в кадре; прожиг долгий, ход — в процентах
                asyncio.run(media.burn_subs(src, ass, tmp, project.kept(proj) if project.cuts(proj) else None,
                                            on_progress=lambda p: self._notify("burn", f"{int(p * 100)}%")))
                shutil.move(tmp, dst)   # подменяем готовый файл только целиком
                # копия с отключаемыми дорожками — из SRT после правок, иначе в QuickTime остаются старые субтитры;
                # без вырезок: -c copy режет только по ключевым кадрам, а SRT — в исходном времени
                if f.get("soft") and f.get("en_srt") and f.get("ru_srt"):
                    soft = asyncio.run(media.mux_soft_subs(src, d / f["en_srt"], d / f["ru_srt"], d / ".kadr-rebuild-soft"))
                    shutil.move(soft, d / f["soft"])
                    self._notify("soft", f["soft"])
            finally:
                for p in d.glob(".kadr-rebuild*"):
                    p.unlink(missing_ok=True)
            self._notify("done", dst.name)
        except Exception as e:
            log.exception("Редактор: пересборка не удалась")
            self._notify("error", str(e)[:200])

    def _notify(self, state, text):
        js = f"window.onRebuild && onRebuild({json.dumps(state)}, {json.dumps(text, ensure_ascii=False)})"
        AppHelper.callAfter(lambda: self.web and self.web.evaluateJavaScript_completionHandler_(js, None))


class Bridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler"),
                                  objc.protocolNamed("WKScriptMessageHandlerWithReply")]):
    """JS: await webkit.messageHandlers.kadr.postMessage(JSON-строка {cmd, …}) → JSON-строка с ответом.
    Логика — в Session: у наследника NSObject каждый метод PyObjC считает селектором Objective-C."""

    def initWithSession_(self, session):
        self = objc.super(Bridge, self).init()
        self.session = session
        return self

    def userContentController_didReceiveScriptMessage_(self, ctl, msg):
        log.info("[editor] %s", msg.body())

    def userContentController_didReceiveScriptMessage_replyHandler_(self, ctl, msg, reply):
        data = json.loads(str(msg.body()))
        cmd = data.pop("cmd")

        def run():
            try:
                out = json.dumps(getattr(self.session, "cmd_" + cmd)(**data), ensure_ascii=False)
            except Exception as e:
                log.exception("Редактор: ошибка команды %s", cmd)
                out, err = None, str(e)[:300]
            else:
                err = None
            # ответ — только с главного потока: это блок WebKit
            AppHelper.callAfter(reply, out, err) if cmd in Session.SLOW else reply(out, err)

        threading.Thread(target=run, daemon=True).start() if cmd in Session.SLOW else run()


_open = []   # WKWebView держит обработчики слабо, а NSWindow не даёт повесить на себя атрибут


def open_editor(target: Path, page: str = "index.html") -> tuple[NSWindow, WKWebView]:
    pj = project.locate(target)
    session = Session(pj)
    bridge = Bridge.alloc().initWithSession_(session)
    roots = {"app": STATIC, "project": pj.parent}
    if v := project.video(pj.parent, session.proj):
        roots["video"] = v
    cfg = WKWebViewConfiguration.alloc().init()
    scheme = Scheme.alloc().initWithRoots_(roots)
    cfg.setURLSchemeHandler_forURLScheme_(scheme, "kadr")
    ucc = cfg.userContentController()
    ucc.addScriptMessageHandler_name_(bridge, "console")
    ucc.addScriptMessageHandlerWithReply_contentWorld_name_(bridge, WKContentWorld.pageWorld(), "kadr")
    ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(CONSOLE, 0, False))

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 1280, 800), 1 | 2 | 4 | 8, 2, False)   # + resizable
    win.setTitle_(f"Редактор — {pj.parent.name}")
    win.setReleasedWhenClosed_(False)
    win.setMinSize_((960, 600))
    web = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), cfg)
    web.setAutoresizingMask_(18)   # ширина + высота
    web.setInspectable_(True)      # Safari → Разработка → Kadr: отладка страницы
    session.web = web
    win.contentView().addSubview_(web)
    web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(f"kadr://app/{page}")))
    win.center()
    win.makeKeyAndOrderFront_(None)
    _open.append((win, web, scheme, bridge))
    return win, web


if __name__ == "__main__":
    # dev: .venv/bin/python -m app.editor dev/done/project-3
    import sys

    from AppKit import NSApplication

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    NSApplication.sharedApplication().setActivationPolicy_(0)
    open_editor(Path(sys.argv[1]))
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
