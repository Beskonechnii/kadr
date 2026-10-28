"""Окно приложения: перетащить видео → прогресс → «Показать в Finder». Плюс «Входящие» в фоне."""
from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import objc
from AppKit import (NSAlert, NSApplication, NSBezierPath, NSBox, NSButton, NSColor,
                    NSDragOperationCopy, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSImage, NSImageSymbolConfiguration, NSLevelIndicator,
                    NSMakeRect,
                    NSMenu, NSMenuItem, NSOpenPanel, NSPasteboardTypeFileURL,
                    NSPasteboardURLReadingFileURLsOnlyKey, NSPopUpButton, NSProgressIndicator, NSScrollView, NSSegmentedControl,
                    NSSecureTextField, NSTableColumn, NSTableView, NSTextField, NSTextView, NSView, NSWindow)
from Foundation import NSURL, NSAttributedString, NSBundle, NSInsetRect, NSObject, NSTimer
from PyObjCTools import AppHelper

from . import __version__, config, hardware, project, runner, train, transcribe, translate_local, update, watch
from .hardware import pressure
from .srt_utils import write_srt
from .transcribe import get_model, release

log = logging.getLogger("kadr")

BUNDLE_ID = "local.kadr.app"
TRANSLATORS = {"local": "На этом Mac — бесплатно",
               "claude-sonnet-5-5": "Claude Sonnet 5.5 — точнее, нужен ключ",
               "claude-opus-5-5": "Claude Opus 5.5 — лучше всех, ×2 к цене"}

EFFORTS = {"eco": "Эконом — лёгкие модели, Mac не тормозит",
           "balance": "Баланс — точнее, память освобождается",
           "max": "Максимум — модели всегда наготове"}
PRESSURE = {1: "в норме", 2: "под нагрузкой", 4: "на пределе"}

status = {"text": "Запускаюсь…", "busy": False, "last": None, "update": "", "quit": False}


class _Usage(ctypes.Structure):   # rusage_info_v0 из <libproc.h>
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user", "system", "idle_wkups", "interrupt_wkups", "pageins", "wired", "resident", "footprint",
        "start", "exit")]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_libc = ctypes.CDLL(None)
_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))


def usage() -> tuple[float, float]:
    """(память в ГБ, секунды ЦП). Память — footprint, как в Мониторе системы: RSS не видит GPU-буферы MLX.
    Время ЦП в rusage — в тиках mach (на Apple Silicon это не наносекунды)."""
    u = _Usage()
    _libc.proc_pid_rusage(os.getpid(), 0, ctypes.byref(u))
    return u.footprint / 2**30, (u.user + u.system) * _tb.numer / _tb.denom / 1e9


def prefetch():
    """Докачать недостающие модели на диск (в память не грузит): первое видео не ждёт сеть."""
    if config.WHISPER_BACKEND != "mlx":
        return
    from huggingface_hub import snapshot_download
    from mlx_lm.utils import DEFAULT_ALLOW_PATTERNS

    repos = [("распознавания", config.WHISPER_MODEL, None)]
    if config.TRANSLATOR == "local":
        repos.append(("перевода", config.LOCAL_LLM, DEFAULT_ALLOW_PATTERNS))
    for what, repo, patterns in repos:
        try:
            snapshot_download(repo, local_files_only=True, allow_patterns=patterns)
        except Exception:
            if not status["busy"]:
                status["text"] = f"Загружаю модель {what}…"
            snapshot_download(repo, allow_patterns=patterns)
    if not status["busy"]:
        status["text"] = "Жду видео"


def warm():
    """«Максимум»: модели в памяти заранее, первое видео не ждёт загрузку."""
    if not status["busy"]:
        status["text"] = "Загружаю в память…"
    get_model()
    if config.TRANSLATOR == "local" and not config.ONE_MODEL:
        translate_local.warm()
    if not status["busy"]:
        status["text"] = "Жду видео"


def apply_effort():
    """После смены нагрузки или переводчика: докачать модели, затем прогреть или освободить память."""
    try:
        prefetch()
        if config.KEEP_MODELS is None:
            warm()
        elif not status["busy"]:
            release()
    except Exception as e:
        log.exception("Не удалось применить нагрузку")
        status["text"] = f"❌ {str(e)[:80]}"


class StatusHandler(logging.Handler):
    """Строки прогресса из runner/watch → в окно; отдельного канала прогресса нет."""

    def emit(self, record):
        if record.name != "kadr":
            return
        msg = record.getMessage()
        if "📁 " in msg:
            status["last"] = Path(msg.split("📁 ", 1)[1])
        # тайминги этапов нужны в журнале, а не в окне
        status["text"] = (msg.split("] ", 1)[-1] if msg.startswith("[") else msg).split(", этапы")[0]
        status["busy"] = msg.startswith("[") and not any(m in msg for m in "✅❌📁")


def save_settings(**kw):
    try:
        data = json.loads(config.SETTINGS.read_text())
    except (OSError, ValueError):
        data = {}
    data.update(kw)
    config.DATA.mkdir(parents=True, exist_ok=True)
    config.SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def notify(text: str, title: str = config.APP_NAME):
    """Уведомление от имени приложения; вне .app (dev) — через osascript."""
    try:
        from UserNotifications import (UNMutableNotificationContent, UNNotificationRequest,
                                       UNUserNotificationCenter)

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(text)
        req = UNNotificationRequest.requestWithIdentifier_content_trigger_(str(id(content)), content, None)
        UNUserNotificationCenter.currentNotificationCenter().addNotificationRequest_withCompletionHandler_(
            req, None)
    except Exception:
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "display notification (item 1 of argv) with title (item 2 of argv)",
                        "-e", "end run", text, title], capture_output=True)


