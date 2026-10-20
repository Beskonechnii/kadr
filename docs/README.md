# subbot — английские субтитры к русским видео (macOS)

Это документация разработчика. Пользователю нужны только файлы в корне проекта:
`Субтитры.app`, `Как установить.txt` и `Субтитры.zip` (для знакомых; внутри ещё `Как пользоваться.txt`).

```
subbot/
├── Субтитры.app  Субтитры.zip  Как установить.txt   ← собирает packaging/build_app.sh
├── app/        код
├── packaging/  сборка .app и ffmpeg (кэш — packaging/build/)
├── docs/       эта документация, разбор кода, гайды для пользователей
├── dev/        рабочие папки dev-версии: inbox, done, failed, logs, models
├── tools/      Quick Action и .command для dev-версии
├── test/       тестовые ролик и субтитры
└── .env        настройки dev-версии
```

Dev-версия: кладёшь видео в `dev/inbox/` → в `dev/done/project-N/` появляются:
- `video-N-en.mp4` — с прожжёнными EN-субтитрами (`BURN=1`, по умолчанию; для вертикального видео — мельче и выше)
- `video-N.mp4` — исходник + отключаемые дорожки EN/RU без перекодирования (QuickTime/iPhone);
  `video-N.mkv`, если кодеки исходника не h264/hevc + aac/mp3/alac/ac3
- `subs-N-en.srt` / `subs-N-ru.srt` — перевод и русская расшифровка
- сам исходник под своим именем (и `<имя>.txt`, если был)
`app.cli` пишет рядом с видео по-старому: `<имя>.en.mp4`, `<имя>.subs.mp4`, `<имя>.en.srt`, `<имя>.ru.srt`.

Ошибка → `dev/failed/project-N/` с исходником и `error.txt`. Обе ситуации — уведомление macOS.

## Запуск и остановка
- **Двойной клик по `tools/Сделать субтитры.command`** — следит за `dev/inbox/`. Остановить: закрыть окно или Ctrl+C.
- Из терминала: `.venv/bin/python -m app.watch`
- Разово: `.venv/bin/python -m app.cli video.mp4 [--context "..."] [--burn]` — результат рядом с файлом.
- **Finder**: правый клик по видео → Быстрые действия → «Сделать субтитры (dev)» (можно выделить несколько; у .app — своё, без «(dev)»).
  Результат — рядом с видео, по окончании уведомление. Переустановить: `.venv/bin/python tools/make_quick_action.py`.

Файлы, лежавшие в `dev/inbox/` при запуске, тоже обрабатываются. Строго по одному: watch и Quick Action
ждут друг друга. Пока идёт задача, Mac не засыпает (`caffeinate -i`).

## Контекст (заметно улучшает имена и термины)
- `dev/inbox/<имя>.txt` рядом с видео — тема, имена, термины. Клади **до** видео или одновременно.
- `dev/context.txt` — общий для всех видео (в приложении — меню «Субтитры» → «Общий контекст…»).

Первые 200 символов идут подсказкой в распознавание, весь текст — в перевод.

## Только перевод
Русский `.srt` в `dev/inbox/` (или в cli) → только `<имя>.en.srt` (суффикс `.ru` отрезается).

## Настройки — `.env`
| | |
|---|---|
| `TRANSLATOR` | `local` — локальная LLM, бесплатно (по умолчанию); `claude` — Claude API |
| `LOCAL_LLM` | по умолчанию `mlx-community/Qwen3.5-9B-MLX-4bit` (≥16 GB), `mlx-community/gemma-4-e4b-it-4bit` (8 GB) |
| `ANTHROPIC_API_KEY` | ключ API (только для `TRANSLATOR=claude`) |
| `ANTHROPIC_WORKSPACE_ID` | нужен, если ключ не привязан к workspace |
| `CLAUDE_MODEL` | `claude-sonnet-5-5`; максимум качества — `claude-opus-5-5` |
| `WHISPER_BACKEND` | `mlx` (Apple Silicon) / `faster` (Intel: `pip install faster-whisper`) |
| `WHISPER_MODEL` | `mlx-community/whisper-large-v3-mlx`; быстрее/меньше RAM — `mlx-community/whisper-large-v3-turbo` |
| `BURN` | `1` (по умолчанию) — MP4 с прожжёнными EN-субтитрами; `0` — не делать |
| `BURN_STYLE` | стиль libass (`force_style`): шрифт, размер, отступ |

