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
- 書き出し設定のプリセット（`operators.RENDER_PRESETS`、`apply_render_preset`）: Blender 5 は `image_settings.media_type = "VIDEO"`
  にしてから `file_format = "FFMPEG"`。解像度・fps はセリフの連番・立ち位置・大きさに効くので、置いたあとに変えたら全再生成を案内する。
- `src/tachie_studio/srt.py` — 字幕ファイル（SRT / WebVTT）。アドオンの「書き出し」パネルは置いた字幕ストリップの位置（シーン開始 = 0 秒、ミュートは除く）、CLI の `--srt` はセリフを順に `--gap` 秒あけて並べた時刻。
- `src/tachie_studio/ymmp.py` — YMM4 のプロジェクト（.ymmp、UTF-8 BOM の JSON）を読む（#15）。`Timelines`（古い版は `Timeline`）、
  アニメーションできる値は最初の値（`value`）。パーツは画像の Windows パスのファイル名。VoiceParameter → audio_query は
  Speed/Intonation/PauseLengthScale が %、Pitch は 100 が 0（(Pitch-100)/100）。`Pronounce.AudioQuery` があればその値と
  accent_phrases を優先。ボイスの `Length` = 声 + AdditionalTime。`VoiceCache`（圧縮された音声）は使わない。
  アドオンは `blender_addon/ymmp_import.py`（`tachie.import_ymmp`）: 立ち絵の無いキャラは素材を探さずに追加する。素材フォルダは `ymmp_material_root` の下（2 階層）から
  YMM4 と同じ名前で探す（`add_character` を使う）。既にあるキャラは設定を変えない。表情は 立ち絵アイテムの基本 + ボイス／表情アイテムの差分 を
  キャラの基本との差分にして、同じ中身のプリセットを使うか作る（`preset_name`）。YMM4 の空のパーツは素材の「(非表示)」「_」にする。
  立ち絵アイテムの基本がキャラの基本と違う区間は、セリフの区間を除いて表情ストリップにする。余韻は次のセリフに 1 フレーム
  あけて重ならないように縮める（声の終わりより `SIMULTANEOUS_FRAMES` 以上前から始まる別のキャラのセリフは同時に喋るので相手にしない）。YMM4 の音量 50 = 等倍（未確認）、立ち絵の X/Y は画像の中心（`position_x`/`position_y`）、
  Zoom は素材の高さに対する %（`height`）。キャラの字幕の大きさは FontSize × Zoom（字幕の拡大率）。実ファイルのテストは `YMMP_DIR`。
  画像・動画・音声・テキスト（段階 2）: `ymmp.Media`（`Project.media`）を `blender_addon/ymmp_media.py` が置く。座標は画面中央から
  下向きのピクセル、Zoom 100 = 素材の 1px が出力の 1px なので、`fit_method="ORIGINAL"` の transform（offset・scale・rotation は
  符号を反転）に写す。テキストは BasePoint → anchor / location。2 値の直線移動はキーフレーム（直線）。`front_layer`（立ち絵
  アイテムの一番下のレイヤー）より下の画像・動画は、アドオンのチャンネルの下にレイヤーの順で置き、空きが足りなければ
  `shift_addon_channels` で設定と上のストリップをまとめてずらす。手前は見出しなどより上、音声は効果音と同じ探し方。
  Blender の動画ストリップは素材の 1 フレーム = シーンの 1 フレーム（フレームレートを合わせない）ので、素材の fps × 再生速度が
  シーンと違えば SPEED ストリップ（`FRAME_NUMBER` に素材のフレーム番号の直線キー）を重ねる。retiming キーの
  `timeline_frame` を API で動かすと、他のストリップがあるシーンで Blender 5.2.2 が落ちる。置いたストリップは `tachie_ymmp`。
  ファイルは `MediaFinder`（元のパス → .ymmp のフォルダ → その下 3 階層 → `ymmp_media_root` の下）。
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
  表情ストリップ（`tachie_face` = キャラ名、`tachie_expression`）はミュートしたカラーストリップで、字幕より上の空き
  チャンネルに置く。重なる範囲だけ立ち絵（セリフ・セリフなし・待機）がその表情になる。重なったらチャンネルが上、
  同じなら後から始まるものが優先。セリフは書き出し時に保存した動き（`lines/<id>.json`、`render.LineMotion`: 母音・音量・
  まばたきの seed）から `segment_frames` で並べ直し、`lines/<id>_f<hash>/` にリンクして `strip.directory` を差し替える
  （音声は合成し直さない。元は `tachie_frames_dir`、今の区間は `tachie_faced`）。セリフなしは作り直し、待機は
  `refresh_idle` が表情ストリップの境目で分ける。json の無い古いセリフは再生成するまで効かない。
  自動の反映は `depsgraph_update_post` のハンドラで署名（`faces_signature`）の変化を見て、タイマーでモーダル操作の
  終わりを待ってから `refresh_faces` する（`_applying_faces` で再入防止）。反映では undo の段を積まない（直前の操作の段は反映前の
  状態で、元に戻すと署名（`tachie_faced` を含む）が変わって描き直されるため）。キャラに無い表情の表情ストリップは無視する。
  同時に喋るセリフ（#17）: `line_channels` が置く前にチャンネルを決める。立ち絵はキャラのチャンネル（同じキャラは重ねられない）、
  音声・字幕・キャラ名は設定のチャンネルが埋まっていれば上の空き（立ち絵・見出し・他の役割の設定チャンネルは飛ばす、
  キャラ名は字幕より上）。セリフのストリップはチャンネルではなく `tachie_id` と種類・`tachie_role` で探す。
  字幕をセリフごとにずらす量は字幕ストリップの `tachie_sub_x` / `tachie_sub_y`（`properties.place_text`、再生成で引き継ぐ）。
  自動反映（`motion_signature` に含める）で `refresh_motion` がキャラ名にも写して location を当て直す。
  `subtitle_clashes` で時間も位置も重なる字幕を知らせる。
  セリフの形（#18、`properties.LINE_LAYOUTS`）: FULL（立ち絵あり）/ SUBTITLE（音声と字幕）/ AUDIO（音声だけ）。全ストリップの
  `tachie_layout`（無い古いセリフは立ち絵があれば FULL、`line_layout_of`）で、再生成で引き継ぐ。新しく置くセリフはキャラの
  `line_layout`（TOML は `[キャラ.layout]` の `lines`）。素材フォルダの無いキャラ（`folder = ""`、TOML は folder を省略、
  `CharacterConfig.folder = None`、CLI では使えない）の FULL は SUBTITLE（`operators.line_layout`）。立ち絵の無い形は
  `render_line(cache=None)` で連番を書かず（wav・json・長さは同じ）、`line_channels` はキャラのチャンネルを見ない（同じキャラでも
  重ねてよい）。`refresh_idle` はそのキャラの立ち絵の無いセリフの区間を待機で埋めない（立ち絵を消す）。
  `place_strips` の戻り値は置いたものだけなので、番号ではなく `line_image` / `line_subtitle` / `line_texts` / `line_end` /
  `line_focus` で取る。音声だけのセリフは音声ストリップが余韻の分短いので、セリフの長さは全ストリップの
  `tachie_frames`（`line_end`）。立ち絵の無いキャラのセリフを立ち絵のあるキャラに変えたら形はそのキャラの既定。立ち絵の無いキャラは `add_voice_character`（メニュー「立ち絵なしのキャラクターを追加」）、
  素材に関わるパネルは `_WithTachie` で隠す。YMM4 の立ち絵の無いキャラ（`has_tachie` が False）もこれで読み込む。
  ストリップの位置は `content_start` / `duration` を使う（`frame_start` / `frame_final_duration` は 6.0 で削除予定）。
  立ち絵の動き（#11、`blender_addon/motion.py`）: 立ち絵ストリップの transform.offset_x / offset_y / rotation と
  blend_alpha にキーフレームを打つ（シーンの Action、Blender 5 はスロットの channelbag。`anim_utils.action_get_channelbag_for_slot`）。
  `refresh_motion` は管理しているパス（と、もう無いストリップのパス）の F カーブを全部消してから作り直す。喋っている区間は
  lines/<id>.json の音量、登場・退場はキャラのチャンネルで続いて並ぶ立ち絵（待機を含む）のまとまりの最初と最後。
  セリフごとの位置・大きさは立ち絵ストリップの `tachie_pos_x` / `tachie_pos_y` / `tachie_scale`（再生成で引き継ぐ）。
  拡大・回転の中心 `transform.origin` は足元 (0.5, 0)。`refresh_idle` の最後と、自動反映（`auto_signature` に
  `motion.motion_signature` を含める）で作り直す。自動反映は動きだけが変わったときは `refresh_faces` を呼ばない（`auto_signature` は (表情, 動き) の組）。
  キャラの動きの設定は変えるとすぐ作り直す。TOML では `[キャラ.layout]` の bounce / sway / period / enter / exit / effect_seconds（既定値は書かない）。
  字幕の装飾（#12）: テキストストリップの box（`use_box` / `box_color` / `box_margin` / `box_roundness`）。キャラ名は
  セリフの 4 本目のストリップ（`tachie_role = "name"`、`name_channel`、字幕の行数から高さを見積もる `properties.apply_name`）で、
  字幕ファイル（`subtitle_cues`）には入れない。字幕のフェードインは `motion.refresh_motion` が blend_alpha に打つ。
  見出しテロップは `tachie_headline` のテキストストリップ（`headline_channel`、スタイルはシーンの `headline_*` で即反映）。
  表情ストリップは字幕・キャラ名・見出しのチャンネルより上の空きに置く。1 文字ずつ出す字幕は作っていない
  （テキストストリップに文字を段階的に出す仕組みが無い）。
  BGM のダッキング（#14、`blender_addon/ducking.py`）: `tachie_bgm` の印を付けた音声ストリップ（元の音量は
  `tachie_bgm_volume`）の volume に、セリフの音声ストリップ（ミュートは除く）が鳴っている区間だけ下げるキーフレームを打つ。
  切れ目が `MERGE_SECONDS` + フェード 2 回分より短ければつなぐ。設定はシーンの `duck_volume`（割合）・`duck_fade`。
  自動反映の署名は (表情, 動き, ダッキング) の 3 つ組で、ダッキングだけ変わったら `refresh_ducking` だけ呼ぶ。
  `refresh_motion_now` もダッキングを作り直す。
  効果音（#14）: アドオン設定の `sound_effects`（`TachieSoundEffect`: 名前・ファイル・音量）。`tachie.add_sound_effect` は
  再生位置か選んだセリフ（`selected_targets`）の頭に置き、`tachie_se` = 名前を付ける。チャンネルは `se_channel` から上で
  空いていて、音声・字幕・キャラ名・見出し・立ち絵のチャンネルでないところ（`_se_channel`）。
  まとめて変える（`tachie.batch_edit`）: 選択中のセリフ（`tachie_id` 単位）・セリフなし・表情ストリップを対象に、
  ダイアログで値を変えた項目（`use_*`、値の update で自動で立つ）だけを全部に当てる。音量・パンだけなら作り直さず
  音声ストリップに直接書き、他は後ろのセリフから `regenerate` する。表情はキャラ全員が持つプリセットだけ選べる。
  「立ち絵だけを置く」はセリフなしの立ち絵（まばたきのみ、`tachie_still` = キャラ名、`tachie_expression`）を
  再生位置に置く。待機の作り直しでは消えず、待機の範囲の終わりにも数える。伸ばしたら「作り直す」で今の長さに合わせる。
  セリフごとの声（読み上げ速度・声の高さ・抑揚・音量・開始/終了無音・間の長さ、`properties.VOICE_PROPS`）は
  **音声ストリップだけ**の `tachie_voice_<prop>` に持つ（3 本の不一致を避ける）。置くときにキャラの既定値をコピーし、
  以後キャラの既定値を変えても置いたセリフは変わらない（YMM4 と同じ。「置いたセリフにも当て直す」で反映）。
  スタイルは `tachie_speaker`（3 本とも）。再生成はこれらを引き継ぐ。セリフのキャラクターを変えると、
  スタイルと声はそのキャラの既定値、無い表情プリセットは基本に戻る。範囲は `id_properties_ui` で付ける（Strip でも使える）。
  音量・パンは音声ストリップの `volume` / `pan` をそのまま使う（すぐ反映、再生成でも引き継ぐ）。フェードイン/アウトと
  セリフ後の余韻は `tachie_fade_in` / `tachie_fade_out` / `tachie_after`（秒）で、wav と連番に焼き込む（再生成で反映）。
  余韻は音声の後ろに口を閉じた立ち絵（まばたきは続ける）を足すので、立ち絵と字幕だけが音声より長い。
  キャラの既定値は `audio_*`（`properties.AUDIO_PROPS`）、TOML は `[キャラ.audio]`。再生速度（リサンプリング）は
  口パクの連番も伸ばす必要があるので作らない（VOICEVOX の読み上げ速度・声の高さで代わりにする）。
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
- `scripts/build_addon.py` — Pillow の wheel（cp313、macOS arm64 / x64・Windows x64 / arm64・Linux x64）を取ってきて
  `blender --command extension build --split-platforms` で OS ごとの `dist/blender_tachie-<ver>-<OS>.zip` を作る
  （zip の manifest には Blender が `[build.generated]` でその OS の platforms / wheels を書き足す）。`--install` でこの OS 用を
  Blender に入れる（普段の Blender に入るので、試すだけなら smoke.sh の一時ユーザーフォルダを使う）。
  Blender は `BLENDER` → PATH → 標準の場所の順で探す。動作確認は macOS arm64 だけ、Linux はコンテナで pytest のみ。
  Windows ではドライブをまたぐと `bpy.path.relpath` が ValueError を出すので、`_path_for_blend` は絶対パスに戻す。