def models_size_gb() -> float:
    return sum(f.stat().st_size for f in Path(config.MODELS_DIR).rglob("*") if f.is_file()) / 2**30


def choose(exts, many: bool = True, title: str = "") -> list[Path]:
    panel = NSOpenPanel.openPanel()
    panel.setAllowsMultipleSelection_(many)
    panel.setAllowedFileTypes_([e.lstrip(".") for e in exts])
    panel.setMessage_(title)
    return [Path(u.path()) for u in panel.URLs()] if panel.runModal() == 1 else []


def pick_files(learn: bool = False):
    submit(choose(runner.MEDIA_EXT if learn else runner.SUPPORTED_EXT), learn)


def submit(paths, learn: bool = False):
    """В очередь. Файл, который уже делали, — предложить открыть готовый проект, а не гонять модели заново."""
    idx = [] if learn else project.index((config.DONE,))
    for p in paths:
        d = project.made(p, idx) if p.suffix.lower() in runner.MEDIA_EXT else None
        if d and choice(f"{p.name} уже готов — {d.name}", "Открыть готовый в редакторе или распознать заново?",
                        "Открыть", "Распознать заново"):
            NSApplication.sharedApplication().delegate().open_editor(d)
        else:
            watch.enqueue(p, anywhere=True, learn=learn)


def message(title: str, text: str):
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    a.runModal()


def choice(title: str, text: str, yes: str, no: str) -> bool:
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    a.addButtonWithTitle_(yes)
    a.addButtonWithTitle_(no)
    return a.runModal() == 1000


def ask(title: str, message: str, default: str = "", secure: bool = False) -> str | None:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Сохранить")
    alert.addButtonWithTitle_("Отмена")
    field = (NSSecureTextField if secure else NSTextField).alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
    field.setStringValue_(default)
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    return field.stringValue().strip() if alert.runModal() == 1000 else None   # 1000 = первая кнопка


def label(text, frame, size=13, color=None, bold=False):
    f = NSTextField.labelWithString_(text)
    f.setFrame_(frame)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color:
        f.setTextColor_(color)
    return f


