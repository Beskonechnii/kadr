#!/bin/zsh
# Статический ffmpeg для раздачи: LGPL (без x264/GPL), libass через CoreText, VideoToolbox.
# Зависит только от системных библиотек macOS — ни Homebrew, ни dylib рядом не нужны.
set -euo pipefail

HERE=${0:A:h}
B=$HERE/build
SRC=$B/src
PREFIX=$B/deps
TOOLS=$B/tools
OUT=$B/ffmpeg
export MACOSX_DEPLOYMENT_TARGET=15.0   # = минимум колёс mlx
export CFLAGS="-O2 -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
export CXXFLAGS=$CFLAGS LDFLAGS="-mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
# только наши .pc — чтобы не подтянуть библиотеки Homebrew
export PKG_CONFIG_LIBDIR=$PREFIX/lib/pkgconfig PKG_CONFIG_PATH=
PKGCONF=/opt/homebrew/bin/pkg-config
J=$(sysctl -n hw.ncpu)

typeset -A URL SHA
URL[freetype]=https://downloads.sourceforge.net/project/freetype/freetype2/2.15.0/freetype-2.15.0.tar.xz
SHA[freetype]=93cd47eb719c325107f2d3cf087d06299e944c3b48a40b55537302304dea1dd5
URL[fribidi]=https://github.com/fribidi/fribidi/releases/download/v1.0.17/fribidi-1.0.17.tar.xz
SHA[fribidi]=6949dcde27d41cebad1fd741fcafc36d55a1020d2d872d4a6eb3914caabbada2
URL[harfbuzz]=https://github.com/harfbuzz/harfbuzz/releases/download/14.6.0/harfbuzz-14.6.0.tar.xz
SHA[harfbuzz]=d07a007327277708a2a73ae437887cdbaf282937f6d03ca5467723e9099af586
URL[libass]=https://github.com/libass/libass/releases/download/0.17.5/libass-0.17.5.tar.xz
SHA[libass]=2dca25c0e0c837ddf00b52011b3f82cac1e4ddd3ad018227806b0c2288864acc
URL[ffmpeg]=https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
SHA[ffmpeg]=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e

fetch() {  # fetch <name> → печатает путь к распакованной папке
  local f=$SRC/${URL[$1]:t}
  [[ -f $f ]] || curl -fsSL -o $f ${URL[$1]}
  echo "${SHA[$1]}  $f" | shasum -a 256 -c - >/dev/null || { echo "sha256 не сошёлся: $f" >&2; exit 1; }
  local d=$SRC/$1
  rm -rf $d && mkdir -p $d && tar xf $f -C $d --strip-components 1
  echo $d
}

mkdir -p $SRC $PREFIX $OUT/bin
[[ -x $TOOLS/bin/meson ]] || { python3 -m venv $TOOLS && $TOOLS/bin/pip install -q meson ninja; }
export PATH=$TOOLS/bin:$PATH

meson_build() {  # meson_build <dir> <опции...>
  local d=$1; shift
  (cd $d && meson setup _b --prefix=$PREFIX --libdir=lib --buildtype=release \
     --default-library=static --pkg-config-path=$PKG_CONFIG_LIBDIR "$@" >/dev/null \
   && ninja -C _b >/dev/null && ninja -C _b install >/dev/null)
}

echo "→ freetype"
d=$(fetch freetype)
# hvf отключаем явно: libhvf есть только с macOS 15.4
(cd $d && ac_cv_header_hvf_Scaler_h=no ./configure --prefix=$PREFIX --disable-shared --enable-static \
   --without-harfbuzz --without-png --without-brotli --without-bzip2 --with-zlib >/dev/null \
 && make -j$J >/dev/null && make install >/dev/null)

echo "→ fribidi"
meson_build $(fetch fribidi) -Ddocs=false -Dtests=false -Dbin=false

echo "→ harfbuzz"
meson_build $(fetch harfbuzz) -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled \
  -Dicu=disabled -Dcoretext=disabled -Dtests=disabled -Ddocs=disabled -Dutilities=disabled \
  -Dintrospection=disabled

echo "→ libass"
d=$(fetch libass)
(cd $d && PKG_CONFIG=$PKGCONF ./configure --prefix=$PREFIX --disable-shared --enable-static \
   --disable-fontconfig --enable-coretext --disable-libunibreak >/dev/null \
 && make -j$J >/dev/null && make install >/dev/null)

echo "→ ffmpeg"
d=$(fetch ffmpeg)
(cd $d && ./configure --prefix=$OUT --pkg-config=$PKGCONF --pkg-config-flags=--static \
   --extra-cflags="$CFLAGS -I$PREFIX/include" --extra-ldflags="$LDFLAGS -L$PREFIX/lib" \
   --enable-static --disable-shared --disable-debug --disable-doc --disable-ffplay \
   --disable-autodetect --disable-network \
   --enable-libass --enable-libfreetype --enable-libfribidi --enable-libharfbuzz \
   --enable-videotoolbox --enable-audiotoolbox --enable-zlib >/dev/null \
 && make -j$J >/dev/null && make install >/dev/null)

strip $OUT/bin/ffmpeg $OUT/bin/ffprobe
# распакованные исходники — 450 MB; архивы остаются, пересборка их распакует заново
rm -rf $SRC/{freetype,fribidi,harfbuzz,libass,ffmpeg}
echo "Готово: $OUT/bin"
otool -L $OUT/bin/ffmpeg | tail -n +2
