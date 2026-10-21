#!/bin/zsh
# Выпуск версии: tools/release.sh [patch|minor|major|X.Y.Z]  (по умолчанию patch)
# поднимает версию → CHANGELOG «Не выпущено» → номер версии → коммит + тег → сборка .app → push + GitHub Release
set -euo pipefail
ROOT=${0:A:h:h}
cd $ROOT
part=${1:-patch}

[[ $(git branch --show-current) == main ]] || { echo "Релиз только из main" >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo "Есть незакоммиченные изменения" >&2; exit 1; }
grep -q '^## Не выпущено' CHANGELOG.md || { echo "В CHANGELOG.md нет раздела «## Не выпущено»" >&2; exit 1; }

old=$(sed -nE 's/^__version__ = "(.*)"/\1/p' app/__init__.py)
IFS=. read -r MA MI PA <<< $old
case $part in
  major) new=$((MA+1)).0.0 ;;
  minor) new=$MA.$((MI+1)).0 ;;
  patch) new=$MA.$MI.$((PA+1)) ;;
  <->.<->.<->) new=$part ;;   # точный номер — например, при переходе на 0.x
  *) echo "patch | minor | major | X.Y.Z" >&2; exit 1 ;;
esac

sed -i '' "s/^__version__ = \".*\"/__version__ = \"$new\"/" app/__init__.py
# раздел «Не выпущено» становится разделом версии, над ним — новый пустой
sed -i '' "s/^## Не выпущено$/## Не выпущено\n\n## $new — $(date +%F)/" CHANGELOG.md
notes=$(awk -v v="## $new" '$0 ~ "^"v {f=1; next} f && /^## / {exit} f' CHANGELOG.md)

git commit -qam "Release v$new"
git tag -a v$new -m "v$new"
echo "v$old → v$new, собираю…"

packaging/build_app.sh
git push -q origin main v$new
gh release create v$new Kadr.zip --title "Kadr $new" --notes "$notes"