class DropView(NSView):
    """Зона «перетащите видео сюда»; клик — выбор файлов. learn — сырые видео для базы знаний."""

    def initWithFrame_(self, frame):
        self = objc.super(DropView, self).initWithFrame_(frame)
        if self is not None:
            self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
            self.hover = self.learn = False
            self.on_drop = None   # своя обработка файлов вместо очереди (окно «Обучение»)
            self.texts = ("Перетащите видео сюда", "или нажмите, чтобы выбрать файлы")
        return self

    def _set_hover(self, v):
        self.hover = v
        self.setNeedsDisplay_(True)

    def draggingEntered_(self, sender):
        self._set_hover(True)
        return NSDragOperationCopy

    def draggingExited_(self, sender):
        self._set_hover(False)

    def performDragOperation_(self, sender):
        urls = sender.draggingPasteboard().readObjectsForClasses_options_(
            [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        self._set_hover(False)
        # после возврата: модальный вопрос «уже готов» посреди перетаскивания подвешивает Finder
        AppHelper.callAfter(self.on_drop or (lambda ps: submit(ps, self.learn)), [Path(u.path()) for u in urls])
        return True

    def mouseDown_(self, event):
        if self.on_drop:
            self.on_drop(choose(runner.MEDIA_EXT | set(train.SUBS)))
        else:
            pick_files(self.learn)

    def drawRect_(self, rect):
        accent = NSColor.controlAccentColor()
        r = NSInsetRect(self.bounds(), 4, 4)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 14, 14)
        (accent.colorWithAlphaComponent_(0.12) if self.hover
         else NSColor.quaternaryLabelColor().colorWithAlphaComponent_(0.08)).setFill()
        path.fill()
        path.setLineWidth_(2)
        path.setLineDash_count_phase_([7, 5], 2, 0)
        (accent if self.hover else NSColor.tertiaryLabelColor()).setStroke()
        path.stroke()

        w, h = self.bounds().size
        color = accent if self.hover else NSColor.secondaryLabelColor()
        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(40, 0.2) \
            .configurationByApplyingConfiguration_(
                NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "film.stack", None).imageWithSymbolConfiguration_(cfg)
        iw, ih = icon.size()
        icon.drawInRect_(NSMakeRect((w - iw) / 2, h / 2 + 4, iw, ih))
        for text, size, color, y in (
                (self.texts[0], 17, NSColor.labelColor(), h / 2 - 26),
                (self.texts[1], 12, NSColor.secondaryLabelColor(), h / 2 - 48)):
            s = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                       NSForegroundColorAttributeName: color})
            s.drawAtPoint_(((w - s.size().width) / 2, y))


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, note):
        if why := hardware.blocker():
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            message("Kadr не запустится на этом Mac", why)
            NSApplication.sharedApplication().terminate_(None)
            return
        threading.Thread(target=worker, daemon=True).start()
        threading.Thread(target=update.watch_releases, daemon=True).start()
        self.build_menu()
        self.build_window()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "tick:", None, True)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if config.TRANSLATOR == "claude" and not config.ANTHROPIC_API_KEY:
            self.askKey_(None)

    def application_openFiles_(self, app, filenames):
        """Файлы, брошенные на иконку в Dock/Finder или открытые через Quick Action (open -b).
        Файл проекта или папка project-n — в редактор."""
        files = list(map(Path, filenames))
        for f in files:
            if f.is_dir() or f.name.endswith(project.FILE):
                self.open_editor(f)
        submit([f for f in files if not (f.is_dir() or f.name.endswith(project.FILE))])

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self.window.makeKeyAndOrderFront_(None)   # клик по значку в Dock возвращает окно
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False   # окно закрыли — «Входящие» продолжают обрабатываться

    # ---------- окно ----------

    def build_window(self):
        W, H = 600, 480
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), 1 | 2 | 4, 2, False)   # titled | closable | miniaturizable
        self.window.setTitle_(f"{config.APP_NAME} {__version__}" + ("" if config.FROZEN else " — dev"))
        self.window.setReleasedWhenClosed_(False)
        # развилка в начале: сделать субтитры или учить распознавание — у каждого режима свой вид
        self.seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Сделать субтитры", "Точность"], 0, self, "switchMode:")
        self.seg.setSelectedSegment_(0)
        self.tr = Training.alloc().init()
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self.views = [v, self.tr.view]

        v.addSubview_(DropView.alloc().initWithFrame_(NSMakeRect(20, 260, W - 40, 200)))
        # появляется, когда в GitHub Releases есть версия новее; окно подрастает на строку
        self.update_btn = NSButton.buttonWithTitle_target_action_("", self, "installUpdate:")
        self.update_btn.setHidden_(True)
        self.W = W

        self.state = label("", NSMakeRect(20, 226, W - 40, 20))
        self.progress = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(20, 208, W - 40, 12))
        self.progress.setStyle_(0)   # полоска
        self.progress.setMinValue_(0)
        self.progress.setMaxValue_(100)
        self.recent = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(18, 163, W - 304, 26), False)
        self.recent.setToolTip_("Последние результаты — выберите и откройте в редакторе или Finder")
        self.shown = None
        self.edit = NSButton.buttonWithTitle_target_action_("Редактор", self, "editLast:")
        self.edit.setFrame_(NSMakeRect(W - 280, 160, 106, 32))
        self.edit.setHidden_(True)
        self.reveal = NSButton.buttonWithTitle_target_action_("Показать в Finder", self, "revealLast:")
        self.reveal.setFrame_(NSMakeRect(W - 170, 160, 150, 32))
        self.reveal.setHidden_(True)
        sep = NSBox.alloc().initWithFrame_(NSMakeRect(20, 140, W - 40, 1))
        sep.setBoxType_(2)   # разделитель

        self.translator = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(92, 96, 250, 26), False)
        self.translator.addItemsWithTitles_(list(TRANSLATORS.values()))
        self.translator.setTarget_(self)
        self.translator.setAction_("setTranslator:")
        cur = "local" if config.TRANSLATOR == "local" else config.CLAUDE_MODEL
        self.translator.selectItemAtIndex_(list(TRANSLATORS).index(cur) if cur in TRANSLATORS else 0)
        self.burn = NSButton.checkboxWithTitle_target_action_("Субтитры в картинке", self, "toggleBurn:")
        self.burn.setFrame_(NSMakeRect(352, 98, 160, 22))
        self.burn.setState_(int(config.BURN))

        self.effort = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(92, 62, 330, 26), False)
        self.effort.setAutoenablesItems_(False)
        for k, t in EFFORTS.items():
            ok = hardware.allowed(k)
            self.effort.addItemWithTitle_(t if ok else f"{t.split(' —')[0]} — нужно {config.EFFORTS[k]['ram']} ГБ памяти")
            self.effort.lastItem().setEnabled_(ok)
        self.effort.selectItemAtIndex_(list(EFFORTS).index(config.EFFORT))
        self.effort.setTarget_(self)
        self.effort.setAction_("setEffort:")
        self.effort.setToolTip_("Эконом: Whisper turbo и Gemma, модели выгружаются сразу после видео, "
                                "обработка в фоне.\nБаланс: Whisper large-v3 и Qwen 9B по очереди, "
                                "выгрузка через 3 минуты простоя.\nМаксимум: обе модели всегда в памяти.")
        self.meter = NSLevelIndicator.alloc().initWithFrame_(NSMakeRect(92, 35, 120, 14))
        self.meter.setLevelIndicatorStyle_(1)   # сплошная полоска
        self.meter.setMaxValue_(config.RAM_GB)
        self.meter.setWarningValue_(config.RAM_GB * 0.5)
        self.meter.setCriticalValue_(config.RAM_GB * 0.75)
        self.load = label("", NSMakeRect(220, 33, W - 240, 18), size=12)
        self.models = label("", NSMakeRect(92, 12, W - 112, 16), size=11, color=NSColor.secondaryLabelColor())
        self._cpu = (time.monotonic(), usage()[1])

        gray = NSColor.secondaryLabelColor()
        for x in (self.state, self.progress, self.recent, self.edit, self.reveal, sep,
                  label("Перевод:", NSMakeRect(20, 101, 70, 18), color=gray), self.translator, self.burn,
                  label("Нагрузка:", NSMakeRect(20, 67, 70, 18), color=gray), self.effort,
                  label("Сейчас:", NSMakeRect(20, 33, 70, 18), color=gray), self.meter, self.load,
                  self.models):
            v.addSubview_(x)
        for x in (*self.views, self.update_btn, self.seg):
            self.window.contentView().addSubview_(x)
        self.fill_recent()
        self.relayout()
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)

    def relayout(self):
        """Снизу вверх: вид режима, кнопка обновления (если есть), развилка. Окно меняет высоту, верх стоит."""
        v = self.views[self.seg.selectedSegment()]
        h, up, W = v.frame().size.height, 0 if self.update_btn.isHidden() else 40, self.W
        for x in self.views:
            x.setHidden_(x is not v)
        self.update_btn.setFrame_(NSMakeRect(20, h + 6, W - 40, 28))
        self.seg.sizeToFit()
        sw = self.seg.frame().size.width
        self.seg.setFrame_(NSMakeRect((W - sw) / 2, h + up + 10, sw, 28))
        f = self.window.frame()
        new = self.window.frameRectForContentRect_(NSMakeRect(0, 0, W, h + up + 48))
        top = f.origin.y + f.size.height
        self.window.setFrame_display_animate_(NSMakeRect(f.origin.x, top - new.size.height, new.size.width,
                                                         new.size.height), True, self.window.isVisible())

    def switchMode_(self, sender):
        # не setMode: — так называется селектор NSMatrix с целым аргументом, PyObjC берёт его сигнатуру,
        # и вместо контрола сюда приходит число
        if sender.selectedSegment() == 1:
            self.tr.show()
        self.relayout()

    def tick_(self, timer):
        self.show_update()
        if status["quit"]:
            NSApplication.sharedApplication().terminate_(None)
        text = status["text"]
        if text.startswith("Загружаю модель"):
            text = f"{text} {models_size_gb():.1f} GB (только при первом запуске)"
        waiting = watch.jobs.qsize()
        if waiting:
            text += f" · ещё в очереди: {waiting}"
        self.state.setStringValue_(text)
        pct = text.split("%")[0].rsplit(" ", 1)[-1] if "%" in text else ""
        if status["busy"] and pct.isdigit():
            self.progress.setIndeterminate_(False)
            self.progress.setDoubleValue_(float(pct))
        elif status["busy"] or text.startswith("Загружаю"):
            self.progress.setIndeterminate_(True)
            self.progress.startAnimation_(None)
        else:
            self.progress.stopAnimation_(None)
        self.progress.setHidden_(not (status["busy"] or text.startswith("Загружаю")))
        self.show_load()
        if status["last"] != self.shown:
            self.shown = status["last"]
            self.fill_recent()

    def fill_recent(self):
        """Последние результаты по имени исходника: «project-11» ни о чём не говорит."""
        idx = project.index((config.DONE, config.KNOWLEDGE))[:15]
        self.recent_dirs = [d for d, _ in idx]
        self.recent.removeAllItems()
        for d, s in idx:   # номера project-n в «Базе знаний» свои — без пометки названия совпадут
            self.recent.addItemWithTitle_(f"{s.name} — {d.name}" + (" · база знаний" if d.parent == config.KNOWLEDGE else ""))
        for x in (self.recent, self.edit, self.reveal):
            x.setHidden_(not idx)

    def show_update(self):
        if status["update"]:
            self.update_btn.setTitle_(status["update"])
        elif update.available and self.update_btn.isHidden():
            self.update_btn.setTitle_(f"Доступна версия {update.available[0]} — обновить")
            self.update_btn.setHidden_(False)
            self.relayout()

    def installUpdate_(self, sender):
        self.update_btn.setEnabled_(False)
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        ver, url = update.available
        try:
            status["update"] = f"Скачиваю {ver}… 0%"
            app = update.download(url, lambda p: status.update(update=f"Скачиваю {ver}… {p}%"))
            if config.FROZEN:
                status["update"] = "Перезапускаюсь…"
                update.swap_after_exit(app, Path(NSBundle.mainBundle().bundlePath()))
                status["quit"] = True
            else:   # dev-запуск: подменять нечего, просто показать скачанное
                status["update"] = f"Скачано: {app}"
                subprocess.run(["open", "-R", str(app)])
        except Exception as e:
            log.exception("Обновление не удалось")
            status["update"] = f"Не удалось обновить: {str(e)[:60]}"
            self.update_btn.setEnabled_(True)

    def checkUpdates_(self, sender):
        def run():
            try:
                update.available = update.check()
                if not update.available:
                    notify(f"У вас последняя версия, {__version__}")
            except Exception as e:
                notify(f"Не удалось проверить: {str(e)[:80]}")
        threading.Thread(target=run, daemon=True).start()

    def show_load(self):
        now, (gb, cpu) = time.monotonic(), usage()
        t0, cpu0 = self._cpu
        if now - t0 < 1:   # ЦП за ~секунду, а не за 0,3 с между тиками — иначе цифра скачет
            return
        self._cpu = (now, cpu)
        pct = round((cpu - cpu0) / (now - t0) / (os.cpu_count() or 1) * 100)
        self.meter.setDoubleValue_(gb)
        self.load.setStringValue_(f"Kadr: {gb:.1f} ГБ из {config.RAM_GB} · ЦП {pct}%".replace(".", ","))
        names = [n for n, m in (("распознавание", transcribe._model), ("перевод", translate_local._model))
                 if m is not None]
        self.models.setStringValue_(("В памяти: " + " и ".join(names) if names else "Модели выгружены")
                                    + f" · память Mac {PRESSURE.get(pressure(), 'в норме')}")

    def revealLast_(self, sender):
        last = self.recent_dirs[self.recent.indexOfSelectedItem()]
        main = next(last.glob("video-*-en.mp4"), None) or next(last.glob("subs-*-en.srt"), None)
        subprocess.run(["open", "-R", str(main)] if main else ["open", str(last)])

    def editLast_(self, sender):
        self.open_editor(self.recent_dirs[self.recent.indexOfSelectedItem()])

    def openProject_(self, sender):
        """Папка project-n или файл проекта — например, чтобы поправить старый результат."""
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setAllowedFileTypes_(["json"])
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(config.DONE)))
        if panel.runModal() == 1:
            self.open_editor(Path(panel.URL().path()))

    def open_editor(self, target: Path):
        from .editor import open_editor

        try:
            open_editor(target)
        except Exception as e:
            log.exception("Не открылся редактор")
            a = NSAlert.alloc().init()
            a.setMessageText_("Не получилось открыть редактор")
            a.setInformativeText_(str(e))
            a.runModal()

    def fits_disk(self, effort: str, translator: str | None = None) -> bool:
        """Хватит ли места на модели профиля; нет — объясняем и не переключаемся."""
        if hardware.free_gb() >= (n := hardware.need_gb(effort, translator)):
            return True
        message("Не хватает места на диске",
                f"Для этого режима нужно {n:.0f} ГБ свободного места, сейчас {hardware.free_gb():.1f} ГБ.")
        return False

    def setTranslator_(self, sender):
        k = list(TRANSLATORS)[sender.indexOfSelectedItem()]
        if k == "local" and not self.fits_disk(config.EFFORT, "local"):
            cur = "local" if config.TRANSLATOR == "local" else config.CLAUDE_MODEL
            sender.selectItemAtIndex_(list(TRANSLATORS).index(cur))
            return
        if k == "local":
            config.TRANSLATOR = "local"
        else:
            config.TRANSLATOR, config.CLAUDE_MODEL = "claude", k
            config.load_key()
            if not config.ANTHROPIC_API_KEY:
                self.askKey_(None)
        save_settings(TRANSLATOR=config.TRANSLATOR, CLAUDE_MODEL=config.CLAUDE_MODEL)
        threading.Thread(target=apply_effort, daemon=True).start()

    def setEffort_(self, sender):
        name = list(EFFORTS)[sender.indexOfSelectedItem()]
        if not self.fits_disk(name):
            sender.selectItemAtIndex_(list(EFFORTS).index(config.EFFORT))
            return
        config.set_effort(name)
        save_settings(EFFORT=name)
        threading.Thread(target=apply_effort, daemon=True).start()

    def toggleBurn_(self, sender):
        config.BURN = bool(sender.state())
        save_settings(BURN=int(config.BURN))

    # ---------- меню ----------

    def build_menu(self):
        def menu(title, items):
            m = NSMenu.alloc().initWithTitle_(title)
            for it in items:
                if it is None:
                    m.addItem_(NSMenuItem.separatorItem())
                else:
                    m.addItemWithTitle_action_keyEquivalent_(*it)
            return m

        app_menu = menu(config.APP_NAME, [
            ("Ключ Anthropic API…", "askKey:", ""),
            ("Workspace ID…", "askWorkspace:", ""),
            ("Общий контекст (имена, термины)…", "editContext:", ""),
            None,
            ("Удалить неиспользуемые модели…", "removeModels:", ""),
            ("Добавить «Сделать субтитры» в Finder", "installQuickAction:", ""),
            ("Запускать при входе в систему", "toggleLogin:", ""),
            ("Журнал", "openLog:", ""),
            ("Проверить обновления", "checkUpdates:", ""),
            None,
            ("Скрыть", "hide:", "h"),
            ("Выйти", "terminate:", "q"),
        ])
        self.login_item = app_menu.itemWithTitle_("Запускать при входе в систему")
        self.login_item.setState_(int(self.login_enabled()))
        file_menu = menu("Файл", [
            ("Выбрать видео…", "pickFiles:", "o"),
            ("Открыть в редакторе…", "openProject:", "e"),
            ("База знаний", "showKnowledge:", "k"),
            ("Точность", "showTraining:", "l"),
            ("Открыть «Входящие»", "openInbox:", ""),
            ("Открыть «Готово»", "openDone:", ""),
            None,
            ("Показать окно", "showWindow:", "0"),
            ("Закрыть окно", "performClose:", "w"),
        ])
        # без меню «Правка» в полях ввода не работают ⌘V/⌘C — ключ было бы не вставить
        edit_menu = menu("Правка", [
            ("Вырезать", "cut:", "x"), ("Копировать", "copy:", "c"),
            ("Вставить", "paste:", "v"), ("Выделить всё", "selectAll:", "a"),
        ])
        bar = NSMenu.alloc().init()
        for m in (app_menu, file_menu, edit_menu):
            item = bar.addItemWithTitle_action_keyEquivalent_(m.title(), None, "")
            bar.setSubmenu_forItem_(m, item)
        NSApplication.sharedApplication().setMainMenu_(bar)

    def pickFiles_(self, sender):
        pick_files()

    def showWindow_(self, sender):
        self.window.makeKeyAndOrderFront_(None)

    def openInbox_(self, sender):
        self._open_dir(config.INBOX)

    def openDone_(self, sender):
        self._open_dir(config.DONE)

    def _open_dir(self, d: Path):
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(d)])

    def openLog_(self, sender):
        subprocess.run(["open", str(config.LOGS / "kadr.log")])

    def editContext_(self, sender):
        f = config.GLOBAL_CONTEXT
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("", encoding="utf-8")
        subprocess.run(["open", "-e", str(f)])

    def askKey_(self, sender):
        key = ask("Ключ Anthropic API",
                  "Нужен только для перевода через Claude.\nconsole.anthropic.com → API Keys "
                  "(начинается с sk-ant-). Хранится в Связке ключей этого Mac.", secure=True)
        if key:
            import keyring.backends.macOS

            keyring.backends.macOS.Keyring().set_password(config.APP_NAME, "anthropic", key)
            config.ANTHROPIC_API_KEY = key

    def askWorkspace_(self, sender):
        ws = ask("Workspace ID", "Нужен, только если ключ не привязан к workspace.\n"
                 "console.anthropic.com → Settings → Workspaces (wrkspc_…).",
                 default=config.ANTHROPIC_WORKSPACE_ID)
        if ws is not None:
            config.ANTHROPIC_WORKSPACE_ID = ws
            save_settings(ANTHROPIC_WORKSPACE_ID=ws)

    def installQuickAction_(self, sender):
        from .quick_action import install

        install(f'open -b {BUNDLE_ID} "$@"')
        notify("Готово: правый клик по видео → Быстрые действия → «Сделать субтитры»")

    @staticmethod
    def login_service():
        from ServiceManagement import SMAppService

        return SMAppService.mainAppService()

    def login_enabled(self) -> bool:
        try:
            return self.login_service().status() == 1   # SMAppServiceStatusEnabled
        except Exception:
            return False

    def toggleLogin_(self, sender):
        svc = self.login_service()
        ok, err = (svc.unregisterAndReturnError_(None) if self.login_enabled()
                   else svc.registerAndReturnError_(None))
        if not ok:
            message("Не получилось", str(err))
        sender.setState_(int(self.login_enabled()))

    def removeModels_(self, sender):
        """Модели других профилей: в «Экономе» веса «Баланса» — лишние 8,5 GB, и наоборот."""
        extra = hardware.unused_models()
        if not extra:
            return message("Лишних моделей нет", "На диске только модели текущего режима.")
        a = NSAlert.alloc().init()
        a.setMessageText_(f"Удалить {sum(g for _, g in extra):.1f} ГБ?")
        a.setInformativeText_("\n".join(f"{r.split('/')[1]} — {g:.1f} ГБ" for r, g in extra)
                              + "\n\nПри смене режима нужная модель скачается заново.")
        a.addButtonWithTitle_("Удалить")
        a.addButtonWithTitle_("Отмена")
        if a.runModal() == 1000:
            for r, _ in extra:
                hardware.remove_model(r)

    def showKnowledge_(self, sender):
        if not getattr(self, "kb", None):
            self.kb = Knowledge.alloc().init()
        self.kb.show()

    def showTraining_(self, sender):
        self.seg.setSelectedSegment_(1)
        self.switchMode_(self.seg)
        self.window.makeKeyAndOrderFront_(None)


