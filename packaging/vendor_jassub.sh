#!/bin/zsh
# Пересобирает app/editor/vendor/jassub: libass в wasm для превью редактора (тот же рендер, что у прожига).
# Версии закреплены и не моложе двух недель на момент выбора; abslink — через overrides (1.3.1 была слишком свежей).
set -euo pipefail
HERE=${0:A:h}
DST=${HERE:h}/app/editor/vendor/jassub
B=$(mktemp -d)
trap 'rm -rf $B' EXIT
cd $B
echo '{"private":true,"overrides":{"abslink":"1.3.0"}}' > package.json
# --ignore-scripts: чужие install-скрипты не запускаем
npm install --ignore-scripts --no-audit --no-fund --silent jassub@2.5.18 esbuild@0.28.2
E=$B/node_modules/@esbuild/darwin-arm64/bin/esbuild
J=$B/node_modules/jassub
rm -rf $DST && mkdir -p $DST
# в пакете ES-модули с голыми импортами зависимостей — собираем основной модуль и воркер в два файла
$E $J/dist/jassub.js --bundle --format=esm --minify --outfile=$DST/jassub.js --log-level=warning
$E $J/dist/worker/worker.js --bundle --format=esm --minify --outfile=$DST/jassub-worker.js --log-level=warning
cp $J/dist/wasm/*.wasm $J/dist/default.woff2 $J/LICENSE $DST/
ls -la $DST
