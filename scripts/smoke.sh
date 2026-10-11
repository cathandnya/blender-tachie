#!/bin/sh
# 拡張機能をビルドし、一時的な Blender ユーザーフォルダに入れて結合テストを動かす（普段の設定には触れない）。
# 使い方: scripts/smoke.sh [出力フォルダ]（既定 out/smoke）。VOICEVOX エンジンと char/ の素材が必要。
set -eu
cd "$(dirname "$0")/.."
OUT=$(cd "$(dirname "${1:-out/smoke}")" 2>/dev/null && pwd)/$(basename "${1:-out/smoke}") || OUT="$PWD/out/smoke"
BLENDER=${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}
ZIP=$(uv run python scripts/build_addon.py | tail -1)
export BLENDER_USER_RESOURCES="$OUT/blender_user"
rm -rf "$OUT"; mkdir -p "$OUT"
"$BLENDER" --command extension install-file -r user_default "$ZIP" </dev/null
"$BLENDER" -b --factory-startup --python-exit-code 1 --python scripts/blender_smoke.py -- "$PWD/examples/characters.toml" "$OUT" </dev/null
