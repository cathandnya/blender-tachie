#!/bin/sh
# 拡張機能をビルドし、一時的な Blender ユーザーフォルダに入れて結合テストを動かす（普段の設定には触れない）。
# 使い方: scripts/smoke.sh [出力フォルダ]（既定 out/smoke）。VOICEVOX エンジンと char/ の素材が必要。
# エンジンが別のマシンなら VOICEVOX_URL=http://host:50021 scripts/smoke.sh
# 音声デバイスの無いサーバでは、試聴（OpenAL）で警告が出るだけ。ALSOFT_DRIVERS=null を付けると黙る
set -eu
cd "$(dirname "$0")/.."
OUT_ARG=${1:-out/smoke}
mkdir -p "$(dirname "$OUT_ARG")"
OUT="$(cd "$(dirname "$OUT_ARG")" && pwd)/$(basename "$OUT_ARG")"
# Blender は build_addon.py と同じく、環境変数 BLENDER、PATH の blender、OS ごとの標準の場所の順で探す
# （Windows では Git Bash で動かす）
BLENDER=$(uv run python -c 'import sys; sys.path.insert(0, "scripts"); from build_addon import find_blender; print(find_blender())')
ZIP=$(uv run python scripts/build_addon.py | tail -1)
export BLENDER_USER_RESOURCES="$OUT/blender_user"
export TACHIE_CACHE_DIR="$OUT/cache"  # 立ち絵の合成キャッシュも一時的な場所に（普段の共有キャッシュには触れない）
rm -rf "$OUT"; mkdir -p "$OUT"
"$BLENDER" --command extension install-file -r user_default -e "$ZIP" </dev/null
"$BLENDER" -b -noaudio --factory-startup --python-exit-code 1 --python scripts/blender_smoke.py -- "$PWD/examples/characters.toml" "$OUT" </dev/null
# 画面の無い Blender で開く（`blender -b x.blend -a` での書き出し）と、合成キャッシュが無い立ち絵をその場で作り直す
rm -rf "$TACHIE_CACHE_DIR"
"$BLENDER" -b -noaudio "$OUT/smoke.blend" --python-exit-code 1 --python-expr '
import bpy
from bl_ext.user_default.blender_tachie import operators as ops, prefetch
stills = [s for sc in bpy.data.scenes if sc.sequence_editor for s in sc.sequence_editor.strips
          if s.type == "IMAGE" and s.get("tachie_still")]
assert stills and not any(ops.missing_images(s) for s in stills), "開いたときに立ち絵を作り直していない"
sc = bpy.context.scene
assert sc.sequence_editor.use_prefetch and prefetch.SAVED_PAUSED not in sc, "止めたまま保存したプリフェッチを戻していない"
print("OK (background reopen)")
' </dev/null