def text_area(frame) -> tuple[NSScrollView, NSTextView]:
    sv = NSScrollView.alloc().initWithFrame_(frame)
    sv.setHasVerticalScroller_(True)
    sv.setBorderType_(2)   # рамка как у поля ввода
    # по видимой части, а не по кадру: шире неё — и первые буквы строк уезжают под рамку
    w, h = sv.contentSize()
    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    tv.setAutoresizingMask_(2)
    tv.setRichText_(False)
    tv.setFont_(NSFont.systemFontOfSize_(13))
    tv.setTextContainerInset_((4, 4))
    # «умные» кавычки и тире ломают «->» и слова в заменах
    tv.setAutomaticQuoteSubstitutionEnabled_(False)
    tv.setAutomaticDashSubstitutionEnabled_(False)
    tv.setAutomaticTextReplacementEnabled_(False)
    sv.setDocumentView_(tv)
    return sv, tv


def parse_fixes(text: str) -> tuple[dict[str, str], list[str]]:
    """Строки «было → стало» → словарь замен; строки без стрелки — отдельно, чтобы показать."""
    fixes, bad = {}, []
    for line in filter(None, map(str.strip, text.splitlines())):
        a, arrow, b = line.replace("->", "→").partition("→")
        if arrow and a.strip():
            fixes[a.strip()] = b.strip()
        else:
            bad.append(line)
    return fixes, bad


