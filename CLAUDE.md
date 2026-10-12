# blender-tachie

Blender 用の立ち絵アドオン。VOICEVOX の音声に合わせて口パク・まばたきする立ち絵と字幕を
Video Sequencer に並べる。YMM4 の「動く立ち絵」素材も読み込める。名前やコードに YMM の名前は使わない（互換性の説明は可）。
計画と段階は `PLAN.md` を参照。

## 構成
- `src/tachie_studio/tachie/` — 動く立ち絵素材の読み込み（`loader.py`）と合成（`compose.py`）。Blender 非依存、Pillow のみ。
- `src/tachie_studio/voicevox/` — VOICEVOX エンジンの HTTP クライアント。urllib のみ（Blender 同梱 Python で動かすため）。
- `src/tachie_studio/lipsync/` — audio_query からの音素時刻（`timing.py`）、口の状態（`mouth.py`、あいうえお式・音量式）、まばたき（`blink.py`）、wav 読み込み（`wav.py`）。numpy を使わない。
- `src/tachie_studio/pipeline/` — 台本（`script.py`）とキャラ設定 TOML（`config.py`）から、セリフごとの wav と透明連番画像、
  `manifest.json` を書き出す（`render.py`）。「目 × 口」の組み合わせを `cache/` に 1 回ずつ合成し、連番はハードリンク。
- `examples/` — キャラ設定と台本の例（`char/` の素材を参照）。
- `src/tachie_studio/subtitle.py` — 字幕の改行（全角幅で折り返し、句読点はぶら下げ、開き括弧は次行へ）。
- `blender_addon/` — Blender 拡張機能 `blender_tachie`（`blender_manifest.toml`、`properties.py`、`operators.py`、`panels.py`）。
  `blender_addon/tachie_studio` は `../src/tachie_studio` へのシンボリックリンクで、ビルド時に実体をコピーする。拡張内からは `from .tachie_studio... import`。
  シーケンサーのサイドバー「立ち絵」タブ。キャラクター設定は `Scene.tachie.characters`（.blend に保存、TOML で読み書き）。
  セリフを追加すると再生位置に音声・立ち絵（連番画像）・字幕を置き、カスタムプロパティ
  `tachie_id` `tachie_text` `tachie_character` `tachie_speaker` `tachie_folder` `tachie_mouth` `tachie_expression` を付ける。
  台本の流し込み（テキストエディタの Text かファイル）は再生位置から順に、`line_gap` 秒あけて並べる。
  再生成のテキストと表情は、パネルで編集する選択中のストリップから取る（3 本のうち他の 2 本は古いまま）。
  再生成は同じ `tachie_id` の 3 本を消して同じ位置に置き直す。長さが変わったら後ろのストリップをずらす（重ならない順に動かす）。
  待機ストリップ（`tachie_idle` = キャラ名）は各キャラの立ち絵チャンネルの隙間を、シーン開始から最後のセリフの終わりまで埋める。
  セリフの追加・再生成のたびに全部消して作り直す（重なり判定・ずらしの対象外）。画像は `cache/` の合成画像を
  `elements` で直接並べる（同じファイル名を何度並べてもよい。1.8 万フレームで 1.4 秒）。
  「立ち絵だけを置く」はセリフなしの立ち絵（まばたきのみ、`tachie_still` = キャラ名、`tachie_expression`）を
  再生位置に置く。待機の作り直しでは消えず、待機の範囲の終わりにも数える。伸ばしたら「作り直す」で今の長さに合わせる。
  セリフごとの声（読み上げ速度・声の高さ・抑揚・音量・開始/終了無音・間の長さ、`properties.VOICE_PROPS`）は
  **音声ストリップだけ**の `tachie_voice_<prop>` に持つ（3 本の不一致を避ける）。置くときにキャラの既定値をコピーし、
  以後キャラの既定値を変えても置いたセリフは変わらない（YMM4 と同じ。「置いたセリフにも当て直す」で反映）。
  スタイルは `tachie_speaker`（3 本とも）。再生成はこれらを引き継ぐ。セリフのキャラクターを変えると、
  スタイルと声はそのキャラの既定値、無い表情プリセットは基本に戻る。範囲は `id_properties_ui` で付ける（Strip でも使える）。
  発音（AquesTalk 風記法 `キョ'ウワ/イ'イ/テ'ンキ`）も音声ストリップの `tachie_kana`（今の発音）と `tachie_kana_auto`
  （そのテキストの自動の読み）に持つ。再生成は `tachie_kana` が自動と違い、かつテキストが置いたときのまま
  （`tachie_placed_text`）のときだけ使う（`line_kana`）。発音は `POST /accent_phrases?is_kana=true` で accent_phrases を
  差し替える。エンジンは長音を「オ」、無声化を `_` で表記し、アクセントの無い句はエラーにする。
  台本・セリフ入力欄・置いたセリフのテキスト（再生成時）では最後に `{発音}` と書ける（`script.split_kana`）。
  試聴は `aud` で出力フォルダの `preview.wav` を鳴らす。
  抑揚の編集（モーラごとの pitch / consonant_length / vowel_length）は音声ストリップの `tachie_accent`（accent_phrases の JSON）と
  `tachie_accent_key`（編集したときの [テキスト, 発音, スタイル]）。どれかが変われば捨てて取り直す（`line_accent`）。
  自動の読みはユーザー辞書で変わるので、再生成では編集したときの読みを `Line.accent_kana` で渡し、`render_line` が今の読みと比べる。
  発音を「取り直す」と抑揚の編集も捨てる。優先は 抑揚の編集 > 発音 > 自動。ダイアログは `invoke_props_dialog` で、
  中身は WindowManager の `tachie_accent`（句ごとの `shift` は差分を音高に足す）。バックグラウンドでは開けないので、
  スモークテストは中身を入れて execute だけ呼ぶ。
  ユーザー辞書は WindowManager の `tachie_dict`（入力欄）と `operators._dict_words`（一覧のキャッシュ）。
  キャラクター追加の初期値は アドオン設定（`extension_path_user` の `character_defaults.toml`、キーは素材フォルダ名、
  表示名は `name`）→ 素材フォルダの `tachie.toml`（キャラ名のテーブルなし、`folder` なし）→ 固定値 の順で重ねる（`merge_tables`）。
  立ち位置は指定が無いときだけ左右交互、チャンネルは他と重なるときだけ空き番号へずらす。
  メニューの「初期値として保存」で両方に書ける（tachie.toml にある声のキーは残す）。CLI の `parse_characters` も tachie.toml で補う。
  書き出し（キャラ設定・初期値）は空のパーツも `"目" = ""` / `"他" = []` と明示し、tachie.toml の値で補われないようにする。
  消した表情プリセットは書けないので、tachie.toml にあるプリセットは戻る（仕様）。