Новая модель скачается при первом запуске в `dev/models`. Прожиг кодирует `h264_videotoolbox`
(если не вышло — `libx264`) и требует `ffmpeg-full` (в обычном brew-`ffmpeg` нет libass).

## Логи
`dev/logs/subbot.log` (ротация 5 × 2 MB). В консоли — короткий прогресс: файл, этап, %.

## Устройство
```
файл → ffmpeg (16 kHz wav) → mlx-whisper large-v3 (слова с таймингами)
     → ресегментация в читаемые реплики → ru.srt
     → перевод, 1 реплика = 1 реплика → en.srt:
         local: Qwen3.5-9B (MLX) кусками по 40 реплик, строки «N: текст», повтор пропущенных
         claude: резюме (контекст + глоссарий) → куски по 80 реплик через tool_use
     → ffmpeg: MP4 с прожигом (.en.mp4) + MP4/MKV с отключаемыми дорожками (.subs.mp4)
```
`app/srt_utils.py` — длина/разбиение реплик, `app/translate.py` — промпт перевода.

## Если сломался прожиг после `brew upgrade`
Готовый пакет freetype 2.15 для Sequoia жёстко ссылается на `/usr/lib/libhvf.dylib`, которой нет до macOS 15.4
(«Library not loaded: /usr/lib/libhvf.dylib»). Лечится сборкой из исходников:
`brew reinstall --build-from-source freetype`.

## Приложение для раздачи (`Субтитры.app`)
```bash
packaging/build_app.sh          # → Субтитры.app, Субтитры.zip, «Как установить.txt» в корне проекта
```
- Окно (`app/gui.py`, AppKit через PyObjC): зона перетаскивания, статус и прогресс, «Показать в Finder»,
  переводчик, прожиг. Меню «Субтитры»: ключ API (Связка ключей), Workspace ID, общий контекст,
  Quick Action, автозапуск (SMAppService), журнал. Закрыл окно — «Входящие» обрабатываются дальше.
  Dev-запуск окна: `.venv/bin/python -m app.gui`.
- Папки у пользователя: `~/Movies/Субтитры/{Входящие,Готово,Ошибки}`; модель и настройки —
  `~/Library/Application Support/Субтитры`; лог — `~/Library/Logs/Субтитры`.
- Файлы, брошенные на иконку / из Quick Action (`open -b local.subbot.subtitles`), обрабатываются
  на месте: результат в «Готово», исходник не трогается.
- ffmpeg — свой статический (`packaging/build_ffmpeg.sh`): LGPL, libass через CoreText, VideoToolbox,
  без Homebrew. Python — python-build-standalone через uv, без torch.
- Минимум — macOS 15.0 на Apple Silicon; `build_app.sh` падает, если какой-то бинарник требует новее.
- Подпись ad-hoc: у получателя один раз «Всё равно открыть» в Конфиденциальность и безопасность.
  С Apple Developer ID — `codesign --options runtime -s "Developer ID…"` + `notarytool` в конце сборки.
- LGPL: при раздаче держи под рукой исходники ffmpeg/fribidi (URL и sha256 — в `build_ffmpeg.sh`).

## Локальный перевод
Сравнение на живом монологе (30 реплик, M1 Pro): Qwen3.5-9B — лучший по естественности и именам (21 с);
Gemma 4 e4b — чуть деревяннее, быстрее (14 с); Qwen3.5-4B — путает имена (Катя → Katerina), не годится.
На Mac с памятью < 16 GB Whisper и переводчик держатся в памяти по очереди (`LOW_MEMORY`), +3–5 с на ролик.