class Knowledge(NSObject):
    """База знаний: сырые видео прогоняются без прожига, правки из редактора становятся заменами,
    словарь имён и терминов подсказывает распознаванию и переводу. Всё это Kadr применяет к новым видео."""

    def init(self):
        self = objc.super(Knowledge, self).init()
        W, H = 560, 640
        self.window = w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), 1 | 2 | 4, 2, False)
        w.setTitle_("База знаний")
        w.setReleasedWhenClosed_(False)
        v = w.contentView()
        drop = DropView.alloc().initWithFrame_(NSMakeRect(20, 470, W - 40, 150))
        drop.learn = True
        drop.texts = ("Перетащите сырые видео", "Kadr распознает и переведёт их без прожига")
        hint = label("Откройте результат в редакторе и поправьте русский текст. «Найти в правках» соберёт, "
                     "что вы исправили, в замены — дальше Kadr делает их в новых видео сам.",
                     NSMakeRect(20, 420, W - 40, 40), size=12, color=NSColor.secondaryLabelColor())
        hint.setMaximumNumberOfLines_(3)
        hint.cell().setWraps_(True)
        review = NSButton.buttonWithTitle_target_action_("Проверить в редакторе…", self, "review:")
        review.setFrame_(NSMakeRect(14, 380, 200, 32))
        learn = NSButton.buttonWithTitle_target_action_("Найти в правках", self, "learn:")
        learn.setFrame_(NSMakeRect(214, 380, 160, 32))
        gloss_sv, self.gloss = text_area(NSMakeRect(20, 250, W - 40, 100))
        fixes_sv, self.fixes = text_area(NSMakeRect(20, 64, W - 40, 150))
        self.status = label("", NSMakeRect(20, 24, W - 180, 18), size=12, color=NSColor.secondaryLabelColor())
        save = NSButton.buttonWithTitle_target_action_("Сохранить", self, "save:")
        save.setFrame_(NSMakeRect(W - 134, 16, 120, 32))
        save.setKeyEquivalent_("\r")
        for x in (drop, hint, review, learn, gloss_sv, fixes_sv, self.status, save,
                  label("Словарь — имена и термины, подсказка распознаванию и переводу:",
                        NSMakeRect(20, 354, W - 40, 18), size=12, bold=True),
                  label("Замены — по строке «было → стало», после распознавания и перевода:",
                        NSMakeRect(20, 218, W - 40, 18), size=12, bold=True)):
            v.addSubview_(x)
        w.center()
        return self

    def show(self):
        f = config.GLOBAL_CONTEXT
        self.gloss.setString_(f.read_text(encoding="utf-8") if f.is_file() else "")
        self.fixes.setString_("\n".join(f"{a} → {b}" for a, b in config.fixes().items()))
        self.status.setStringValue_("")
        self.window.makeKeyAndOrderFront_(None)

    def review_(self, sender):
        config.KNOWLEDGE.mkdir(parents=True, exist_ok=True)
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setAllowedFileTypes_(["json"])
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(config.KNOWLEDGE)))
        if panel.runModal() == 1:
            NSApplication.sharedApplication().delegate().open_editor(Path(panel.URL().path()))

    def learn_(self, sender):
        fixes, _ = parse_fixes(self.fixes.string())
        self.suggest(project.learned((config.DONE, config.KNOWLEDGE), fixes), "из правок")

    @objc.python_method
    def suggest(self, found, where: str):
        """Дописать предложенные замены в список — сохраняет человек, проверив."""
        if not found:
            return self.status.setStringValue_(f"Новых замен {where} не нашёл")
        text = self.fixes.string().rstrip()
        self.fixes.setString_((text + "\n" if text else "") + "\n".join(f"{a} → {b}" for a, b, _ in found))
        self.status.setStringValue_(f"Добавил {len(found)} {where} — проверьте и сохраните")

    def save_(self, sender):
        fixes, bad = parse_fixes(self.fixes.string())
        if bad:
            return message("Строки без стрелки", "Нужно «было → стало»:\n" + "\n".join(bad[:10]))
        config.GLOBAL_CONTEXT.parent.mkdir(parents=True, exist_ok=True)
        config.GLOBAL_CONTEXT.write_text(self.gloss.string().strip(), encoding="utf-8")
        config.FIXES.write_text(json.dumps(fixes, ensure_ascii=False, indent=1), encoding="utf-8")
        self.status.setStringValue_(f"Сохранено: замен {len(fixes)}")
        NSApplication.sharedApplication().delegate().tr.show()   # «Точность» показывает, что сейчас влияет


