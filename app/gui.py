"""Окно приложения: перетащить видео → прогресс → «Показать в Finder». Плюс «Входящие» в фоне."""
from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import objc
from AppKit import (NSAlert, NSApplication, NSBezierPath, NSBox, NSButton, NSColor,
                    NSDragOperationCopy, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSImage, NSImageSymbolConfiguration, NSLevelIndicator,
                    NSMakeRect,
                    NSMenu, NSMenuItem, NSOpenPanel, NSPasteboardTypeFileURL,
                    NSPasteboardURLReadingFileURLsOnlyKey, NSPopUpButton, NSProgressIndicator,
                    NSSecureTextField, NSTextField, NSView, NSWindow)
from Foundation import NSURL, NSAttributedString, NSInsetRect, NSObject, NSTimer
from PyObjCTools import AppHelper

from . import __version__, config, project, runner, transcribe, translate_local, watch
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

status = {"text": "Запускаюсь…", "busy": False, "last": None}


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


def pressure() -> int:
    """Давление памяти macOS: 1 — норма, 2 — предупреждение, 4 — критично."""
    v, n = ctypes.c_int(0), ctypes.c_size_t(4)
    _libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(v), ctypes.byref(n), None, 0)
    return v.value


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


def pick_files():
    panel = NSOpenPanel.openPanel()
    panel.setAllowsMultipleSelection_(True)
    panel.setAllowedFileTypes_([e.lstrip(".") for e in runner.SUPPORTED_EXT])
    if panel.runModal() == 1:
        for url in panel.URLs():
            watch.enqueue(Path(url.path()), anywhere=True)


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
    """Зона «перетащите видео сюда»; клик — выбор файлов."""

    def initWithFrame_(self, frame):
        self = objc.super(DropView, self).initWithFrame_(frame)
        if self is not None:
            self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
            self.hover = False
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
        for u in urls:
            watch.enqueue(Path(u.path()), anywhere=True)
        self._set_hover(False)
        return True

    def mouseDown_(self, event):
        pick_files()

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
                ("Перетащите видео сюда", 17, NSColor.labelColor(), h / 2 - 26),
                ("или нажмите, чтобы выбрать файлы", 12, NSColor.secondaryLabelColor(), h / 2 - 48)):
            s = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                       NSForegroundColorAttributeName: color})
            s.drawAtPoint_(((w - s.size().width) / 2, y))


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, note):
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
        for f in map(Path, filenames):
            if f.is_dir() or f.name.endswith(project.FILE):
                self.open_editor(f)
            else:
                watch.enqueue(f, anywhere=True)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self.window.makeKeyAndOrderFront_(None)   # клик по значку в Dock возвращает окно
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False   # окно закрыли — «Входящие» продолжают обрабатываться

    # ---------- окно ----------

    def build_window(self):
        W, H = 520, 480
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), 1 | 2 | 4, 2, False)   # titled | closable | miniaturizable
        self.window.setTitle_(f"{config.APP_NAME} {__version__}" + ("" if config.FROZEN else " — dev"))
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        v = self.window.contentView()

        v.addSubview_(DropView.alloc().initWithFrame_(NSMakeRect(20, 260, W - 40, 200)))

        self.state = label("", NSMakeRect(20, 226, W - 40, 20))
        self.progress = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(20, 208, W - 40, 12))
        self.progress.setStyle_(0)   # полоска
        self.progress.setMinValue_(0)
        self.progress.setMaxValue_(100)
        self.result = label("", NSMakeRect(20, 168, W - 300, 20), color=NSColor.secondaryLabelColor())
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
        self.effort.addItemsWithTitles_(list(EFFORTS.values()))
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
        for x in (self.state, self.progress, self.result, self.edit, self.reveal, sep,
                  label("Перевод:", NSMakeRect(20, 101, 70, 18), color=gray), self.translator, self.burn,
                  label("Нагрузка:", NSMakeRect(20, 67, 70, 18), color=gray), self.effort,
                  label("Сейчас:", NSMakeRect(20, 33, 70, 18), color=gray), self.meter, self.load,
                  self.models):
            v.addSubview_(x)
        self.window.makeKeyAndOrderFront_(None)

    def tick_(self, timer):
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
        last = status["last"]
        if last:
            self.result.setStringValue_(f"Готово: {last.name}")
            self.reveal.setHidden_(False)
            # старые проекты (до 1.2) без project.json тоже открываются — по SRT
            self.edit.setHidden_(not (project.locate(last).exists() or any(last.glob("*en.srt"))))

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
        last = status["last"]
        if not last:
            return
        main = next(last.glob("video-*-en.mp4"), None) or next(last.glob("subs-*-en.srt"), None)
        subprocess.run(["open", "-R", str(main)] if main else ["open", str(last)])

    def editLast_(self, sender):
        if status["last"]:
            self.open_editor(status["last"])

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

    def setTranslator_(self, sender):
        k = list(TRANSLATORS)[sender.indexOfSelectedItem()]
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
            ("Добавить «Сделать субтитры» в Finder", "installQuickAction:", ""),
            ("Запускать при входе в систему", "toggleLogin:", ""),
            ("Журнал", "openLog:", ""),
            None,
            ("Скрыть", "hide:", "h"),
            ("Выйти", "terminate:", "q"),
        ])
        self.login_item = app_menu.itemWithTitle_("Запускать при входе в систему")
        self.login_item.setState_(int(self.login_enabled()))
        file_menu = menu("Файл", [
            ("Выбрать видео…", "pickFiles:", "o"),
            ("Открыть в редакторе…", "openProject:", "e"),
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
            a = NSAlert.alloc().init()
            a.setMessageText_("Не получилось")
            a.setInformativeText_(str(err))
            a.runModal()
        sender.setState_(int(self.login_enabled()))


def worker():
    try:
        prefetch()
        if config.KEEP_MODELS is None:
            warm()
        watch.serve(ready=lambda: config.TRANSLATOR == "local" or bool(config.ANTHROPIC_API_KEY))
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
    threading.Thread(target=worker, daemon=True).start()
    AppHelper.installMachInterrupt()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