- `scripts/build_addon.py` — Pillow の wheel（cp313 / macOS arm64）を取ってきて `dist/blender_tachie-<ver>.zip` を作る。`--install` で Blender に入れる。
- `scripts/smoke.sh` + `scripts/blender_smoke.py` — 一時ユーザーフォルダ（`BLENDER_USER_RESOURCES`）に入れて 2 キャラ分置き、フレームを `out/smoke/` に書き出す結合テスト。
  エンジンが別のマシンなら `VOICEVOX_URL=http://pino.local:50021 scripts/smoke.sh`。
- `scripts/blender_screenshot.py` — smoke.sh のあとに、Blender を画面ありで起動して 立ち絵パネルとシーケンサーを
  `out/screenshots/sequencer.png` に撮る（`bpy.ops.screen.screenshot`。画面収録の許可は不要）。コマンドはファイル冒頭。
- `char/` — 手元の立ち絵素材（ずんだもん、四国めたん）。利用規約があるので git 管理外。テストは無ければ skip する。

## コマンド
- `uv sync` — 環境構築（Python 3.13。Blender 5.2 同梱 Python に合わせる）
- `uv run pytest`
- `uv run python -m tachie_studio.tachie char/ずんだもん --list` — パーツ一覧
- `uv run python -m tachie_studio.tachie char/ずんだもん --eye 普通白目_カメラ目線 --mouth むふ --other 右腕_基本 --other 左腕_基本 -o out/z.png`
- `uv run python -m tachie_studio.lipsync "こんにちは、ずんだもんなのだ。" --speaker 3 -o out/line` — wav とフレームごとの口・目の状態（JSON）
- `uv run python -m tachie_studio.lipsync --speakers` — スタイル ID 一覧
- `uv run python -m tachie_studio.pipeline examples/script.txt -c examples/characters.toml -o out/demo --scale 0.5 --idle 5`
- `uv run python scripts/build_addon.py [--install]` — 拡張機能のビルド（とインストール）
- `scripts/smoke.sh` — Blender での結合テスト（VOICEVOX と char/ が必要、20 秒ほど）