- `scripts/smoke.sh` + `scripts/blender_smoke.py` — 一時ユーザーフォルダ（`BLENDER_USER_RESOURCES`）に入れて 2 キャラ分置き、フレームを `out/smoke/` に書き出す結合テスト。
  エンジンが別のマシンなら `VOICEVOX_URL=http://pino.local:50021 scripts/smoke.sh`。
- `scripts/blender_screenshot.py` — smoke.sh のあとに、Blender を画面ありで起動して 立ち絵パネルとシーケンサーを
  `out/screenshots/sequencer.png` に撮る（`bpy.ops.screen.screenshot`。画面収録の許可は不要）。コマンドはファイル冒頭。
- `char/` — 手元の立ち絵素材（ずんだもん、四国めたん）。利用規約があるので git 管理外。テストは無ければ skip する。

## コマンド
- `uv sync` — 環境構築（Python 3.13。Blender 5.2 同梱 Python に合わせる）
- `uv run pytest`
- `uv run python -m tachie_studio.tachie char/ずんだもん --list` — パーツ一覧
- `uv run python -m tachie_studio.tachie char/ずんだもん --eye 普通白目_カメラ目線 --mouth むふ --other 左腕_基本 --other 右腕_基本 -o out/z.png`
- `uv run python -m tachie_studio.lipsync "こんにちは、ずんだもんなのだ。" --speaker 3 -o out/line` — wav とフレームごとの口・目の状態（JSON）
- `uv run python -m tachie_studio.lipsync --speakers` — スタイル ID 一覧
- `uv run python -m tachie_studio.tachie.psd_animation char/ずんだもん char/ずんだもん/<PSD> [--dry-run]` — 動く立ち絵の連番から PSD 立ち絵用の口パク・まばたき設定（`<PSD 名>-ymm.json`）を作る
- `uv run python -m tachie_studio.pipeline examples/script.txt -c examples/characters.toml -o out/demo --scale 0.5 --idle 5`
- `uv run python scripts/build_addon.py [--install]` — 拡張機能のビルド（とインストール）
- `scripts/smoke.sh` — Blender での結合テスト（VOICEVOX と char/ が必要、20 秒ほど）
- CI（`.github/workflows/ci.yml`、GitHub ホストの ubuntu-latest のみ。セルフホストランナーは使わない。リポジトリは非公開なので Actions の無料枠の分数を使う）:
  - test: `uv sync --locked` → `uv run pytest`（PR・リリースのタグ・手動。main への push では回さない＝PR で回す運用）。素材と VOICEVOX が無いので、それを使うテストは skip。
    結合テスト（smoke.sh）は CI では回さない（手元で回す）
  - build / release: `v*` のタグを push したときだけ。Linux 版 Blender（`BLENDER_VERSION`、actions/cache）で `build_addon.py` →
    OS ごとの zip で GitHub のリリースを作る（既にあれば zip を差し替える）。タグは `blender_manifest.toml` の version と同じに
    （違えば build で止める）。手順: manifest（と pyproject.toml）の version を上げてコミット → `git tag v0.2.0` → `git push origin v0.2.0`
  - `astral-sh/setup-uv` はメジャーバージョンのタグが無いので完全なバージョンで指定する。Linux 版 Blender は画面なしでも
    libX11 などが要る（apt で入れる）

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
- 声のエンジンはキャラごと（`TachieCharacter.engine` = DEFAULT / AIVIS / SHAREVOX / COEIROINK / CUSTOM、`operators.engine_url`、
  TOML は `engine = "URL"`、CLI もキャラごとのクライアント）。互換エンジンは実機では未確認。AivisSpeech は audio_query の
  モーラの長さ・音高が常に 0 なので、`render_line` はモーラの長さの合計が 0 なら音量式にする。スタイル ID は負にもなる。

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
- 表情プリセットは基本の表情から変えるパーツだけを持つ。他・後は**スロットごと**に置き換える（YMM4 と同じ。
  `"他" = ["", "右腕_指差す"]` は他2 だけ。"" のスロットは基本のまま、消すときは素材の透明なパーツ `_` などを選ぶ）。
  `Expression.others` / `backs` は位置を保ったリストで "" は空きスロット（`config.merge_slots`、合成では飛ばす）。
  YMM4 の並びは 他1 = 左腕、他2 = 右腕（実物の preset.ini から）。基本の表情もこの順にしないとプリセットで腕が重なる。
  重ねるのは `CharacterConfig.expression_for`。台本では `キャラ名(表情): セリフ`（`（）` `[]` も可）。
  ストリップには `tachie_expression` を付け、再生成はそれを使う。待機ストリップは基本の表情。
