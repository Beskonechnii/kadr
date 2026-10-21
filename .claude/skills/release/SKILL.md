---
name: release
description: Выпустить версию Kadr (patch/minor/major) — CHANGELOG, тег, сборка .app, GitHub Release. Вызывать, когда Стас говорит «релиз», «выпусти патч», «собери новую версию».
---

1. `git status` чистый, ветка `main`, в `CHANGELOG.md` → «## Не выпущено» есть пункты. Пусто — сказать и остановиться.
2. Назвать одной строкой: «vX → vY, в релиз: …» и ждать «да» (граница релиза — только с подтверждением).
3. `tools/release.sh <patch|minor|major|X.Y.Z>` в фоне (сборка долгая).
4. Проверить: `gh release view vY` содержит `Kadr.zip`, `git log -1` — «Release vY». Не сошлось — так и написать.