## 環境
- Blender 5.2.2 LTS（`brew install --cask blender`、`/opt/homebrew/bin/blender`）。同梱 Python 3.13.13、numpy 2.3 あり、Pillow なし。
  - VSE の API は `scene.sequence_editor.strips`（`sequences` は無い）。`strips.new_image / new_sound / new_effect(type="TEXT") / new_meta`。
  - テキストストリップ: `text`, `font`, `font_size`, `wrap_width`, `location`, `use_outline`, `use_box` など。
  - コマンドラインで試す: `blender -b --factory-startup --python script.py`
  - `frame_final_*` は非推奨（6.0 で削除予定）。`left_handle` / `right_handle` / `duration` を使う。
  - **シーケンサーはワークスペースの `sequencer_scene` を表示する**（5.0 から）。未設定だと何も映らず、
    `sequencer.view_all` などの poll も通らない。アドオンは `seq_scene(context)`（`context.sequencer_scene` か
    `context.scene`）を使い、ストリップを置くときに未設定ならウィンドウのシーンを設定する。
  - サイドバーのタブ（`Region.active_panel_category`）は読み取り専用。
  - 画像ストリップの `use_flip_x` は `transform.offset_x` ごと画面中央で反転する。左右反転したキャラは
    offset の符号を逆にする（`properties.apply_placement`）。立ち位置と反転は置いてあるストリップに即反映する。
  - 字幕のスタイル（フォント・サイズ・色・縁取り・高さ）もキャラクターごとで、変えると置いてある字幕に即反映する
    （`properties.apply_subtitle`、改行も入れ直す）。フォントは `system_fonts()` の一覧から選べる。
    `bpy.data.fonts` の名前は英語表記（例: ヒラギノ丸ゴ → "Hiragino Maru Gothic Pro W4"）。
  - `bpy.ops.sequencer.connect` はシーケンサーの画面のコンテキストが必要（バックグラウンドでは使えない）。
  - API で `frame_start` を動かして他と重なると、Blender が勝手に別チャンネルへ逃がす。ずらすときは順序に注意。
  - 既定のビュー変換 AgX だと立ち絵の色が淡くなるので、セリフ追加時に Standard へ切り替える。
  - 日本語フォントは `/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc`（ファイル名が NFD なので glob で探す）。
- VOICEVOX エンジンは `http://127.0.0.1:50021`（0.25.2）。

## 動く立ち絵の解釈（実装で決めたこと）
- 連番は公式 FAQ どおり `.0` = 閉じ → `.1`, `.2` … → 数字なし = 開き。`Part.frames` はこの順。
  手元のずんだもん・四国めたんの**目**は変換 .bat の都合で逆（`.0` = 開き、`.1` = 閉じ）。
  まばたきは `.0 → .1 → … → .N` をファイル順に再生して数字なしに戻す。この順ならどちらの素材でもまばたきに見える。
- 口も手元の素材は数字なし = `.0` と同じ（閉じ）で、最後の番号が開き。`Part.levels`（音量口パクの段階）は
  数字なしが `.0` と同じ内容なら除く。
- 音素時刻は VOICEVOX エンジンの計算に合わせた: 音素ごとに `/speedScale` してから 93.75fps のフレームに丸める、
  疑問文（`is_interrogative`）は最後の母音を 0.15 秒足す。これで wav の長さと一致する（テストあり、エンジン起動時のみ）。
  念のため `scale_to` で wav の長さに合わせる。
- あいうえお式の口: 母音はその形、N・cl・pau は閉じ、m/b/p の子音は閉じ、他の子音は続く母音を先取りする。
- 他1〜3・後1〜3は「他・後フォルダから最大 3 つ選ぶスロット」と解釈した。
- 髪は 2 回描く: 奥（顔色より手前、口より奥）は不透明、眉より手前は ini の opacity（既定 1.0）。
- 合成モードが通常以外のパーツは全レイヤーの手前に描く。対応は通常と乗算のみ。
- ファイル名は NFC に正規化して比較する（macOS の NFD 対策）。
- 既定のパーツは、連番のあるものを優先して名前順の最初。
- キャラ設定の TOML は日本語キーを `"ずんだもん"` のように引用符で囲む（TOML 1.0 の決まり）。
  `[キャラ.parts]`（表情）、`[キャラ.expressions.名前]`（表情プリセット）、`[キャラ.layout]`（position / flip / height / channel）、
  `[キャラ.subtitle]`（font / size / color …）。
- 表情プリセットは基本の表情から変えるパーツだけを持つ。他・後はどれか 1 つでも指定すると丸ごと置き換え
  （`CharacterConfig.expression_for`）。台本では `キャラ名(表情): セリフ`（`（）` `[]` も可）。
  ストリップには `tachie_expression` を付け、再生成はそれを使う。待機ストリップは基本の表情。
- 口の動かし方 `mouth = "auto"`: 母音画像があればあいうえお式、無ければ音量式。`vowel` で母音画像の無い素材は
  母音を開き具合に置き換える（a=最大、i/u=小さめ）。
