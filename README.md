# blender-tachie

Blender 用の立ち絵アドオンです。VOICEVOX の音声に合わせて口パク・まばたきする立ち絵と字幕を、
Video Sequencer に並べます。YMM4 の「動く立ち絵」素材も読み込めます。

## できること

- セリフを入力すると、音声・口パクする立ち絵・字幕の 3 本を再生位置に置く
- 台本（1 行に `キャラ名: セリフ`）を一括で流し込む
- キャラクターごとの設定: 話者、表情、表情プリセット（`キャラ名(困り): セリフ`）、立ち位置、左右反転、字幕のフォントと色
- セリフの修正と再生成（長さが変わったら後ろのストリップをずらす）
- 喋っていないキャラクターは、まばたきだけする待機の立ち絵で表示
- 口パクはあいうえお式（音素の母音）と音量式
- 立ち絵の合成と口パクの計算は Blender に依存しない Python モジュールで、コマンドラインからも使える

## 立ち絵素材

YMM4 の動く立ち絵素材（「眉」「目」「口」「髪」「顔色」「体」「他」「後」のフォルダ）をそのまま読み込みます。
まばたき・口パクの連番（`パーツ名.0.png` など）、`パーツ名.ini` の opacity、合成モードの通常と乗算に対応しています。
仕様: https://www.manjubox.net/ymm4/faq/立ち絵機能/動く立ち絵素材の作り方

素材ごとの利用規約（Blender での使用可否など）は各自で確認してください。

## 必要なもの

- Blender 5.2（macOS / Apple Silicon で確認）
- VOICEVOX エンジン（`http://127.0.0.1:50021`）
- 開発: [uv](https://docs.astral.sh/uv/)

## インストール

```
uv run python scripts/build_addon.py --install
```

`dist/blender_tachie-<version>.zip` を作り、Blender に入れます（Pillow を同梱）。
Video Sequencer のサイドバー（N キー）の「立ち絵」タブから使います。

## コマンドライン

```
uv run python -m tachie_studio.tachie <素材フォルダ> --list                  # パーツ一覧
uv run python -m tachie_studio.lipsync "こんにちは" --speaker 3 -o out/line  # wav と口パクの JSON
uv run python -m tachie_studio.pipeline examples/script.txt -c examples/characters.toml -o out/demo
```

最後のコマンドは、台本からセリフごとの wav と口パク入りの透明連番画像を書き出します（他の動画編集ソフト用）。

## 開発

```
uv sync
uv run pytest
scripts/smoke.sh   # Blender での結合テスト（VOICEVOX エンジンと char/ の素材が必要）
```
