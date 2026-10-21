#!/bin/zsh
# Собирает Kadr.app, Kadr.zip и «Как установить.txt» в корень проекта. Нужен Mac на Apple Silicon.
set -euo pipefail
HERE=${0:A:h}
ROOT=${HERE:h}
B=$HERE/build
VENV=$B/venv

[[ -x $B/ffmpeg/bin/ffmpeg ]] || $HERE/build_ffmpeg.sh
[[ -x $B/tools/bin/uv ]] || $B/tools/bin/pip install -q uv
UV=$B/tools/bin/uv
MIN_MACOS=15.0
# без этого uv на новой macOS берёт колёса под неё (mlx под 26.x) — у знакомых на 15.x не запустится
export MACOSX_DEPLOYMENT_TARGET=$MIN_MACOS
PIP=($UV pip install -q --python $VENV/bin/python --python-platform aarch64-apple-darwin)
# python-build-standalone: собран под старые macOS, не зависит от Homebrew
export UV_PYTHON_INSTALL_DIR=$B/python
if [[ ! -x $VENV/bin/python ]]; then
  $UV python install 3.12
  $UV venv -q --python 3.12 $VENV
fi
$PIP --reinstall -r $HERE/requirements-app.txt pyinstaller
$PIP --no-deps mlx-whisper==0.4.3

$ROOT/.venv/bin/python $HERE/make_icon.py $B/Kadr.icns >/dev/null
cd $ROOT
$VENV/bin/python -m PyInstaller --noconfirm --log-level WARN \
  --workpath $B/pyinstaller --distpath $B/dist $HERE/kadr.spec
APP=$B/dist/Kadr.app
codesign --force --deep --sign - $APP

# ни один бинарник не должен требовать macOS новее MIN_MACOS
bad=$(find $APP -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) -print0 | xargs -0 file \
  | grep Mach-O | cut -d: -f1 | while read f; do
      m=$(otool -l "$f" | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; exit}')
      if [[ -n $m && $(printf '%s\n' $m $MIN_MACOS | sort -V | tail -1) != $MIN_MACOS ]]; then echo "$m $f"; fi
    done)
[[ -z $bad ]] || { echo "Требуют macOS новее $MIN_MACOS:\n$bad" >&2; exit 1; }
# в корень: приложение + гайд; zip = папка «Kadr» с тем же для знакомых
DOCS=$ROOT/docs
rm -rf $ROOT/Kadr.app && ditto $APP $ROOT/Kadr.app
cp "$DOCS/Как установить.txt" $ROOT/
STAGE=$B/stage/Kadr
rm -rf $B/stage && mkdir -p $STAGE
ditto $APP $STAGE/Kadr.app
cp "$DOCS/Как установить.txt" "$DOCS/Как пользоваться.txt" $STAGE/
rm -f $ROOT/Kadr.zip
ditto -c -k --norsrc --noextattr --keepParent $STAGE $ROOT/Kadr.zip
du -sh $ROOT/Kadr.app $ROOT/Kadr.zip
