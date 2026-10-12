#!/bin/sh
# 拡張機能をビルドし、一時的な Blender ユーザーフォルダに入れて結合テストを動かす（普段の設定には触れない）。
# 使い方: scripts/smoke.sh [出力フォルダ]（既定 out/smoke）。VOICEVOX エンジンと char/ の素材が必要。
# エンジンが別のマシンなら VOICEVOX_URL=http://host:50021 scripts/smoke.sh
set -eu
cd "$(dirname "$0")/.."
OUT=$(cd "$(dirname "${1:-out/smoke}")" 2>/dev/null && pwd)/$(basename "${1:-out/smoke}") || OUT="$PWD/out/smoke"
# Blender は build_addon.py と同じく、環境変数 BLENDER、PATH の blender、OS ごとの標準の場所の順で探す
# （Windows では Git Bash で動かす）
BLENDER=$(uv run python -c 'import sys; sys.path.insert(0, "scripts"); from build_addon import find_blender; print(find_blender())')
ZIP=$(uv run python scripts/build_addon.py | tail -1)
export BLENDER_USER_RESOURCES="$OUT/blender_user"
rm -rf "$OUT"; mkdir -p "$OUT"
"$BLENDER" --command extension install-file -r user_default -e "$ZIP" </dev/null
"$BLENDER" -b --factory-startup --python-exit-code 1 --python scripts/blender_smoke.py -- "$PWD/examples/characters.toml" "$OUT" </dev/null