class Training(NSObject, protocols=[objc.protocolNamed("NSTableViewDataSource")]):
    """«Точность»: сверху — что влияет на каждое новое видео (замены, словарь, фильтры), ниже — замер на эталонах.
    Эталоны модель не учат: Kadr распознаёт их заново и сравнивает с текстом — видно, помогли ли замены и словарь."""

    def init(self):
        self = objc.super(Training, self).init()
        W, H = 600, 640
        self.busy, self.rows = False, []
        self.view = v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        gray = NSColor.secondaryLabelColor()
        drop = DropView.alloc().initWithFrame_(NSMakeRect(20, 494, W - 40, 136))
        drop.on_drop = self.dropped
        drop.texts = ("Перетащите готовый ролик — станет эталоном",
                      "Субтитры прочитаются с кадра. Или видео вместе с его .srt")
        # что влияет: подпись слева, состояние справа — значения в show()
        self.fixes_l, self.dict_l, on = (label(t, NSMakeRect(300, y, W - 320, 18), color=gray)
                                         for t, y in (("", 446), ("", 424), ("всегда включены", 402)))
        for x in (self.fixes_l, self.dict_l, on):
            x.setAlignment_(2)   # вправо
        kb = NSButton.buttonWithTitle_target_action_("Словарь и замены…", self, "showKnowledge:")
        kb.setFrame_(NSMakeRect(W - 186, 368, 172, 30))
        # замер: три числа и таблица
        self.wer_l, self.delta_l, self.refs_l = (label("", NSMakeRect(20 + i * 190, 300, 180, 28), size=22)
                                                 for i in range(3))
        cols = (("name", "Ролик", 130), ("wer", "Ошибок", 64), ("errs", "Чаще всего путает", 360))
        self.tv = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 40, 170))
        for ident, title, width in cols:
            c = NSTableColumn.alloc().initWithIdentifier_(ident)
            c.setTitle_(title)
            c.setWidth_(width)
            self.tv.addTableColumn_(c)
        self.tv.setDataSource_(self)
        self.tv.setUsesAlternatingRowBackgroundColors_(True)
        sv = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 92, W - 40, 180))
        sv.setHasVerticalScroller_(True)
        sv.setBorderType_(2)
        sv.setDocumentView_(self.tv)
        self.status = label("", NSMakeRect(20, 66, W - 40, 18), size=12, color=gray)
        x = 14
        for title, action, width in (("Проверить все", "checkAll:", 140), ("Ошибки → в замены", "toFixes:", 170),
                                     ("Папка эталонов", "openRefs:", 140)):
            b = NSButton.buttonWithTitle_target_action_(title, self, action)
            b.setFrame_(NSMakeRect(x, 20, width, 32))
            v.addSubview_(b)
            x += width
        for x in (drop, label("Влияет на каждое новое видео", NSMakeRect(20, 472, W - 40, 18), bold=True),
                  label("Замены после распознавания и перевода", NSMakeRect(20, 446, 290, 18)), self.fixes_l,
                  label("Словарь — подсказка Whisper", NSMakeRect(20, 424, 290, 18)), self.dict_l,
                  label("Фильтр выдуманных титров, второй проход", NSMakeRect(20, 402, 290, 18)), on, kb,
                  label("Замер на эталонах", NSMakeRect(20, 346, 160, 18), bold=True),
                  label("— только замер, на новые видео не влияет", NSMakeRect(160, 346, W - 180, 18),
                        size=12, color=gray),
                  *(label(t, NSMakeRect(20 + i * 190, 284, 180, 16), size=11, color=gray)
                    for i, t in enumerate(("ошибок в словах", "с прошлой проверки", "эталонов"))),
                  self.wer_l, self.delta_l, self.refs_l, sv, self.status):
            v.addSubview_(x)
        return self

    def show(self):
        f, gloss = config.fixes(), runner._read(config.GLOBAL_CONTEXT)
        self.fixes_l.setStringValue_(f"{len(f)} · " + ", ".join(sorted(set(f.values()))[:2]) if f else "нет")
        self.dict_l.setStringValue_(gloss[:32] + "…" if len(gloss) > 32 else gloss or "пусто")
        s = train.summary()
        self.wer_l.setStringValue_(f"{s['wer']}%" if s["wer"] is not None else "—")
        d = s["delta"]
        self.delta_l.setStringValue_("—" if d is None else f"{d:+}%")
        self.delta_l.setTextColor_(NSColor.labelColor() if not d else NSColor.systemGreenColor() if d < 0
                                   else NSColor.systemRedColor())
        self.refs_l.setStringValue_(f"{s['refs']} · {s['minutes']} мин")
        self.rows = train.rows()
        self.tv.reloadData()

    def numberOfRowsInTableView_(self, tv):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):
        return self.rows[row][("name", "wer", "errs").index(col.identifier())]

    @objc.python_method
    def say(self, text):
        AppHelper.callAfter(self.status.setStringValue_, text)

    @objc.python_method
    def dropped(self, paths):
        subs = {train.stem(p): p for p in paths if p.suffix.lower() in train.SUBS}
        pairs, burned = [], []
        for v in (p for p in paths if p.suffix.lower() in runner.MEDIA_EXT):
            s = subs.pop(train.stem(v), None)
            pairs.append((v, s)) if s else burned.append(v)   # без .srt — читаем субтитры в кадре
        if subs:
            message("Субтитры без видео", "Перетащите вместе с ними видео: " + ", ".join(p.name for p in subs.values()))
        if pairs or burned:
            self.work(lambda: [self._add(*p) for p in pairs] + [self._burned(v) for v in burned])

    @objc.python_method
    def _add(self, video, subs):
        self.say(f"Сохраняю эталон {video.name}…")
        d = train.add(video, subs)
        self._check(d)

    @objc.python_method
    def _burned(self, video):
        """Готовый ролик: субтитры в кадре → частичный эталон (монтажёр подписал не каждую фразу)."""
        from . import ocr   # Vision грузим, только когда нужен

        self.say(f"Читаю субтитры в кадре {video.name}…")
        cues = ocr.to_cues(ocr.frames_lines(video))
        if len(cues) < ocr.MIN_CUES:
            return self.say(f"{video.name}: субтитров в кадре не нашёл — перетащите видео вместе с его .srt")
        with tempfile.TemporaryDirectory() as t:
            write_srt(cues, Path(t) / "ref.ru.srt")
            d = train.add(video, Path(t) / "ref.ru.srt", partial=True)
        self._check(d)

    @objc.python_method
    def _check(self, d):
        self.say(f"Распознаю {d.name}…")
        r = train.check(d)
        self.say(f"{d.name}: ошибок {r['wer']}%")
        AppHelper.callAfter(self.show)

    @objc.python_method
    def work(self, fn):
        """Тяжёлое — в фоне, по одному; модели после проверки — как после видео: выгружаются, если не «Максимум»."""
        if self.busy:
            return message("Подождите", "Проверка ещё идёт.")
        self.busy = True

        def run():
            try:
                fn()
            except Exception as e:
                log.exception("Обучение: ошибка")
                self.say(f"❌ {str(e)[:100]}")
            finally:
                if config.KEEP_MODELS is not None and not status["busy"]:
                    release()
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def checkAll_(self, sender):
        refs = train.refs()
        if not refs:
            return self.status.setStringValue_("Эталонов пока нет")
        self.work(lambda: [self._check(d) for d in refs])

    def showKnowledge_(self, sender):
        NSApplication.sharedApplication().delegate().showKnowledge_(sender)

    def toFixes_(self, sender):
        delegate = NSApplication.sharedApplication().delegate()
        delegate.showKnowledge_(None)
        delegate.kb.suggest(train.suggestions(parse_fixes(delegate.kb.fixes.string())[0]), "из эталонов")

    def openRefs_(self, sender):
        train.REFS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(train.REFS)])


def worker():
    try:
        prefetch()
        if config.KEEP_MODELS is None:
            warm()
        watch.serve(hold=lambda: hardware.hold() or (
            None if config.TRANSLATOR == "local" or config.ANTHROPIC_API_KEY
            else "Жду ключ Anthropic API — меню Kadr → «Ключ Anthropic API…»"))
    except Exception as e:
        log.exception("Ошибка воркера")
        status["text"] = f"❌ {str(e)[:80]}"
        notify(f"Ошибка: {str(e)[:150]}")


def main():
    # второй экземпляр (двойной запуск, автозапуск + ручной) не нужен: файлы примет первый
    config.DATA.mkdir(parents=True, exist_ok=True)
    global _instance_lock
    _instance_lock = open(config.DATA / "app.lock", "w")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    runner.setup_logging()
    logging.getLogger().addHandler(StatusHandler())
    runner.notify = notify
    try:
        from UserNotifications import UNUserNotificationCenter

        UNUserNotificationCenter.currentNotificationCenter() \
            .requestAuthorizationWithOptions_completionHandler_(0b111, None)
    except Exception:
        pass
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)   # обычное приложение: Dock + меню (и при запуске не из .app)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.installMachInterrupt()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