- YMM4 の表情プリセットは素材フォルダの `preset.ini`（UTF-8 BOM、`[名前]` に `眉=x.png`、`他1=x.png` …）。
  `tachie.presets.load_presets` で読み、素材に無いパーツは警告して飛ばす。`config.load_material_defaults` が
  tachie.toml の下に重ねるので、キャラ追加と CLI の両方で補われる（優先: アドオン設定 → tachie.toml → preset.ini）。
  メニュー「YMM4 の表情プリセットを読み込む」でも読める（同じ名前は上書き）。
- シンプル立ち絵（YMM4。眉・目などのフォルダが無く、画像だけのフォルダ）は `Character.kind == "simple"`。
  同じ画像の一覧を体（口を閉じた画像、既定は名前に「デフォルト」）と口（セリフ中の画像、既定は「口開」）の両方に入れ、
  合成は画像 1 枚（`compose._simple_layers`: mouth_frame 0 なら体、それ以外は口）。口パクは `simple_mouth` が
  閉じ = 体・開き = 口の 2 段階の口パーツを作るので、音量式でそのまま動く。まばたきは無い。GIF / WebP は最初のフレームだけ。
  パネルの表情の欄は 2 つだけ（`panels.part_rows`）。
- PSD 立ち絵（#7）: `load_character(<.psd>)` が `kind == "psd"` の Character（`psd` に PsdDocument、`animations` に
  `-ymm.json` の口パク・まばたき）。表情は `Expression.layers`（{パス: 表示するか}、TOML は `"レイヤー" = { … }`、
  アドオンは `psd_layers` に JSON）で、`*` を True にすると同じフォルダの他の `*` は消える（`psd.apply_layers`）。
  口パク・まばたきは表示中のレイヤーを含む組（`active_animation`）を、閉じ → 開きの段階として `PsdPart` に見せるので、
  音量式の口パク・まばたきがそのまま動く（`compose.psd_state` が mouth_frame / eye_frame の段のレイヤーを選ぶ）。
  キャラ追加で PSD の表示状態の口が口パクの組に無ければ、最初の組の閉じた口を基本にする。
  合成は通常・通過・乗算（他はクリッピングも含め通常）。圧縮は無圧縮・RLE・ZIP。
- PSD の読み込み: `tachie/psd.py` がレイヤーの木（フォルダ・表示・名前、`index` は下から 0 始まりで
  フォルダの終わりの記録も数える）を自前で読む（Pillow はフォルダを読めず、手元の PSD ではレイヤー情報で落ちる）。
  YMM4 の `<PSD 名>-ymm.json` は `{"Presets": [], "MouthAnimations": [{"Layers": ["n5", "n4"]}], "EyeAnimations": …}`、
  `Layers` は開き → 閉じの順で `n<index>`。`tachie/psd_animation.py` は動く立ち絵の連番の中身（どのパーツの画像と同じか）
  から口パク・まばたきの相手を割り出して書く。フォルダ（`*目セット`）をフォルダ本体の記録の番号で指してよいかと、
  `Presets` の書式は YMM4 で未確認。
- 口の動かし方 `mouth = "auto"`: 母音画像があればあいうえお式、無ければ音量式。`vowel` で母音画像の無い素材は
  母音を開き具合に置き換える（a=最大、i/u=小さめ）。
