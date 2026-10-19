import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import bpy

from . import prefetch
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import channels as lanes
from .properties import AUDIO_PROPS, BAKED_AUDIO, ENGINE_PRESETS, LINE_LAYOUTS, apply_headline, apply_name, PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, apply_placement, apply_subtitle, clear_text_input, place_text, still_flip, system_fonts, text_offset
from .tachie_studio.pipeline import (CharacterConfig, FrameCache, Line, idle_frames, load_characters, load_script,
                                     parse_script, render_line)
from .tachie_studio.pipeline.render import LineMotion, link_frames, still_frames
from .tachie_studio.pipeline.script import script_format, split_kana
from .tachie_studio.subtitle import breaks, mark_breaks
from .tachie_studio.pipeline.config import (LAYERS_KEY, MATERIAL_CONFIG, VOICE_DEFAULTS, VOICE_KEYS, dump_characters, dump_raw_table, dump_table,
                                            load_material_defaults, load_material_table, merge_tables,
                                            overlay_parts, parse_characters)
from .tachie_studio.tachie import PRESET_FILE, Expression, load_character, load_presets
from .tachie_studio.tachie.compose import load_image
from .tachie_studio.tachie.psd import (animation_config_path, apply_layers, load_ymm_presets, read_animation_config,
                                       write_animation_config, ymm_preset)
from .tachie_studio.voicevox import VoicevoxClient, VoicevoxError
from .tachie_studio.voicevox.accent import join_phrases, kana_from_phrases, split_phrase

MORA_UNITS = 2.4  # 抑揚の編集の 1 モーラの幅（UI の単位）

# 読み込んだ素材と合成キャッシュ（Blender を閉じるまで使い回す）
_characters = {}
_caches = {}
# EnumProperty の動的な items は Python 側で参照を持ち続ける必要がある
_style_items = []
_part_items = []
_font_items = []
_font_paths = {}
_expression_items = []
_character_items = []
_dict_words = None  # ユーザー辞書の一覧 [(uuid, 表記, 読み, アクセント)]。None は未取得
_preview = None  # 試聴中の aud のハンドル

NONE = "__none__"
ERRORS = (VoicevoxError, ValueError, FileNotFoundError, KeyError)


def seq_scene(context):
    """シーケンサーが表示しているシーン。Blender 5 からはワークスペースごとに持つ（未設定ならウィンドウのシーン）。"""
    return getattr(context, "sequencer_scene", None) or context.scene


def ensure_sequencer_scene(context) -> None:
    """ワークスペースのシーケンサー用シーンが未設定なら、ウィンドウのシーンにする（置いたストリップが見えるように）。"""
    workspace = getattr(context, "workspace", None)
    if workspace is not None and getattr(workspace, "sequencer_scene", "missing") is None:
        workspace.sequencer_scene = context.scene


def prefs(context):
    return context.preferences.addons[__package__].preferences


def engine_url(context, ch=None) -> str:
    """キャラクターの声のエンジンの URL（ch が None か「既定」ならアドオン設定のもの）。"""
    if ch is None or ch.engine == "DEFAULT":
        return prefs(context).voicevox_url
    if ch.engine == "CUSTOM":
        return ch.engine_url or prefs(context).voicevox_url
    return ENGINE_PRESETS[ch.engine][1]


class _Client(VoicevoxClient):
    """セリフのテキストの改行の書き方（\\n）を改行にしてから読ませる（そのままだと「エヌ」と読む）。"""

    def audio_query(self, text: str, speaker: int):
        return super().audio_query(breaks(text), speaker)


def client(context, ch=None, timeout: float = 60.0) -> VoicevoxClient:
    return _Client(engine_url(context, ch), timeout=timeout)


def output_dir(context) -> Path:
    custom = prefs(context).output_dir
    if custom:
        return Path(bpy.path.abspath(custom))
    if bpy.data.filepath:
        return Path(bpy.path.abspath("//tachie"))
    return _temp_output()


def _temp_output() -> Path:
    return Path(tempfile.gettempdir()) / "blender_tachie"


def _under(path: Path, root: Path) -> Path | None:
    """`path` が `root` の下なら `root` からの相対パス。"""
    try:
        return path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None


@persistent
def _move_temp_output(filepath, *_args):
    """未保存のときに一時フォルダへ書き出したもの（音声・口の動き・立ち絵の連番）を、保存する .blend のフォルダの
    tachie/ へコピーしてストリップの参照を付け替える（保存の前に呼ぶので、保存した .blend には新しい場所が入る）。
    一時フォルダは OS が消すことがあり、出力フォルダも保存すると //tachie に変わるので、そのままだと口の動きが
    見つからず閉じた口になり、いずれ音声も消える。出力フォルダを指定しているときは何もしない。"""
    if not filepath or prefs(bpy.context).output_dir:
        return
    temp, dest = _temp_output(), Path(filepath).parent / "tachie"
    if not temp.is_dir() or temp.resolve() == dest.resolve():
        return
    copied: set[Path] = set()

    failed: set[Path] = set()

    def copy(rel: Path) -> bool:
        """コピーできた（か、元が無い）なら True。失敗したものは付け替えない（一時フォルダの方を指したままにする）。"""
        src, dst = temp / rel, dest / rel
        if rel in copied or not src.exists():
            return True
        if rel in failed:
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except OSError as e:
            print(f"{src} を {dst} にコピーできませんでした: {e}")
            failed.add(rel)
            return False
        copied.add(rel)
        return True

    moved = 0
    with prefetch.paused():  # 裏で描いている途中のストリップの参照を書き換えると落ちることがある
        for scene in bpy.data.scenes:
            sed = scene.sequence_editor
            for s in sed.strips_all if sed else ():
                if s.type == "SOUND" and s.sound and (rel := _under(Path(bpy.path.abspath(s.sound.filepath)), temp)):
                    if copy(rel) and copy(rel.with_suffix(".json")):  # 音声と口の動き
                        s.sound.filepath = str(dest / rel)  # 相対パスにするかは Blender の保存の設定に任せる
                        moved += 1
                elif s.type == "IMAGE" and (rel := _under(Path(bpy.path.abspath(s.directory)), temp)):
                    if copy(rel):
                        s.directory = str(dest / rel) + os.sep
                        moved += 1
    if moved:
        print(f"一時フォルダのセリフ・立ち絵 {moved} 本を {dest} にコピーしました")


def scene_fps(scene) -> float:
    return scene.render.fps / scene.render.fps_base


def load_material(folder: str):
    if not folder:
        raise ValueError("素材フォルダを指定してください")
    folder = os.path.normpath(bpy.path.abspath(folder))
    if folder not in _characters:
        _characters[folder] = load_character(folder)
    return _characters[folder]


def _cache(character, expr: Expression, directory: Path, scale: float) -> FrameCache:
    key = (str(character.path), repr(expr), round(scale, 4), str(directory))
    if key not in _caches:
        _caches[key] = FrameCache(character, expr, directory, scale)
    return _caches[key]


# ---- キャラクター設定 ⇔ pipeline の設定 ----

def _parts_dict(item) -> dict:
    """キャラクターか表情プリセットのパーツ指定を {フォルダ名: 値} にする（空は省く）。

    他・後はスロットの位置を保ったリスト（"" は空きスロット、末尾の空きは落とす）。
    """
    parts = {}
    for category, props in PART_PROPS.items():
        values = [getattr(item, p) for p in props]
        while values and not values[-1]:
            values.pop()
        if values:
            parts[category] = values if category in SLOT_CATEGORIES else values[0]
    layers = item_layers(item)
    if layers:
        parts[LAYERS_KEY] = layers
    return parts


def item_layers(item) -> dict:
    """キャラクターか表情プリセットの PSD のレイヤーの上書き {パス: 表示するか}。"""
    try:
        value = json.loads(item.psd_layers) if item.psd_layers else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def set_item_layers(item, layers: dict) -> None:
    item.psd_layers = json.dumps(layers, ensure_ascii=False) if layers else ""


def find_expression(ch, preset: str):
    for e in ch.expressions:
        if e.name == preset:
            return e
    raise ValueError(f"{ch.name} に表情 {preset!r} がありません")


def config_of(ch) -> CharacterConfig:
    base = parse_characters({ch.name: {"folder": "", "speaker": ch.speaker, "parts": _parts_dict(ch)}}, Path("."),
                            use_material=False)
    return CharacterConfig(
        name=ch.name, folder=Path(bpy.path.abspath(ch.folder)) if ch.folder else None, speaker=ch.speaker,
        expression=base[ch.name].expression, mouth=ch.mouth,
        voice=_config_voice(ch),
        expressions={e.name: _parts_dict(e) for e in ch.expressions},
        layout={"position": ch.position_x, "flip": ch.flip_x, "height": ch.height,
                **({"position_y": round(ch.position_y, 6)} if ch.position_y else {}),
                **_config_motion(ch)},
        subtitle={"font": ch.font_path, "size": ch.font_size, "color": list(ch.text_color),
                  "outline_color": list(ch.outline_color), "outline_width": ch.outline_width,
                  "line_spacing": ch.line_spacing, "y": ch.subtitle_y, "box": ch.subtitle_box, "box_color": list(ch.box_color),
                  "box_margin": ch.box_margin, "box_roundness": ch.box_roundness, "name": ch.show_name,
                  "fade": ch.subtitle_fade},
        audio=character_audio(ch),
        engine="" if ch.engine == "DEFAULT" else engine_url(bpy.context, ch),
    )


# 立ち絵の動きの設定（キャラ設定の layout に書く。既定値のものは書かない）
MOTION_KEYS = {"talk_bounce": "bounce", "talk_sway": "sway", "talk_period": "period",
               "enter_effect": "enter", "exit_effect": "exit", "effect_seconds": "effect_seconds",
               "line_layout": "lines"}


def _config_motion(ch) -> dict:
    out = {}
    for prop, key in MOTION_KEYS.items():
        value, default = getattr(ch, prop), ch.bl_rna.properties[prop].default
        if isinstance(value, str) and value != default or not isinstance(value, str) and abs(value - default) > 1e-6:
            out[key] = round(value, 6) if isinstance(value, float) else value
    return out


def character_audio(ch) -> dict:
    """キャラクターの音声ストリップ側の既定値（volume / pan / fade_in / fade_out / after）。"""
    return {k: round(getattr(ch, f"audio_{k}"), 6) for k in AUDIO_PROPS}


def _config_voice(ch) -> dict:
    """設定ファイルに書く声。既定値と違うものと、素材の tachie.toml にある項目（既定値に戻したことが
    tachie.toml の値で上書きされないように）。"""
    try:
        material = load_material_table(bpy.path.abspath(ch.folder)) if ch.folder else {}
    except (ValueError, OSError):
        material = {}
    keys = {VOICE_KEYS[k] for k in material if k in VOICE_KEYS}
    return {k: v for k, v in character_voice(ch).items() if k in keys or abs(v - VOICE_DEFAULTS[k]) > 1e-6}


def character_voice(ch) -> dict:
    """キャラクターの声の既定値（audio_query のキー → 値、全項目）。"""
    return {key: round(getattr(ch, prop), 6) for prop, (key, *_) in VOICE_PROPS.items()}


def expression_of(ch, preset: str | None = None) -> Expression:
    """キャラクターの基本の表情に、表情プリセットを重ねたもの。"""
    if preset:
        find_expression(ch, preset)
    return config_of(ch).expression_for(preset or None)


def apply_config(ch, config: CharacterConfig) -> None:
    ch.name = config.name
    ch.folder = str(config.folder) if config.folder is not None else ""
    ch.speaker = config.speaker
    ch.mouth = config.mouth
    preset_engine = next((k for k, (_, url) in ENGINE_PRESETS.items() if url == config.engine.rstrip("/")), None)
    ch.engine = "DEFAULT" if not config.engine else (preset_engine or "CUSTOM")
    if ch.engine == "CUSTOM":
        ch.engine_url = config.engine
    for prop, (key, *_) in VOICE_PROPS.items():
        setattr(ch, prop, config.voice.get(key, VOICE_DEFAULTS[key]))
    for key, (_, lo, hi, default, _) in AUDIO_PROPS.items():
        setattr(ch, f"audio_{key}", min(hi, max(lo, config.audio.get(key, default))))
    e = config.expression
    for prop, value in (("part_eyebrow", e.eyebrow), ("part_eye", e.eye), ("part_mouth", e.mouth),
                        ("part_hair", e.hair), ("part_complexion", e.complexion), ("part_body", e.body)):
        setattr(ch, prop, value or "")
    for category, values in (("他", e.others), ("後", e.backs)):
        for i, prop in enumerate(PART_PROPS[category]):
            setattr(ch, prop, values[i] if i < len(values) else "")
    set_item_layers(ch, e.layers)
    ch.expressions.clear()
    for preset, parts in config.expressions.items():
        item = ch.expressions.add()
        item.name = preset
        for category, value in parts.items():
            if category == LAYERS_KEY:
                set_item_layers(item, dict(value))
                continue
            values = [value] if isinstance(value, str) else list(value)
            for prop, v in zip(PART_PROPS[category], values):
                setattr(item, prop, v)
    layout, sub = config.layout, config.subtitle
    # チャンネルはキャラ × 役割のレーンで決まるので、古い設定の channel は読まない
    for prop, key in (("position_x", "position"), ("flip_x", "flip"), ("height", "height")):
        if key in layout:
            setattr(ch, prop, layout[key])
    ch.position_y = layout.get("position_y", 0.0)
    for prop, key in MOTION_KEYS.items():  # 書いていないものは既定値（書き出しで既定値を省くため）
        value = layout.get(key, ch.bl_rna.properties[prop].default)
        if prop == "line_layout":
            value = line_layout(ch, value)  # 前の版の FULL（立ち絵あり）は SUBTITLE
        setattr(ch, prop, value)
    for prop, key in (("font_path", "font"), ("font_size", "size"), ("text_color", "color"),
                      ("outline_color", "outline_color"), ("outline_width", "outline_width"),
                      ("line_spacing", "line_spacing"), ("subtitle_y", "y"), ("subtitle_box", "box"), ("box_color", "box_color"),
                      ("box_margin", "box_margin"), ("box_roundness", "box_roundness"), ("show_name", "name"),
                      ("subtitle_fade", "fade")):
        if key in sub:
            setattr(ch, prop, sub[key])


# ---- キャラクター追加の初期値 ----

def character_defaults_path() -> Path:
    """アドオン設定のキャラクターごとの既定値（全 .blend 共通）。キーは素材フォルダ名、立ち絵の無いキャラは
    `VOICE_DEFAULTS_TABLE` の下にキャラ名。"""
    return Path(bpy.utils.extension_path_user(__package__, create=True)) / "character_defaults.toml"


# 立ち絵の無いキャラの初期値を入れるテーブル（素材フォルダ名のキーとぶつからないように分ける）
VOICE_DEFAULTS_TABLE = "立ち絵なし"


def _load_defaults_file() -> dict:
    import tomllib

    path = character_defaults_path()
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path} を読めません: {e}") from None


def load_character_defaults() -> dict:
    """{素材フォルダ名: テーブル}。テーブルでない値（手で書いた誤り）は無視する。"""
    return {k: v for k, v in _load_defaults_file().items() if isinstance(v, dict) and k != VOICE_DEFAULTS_TABLE}


def load_voice_defaults() -> dict:
    """立ち絵の無いキャラの初期値 {キャラ名: テーブル}。"""
    voice = _load_defaults_file().get(VOICE_DEFAULTS_TABLE, {})
    return {k: v for k, v in voice.items() if isinstance(v, dict)} if isinstance(voice, dict) else {}


def save_character_defaults(tables: dict) -> None:
    """{キー: TOML 文字列} を書く（`_defaults_texts` の形）。"""
    character_defaults_path().write_text("\n".join(tables.values()), encoding="utf-8")


def _defaults_texts() -> dict:
    """今の既定値ファイルを {キー: TOML 文字列} にする（1 キャラ分だけ書き換えるため）。キーは素材フォルダ名、
    立ち絵の無いキャラは (VOICE_DEFAULTS_TABLE, キャラ名)。

    他のキャラの分は解釈せずにそのまま書き戻す（speaker や mouth を足すと素材の tachie.toml より優先されてしまい、
    検査で落ちる項目があると消すこともできなくなるため）。
    """
    texts: dict = {key: dump_raw_table(table, key) for key, table in load_character_defaults().items()}
    for name, table in load_voice_defaults().items():
        texts[(VOICE_DEFAULTS_TABLE, name)] = dump_raw_table(table, name, parent=VOICE_DEFAULTS_TABLE)
    return texts


def voice_defaults_config(ch) -> CharacterConfig:
    """立ち絵の無いキャラの初期値として保存する設定（立ち絵に関係する位置・大きさ・表情・動きは持たない）。"""
    config = config_of(ch)
    layout = {k: v for k, v in config.layout.items() if k == "lines"}
    return dataclasses.replace(config, folder=None, expression=Expression(), expressions={}, layout=layout)


def material_key(folder: str) -> str:
    import unicodedata

    name = os.path.basename(os.path.normpath(bpy.path.abspath(folder)))
    if name.lower().endswith(".psd"):
        name = name[:-4]
    return unicodedata.normalize("NFC", name)


def psd_default_mouth(ch, character) -> None:
    """PSD の表示状態の口が口パクの組に入っていなければ、最初の組の閉じた口を基本の表情にする。"""
    from .tachie_studio.tachie.compose import psd_part

    mouths = character.animations.get("mouth") or []
    if mouths and psd_part(character, Expression(), "mouth") is None:
        set_item_layers(ch, {mouths[0][-1].path(): True})


def find_character(settings, name: str):
    for ch in settings.characters:
        if ch.name == name:
            return ch
    raise ValueError(f"キャラクター {name!r} が設定にありません")


# ---- ストリップ ----

def _overlaps(sed, channel: int, start: int, end: int, ignore=()) -> bool:
    return lanes.overlaps(sed, channel, start, end, ignore)


def _path_for_blend(path: Path) -> str:
    if not bpy.data.filepath:
        return str(path)
    try:
        return bpy.path.relpath(str(path))
    except ValueError:  # Windows で .blend と別のドライブ（相対パスにできない）
        return str(path)


def line_strips(sed, clip_id: str) -> list:
    return [s for s in sed.strips if s.get("tachie_id") == clip_id]


def character_cache(context, ch, preset: str | None = None, parts: dict | None = None) -> FrameCache:
    """キャラクターの表情・大きさでの「目 × 口」合成キャッシュ。`parts` は表情プリセットの上に重ねる立ち絵ごとのパーツ。"""
    character = load_material(ch.folder)
    scale = seq_scene(context).render.resolution_y * ch.height / character.height
    expression = expression_of(ch, preset)
    if parts:
        expression = overlay_parts(expression, parts, f"{ch.name} の立ち絵のパーツ")
    return _cache(character, expression, output_dir(context) / "cache", scale)


def still_parts(strip) -> dict:
    """立ち絵ごとのパーツ（`tachie_parts`、表情プリセットと同じ形で変えるパーツだけ。YMM4 の「表情: カスタム」）。"""
    try:
        parts = json.loads(strip.get("tachie_parts") or "{}")
    except ValueError:
        return {}
    return parts if isinstance(parts, dict) else {}


def clean_still_parts(parts: dict | None, ch=None, problems: list | None = None) -> dict:
    """立ち絵ごとのパーツから空の指定（"" / 全部空きのスロット）を落とす。`ch` を渡すと素材に無いパーツも
    落とし（他・後はそのスロットを空きに）、`problems` に書く（素材を選び直した・別のキャラのテンプレートなど）。"""
    parts = {k: v for k, v in (parts or {}).items() if v and (not isinstance(v, list) or any(v))}
    material = load_material(ch.folder) if ch is not None and parts else None
    if material is None or material.is_psd:
        return parts
    out = {}
    for category, v in parts.items():
        names = material.parts.get(category, {})
        kept = [n if not n or n in names else "" for n in (v if isinstance(v, list) else [v])]
        for n in (v if isinstance(v, list) else [v]):
            if n and n not in names and problems is not None:
                problems.append(f"{ch.name}: 素材に {category}/{n} が無いので立ち絵のパーツから外しました")
        if any(kept):
            out[category] = kept if isinstance(v, list) else kept[0]
    return out


def set_still_parts(strip, parts: dict | None) -> None:
    parts = clean_still_parts(parts)
    if parts:
        strip["tachie_parts"] = json.dumps(parts, ensure_ascii=False)
    elif "tachie_parts" in strip:
        del strip["tachie_parts"]


def line_layout(ch, layout: str | None = None) -> str:
    """セリフの形（SUBTITLE / AUDIO）。None ならキャラの既定。前の版の FULL（立ち絵あり）は SUBTITLE。"""
    layout = layout or ch.line_layout
    return "SUBTITLE" if layout not in ("SUBTITLE", "AUDIO") else layout


def clip_job(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
             voice: dict | None = None, kana: str | None = None, accent: list | None = None,
             accent_kana: str | None = None, audio: dict | None = None, wav: bytes | None = None):
    """セリフの合成の準備（Blender に触る部分）だけをして、合成して書き出す関数を返す。返した関数は
    (RenderedClip, 書き出し先フォルダ) を返し、Blender に触らないので別スレッドで呼べる（音声合成を待つ間、画面を固めない）。

    `speaker`（スタイル）と `voice`（audio_query の値）はセリフごとの値。None ならキャラクターの既定値。
    `kana` は発音（AquesTalk 風記法）。None や空なら VOICEVOX が自動で付けた読み。
    `accent` は抑揚を編集した accent_phrases（kana より優先）。`accent_kana` は編集したときの発音で、
    今の発音（`kana` か自動の読み）と違えば `accent` は使わない（ユーザー辞書で自動の読みが変わったときなど）。
    `audio` はフェードと余韻（None ならキャラクターの既定値）。立ち絵の連番は書かない（立ち絵は別に置き、
    重なるセリフの lines/<id>.json から口パクさせる）。
    `wav` は合成済みの音声（YMM4 が保存した音声）。あれば VOICEVOX では合成しない（口パクの計算に audio_query は使う）。
    """
    audio = audio if audio is not None else character_audio(ch)
    if not ch.folder:
        preset = None  # 立ち絵の無いキャラに表情は無い（立ち絵のあるキャラで選んだ表情が残っていても使わない）
    config = CharacterConfig(name=ch.name, folder=None, speaker=ch.speaker,
                             expression=expression_of(ch, preset), mouth=ch.mouth)
    clip_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    lines_dir = output_dir(context) / "lines"
    # 読み上げとまばたきの seed は改行で（\\n の書き方でも改行でも同じセリフにする）
    line = Line(0, ch.name, breaks(text), preset or None, speaker, voice if voice is not None else character_voice(ch),
                kana or None, accent or None, accent_kana=accent_kana, fade_in=audio.get("fade_in", 0.0),
                fade_out=audio.get("fade_out", 0.0), after=audio.get("after", 0.0), wav=wav)
    engine, fps = client(context, ch), scene_fps(seq_scene(context))

    def run():
        clip = render_line(engine, config, None, line, lines_dir, fps=fps, cache=None, stem=clip_id)
        return clip, lines_dir
    return run


def render_clip(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
                voice: dict | None = None, kana: str | None = None, accent: list | None = None,
                accent_kana: str | None = None, audio: dict | None = None):
    """セリフを合成して書き出す。(RenderedClip, 書き出し先フォルダ) を返す。引数は `clip_job` と同じ。"""
    return clip_job(context, ch, text, preset, speaker, voice, kana, accent, accent_kana, audio)()


def line_channels(context, ch, start: int, frames: int, ignore=(), layout: str = "SUBTITLE",
                  prefer: dict | None = None) -> dict:
    """セリフを start に置くチャンネル {"audio", "subtitle", "name"}（形 `layout` で置くものだけ）。

    どれもそのキャラのレーン（`channels`）。レーンが埋まっていれば（同じキャラのセリフが重なるとき）
    レーンより上の空き。キャラ名は字幕より上。
    `prefer`（役割 → チャンネル。作り直すときの今のチャンネル）があり、そこが空いていればそこ（手で動かした位置を残す）。
    """
    prefer = prefer or {}
    scene = seq_scene(context)
    lanes.ensure_layout(scene)
    sed = scene.sequence_editor
    end = start + frames
    result = {}
    taken: set[int] = set()
    roles = ["audio"] + (["subtitle"] + (["name"] if ch.show_name else []) if layout != "AUDIO" else [])
    for role in roles:
        first = result["subtitle"] + 1 if role == "name" else None  # キャラ名は字幕より上
        want = prefer.get(role)
        if want and want not in taken and (first is None or want >= first) and (
                sed is None or not _overlaps(sed, want, start, end, ignore)):
            result[role] = want
            taken.add(want)
            continue
        result[role] = lanes.role_channel(scene, role, ch.name, start, end, taken, ignore, first)
        taken.add(result[role])
    return result


def place_strips(context, ch, clip, lines_dir: Path, text: str, start: int, voice: dict | None = None,
                 accent: list | None = None, audio: dict | None = None, channels: dict | None = None,
                 layout: str = "SUBTITLE") -> list:
    """書き出したセリフを start に置く。置いたストリップ（音声・字幕・キャラ名のうち形 `layout` で置くもの）を返す。
    立ち絵は置かない（そのキャラの立ち絵が重なっていれば、自動の反映でその間だけ口パクする）。

    セリフごとの声（`voice`、None ならキャラクターの既定値）は音声ストリップの `tachie_voice_*` に持つ。
    `channels` は `line_channels` の結果（None なら設定のチャンネル）。
    """
    scene = seq_scene(context)
    settings = scene.tachie
    if channels is None:  # レーンのまま（重なりは確かめない）
        lanes.ensure_layout(scene)
        lay = lanes.scene_layout(scene)
        channels = {role: lay.channel(role, ch.name) for role in ("audio", "subtitle", "name")}
        channels["name"] = channels["name"] or lanes.free_channel(None, lay, start, start + 1)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    text = mark_breaks(text)  # YMM4 のセリフなどの改行も、パネルで見える \\n の書き方で持つ
    label = f"{ch.name}: " + (text if len(text) <= 16 else text[:15] + "…")
    sound = sed.strips.new_sound(name=f"音声 {label}", filepath=_path_for_blend(lines_dir / clip.wav),
                                 channel=channels["audio"], frame_start=start)
    strips = [sound]

    if layout != "AUDIO":
        subtitle = sed.strips.new_effect(name=f"字幕 {label}", type="TEXT", channel=channels["subtitle"],
                                         frame_start=start, length=clip.frames)
        set_text_offset(subtitle, None)
        apply_subtitle(subtitle, ch, text)
        strips.append(subtitle)
        if ch.show_name:  # 字幕の上のキャラ名（字幕と同じ長さ）
            name = sed.strips.new_effect(name=f"名前 {label}", type="TEXT", channel=channels["name"],
                                         frame_start=start, length=clip.frames)
            apply_name(name, ch, text)
            strips.append(name)

    end = start + clip.frames
    if end - 1 > scene.frame_end:
        # 再生・レンダリングがセリフの途中で切れないように
        scene.frame_end = end - 1

    for s in strips:
        s["tachie_id"] = clip.frames_dir
        s["tachie_text"] = text
        s["tachie_placed_text"] = text  # 置いたときのテキスト（パネルで編集されたストリップを見分ける）
        s["tachie_character"] = ch.name
        s["tachie_speaker"] = clip.speaker
        s["tachie_folder"] = ch.folder
        s["tachie_mouth"] = clip.extra.get("mouth_mode", "")
        s["tachie_expression"] = clip.extra.get("expression", "")
        s["tachie_layout"] = layout
        s["tachie_frames"] = clip.frames  # 余韻を含む長さ（音声だけのセリフは音声ストリップより長い）
    set_line_voice(sound, voice if voice is not None else character_voice(ch))
    set_line_audio(sound, audio if audio is not None else character_audio(ch))
    # 発音は音声ストリップに。自動の読みと違えば「編集した発音」として再生成でも使う
    sound["tachie_kana"] = clip.extra.get("kana", "")
    sound["tachie_kana_auto"] = clip.extra.get("kana_auto", "")
    # 抑揚の編集は、そのときのテキスト・発音・スタイルでだけ使う（どれかが変われば取り直す）
    if accent and clip.extra.get("accent"):
        sound["tachie_accent"] = json.dumps(accent, ensure_ascii=False)
        sound["tachie_accent_key"] = json.dumps([text, clip.extra.get("kana", ""), clip.speaker], ensure_ascii=False)
    return strips


def line_texts(strips) -> list:
    """字幕とキャラ名のストリップ。"""
    return [s for s in strips if s.type == "TEXT"]


def line_end(strips) -> int:
    """セリフの終わり（余韻を含む。音声だけのセリフは音声ストリップの終わりより後ろ）。"""
    start = min(s.left_handle for s in strips)
    return max(max(s.right_handle for s in strips), start + int(strips[0].get("tachie_frames", 0)))


def line_layout_of(strips) -> str:
    """置いてあるセリフの形（前の版の FULL と印の無い古いセリフは、字幕があれば SUBTITLE）。"""
    for s in strips:
        if s.get("tachie_layout") in ("SUBTITLE", "AUDIO"):
            return s["tachie_layout"]
    return "SUBTITLE" if line_texts(strips) else "AUDIO"


def line_focus(strips):
    """パネルで編集するときに選ぶストリップ（字幕、無ければ音声）。"""
    return line_subtitle(strips) or line_sound(strips) or strips[0]


def line_accent(strips, text: str, kana: str | None, speaker: int, auto: str | None = None) -> list | None:
    """抑揚を編集した accent_phrases。テキスト・発音（None なら自動の読み）・スタイルが編集したときと同じ場合だけ。

    `auto` は今の自動の読み（None なら置いたときの読み）。ユーザー辞書の登録で変わることがある。
    """
    sound = line_sound(strips)
    if sound is None or not sound.get("tachie_accent"):
        return None
    try:
        key = json.loads(sound.get("tachie_accent_key", "[]"))
        accent = json.loads(sound["tachie_accent"])
    except ValueError:
        return None
    if input_text(text) != input_text(sound.get("tachie_placed_text", text)):
        return None  # テキストが変わった（自動の読みも変わる）
    if auto is None:
        auto = sound.get("tachie_kana_auto", "")
    if key[:1] and isinstance(key[0], str):
        key[0] = input_text(key[0])  # 前の版は改行をそのまま持っていた
    if key != [input_text(text), kana or auto, speaker]:
        return None
    return accent


def set_line_voice(sound, voice: dict) -> None:
    for prop, (key, label, lo, hi, desc) in VOICE_PROPS.items():
        name = f"tachie_voice_{prop}"
        sound[name] = float(voice.get(key, VOICE_DEFAULTS[key]))
        sound.id_properties_ui(name).update(min=lo, max=hi, soft_min=lo, soft_max=hi, step=1, precision=2,
                                            description=desc)


def set_line_audio(sound, audio: dict) -> None:
    """音量とパンはストリップの値に、フェードと余韻はカスタムプロパティ `tachie_<名前>` に持つ。"""
    sound.volume = audio.get("volume", 1.0)
    sound.pan = audio.get("pan", 0.0)
    for key in BAKED_AUDIO:
        label, lo, hi, default, desc = AUDIO_PROPS[key]
        name = f"tachie_{key}"
        sound[name] = float(audio.get(key, default))
        sound.id_properties_ui(name).update(min=lo, max=hi, soft_min=lo, soft_max=hi, step=1, precision=2,
                                            subtype="TIME_ABSOLUTE", description=desc)


def line_audio(strips, ch) -> dict:
    """セリフの音量・パン・フェード・余韻（音声ストリップの値。無いものはキャラクターの既定値）。"""
    audio = character_audio(ch)
    sound = line_sound(strips)
    if sound is not None:
        audio["volume"], audio["pan"] = sound.volume, sound.pan
        for key in BAKED_AUDIO:
            value = sound.get(f"tachie_{key}")
            if value is not None:
                _, lo, hi, _, _ = AUDIO_PROPS[key]
                audio[key] = min(hi, max(lo, float(value)))
    return audio


def line_sound(strips):
    return next((s for s in strips if s.type == "SOUND"), None)


def line_kana(strips, text: str) -> str | None:
    """編集した発音（自動の読みのままなら None）。セリフのテキストが変わっていたら使わない（読みが合わなくなる）。"""
    sound = line_sound(strips)
    if sound is None:
        return None
    kana = (sound.get("tachie_kana") or "").strip()
    # 前の版（YMM4 の読み込み）は改行をそのまま持っていたので、\\n の書き方にそろえて比べる
    if not kana or kana == sound.get("tachie_kana_auto") or input_text(text) != input_text(sound.get("tachie_placed_text", text)):
        return None
    return kana


def line_voice(strips, ch) -> dict:
    """セリフの声（音声ストリップの値。無い項目はキャラクターの既定値）。範囲外は丸める。"""
    voice = character_voice(ch)
    sound = line_sound(strips)
    if sound is not None:
        for prop, (key, _, lo, hi, _) in VOICE_PROPS.items():
            value = sound.get(f"tachie_voice_{prop}")
            if value is not None:
                voice[key] = min(hi, max(lo, float(value)))
    return voice


def shift_after(sed, frame: int, delta: int, ignore=()) -> None:
    """frame 以降に始まるストリップを delta フレームずらす。重ならない順に動かす。"""
    targets = [s for s in sed.strips if s.left_handle >= frame and s not in ignore]
    targets.sort(key=lambda s: s.left_handle, reverse=delta > 0)
    for s in targets:
        s.frame_start += delta


def line_text(strips) -> str:
    """セリフの今のテキスト。パネルの編集は 3 本のうち 1 本にしか入らないので、置いたときから変わったものを優先する。"""
    for s in strips:
        if s.get("tachie_text") != s.get("tachie_placed_text", s.get("tachie_text")):
            return s["tachie_text"]
    return strips[0]["tachie_text"]


def regenerate(context, clip_id: str, text: str | None = None, preset: str | None = None,
               speaker: int | None = None, voice: dict | None = None, character: str | None = None,
               kana: str | None = None, accent: list | None = None, audio: dict | None = None,
               layout: str | None = None) -> list:
    """セリフを作り直して同じ位置に置き直す。新しいストリップを返す。

    指定しなかった値は置いてあるセリフから取る（声とスタイルは音声ストリップ、形は `tachie_layout`）。
    `character` で別のキャラにすると、
    スタイルと声はそのキャラの既定値になり、そのキャラに無い表情プリセットは基本の表情に戻る。
    `kana` は "" で自動の読みに戻す（抑揚の編集も捨てる）。`accent` は編集した accent_phrases、[] で編集を捨てる。
    """
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    old = line_strips(sed, clip_id)
    if not old:
        raise ValueError("セリフのストリップが見つかりません")
    text = text if text is not None else line_text(old)
    # セリフ入力欄と同じく、テキストの最後の {発音} は発音として使う（字幕と読み上げに括弧を残さない）
    text, inline_kana = split_kana(input_text(text).strip())
    if inline_kana and kana is None:
        kana = inline_kana
    preset = preset if preset is not None else old[0].get("tachie_expression", "")
    sound = line_sound(old) or old[0]
    if character is not None and character != old[0]["tachie_character"]:
        ch = find_character(settings, character)
        if preset and preset not in {e.name for e in ch.expressions}:
            preset = ""
        speaker = ch.speaker if speaker is None else speaker
        voice = character_voice(ch) if voice is None else voice
        audio = character_audio(ch) if audio is None else audio
    else:
        ch = find_character(settings, old[0]["tachie_character"])
        speaker = int(sound.get("tachie_speaker", ch.speaker)) if speaker is None else speaker
        voice = line_voice(old, ch) if voice is None else voice
        audio = line_audio(old, ch) if audio is None else audio
    if kana == "":
        accent = []
    kana = line_kana(old, text) if kana is None else kana
    accent_kana = None
    if accent is None:
        accent = line_accent(old, text, kana, speaker)
        if accent:
            # 自動の読みが置いたときから変わっていたら使わない（render_line で確かめる）
            accent_kana = kana or (line_sound(old) or old[0]).get("tachie_kana_auto", "")
    start = min(s.left_handle for s in old)
    old_end = line_end(old)

    layout = line_layout(ch, layout or line_layout_of(old))

    clip, lines_dir = render_clip(context, ch, text, preset, speaker, voice, kana, accent, accent_kana, audio)
    delta = start + clip.frames - old_end
    # 消す前に置けるか確かめる（ずらすストリップは邪魔にならない）
    movers = [s for s in sed.strips if settings.ripple and delta and s.left_handle >= old_end]
    # 手で動かしたもの（レーンにいないもの）は、今のチャンネルが空いていればそこに置き直す。レーンにいるものは
    # 今のキャラのレーンへ（セリフのキャラを変えたときはそのキャラのレーン）
    scene = seq_scene(context)
    lanes.ensure_layout(scene)  # 古いレイアウトのままだと、レーンにいるものまで手で動かしたものに見える
    current = lanes.scene_layout(scene)
    prefer = {key[0]: s.channel for s in old
              if (key := lanes.lane_key(s)) and s.channel != current.channel(*key)}
    channels = line_channels(context, ch, start, clip.frames, ignore=old + movers, layout=layout, prefer=prefer)
    shift = delta
    if delta < 0:
        # 縮めるとき、このセリフの途中から始まって後ろまで続くストリップ（同時に喋る別のキャラのセリフなど）は
        # 動かさないので、同じチャンネルの後ろのストリップがそこへ重ならないところまでしか詰めない
        # （重なると Blender が別のチャンネルへ逃がす）
        stay = [s for s in sed.strips if s not in old and s not in movers and s.right_handle > old_end + delta]
        for m in movers:
            for s in stay:
                if s.channel == m.channel:
                    shift = max(shift, s.right_handle - m.left_handle)

    old_subtitle = line_subtitle(old)
    offset = text_offset(old_subtitle) if old_subtitle is not None else None
    for s in old:
        sed.strips.remove(s)
    if settings.ripple and shift:
        shift_after(sed, old_end, shift)
    strips = place_strips(context, ch, clip, lines_dir, text, start, voice, accent, audio, channels, layout)
    if offset is not None:
        for t in line_texts(strips):
            set_text_offset(t, offset)
            place_text(t, ch, text)
    return strips


POSE_PROPS = {
    "tachie_pos_x": ("横の位置", -1.0, 1.0, 0.0, "キャラクターの立ち位置からずらす量（画面の幅に対する割合）"),
    "tachie_pos_y": ("縦の位置", -1.0, 1.0, 0.0, "上下にずらす量（画面の高さに対する割合）"),
    "tachie_scale": ("大きさ", 0.1, 5.0, 1.0, "拡大率（足元を中心に拡大する）"),
}


# 立ち絵ごとの描画の設定（YMM4 の立ち絵アイテムと同じ）: 設定の名前 → (プロパティ, 表示名, 最小, 最大, 既定, 説明)。
# 動き（motion.refresh_motion）が当てる
STILL_SETTINGS = {
    "opacity": ("tachie_opacity", "不透明度", 0.0, 1.0, 1.0, "立ち絵の不透明度（1 で不透明）"),
    "rotation": ("tachie_rotation", "回転角", -360.0, 360.0, 0.0, "立ち絵の回転（度、足元が中心）"),
    "fade_in": ("tachie_still_fade_in", "フェードイン", 0.0, 10.0, 0.0, "立ち絵の頭のフェード（秒）"),
    "fade_out": ("tachie_still_fade_out", "フェードアウト", 0.0, 10.0, 0.0, "立ち絵の終わりのフェード（秒）"),
    "speech_only": ("tachie_speech_only", "喋る時のみ表示", False, True, False,
                    "重なるそのキャラのセリフの間だけ表示する（フェードは使わない）"),
}


def still_settings(strip) -> dict:
    """立ち絵ごとの描画の設定（無いものは既定値）。"""
    return {key: type(default)(strip.get(prop, default)) for key, (prop, _, _, _, default, _) in STILL_SETTINGS.items()}


def set_still_settings(strip, settings: dict | None) -> None:
    for key, (prop, label, lo, hi, default, desc) in STILL_SETTINGS.items():
        value = (settings or {}).get(key)
        if value is None:  # 無い・null（手で書いたテンプレートの JSON）は既定値
            value = default
        if isinstance(default, bool):
            strip[prop] = bool(value)
            strip.id_properties_ui(prop).update(description=desc)
        else:
            strip[prop] = float(min(hi, max(lo, value)))
            strip.id_properties_ui(prop).update(min=lo, max=hi, soft_min=lo, soft_max=hi, step=1, precision=2,
                                                description=desc)


# 字幕（とキャラ名）をセリフごとにずらす量。字幕ストリップで編集し、自動反映でキャラ名にも写す
TEXT_POSE_PROPS = {
    "tachie_sub_x": ("字幕の横の位置", "字幕を左右にずらす量（画面の幅に対する割合）"),
    "tachie_sub_y": ("字幕の縦の位置", "字幕を上下にずらす量（画面の高さに対する割合）"),
}


def line_subtitle(strips):
    return next((s for s in strips if s.type == "TEXT" and s.get("tachie_role") != "name"), None)


def set_text_offset(strip, offset: tuple[float, float] | None) -> None:
    for key, value in zip(TEXT_POSE_PROPS, offset or (0.0, 0.0)):
        strip[key] = float(value)
        strip.id_properties_ui(key).update(min=-1.0, max=1.0, soft_min=-1.0, soft_max=1.0, step=1, precision=3,
                                           description=TEXT_POSE_PROPS[key][1])


def subtitle_clashes(sed, strips) -> list[str]:
    """置いた字幕と、時間も位置も重なっている別のセリフの字幕のキャラ名。"""
    mine = line_subtitle(strips)
    if mine is None:
        return []
    names = []
    for s in sed.strips:
        if (s.type == "TEXT" and s.get("tachie_id") and s.get("tachie_role") != "name"
                and s.get("tachie_id") != mine.get("tachie_id") and not s.mute
                and s.left_handle < mine.right_handle and mine.left_handle < s.right_handle
                and abs(s.location[0] - mine.location[0]) < 0.2 and abs(s.location[1] - mine.location[1]) < 0.08):
            names.append(s.get("tachie_character", "?"))
    return sorted(set(names))


def get_pose(strip) -> dict:
    return {k: float(strip.get(k, default)) for k, (_, _, _, default, _) in POSE_PROPS.items()}


def set_pose(strip, pose: dict | None, ch) -> None:
    """立ち絵ストリップにセリフごとの位置・大きさ（パネルで編集する）を持たせて当てる。"""
    for key, (label, lo, hi, default, desc) in POSE_PROPS.items():
        strip[key] = float((pose or {}).get(key, default))
        strip.id_properties_ui(key).update(min=lo, max=hi, soft_min=lo, soft_max=hi, step=1, precision=3,
                                           description=desc)
    apply_placement(strip, ch)


# ---- 表情ストリップ（セリフの途中で表情を変える） ----

def face_strips(sed, character: str | None = None) -> list:
    """表情ストリップ（優先の低い順: チャンネルが下、同じなら先に始まるもの）。"""
    faces = [s for s in sed.strips if s.get("tachie_face") and (character is None or s["tachie_face"] == character)]
    return sorted(faces, key=lambda s: (s.channel, s.left_handle))


def face_segments(sed, ch, start: int, end: int) -> list[tuple[int, int, str]]:
    """[start, end) に重なる表情ストリップの区間（start からの相対、優先の低い順）。

    キャラクターに無い表情（プリセットを消した・名前を変えたなど）の表情ストリップは無視する。
    """
    names = {e.name for e in ch.expressions}
    out = []
    for f in face_strips(sed, ch.name) if sed else []:
        preset = f.get("tachie_expression", "")
        if preset and preset not in names:
            continue
        a, b = max(start, f.left_handle), min(end, f.right_handle)
        if a < b:
            out.append((a - start, b - start, preset))
    return out


def _strip_from_paths(context, sed, ch, name: str, start: int, paths: list, folder: str, key: str,
                      channel: int | None = None):
    """画像のパスの列をハードリンクで 1 つのフォルダに並べ、立ち絵ストリップにする（既定は立ち絵のレーン）。"""
    directory = output_dir(context) / folder / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    link_frames(paths, directory)
    files = sorted(os.listdir(directory))
    strip = sed.strips.new_image(name=name, filepath=_path_for_blend(directory / files[0]),
                                 channel=channel or ch.channel, frame_start=start, fit_method="ORIGINAL")
    for f in files[1:]:
        strip.elements.append(f)
    strip.blend_type = "ALPHA_OVER"
    apply_placement(strip, ch)
    return strip


def apply_faces(context, strips=None, problems: list | None = None) -> int:
    """立ち絵（セリフなしの立ち絵）を、重なるそのキャラのセリフ（口パクと表情）と表情ストリップに合わせて
    作り直す。作り直した本数を返す。

    鍵（`still_key`）が置いたときと違うもの（セリフや表情ストリップを足した・動かした・消したなど）と、
    長さを変えた（ハンドルを動かした）ものを作り直す。前の版で置いたセリフの立ち絵・待機はそのまま。
    作り直せなかったものと、口パクに使えなかったセリフは `problems` に理由を足す。
    """
    scene = seq_scene(context)
    sed = scene.sequence_editor
    if sed is None:
        return 0
    settings = scene.tachie
    if strips is None:
        strips = [s for s in sed.strips if s.type == "IMAGE" and s.get("tachie_still")]
    count = 0
    for strip in list(strips):
        ch = next((c for c in settings.characters if c.name == strip.get("tachie_still")), None)
        if ch is None or not ch.folder:
            continue
        start, end = strip.left_handle, strip.right_handle
        stretched = int(strip.content_start) != start or len(strip.elements) != end - start
        if not stretched and strip.get("tachie_faced", "") == still_key(context, sed, ch, strip):
            continue
        try:
            remake_still(context, strip, problems=problems)
        except ERRORS as e:
            if problems is not None:
                problems.append(str(e))
            continue
        count += 1
    return count


def line_sounds(sed, name: str) -> list:
    """キャラ `name` のセリフの音声ストリップ（ミュートは除く。始まりの順）。"""
    return sorted((s for s in sed.strips if s.type == "SOUND" and s.get("tachie_id")
                   and s.get("tachie_character") == name and not s.mute), key=lambda s: s.left_handle)


def _talking_lines(sed, ch, start: int, end: int) -> list[tuple]:
    """[start, end) に重なるそのキャラのセリフ [(id, 動きの頭のずれ, 表情, 長さ)]（ずれは start から）。

    動きの頭は音声ストリップの中身の頭（content_start）、長さは余韻を含む `tachie_frames`。セリフの表情が
    キャラに無い（消した・名前を変えた）ときと "" のときは立ち絵の表情のまま（None）。
    """
    names = {e.name for e in ch.expressions}
    out = []
    for sound in line_sounds(sed, ch.name):
        head = int(sound.content_start)
        frames = int(sound.get("tachie_frames", 0)) or (sound.right_handle - head)
        if head + frames <= start or end <= head:
            continue
        preset = sound.get("tachie_expression", "")
        out.append((sound["tachie_id"], head - start, preset if preset in names else None, frames))
    return out


def appearance_key(ch, presets=None) -> list:
    """立ち絵の見た目に関わるキャラの設定（素材・高さ・基本の表情・表情プリセット）。変われば立ち絵を作り直す。

    `presets` を渡すとその表情プリセットだけ（立ち絵で使っていない表情を変えても作り直さない）。口パクの方式
    （`ch.mouth`）はセリフの動きの json に入っているので含めない。
    """
    def parts(item) -> list:
        return [getattr(item, prop) for props in PART_PROPS.values() for prop in props] + [item.psd_layers]

    expressions = [e for e in ch.expressions if presets is None or e.name in presets]
    return [ch.folder, round(ch.height, 6), parts(ch), [[e.name, *parts(e)] for e in expressions]]


def still_key(context, sed, ch, strip) -> str:
    """立ち絵の作り直しの鍵（キャラの見た目・表情・立ち絵ごとのパーツ・表情ストリップの区間・重なるセリフ）。
    置いたときと違えば作り直す。"""
    start, end = strip.left_handle, strip.right_handle
    return _still_key(ch, strip.get("tachie_expression", ""), face_segments(sed, ch, start, end),
                      _talking_lines(sed, ch, start, end), still_parts(strip))


def _still_key(ch, preset: str, segments, talking, parts: dict | None = None) -> str:
    used = {preset} | {p for _, _, p in segments} | {p for _, _, p, _ in talking if p is not None}
    key = [appearance_key(ch, used), preset, segments, talking]
    if parts:
        key.append(parts)
    return json.dumps(key, ensure_ascii=False)


def set_line_expression(context, clip_id: str, preset: str) -> list:
    """セリフの表情を変える（音声は合成し直さない）。重なる立ち絵は自動の反映で描き直す（呼び出し側で
    `refresh_faces` してもよい）。"""
    sed = seq_scene(context).sequence_editor
    strips = line_strips(sed, clip_id)
    if not strips:
        raise ValueError("セリフのストリップが見つかりません")
    ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
    if preset and preset not in {e.name for e in ch.expressions}:
        raise ValueError(f"{ch.name} に表情 {preset!r} がありません")
    for s in strips:
        s["tachie_expression"] = preset
    return strips


def refresh_faces(context, problems: list | None = None) -> int:
    """全体を合わせる（立ち絵の作り直しと、動き・ダッキングの作り直し）。作り直した立ち絵の本数を返す。"""
    count = apply_faces(context, problems=problems)
    # 呼び出し側は反映済みとして署名を記録するので、動きとダッキングも作り直す
    # （まとめて変えるで読み上げ速度を変えるなど、セリフの長さが変わったとき）
    refresh_motion_now(seq_scene(context))
    return count


def auto_signature(scene) -> tuple[str, str, str]:
    """自動で描き直すきっかけ: （表情ストリップに関わる値, 立ち絵の動きに関わる値, BGM のダッキングに関わる値）。"""
    from .ducking import ducking_signature
    from .motion import motion_signature

    return faces_signature(scene), motion_signature(scene), ducking_signature(scene)


def refresh_motion_now(scene) -> int:
    """立ち絵の動き（跳ねる・揺れる・登場・退場、セリフごとの位置・大きさ）と BGM のダッキングのキーフレームを作り直す。"""
    from .ducking import refresh_ducking
    from .motion import refresh_motion

    refresh_ducking(scene)
    return refresh_motion(scene, output_dir(bpy.context) / "lines")


def faces_signature(scene) -> str:
    """立ち絵の作り直しに関わる値（表情ストリップ、セリフの位置・長さ・表情、立ち絵の位置・長さ・表情、
    立ち絵のあるキャラの見た目）。"""
    sed = scene.sequence_editor
    if sed is None:
        return ""
    items = []
    stills = False
    still_chars: set[str] = set()
    for s in sed.strips:
        if s.get("tachie_face"):
            items.append(("face", s.name, s.channel, s.left_handle, s.right_handle, s["tachie_face"],
                          s.get("tachie_expression", "")))
        elif s.type == "SOUND" and s.get("tachie_id"):
            items.append(("line", s.name, s.get("tachie_character", ""), int(s.content_start),
                          int(s.get("tachie_frames", 0)), s.get("tachie_expression", ""), s.mute))
        elif s.type == "IMAGE" and s.get("tachie_still"):
            stills = True
            still_chars.add(s["tachie_still"])
            items.append(("still", s.name, int(s.content_start), s.left_handle, s.right_handle, len(s.elements),
                          s.get("tachie_expression", ""), s.get("tachie_faced", "")))
    if not stills:
        return ""  # 作り直す立ち絵が無い
    # キャラのパーツ・表情プリセットを選び直したら作り直す
    items += [("character", ch.name, appearance_key(ch)) for ch in scene.tachie.characters if ch.name in still_chars]
    return json.dumps(sorted(items, key=repr), ensure_ascii=False)


_applied_signatures: dict[str, tuple[str, str, str]] = {}
_face_timer_running = False
_applying_faces = False
# 裏で待つだけのモーダル操作（AI で表情を割り当てる。数分かかる）。ドラッグではないので自動の反映を止めない
_BACKGROUND_MODALS = {"TACHIE_OT_assign_faces"}


@persistent
def _on_depsgraph_update(scene, depsgraph):
    global _face_timer_running
    if _applying_faces or _face_timer_running:
        return
    # 引数はウィンドウのシーン。シーケンサーが別のシーンを映していればそちらも見る
    from .motion import moved_texts

    scenes = {scene, getattr(bpy.context, "sequencer_scene", None) or scene}
    if any(auto_signature(sc) != _applied_signatures.get(sc.name, ("", "", "")) or moved_texts(sc) for sc in scenes):
        _face_timer_running = True
        bpy.app.timers.register(_apply_faces_later, first_interval=0.3)


def _apply_faces_later():
    """ドラッグなどが終わってから描き直す（自動の反映）。"""
    global _face_timer_running, _applying_faces
    wm = bpy.context.window_manager
    if wm and any(op.bl_idname not in _BACKGROUND_MODALS for w in wm.windows
                  for op in getattr(w, "modal_operators", ())):
        return 0.3  # まだ動かしている
    _face_timer_running = False
    window = wm.windows[0] if wm and wm.windows else None
    with bpy.context.temp_override(window=window) if window else _nullcontext(), prefetch.paused():
        # refresh_faces は seq_scene(context) を描き直すので、そのシーンだけを見る（他のシーンは
        # シーケンサーに映ったときに反映する）
        from .motion import sync_text_offsets

        scene = seq_scene(bpy.context)
        _applying_faces = True
        try:
            # キャラの名前を変えたなど、きっかけの無い変化でもレーンを合わせる（undo の段は積まない）
            for p in lanes.ensure_layout(scene):
                print("チャンネルを整理できません:", p)
            # プレビューで動かした字幕は、ずらす量に書き戻してから（動きの作り直しで元の位置に戻さないため）
            sync_text_offsets(scene)
            signature = auto_signature(scene)
            applied = _applied_signatures.get(scene.name, ("", "", ""))
            if signature == applied:
                return None
            problems = []
            # 動きだけが変わったとき（セリフごとの位置を変えた・動きのあるセリフを動かしたなど）は、
            # 立ち絵の描き直しと待機の作り直しをせずにキーフレームだけ作り直す
            if signature[:2] != applied[:2]:
                if signature[0] != applied[0]:
                    refresh_faces(bpy.context, problems)  # 待機を作り直すときに動きも作り直す
                refresh_motion_now(scene)  # ダッキングも作り直す
            else:  # BGM のダッキングだけ（BGM を動かした・音量を変えたなど）
                from .ducking import refresh_ducking

                refresh_ducking(scene)
            for p in dict.fromkeys(problems):
                print("表情を反映できません:", p)
            # undo_push はしない: 直前の操作（ドラッグなど）の段は反映前の状態なので、元に戻すと署名が
            # 変わってまた描き直される。ここで段を積むと 1 回目の取り消しが反映だけを戻し、表情ストリップは
            # 動かしたまま立ち絵だけ古い表情になる
        except ERRORS as e:
            print("表情の反映に失敗しました:", e)
        finally:
            _applying_faces = False
            _applied_signatures[scene.name] = auto_signature(scene)
    return None


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _still_overlaps(sed, ch, start: int, end: int, ignore=()) -> bool:
    """キャラ `ch` の立ち絵（手で別のチャンネルに動かしたものも）が [start, end) に重なるか。"""
    return any(s.type == "IMAGE" and s.get("tachie_still") == ch.name and s not in ignore
               and s.left_handle < end and start < s.right_handle for s in sed.strips)


def _blink_strip(sed, ch, cache: FrameCache, name: str, start: int, frames: int, fps: float,
                 channel: int | None = None):
    """まばたきだけの立ち絵（口は閉じ）のストリップを置く（既定は立ち絵のレーン）。"""
    paths = idle_frames(cache, frames, fps, seed=(ch.name, start))
    strip = sed.strips.new_image(name=name, filepath=_path_for_blend(paths[0]),
                                 channel=channel or ch.channel, frame_start=start, fit_method="ORIGINAL")
    for p in paths[1:]:
        strip.elements.append(p.name)
    strip.blend_type = "ALPHA_OVER"
    apply_placement(strip, ch)
    return strip


def place_still(context, ch, preset: str, start: int, frames: int, pose: dict | None = None,
                channel: int | None = None, problems: list | None = None, flip: bool | None = None,
                jump: dict | None = None, settings: dict | None = None, parts: dict | None = None):
    """立ち絵（`tachie_still`）を置く。まばたきし、重なるそのキャラのセリフの間だけ口パクしてセリフの表情に
    なる（表情ストリップが重なればその表情）。口パクに使えなかったセリフ（動きの json が無い前の版のセリフなど）は
    `problems` に理由を足し、その間は口を閉じる。

    `flip` は立ち絵ごとの左右反転（`tachie_flip`、YMM4 の立ち絵アイテムと同じ。None ならキャラの設定に従う）、
    `jump` は跳ねながら登場退場（`tachie_jump`、`ymmp.jump_on_screen` の形。動きは motion が打つ）。
    `settings` は不透明度・回転角・フェード・喋る時のみ表示（`STILL_SETTINGS`、None なら既定値）。
    `parts` は立ち絵ごとのパーツ（`tachie_parts`、表情プリセットの上に重ねる）。セリフの表情が「立ち絵のまま」の間も
    このパーツの見た目。表情プリセットを指定したセリフ・表情ストリップの区間はそのプリセットの表情。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    lanes.ensure_layout(scene)  # 立ち絵のレーン（ch.channel）に置く
    if scene.view_settings.view_transform != "Standard":
        # 3D 用の AgX / Filmic のままだと立ち絵の色が淡くなる
        scene.view_settings.view_transform = "Standard"
    parts = clean_still_parts(parts, ch, problems)
    cache = character_cache(context, ch, preset, parts)
    name = f"立ち絵 {ch.name}" + (f"({preset})" if preset else "") + ("*" if parts else "")
    if not channel or _overlaps(sed, channel, start, start + frames):  # 作り直す前のチャンネルが埋まっていればレーン
        channel = ch.channel
    end = start + frames
    segments = face_segments(sed, ch, start, end)
    talking = _talking_lines(sed, ch, start, end)
    lines = []
    lines_dir = output_dir(context) / "lines"
    # プリセットを指定したセリフ・表情ストリップの区間は立ち絵ごとのパーツを重ねない（同じプリセットでも）
    caches = {} if parts else {preset or "": cache}

    def cache_of(p: str):
        if p not in caches:
            caches[p] = character_cache(context, ch, p)
        return caches[p]

    from .motion import motion_paths

    motion_path = motion_paths(sed, lines_dir)
    for clip_id, offset, line_preset, length in talking:
        path = motion_path(clip_id)
        motion = LineMotion.load(path) if path.is_file() else None
        if motion is None or motion.frames != length:
            if problems is not None:
                problems.append(f"{ch.name}のセリフ {clip_id} の口の動きがありません（再生成すると口パクします）")
            continue
        lines.append((offset, motion, cache if line_preset is None else cache_of(line_preset)))
    key = _still_key(ch, preset, segments, talking, parts)
    if segments or lines:
        paths = still_frames(cache, [(a, b, cache_of(p)) for a, b, p in segments], lines, frames,
                             scene_fps(scene), seed=(ch.name, start))
        # フォルダはキャラ・位置・長さごと（同じ区間の別の立ち絵と連番を取り合わないように）
        strip = _strip_from_paths(context, sed, ch, name, start, paths, "faces",
                                  json.dumps([ch.name, start, frames, key], ensure_ascii=False), channel)
    else:
        strip = _blink_strip(sed, ch, cache, name, start, frames, scene_fps(scene), channel)
    strip["tachie_faced"] = key
    strip["tachie_still"] = ch.name
    strip["tachie_character"] = ch.name
    strip["tachie_expression"] = preset or ""
    set_still_parts(strip, parts)
    if flip is not None:
        strip["tachie_flip"] = bool(flip)
    if jump:
        strip["tachie_jump"] = json.dumps(jump, ensure_ascii=False)
    set_still_settings(strip, settings)
    set_pose(strip, pose, ch)
    if strip.right_handle - 1 > scene.frame_end:
        scene.frame_end = strip.right_handle - 1
    return strip


def item_still_pose(ch, x: float, y: float, zoom: float, width: float, height: float) -> dict:
    """YMM4 の立ち絵アイテムの X / Y（画面中央からのピクセル、下が正）と Zoom（素材の高さに対する %）を、キャラの立ち位置・
    高さとの差（立ち絵ごとの位置・大きさ）にする（.ymmp の読み込みでキャラに写すのと同じ換算）。"""
    material = load_material(ch.folder)
    pose = {"tachie_pos_x": x / width - ch.position_x, "tachie_pos_y": -y / height - ch.position_y,
            "tachie_scale": material.height * zoom / 100 / height / ch.height}
    for key, (_, lo, hi, default, _) in POSE_PROPS.items():
        # 丸めの誤差（キャラの設定と同じ位置・大きさ）は既定値に。範囲（パネルで編集できる値）に収める
        value = default if abs(pose[key] - default) < 1e-3 else pose[key]
        pose[key] = min(hi, max(lo, value))
    return pose


def remake_still(context, strip, preset: str | None = None, problems: list | None = None):
    """立ち絵を今の位置・長さ・キャラクター設定・重なるセリフで作り直す。新しいストリップを返す。"""
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    ch = find_character(settings, strip["tachie_still"])
    preset = preset if preset is not None else strip.get("tachie_expression", "")
    if preset:
        find_expression(ch, preset)
    start, frames = strip.left_handle, strip.right_handle - strip.left_handle
    character_cache(context, ch, preset)  # 素材が読めないときは消す前に失敗させる
    pose = get_pose(strip)
    # 今のチャンネル（空いていれば、そこに置き直す）。先にレーンを合わせる（古いレーンのチャンネルを持ったまま
    # 置き直すと、手で動かしたものに見えてレーンに戻らない）
    lanes.ensure_layout(seq_scene(context))
    channel = strip.channel
    flip = strip.get("tachie_flip")
    from .motion import jump_of

    jump = jump_of(strip)
    look = still_settings(strip)
    parts = still_parts(strip)
    sed.strips.remove(strip)
    return place_still(context, ch, preset, start, frames, pose, channel, problems,
                       None if flip is None else bool(flip), jump, look, parts)


def connect(context, strips) -> None:
    """3 本をまとめて動かせるようにつなぐ（シーケンサーの画面から呼んだときだけ）。"""
    if context.area is None or context.area.type != "SEQUENCE_EDITOR":
        return
    for s in seq_scene(context).sequence_editor.strips_all:
        s.select = False
    for s in strips:
        s.select = True
    if bpy.ops.sequencer.connect.poll():
        bpy.ops.sequencer.connect()


def active_line_id(context) -> str | None:
    sed = seq_scene(context).sequence_editor
    strip = sed.active_strip if sed else None
    return strip.get("tachie_id") if strip else None


# ---- オペレーター ----

def input_text(text: str) -> str:
    """入力欄・パネルで書いたセリフの改行の書き方をそろえる（¥n → \\n）。ストリップのテキストは \\n のまま持ち
    （入力欄に改行を入れると見えない）、字幕と読み上げのときに改行にする（`subtitle.breaks`）。"""
    return mark_breaks(text)


class TACHIE_OT_add_line(bpy.types.Operator):
    """選択中のキャラクターでセリフを合成し、音声と字幕を再生位置に置く（重なる立ち絵が口パクする）"""

    bl_idname = "tachie.add_line"
    bl_label = "セリフを追加"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = seq_scene(context).tachie
        ch = settings.character()
        text, kana = split_kana(input_text(settings.text).strip())  # 字幕の改行は \n（¥n）
        if ch is None:
            self.report({"ERROR"}, "キャラクターを追加してください")
            return {"CANCELLED"}
        if not text:
            self.report({"ERROR"}, "セリフを入力してください")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        start = seq_scene(context).frame_current
        layout = line_layout(ch)
        try:
            clip, lines_dir = render_clip(context, ch, text, settings.expression, kana=kana)
            channels = line_channels(context, ch, start, clip.frames, layout=layout)
            strips = place_strips(context, ch, clip, lines_dir, text, start, channels=channels, layout=layout)
            refresh_faces(context)  # 重なる立ち絵を口パクさせる
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        connect(context, strips)
        clear_text_input(settings)  # YMM4 と同じく、追加したら入力欄を空にする
        if settings.advance:
            seq_scene(context).frame_current = line_end(strips)
        clashes = subtitle_clashes(seq_scene(context).sequence_editor, strips)
        if clashes:
            self.report({"WARNING"}, f"{clip.frames} フレームのセリフを追加しました。字幕が"
                                     f"{'・'.join(clashes)}の字幕と重なっています（キャラの字幕の高さか、"
                                     "セリフの「字幕の縦の位置」でずらしてください）")
        else:
            self.report({"INFO"}, f"{clip.frames} フレームのセリフを追加しました")
        return {"FINISHED"}


class TACHIE_OT_add_still(bpy.types.Operator):
    """選択中のキャラクターの立ち絵を再生位置に置く（まばたきし、重なるそのキャラのセリフの間だけ口パクする）"""

    bl_idname = "tachie.add_still"
    bl_label = "立ち絵だけを置く"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        ch = settings.character()
        if ch is None:
            self.report({"ERROR"}, "キャラクターを追加してください")
            return {"CANCELLED"}
        if not ch.folder:
            self.report({"ERROR"}, f"{ch.name}は立ち絵の無いキャラクターです")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        start = scene.frame_current
        frames = max(1, round(settings.still_seconds * scene_fps(scene)))
        sed = scene.sequence_editor
        lanes.ensure_layout(scene)  # ch.channel を今のレーンにしてから確かめる
        if sed and (_overlaps(sed, ch.channel, start, start + frames)
                    or _still_overlaps(sed, ch, start, start + frames)):
            self.report({"ERROR"}, f"チャンネル {ch.channel} の {start}〜{start + frames} フレームに"
                                   "他のストリップがあります")
            return {"CANCELLED"}
        problems = []
        try:
            strip = place_still(context, ch, settings.expression, start, frames, problems=problems)
            refresh_motion_now(scene)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        scene.sequence_editor.active_strip = strip
        if settings.advance:
            scene.frame_current = strip.right_handle
        if problems:
            self.report({"WARNING"}, f"{ch.name}の立ち絵を {frames} フレーム置きました（{problems[0]}）")
        else:
            self.report({"INFO"}, f"{ch.name}の立ち絵を {frames} フレーム置きました")
        return {"FINISHED"}


class TACHIE_OT_remake_still(bpy.types.Operator):
    """選択中の立ち絵を、今の位置・長さ・キャラクター設定・重なるセリフで作り直す"""

    bl_idname = "tachie.remake_still"
    bl_label = "作り直す"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        sed = seq_scene(context).sequence_editor
        return bool(sed and sed.active_strip and sed.active_strip.get("tachie_still"))

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        try:
            strip = remake_still(context, sed.active_strip)
            refresh_motion_now(seq_scene(context))
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        sed.active_strip = strip
        return {"FINISHED"}


class TACHIE_OT_flip_still(bpy.types.Operator):
    """選択中の立ち絵を左右反転する（立ち絵ごと。キャラクターの設定と同じ向きに戻すと、キャラクターの設定に従う）"""

    bl_idname = "tachie.flip_still"
    bl_label = "左右反転"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        sed = seq_scene(context).sequence_editor
        return bool(sed and sed.active_strip and sed.active_strip.get("tachie_still"))

    def execute(self, context):
        strip = seq_scene(context).sequence_editor.active_strip
        try:
            ch = find_character(seq_scene(context).tachie, strip["tachie_still"])
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        flip = not still_flip(strip, ch)
        if flip == ch.flip_x:
            if "tachie_flip" in strip:
                del strip["tachie_flip"]
        else:
            strip["tachie_flip"] = flip
        refresh_motion_now(seq_scene(context))  # 位置を当て直し、動き（スライド・ジャンプの向き）も作り直す
        return {"FINISHED"}


class TACHIE_OT_regenerate(bpy.types.Operator):
    """選択中のセリフを、今のテキストとキャラクター設定で作り直す"""

    bl_idname = "tachie.regenerate"
    bl_label = "再生成"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        # 編集はパネルで選択中のストリップに入るので、テキストと表情はそこから取る
        text = sed.active_strip["tachie_text"]
        preset = sed.active_strip.get("tachie_expression", "")
        try:
            strips = regenerate(context, active_line_id(context), text, preset)
            refresh_faces(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        sed.active_strip = line_focus(strips)
        connect(context, strips)
        return {"FINISHED"}


def _regenerate_active(op, context, **kwargs):
    """選択中のセリフを作り直す（テキストと表情は選択中のストリップから）。"""
    sed = seq_scene(context).sequence_editor
    active = sed.active_strip
    try:
        strips = regenerate(context, active_line_id(context), active["tachie_text"],
                            active.get("tachie_expression", ""), **kwargs)
        refresh_faces(context)
    except ERRORS as e:
        op.report({"ERROR"}, str(e))
        return {"CANCELLED"}
    sed.active_strip = line_focus(strips)
    connect(context, strips)
    return {"FINISHED"}


def _character_names(self, context):
    global _character_items
    _character_items = [(c.name, c.name, "") for c in seq_scene(context).tachie.characters]
    return _character_items


class TACHIE_OT_pick_line_character(bpy.types.Operator):
    """選択中のセリフを別のキャラクターで作り直す（スタイルと声はそのキャラクターの既定値になる）"""

    bl_idname = "tachie.pick_line_character"
    bl_label = "セリフのキャラクターを変える"
    bl_property = "character"
    bl_options = {"REGISTER", "UNDO"}

    character: bpy.props.EnumProperty(items=_character_names)

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        return _regenerate_active(self, context, character=self.character)


class TACHIE_OT_pick_line_layout(bpy.types.Operator):
    """選択中のセリフの形（立ち絵あり・音声と字幕・音声だけ）を変えて作り直す"""

    bl_idname = "tachie.pick_line_layout"
    bl_label = "セリフの形を変える"
    bl_property = "line_layout"
    bl_options = {"REGISTER", "UNDO"}

    line_layout: bpy.props.EnumProperty(name="セリフの形", items=LINE_LAYOUTS)

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        return _regenerate_active(self, context, layout=self.line_layout)


class TACHIE_OT_reset_line_voice(bpy.types.Operator):
    """選択中のセリフの声・スタイル・音量などを、キャラクターの今の既定値に戻して作り直す"""

    bl_idname = "tachie.reset_line_voice"
    bl_label = "キャラクターの声に戻す"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        try:
            ch = find_character(seq_scene(context).tachie, sed.active_strip["tachie_character"])
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return _regenerate_active(self, context, speaker=ch.speaker, voice=character_voice(ch),
                                  audio=character_audio(ch))


class TACHIE_OT_reset_kana(bpy.types.Operator):
    """選択中のセリフの発音を、セリフから自動で付けた読みに戻して作り直す"""

    bl_idname = "tachie.reset_kana"
    bl_label = "発音を取り直す"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        return _regenerate_active(self, context, kana="")


class TACHIE_OT_preview_line(bpy.types.Operator):
    """選択中のセリフを、今の発音・声で合成して試聴する（置いてあるセリフは変えない）"""

    bl_idname = "tachie.preview_line"
    bl_label = "試聴"

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        strips = line_strips(sed, active_line_id(context))
        sound = line_sound(strips)
        # 再生成と同じテキストと発音（テキストの最後の {発音} > 編集した発音 > 自動の読み）
        text, kana = split_kana(input_text(sed.active_strip["tachie_text"]).strip())
        try:
            ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
            speaker = int((sound or strips[0]).get("tachie_speaker", ch.speaker))
            c = client(context, ch)
            query = c.audio_query(text, speaker)
            query.update(line_voice(strips, ch))
            kana = kana or line_kana(strips, text)
            accent = line_accent(strips, text, kana, speaker, auto=query.get("kana", ""))
            if accent:
                query["accent_phrases"] = accent
            elif kana:
                query["accent_phrases"] = c.accent_phrases(kana, speaker, is_kana=True)
            wav = c.synthesis(query, speaker)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        problem = play_preview(context, wav)
        if problem:
            self.report({"WARNING"}, problem)
        return {"FINISHED"}


def play_preview(context, wav: bytes) -> str | None:
    """合成した wav を鳴らす（前の試聴は止める）。鳴らせなかったら理由を返す（音声デバイスの無いサーバなど）。"""
    global _preview
    import aud

    path = output_dir(context) / "preview.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    if _preview is not None:
        _preview.stop()
    path.write_bytes(wav)
    if bpy.app.background:
        # 画面の無い Blender（テストなど）では鳴らさない。aud.Device() は -noaudio でも音声デバイスを開いてしまう
        _preview = None
        return f"画面の無い Blender では試聴を鳴らしません。合成した音声は {path} にあります"
    try:
        _preview = aud.Device().play(aud.Sound(str(path)).cache())
    except aud.error as e:
        _preview = None
        return f"音声デバイスを開けないので試聴できません（{e}）。合成した音声は {path} にあります"
    return None


# ---- 抑揚の編集 ----

def _line_context(context):
    """選択中のセリフの (strips, テキスト, キャラクター, スタイル, 発音)。発音は編集したものか None。"""
    sed = seq_scene(context).sequence_editor
    strips = line_strips(sed, active_line_id(context))
    sound = line_sound(strips)
    text, kana = split_kana(input_text(sed.active_strip["tachie_text"]).strip())
    ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
    speaker = int((sound or strips[0]).get("tachie_speaker", ch.speaker))
    return strips, text, ch, speaker, kana or line_kana(strips, text)


def load_accent_edit(edit, phrases: list) -> None:
    from . import properties

    properties._loading_accent = True  # accent の update（音高の計算し直し）を走らせない
    try:
        edit.phrases.clear()
        for ap in phrases:
            item = edit.phrases.add()
            item.rest = json.dumps({k: v for k, v in ap.items() if k not in ("moras", "accent")}, ensure_ascii=False)
            item.accent = max(1, min(len(ap["moras"]), int(ap.get("accent") or 1)))
            _load_moras(item, ap["moras"])
    finally:
        properties._loading_accent = False
    fit_accent_range(edit)


def fit_accent_range(edit) -> None:
    """棒の値の範囲（音高は有声のモーラの最小〜最大の少し外、長さは 0〜最大の少し上）。"""
    moras = [m for item in edit.phrases for m in item.moras]
    pitches = [m.pitch for m in moras if m.voiced and m.pitch > 0]
    edit.pitch_lo, edit.pitch_hi = (min(pitches) - 0.4, max(pitches) + 0.4) if pitches else (3.0, 6.5)
    # 長さのいちばん下の段でも音が消えないように、下限は 0 にしない
    edit.length_lo, edit.length_hi = 0.02, max([m.vowel_length for m in moras] + [0.15]) * 1.4


def _load_moras(item, moras: list) -> None:
    item.moras.clear()
    for mora in moras:
        m = item.moras.add()
        m.name = mora["text"]
        m.consonant = mora.get("consonant") or ""
        m.vowel = mora["vowel"]
        m.pitch = mora.get("pitch") or 0.0
        m.voiced = (mora.get("pitch") or 0.0) > 0
        m.consonant_length = mora.get("consonant_length") or 0.0
        m.vowel_length = mora.get("vowel_length") or 0.0


def accent_from_edit(edit) -> list:
    phrases = []
    for item in edit.phrases:
        ap = json.loads(item.rest or "{}")
        ap["accent"] = min(item.accent, len(item.moras))
        ap["moras"] = [{
            "text": m.name,
            "consonant": m.consonant or None,
            "consonant_length": round(m.consonant_length, 6) if m.consonant else None,
            "vowel": m.vowel,
            "vowel_length": round(m.vowel_length, 6),
            "pitch": round(m.pitch, 6),
        } for m in item.moras]
        phrases.append(ap)
    return phrases


def _accent_client(context, edit):
    settings = seq_scene(context).tachie
    ch = next((c for c in settings.characters if c.name == edit.character), None)
    return client(context, ch, timeout=10.0)


def refresh_phrase_pitch(context, item) -> None:
    """句のアクセントの位置を変えたあと、その句の音高だけエンジンで計算し直す（他の句の編集は残す）。"""
    edit = context.window_manager.tachie_accent
    index = next((i for i, p in enumerate(edit.phrases) if p == item), None)
    if index is None:
        return
    edit.changed = True  # 音高を計算し直せなくても、アクセントの位置は変わっている（発音を句から作り直す）
    try:
        fresh = _accent_client(context, edit).mora_pitch(accent_from_edit(edit), edit.speaker)
    except ERRORS as e:
        print("音高を計算し直せません:", e)
        return
    for m, new in zip(item.moras, fresh[index]["moras"]):
        m.pitch = new.get("pitch") or 0.0
        m.voiced = m.pitch > 0
    fit_accent_range(edit)


def restructure_accent(context, phrases: list, changed: list[int]) -> None:
    """句を結合・分割したあとの句の並びを読み込む。`changed`（新しい並びでの番号）の句だけ音高と長さをエンジンで
    計算し直し、他の句は編集したまま。"""
    edit = context.window_manager.tachie_accent
    fresh = _accent_client(context, edit).mora_data(phrases, edit.speaker)
    merged = [fresh[i] if i in changed else ap for i, ap in enumerate(phrases)]
    load_accent_edit(edit, merged)
    edit.changed = True


class TACHIE_OT_accent_join(bpy.types.Operator):
    """この句と次の句を 1 つにする（アクセント句を結合）"""

    bl_idname = "tachie.accent_join"
    bl_label = "アクセント句を結合"
    bl_options = {"INTERNAL"}

    phrase: bpy.props.IntProperty()

    def execute(self, context):
        try:
            restructure_accent(context, join_phrases(accent_from_edit(context.window_manager.tachie_accent),
                                                     self.phrase), [self.phrase])
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class TACHIE_OT_accent_split(bpy.types.Operator):
    """このモーラの手前で句を 2 つに分ける"""

    bl_idname = "tachie.accent_split"
    bl_label = "ここで句を分ける"
    bl_options = {"INTERNAL"}

    phrase: bpy.props.IntProperty()
    mora: bpy.props.IntProperty()

    def execute(self, context):
        try:
            restructure_accent(context, split_phrase(accent_from_edit(context.window_manager.tachie_accent),
                                                     self.phrase, self.mora), [self.phrase, self.phrase + 1])
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


def _fresh_accent(context, text: str, speaker: int, kana: str | None, ch=None) -> list:
    c = client(context, ch)
    if kana:
        return c.accent_phrases(kana, speaker, is_kana=True)
    return c.audio_query(text, speaker)["accent_phrases"]


class TACHIE_OT_edit_accent(bpy.types.Operator):
    """選択中のセリフの抑揚（モーラごとの音高・長さ）を編集する"""

    bl_idname = "tachie.edit_accent"
    bl_label = "抑揚を編集"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def invoke(self, context, event):
        edit = context.window_manager.tachie_accent
        try:
            strips, text, ch, speaker, kana = _line_context(context)
            if kana:
                phrases = line_accent(strips, text, kana, speaker) or _fresh_accent(context, text, speaker, kana, ch)
            else:  # 自動の読みは今のもので確かめる（ユーザー辞書で変わることがある）
                query = client(context, ch).audio_query(text, speaker)
                phrases = line_accent(strips, text, None, speaker, auto=query.get("kana", "")) or query["accent_phrases"]
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        edit.clip_id, edit.text, edit.kana, edit.speaker = active_line_id(context), text, kana or "", speaker
        edit.character, edit.changed = ch.name, False
        load_accent_edit(edit, phrases)
        # 句を横に並べ、入りきらなければ次の段へ（YMM4 の「抑揚を編集」と同じ並び）
        total = sum(len(p["moras"]) for p in phrases) + len(phrases)
        edit.width = max(480, min(1500, int(MORA_UNITS * 20 * total) + 60))
        return context.window_manager.invoke_props_dialog(self, width=edit.width, confirm_text="反映して作り直す")

    def draw(self, context):
        edit = context.window_manager.tachie_accent
        layout = self.layout
        row = layout.row(align=True)
        row.prop(edit, "tab", expand=True)
        row.operator("tachie.preview_accent", text="", icon="PLAY")
        row.operator("tachie.reload_accent", text="", icon="FILE_REFRESH")
        pitch = edit.tab == "PITCH"
        attr, level = ("pitch", "pitch_level") if pitch else ("vowel_length", "length_level")
        # ダイアログの幅も UI の単位も画面の倍率で大きくなるので、倍率で割らない
        per_row = max(4, int((edit.width - 40) / (MORA_UNITS * 20)))
        row, used = None, per_row
        for i, item in enumerate(edit.phrases):
            n = len(item.moras) + (1 if i < len(edit.phrases) - 1 else 0)
            if row is None or used + n > per_row:
                row, used = layout.row(align=False), 0
            used += n
            box = row.box()
            box.ui_units_x = MORA_UNITS * len(item.moras) + 0.6
            if pitch:
                box.prop(item, "shift", text="句の高さ", slider=True)
            else:
                box.label(text="")
            grid = box.row(align=True)
            for k, m in enumerate(item.moras):
                col = grid.column(align=True)
                col.ui_units_x = MORA_UNITS
                if pitch:  # アクセントの位置（押したモーラにする。今の位置に ▼）
                    col.prop(m, "is_accent", text="▼" if item.accent == k + 1 else "", toggle=True)
                values = col.column(align=True)
                values.enabled = m.voiced or not pitch  # 無声化・促音の音高は 0 のまま
                values.prop(m, attr, text="")
                bars = values.column(align=True)
                bars.scale_y = 0.42
                bars.prop(m, level, expand=True)
                if not pitch:
                    sub = col.column(align=True)
                    sub.enabled = bool(m.consonant)
                    sub.prop(m, "consonant_length", text="")
                if k:  # モーラの文字を押すと、その手前で句を分ける
                    op = col.operator("tachie.accent_split", text=m.name, emboss=False)
                    op.phrase, op.mora = i, k
                else:
                    name = col.row()
                    name.alignment = "CENTER"
                    name.label(text=m.name)
            if i < len(edit.phrases) - 1:
                join = row.column(align=True)
                join.ui_units_x = 1.0
                join.label(text="")
                join.operator("tachie.accent_join", text="", icon="LINKED").phrase = i  # アクセント句を結合
        layout.label(text="上のボタンでアクセントの位置（▼）、棒で音高。モーラの文字で句を分け、鎖のボタンで次の句と結合" if pitch else
                     "棒・上の数値は母音の長さ、下の数値は子音の長さ（秒）", icon="INFO")

    def execute(self, context):
        edit = context.window_manager.tachie_accent
        if active_line_id(context) != edit.clip_id:
            self.report({"ERROR"}, "編集中に選択中のセリフが変わりました")
            return {"CANCELLED"}
        accent = accent_from_edit(edit)
        # アクセントの位置・句の区切りを変えたら、発音も句から作り直す（パネルの発音の欄に出る）
        kana = kana_from_phrases(accent) if edit.changed else (edit.kana or None)
        return _regenerate_active(self, context, kana=kana, accent=accent)


class TACHIE_OT_preview_accent(bpy.types.Operator):
    """編集中の抑揚で合成して試聴する"""

    bl_idname = "tachie.preview_accent"
    bl_label = "試聴"

    def execute(self, context):
        edit = context.window_manager.tachie_accent
        sed = seq_scene(context).sequence_editor
        try:
            strips = line_strips(sed, edit.clip_id)
            ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
            c = client(context, ch)
            query = c.audio_query(edit.text, edit.speaker)
            query.update(line_voice(strips, ch))
            query["accent_phrases"] = accent_from_edit(edit)
            wav = c.synthesis(query, edit.speaker)
        except (*ERRORS, IndexError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        problem = play_preview(context, wav)
        if problem:
            self.report({"WARNING"}, problem)
        return {"FINISHED"}


class TACHIE_OT_reload_accent(bpy.types.Operator):
    """編集中の抑揚を、発音から自動で付けたものに戻す"""

    bl_idname = "tachie.reload_accent"
    bl_label = "抑揚を取り直す"

    def execute(self, context):
        edit = context.window_manager.tachie_accent
        try:
            strips = line_strips(seq_scene(context).sequence_editor, edit.clip_id)
            name = strips[0].get("tachie_character") if strips else None
            ch = next((c for c in seq_scene(context).tachie.characters if c.name == name), None)
            load_accent_edit(edit, _fresh_accent(context, edit.text, edit.speaker, edit.kana or None, ch))
            edit.changed = False  # 句の区切り・アクセントの位置も発音のものに戻った
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class TACHIE_OT_clear_accent(bpy.types.Operator):
    """選択中のセリフの抑揚の編集を捨てて作り直す"""

    bl_idname = "tachie.clear_accent"
    bl_label = "抑揚の編集を消す"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_line_id(context) is not None

    def execute(self, context):
        return _regenerate_active(self, context, accent=[])


def _katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


class TACHIE_OT_dict_refresh(bpy.types.Operator):
    """VOICEVOX エンジンのユーザー辞書を読み込む"""

    bl_idname = "tachie.dict_refresh"
    bl_label = "ユーザー辞書を読み込む"

    def execute(self, context):
        global _dict_words
        try:
            words = client(context).user_dict()
        except VoicevoxError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        _dict_words = sorted(((uuid, w["surface"], w["pronunciation"], w["accent_type"]) for uuid, w in words.items()),
                             key=lambda w: w[2])
        return {"FINISHED"}


class TACHIE_OT_dict_add(bpy.types.Operator):
    """入力した単語をユーザー辞書に登録する（置いてあるセリフには再生成で反映）"""

    bl_idname = "tachie.dict_add"
    bl_label = "辞書に登録"

    def execute(self, context):
        d = context.window_manager.tachie_dict
        surface, pronunciation = d.surface.strip(), _katakana(d.pronunciation.strip())
        if not surface or not pronunciation:
            self.report({"ERROR"}, "表記と読みを入力してください")
            return {"CANCELLED"}
        try:
            client(context).add_user_dict_word(surface, pronunciation, d.accent_type)
        except VoicevoxError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        d.surface = d.pronunciation = ""
        d.accent_type = 0
        bpy.ops.tachie.dict_refresh()
        self.report({"INFO"}, f"「{surface}」（{pronunciation}）を登録しました。置いてあるセリフは再生成で反映されます")
        return {"FINISHED"}


class TACHIE_OT_dict_remove(bpy.types.Operator):
    """ユーザー辞書から単語を消す"""

    bl_idname = "tachie.dict_remove"
    bl_label = "辞書から消す"

    uuid: bpy.props.StringProperty()

    def execute(self, context):
        try:
            client(context).delete_user_dict_word(self.uuid)
        except VoicevoxError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        bpy.ops.tachie.dict_refresh()
        return {"FINISHED"}


def _free_face_channel(sed, settings, start: int, end: int, char: str) -> int:
    """キャラ `char` の表情ストリップのチャンネル（表情のレーン。重なる表情があればその上）。"""
    scene = settings.id_data
    lanes.ensure_layout(scene)
    return lanes.face_channel(scene, char, start, end)


def _face_name(character: str, preset: str) -> str:
    return f"表情 {character}: {preset or '基本'}"


class TACHIE_OT_add_face(bpy.types.Operator):
    """選択中のキャラクターの表情ストリップを再生位置に置く（重なる範囲だけ立ち絵がその表情になる）"""

    bl_idname = "tachie.add_face"
    bl_label = "表情を置く"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def execute(self, context):
        global _applying_faces
        scene = seq_scene(context)
        settings = scene.tachie
        ch = settings.character()
        if not ch.folder:
            self.report({"ERROR"}, f"{ch.name}は立ち絵の無いキャラクターです")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        sed = scene.sequence_editor or scene.sequence_editor_create()
        start = scene.frame_current
        # 再生位置にこのキャラのセリフがあれば、その終わりまで
        line = next((strips for s in line_sounds(sed, ch.name) if s.left_handle <= start
                     for strips in [line_strips(sed, s["tachie_id"])] if start < line_end(strips)), None)
        end = line_end(line) if line else start + max(1, round(settings.face_seconds * scene_fps(scene)))
        preset = settings.expression
        face = sed.strips.new_effect(name=_face_name(ch.name, preset), type="COLOR",
                                     channel=_free_face_channel(sed, settings, start, end, ch.name),
                                     frame_start=start, length=end - start)
        face.color = (0.95, 0.6, 0.25)
        face.color_tag = "COLOR_02"
        face.mute = True  # 画面には出さない（立ち絵の表情を変える印）
        face["tachie_face"] = ch.name
        face["tachie_expression"] = preset
        problems = []
        _applying_faces = True
        try:
            refresh_faces(context, problems)
        finally:
            _applying_faces = False
            _applied_signatures[scene.name] = auto_signature(scene)
        for s in sed.strips_all:
            s.select = False
        face.select = True
        sed.active_strip = face
        if problems:
            self.report({"WARNING"}, problems[0])
        return {"FINISHED"}


class TACHIE_OT_apply_faces(bpy.types.Operator):
    """表情ストリップに合わせて立ち絵を描き直す（音声は合成し直さない）"""

    bl_idname = "tachie.apply_faces"
    bl_label = "表情を反映"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _applying_faces
        problems = []
        _applying_faces = True
        try:
            count = refresh_faces(context, problems)
        finally:
            _applying_faces = False
            _applied_signatures[seq_scene(context).name] = auto_signature(seq_scene(context))
        if problems:
            self.report({"WARNING"}, f"{len(problems)} 本は描き直せませんでした: {problems[0]}")
        else:
            self.report({"INFO"}, f"{count} 本の立ち絵を描き直しました")
        return {"FINISHED"}


# ---- まとめて変える ----

def selected_targets(sed) -> tuple[list[str], list, list]:
    """選択中のセリフ（tachie_id、始まり順）、セリフなしの立ち絵、表情ストリップ。"""
    if sed is None:
        return [], [], []
    ids, stills, faces = {}, [], []
    for s in sed.strips:
        if not s.select:
            continue
        if s.get("tachie_id"):
            ids[s["tachie_id"]] = min(ids.get(s["tachie_id"], s.left_handle), s.left_handle)
        elif s.get("tachie_still"):
            stills.append(s)
        elif s.get("tachie_face"):
            faces.append(s)
    return sorted(ids, key=ids.get), stills, faces


_batch_mixed: set[str] = set()
_batch_styles: list = []
_batch_engine = ""  # スタイルの一覧を取ったエンジンの URL
_batch_character_items: list = []
_batch_expression_items: list = []


def _batch_use(name):
    def update(self, context):
        setattr(self, f"use_{name}", True)
    return update


def _batch_characters(self, context):
    global _batch_character_items
    _batch_character_items = [(c.name, c.name, "") for c in seq_scene(context).tachie.characters]
    return _batch_character_items


def _batch_expressions(self, context):
    """対象のキャラ全員が持っている表情プリセット（キャラクターも変えるなら変更先のもの）と基本。"""
    global _batch_expression_items
    scene = seq_scene(context)
    sed = scene.sequence_editor
    ids, stills, faces = selected_targets(sed)
    names = {line_strips(sed, cid)[0]["tachie_character"] for cid in ids}
    if self.use_character and self.character:
        names = {self.character}
    names |= {s["tachie_still"] for s in stills} | {f["tachie_face"] for f in faces}
    common = None
    for ch in scene.tachie.characters:
        if ch.name in names:
            presets = [e.name for e in ch.expressions]
            common = presets if common is None else [p for p in common if p in presets]
    # 動的な EnumProperty は値を番号で持つ。候補が変わっても同じ番号が同じ表情を指すように、
    # シーンの全表情の名前順で番号を振る（並び順の番号だと、キャラクターを変えたときに別の表情に化ける）
    numbers = {p: i for i, p in enumerate(sorted({e.name for c in scene.tachie.characters for e in c.expressions}), 1)}
    _batch_expression_items = [(NONE, "（基本）", "", 0)] + [(p, p, "", numbers[p]) for p in common or []]
    return _batch_expression_items


def _batch_style_items(self, context):
    return _batch_styles or [(NONE, "（取得できません）", "")]


class TACHIE_OT_batch_edit(bpy.types.Operator):
    """選択中のセリフ・立ち絵・表情ストリップの表情や音量などをまとめて変える（変えた項目だけを全部に当てる）"""

    bl_idname = "tachie.batch_edit"
    bl_label = "まとめて変える"
    bl_options = {"REGISTER", "UNDO"}

    use_expression: bpy.props.BoolProperty(name="表情", options={"SKIP_SAVE"})
    expression: bpy.props.EnumProperty(name="表情", items=_batch_expressions, update=_batch_use("expression"))
    use_character: bpy.props.BoolProperty(name="キャラクター", options={"SKIP_SAVE"})
    character: bpy.props.EnumProperty(name="キャラクター", items=_batch_characters, update=_batch_use("character"))
    use_speaker: bpy.props.BoolProperty(name="スタイル", options={"SKIP_SAVE"})
    speaker: bpy.props.EnumProperty(name="スタイル", items=_batch_style_items, update=_batch_use("speaker"))
    use_line_layout: bpy.props.BoolProperty(name="セリフの形", options={"SKIP_SAVE"})
    line_layout: bpy.props.EnumProperty(name="セリフの形", items=LINE_LAYOUTS, update=_batch_use("line_layout"))

    @classmethod
    def poll(cls, context):
        ids, stills, faces = selected_targets(seq_scene(context).sequence_editor)
        return len(ids) + len(stills) + len(faces) > 0

    def invoke(self, context, event):
        global _batch_styles, _batch_engine
        sed = seq_scene(context).sequence_editor
        ids, stills, faces = selected_targets(sed)
        _batch_mixed.clear()
        if ids:
            settings = seq_scene(context).tachie
            first_ch = next((c for c in settings.characters
                             if c.name == line_strips(sed, ids[0])[0].get("tachie_character")), None)
            _batch_engine = engine_url(context, first_ch)
            try:  # スタイルの一覧は先頭のセリフのキャラクターのエンジンから
                _batch_styles = [(str(i), name, "", i) for i, name in
                                 client(context, first_ch, timeout=3.0).styles()]
            except (VoicevoxError, OSError):
                _batch_styles = []
            values = []
            for cid in ids:
                strips = line_strips(sed, cid)
                ch = next((c for c in settings.characters if c.name == strips[0]["tachie_character"]), None)
                if ch is None:
                    continue  # キャラクターを消したセリフ（execute で「変えられなかった」に数える）
                sound = line_sound(strips) or strips[0]
                v = {"character": ch.name, "speaker": str(sound.get("tachie_speaker", ch.speaker)),
                     "expression": strips[0].get("tachie_expression", "") or NONE,
                     "line_layout": line_layout_of(strips)}
                v.update({prop: line_voice(strips, ch)[key] for prop, (key, *_) in VOICE_PROPS.items()})
                v.update({f"audio_{k}": val for k, val in line_audio(strips, ch).items()})
                values.append(v)
            first = values[0] if values else {}
            for name, value in first.items():
                if any(v[name] != value for v in values[1:]):
                    _batch_mixed.add(name)
            # 先頭のセリフの値を入れておく（update でチェックが入らないよう、use_ は後で戻す）
            # 選べる候補に無い値（共通の表情に無いなど）は基本にする
            if first and first["expression"] not in {i[0] for i in _batch_expressions(self, context)}:
                first["expression"] = NONE
            for name, value in first.items():
                try:
                    setattr(self, name, value)
                except (TypeError, ValueError):
                    pass
        else:
            # 立ち絵・表情ストリップだけ: 先頭の表情を入れておく（前回の値が候補に無いと空になる）
            expression = (stills or faces)[0].get("tachie_expression", "") or NONE
            if expression not in {i[0] for i in _batch_expressions(self, context)}:
                expression = NONE
            self.expression = expression
        for name in ["expression", "character", "speaker", "line_layout", *VOICE_PROPS,
                     *(f"audio_{k}" for k in AUDIO_PROPS)]:
            setattr(self, f"use_{name}", False)
        return context.window_manager.invoke_props_dialog(self, width=420, confirm_text="まとめて変える")

    def draw(self, context):
        ids, stills, faces = selected_targets(seq_scene(context).sequence_editor)
        layout = self.layout
        parts = [f"セリフ {len(ids)}" if ids else "", f"立ち絵 {len(stills)}" if stills else "",
                 f"表情ストリップ {len(faces)}" if faces else ""]
        layout.label(text="対象: " + "、".join(p for p in parts if p) + "（値を変えた項目だけを当てます）")

        def row(name, label=None, menu=False):
            r = layout.row(align=True)
            r.prop(self, f"use_{name}", text="")
            sub = r.row(align=True)
            sub.active = getattr(self, f"use_{name}")
            mixed = name in _batch_mixed and not getattr(self, f"use_{name}")
            if menu:  # 選択欄はラベルを別に置く（prop の text だと切れる）
                split = sub.split(factor=0.3, align=True)
                split.label(text=label + ("（混在）" if mixed else ""))
                split.prop(self, name, text="")
                return
            sub.prop(self, name, text=label or "")
            if mixed:
                sub.label(text="（混在）")

        row("expression", "表情", menu=True)
        if ids:
            row("character", "キャラクター", menu=True)
            if _batch_styles:
                row("speaker", "スタイル", menu=True)
            row("line_layout", "セリフの形", menu=True)
            layout.separator()
            for prop, (_, label, *_) in VOICE_PROPS.items():
                row(prop, label)
            layout.separator()
            for key, (label, *_) in AUDIO_PROPS.items():
                row(f"audio_{key}", label)

    def execute(self, context):
        scene = seq_scene(context)
        sed = scene.sequence_editor
        settings = scene.tachie
        ids, stills, faces = selected_targets(sed)
        if self.use_expression and not self.expression:
            # 選べる候補に無い値（キャラクターを変えて、そのキャラに無い表情になったなど）。基本に化けさせない
            self.report({"WARNING"}, "表情を選び直してください")
            return {"CANCELLED"}
        preset = ("" if self.expression == NONE else self.expression) if self.use_expression else None
        voice_changes = {key: round(getattr(self, prop), 6) for prop, (key, *_) in VOICE_PROPS.items()
                         if getattr(self, f"use_{prop}")}
        audio_changes = {k: round(getattr(self, f"audio_{k}"), 6) for k in AUDIO_PROPS if getattr(self, f"use_audio_{k}")}
        use_speaker = self.use_speaker and self.speaker not in (NONE, "")
        if preset is None and not (voice_changes or audio_changes or self.use_character or use_speaker
                                   or self.use_line_layout):
            self.report({"WARNING"}, "変える項目がありません（値を変えるか、左のチェックを入れてください）")
            return {"CANCELLED"}
        mix_only = set(audio_changes) <= {"volume", "pan"}
        regen = (preset is not None or self.use_character or use_speaker or self.use_line_layout
                 or bool(voice_changes) or not mix_only)
        count, problems = 0, []
        wm = context.window_manager
        wm.progress_begin(0, len(ids) + len(stills))
        try:
            # 後ろから作り直すと、長さが変わって後ろがずれても未処理のセリフの位置を追わなくてよい
            for cid in reversed(ids):
                strips = line_strips(sed, cid)
                try:
                    ch = find_character(settings, strips[0]["tachie_character"])
                except ERRORS as e:
                    problems.append(str(e))
                    continue
                if not regen:
                    sound = line_sound(strips)
                    if sound is not None:
                        set_line_audio(sound, {**line_audio(strips, ch), **audio_changes})
                    count += 1
                    continue
                kwargs = {}
                if self.use_character and self.character and self.character != ch.name:
                    kwargs["character"] = self.character
                    ch = find_character(settings, self.character)
                    base_voice, base_audio = character_voice(ch), character_audio(ch)
                else:
                    base_voice, base_audio = line_voice(strips, ch), line_audio(strips, ch)
                if use_speaker:
                    if engine_url(context, ch) != _batch_engine:
                        # スタイル ID はエンジンごとに違う（一覧は先頭のセリフのエンジンのもの）
                        problems.append(f"{ch.name} は声のエンジンが違うのでスタイルを変えられません")
                        continue
                    kwargs["speaker"] = int(self.speaker)
                if voice_changes or "character" in kwargs:
                    kwargs["voice"] = {**base_voice, **voice_changes}
                if audio_changes or "character" in kwargs:
                    kwargs["audio"] = {**base_audio, **audio_changes}
                if preset is not None:
                    if preset and preset not in {e.name for e in ch.expressions}:
                        problems.append(f"{ch.name} に表情 {preset!r} がありません")
                        continue
                    kwargs["preset"] = preset
                if self.use_line_layout:
                    kwargs["layout"] = self.line_layout
                try:
                    if set(kwargs) == {"preset"}:
                        set_line_expression(context, cid, preset)  # 表情だけなら音声は合成し直さない
                    else:
                        regenerate(context, cid, **kwargs)
                except ERRORS as e:
                    problems.append(str(e))
                    continue
                count += 1
                wm.progress_update(count)
            if preset is not None:
                # 表情ストリップを先に変える（立ち絵を古い表情ストリップで作ってから描き直さないように）
                for face in faces:
                    ch = next((c for c in settings.characters if c.name == face["tachie_face"]), None)
                    if preset and (ch is None or preset not in {e.name for e in ch.expressions}):
                        problems.append(f"{face['tachie_face']} に表情 {preset!r} がありません")
                        continue
                    face["tachie_expression"] = preset
                    face.name = _face_name(face["tachie_face"], preset)
                    count += 1
                for still in stills:
                    try:
                        remake_still(context, still, preset)
                        count += 1
                    except ERRORS as e:
                        problems.append(str(e))
        finally:
            wm.progress_end()
            refresh_faces(context)
            _applied_signatures[scene.name] = auto_signature(scene)
        if problems:
            self.report({"WARNING"}, f"{count} 個を変えました。{len(problems)} 個は変えられませんでした: {problems[0]}")
        else:
            self.report({"INFO"}, f"{count} 個を変えました")
        return {"FINISHED"}


for _prop, (_key, _label, _min, _max, _desc) in VOICE_PROPS.items():
    TACHIE_OT_batch_edit.__annotations__[f"use_{_prop}"] = bpy.props.BoolProperty(name=_label, options={"SKIP_SAVE"})
    TACHIE_OT_batch_edit.__annotations__[_prop] = bpy.props.FloatProperty(
        name=_label, min=_min, max=_max, default=VOICE_DEFAULTS[_key], step=1, precision=2, description=_desc,
        update=_batch_use(_prop))
for _key, (_label, _min, _max, _default, _desc) in AUDIO_PROPS.items():
    TACHIE_OT_batch_edit.__annotations__[f"use_audio_{_key}"] = bpy.props.BoolProperty(name=_label,
                                                                                       options={"SKIP_SAVE"})
    TACHIE_OT_batch_edit.__annotations__[f"audio_{_key}"] = bpy.props.FloatProperty(
        name=_label, min=_min, max=_max, default=_default, step=1, precision=2, description=_desc,
        unit="TIME_ABSOLUTE" if _key in BAKED_AUDIO else "NONE", update=_batch_use(f"audio_{_key}"))


class TACHIE_OT_regenerate_all(bpy.types.Operator):
    """すべてのセリフを、今のキャラクター設定で作り直す（表情を変えたとき。声とスタイルはセリフごとの値のまま）"""

    bl_idname = "tachie.regenerate_all"
    bl_label = "全セリフを再生成"
    bl_options = {"REGISTER", "UNDO"}

    character_only: bpy.props.BoolProperty(name="選択中のキャラクターだけ", default=False)
    use_character_voice: bpy.props.BoolProperty(
        name="キャラクターの声を当て直す", default=False,
        description="セリフごとの声・スタイル・音量などを、キャラクターの今の既定値に置き換える",
        options={"SKIP_SAVE"})  # 前回の値が残ると、ふつうの「全セリフを再生成」でセリフごとの声が消える

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        if sed is None:
            return {"CANCELLED"}
        settings = seq_scene(context).tachie
        only = settings.character().name if self.character_only and settings.character() else None
        firsts = {}
        for s in sed.strips:
            cid = s.get("tachie_id")
            if cid and (only is None or s.get("tachie_character") == only):
                firsts[cid] = min(firsts.get(cid, s.left_handle), s.left_handle)
        stills = [s for s in sed.strips if s.get("tachie_still") and (only is None or s["tachie_still"] == only)]
        # 後ろから作り直すと、長さが変わってずらしても未処理のセリフの位置を追いかけなくてよい
        count = 0
        wm = context.window_manager
        wm.progress_begin(0, len(firsts))
        try:
            for cid in sorted(firsts, key=firsts.get, reverse=True):
                kwargs = {}
                if self.use_character_voice:
                    ch = find_character(settings, line_strips(sed, cid)[0]["tachie_character"])
                    kwargs = {"speaker": ch.speaker, "voice": character_voice(ch), "audio": character_audio(ch)}
                connect(context, regenerate(context, cid, **kwargs))
                count += 1
                wm.progress_update(count)
            # 立ち絵はずらし終わった位置と、作り直したセリフで作り直す
            for s in stills:
                remake_still(context, s)
        except ERRORS as e:
            self.report({"ERROR"}, f"{count} 件作り直したところで失敗しました: {e}")
            return {"FINISHED"} if count else {"CANCELLED"}
        finally:
            wm.progress_end()
            if count or stills:
                refresh_faces(context)
        self.report({"INFO"}, f"{count} 件のセリフ" + (f"と {len(stills)} 本の立ち絵" if stills else "") + "を作り直しました")
        return {"FINISHED"}


class TACHIE_OT_character_add(bpy.types.Operator):
    """立ち絵の素材フォルダ（動く立ち絵・シンプル立ち絵）か PSD ファイルを選んでキャラクターを追加する"""

    bl_idname = "tachie.character_add"
    bl_label = "キャラクターを追加"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.psd", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        # PSD を選んだらそのファイル、フォルダを選んだら（ファイル名が空）フォルダ
        source = self.filepath if self.filepath.lower().endswith(".psd") else self.directory
        try:
            ch, sources = add_character(context, source)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if sources:
            self.report({"INFO"}, f"{ch.name} を {'と'.join(sources)} の初期値で追加しました")
        return {"FINISHED"}


class TACHIE_OT_change_material(bpy.types.Operator):
    """選択中のキャラクターの素材（動く立ち絵・シンプル立ち絵のフォルダか PSD）を選び直す（声・字幕などの設定はそのまま）"""

    bl_idname = "tachie.change_material"
    bl_label = "素材を選び直す"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.psd", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def invoke(self, context, event):
        ch = seq_scene(context).tachie.character()
        if ch.folder:  # 今の素材の場所から選び始める
            current = os.path.normpath(bpy.path.abspath(ch.folder))
            if os.path.isdir(current):
                # filepath に末尾の / が無いフォルダを入れると、親フォルダでファイル名欄にフォルダ名が入る
                self.directory = os.path.join(current, "")
            else:
                self.filepath = current
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        # 今の PSD から選び始めてフォルダへ移ると、ファイル名欄に前の PSD の名前が残る。その PSD が無ければフォルダを使う
        is_psd = self.filepath.lower().endswith(".psd") and os.path.isfile(self.filepath)
        source = self.filepath if is_psd else self.directory
        try:
            removed = change_material(context, ch, source)
        except (*ERRORS, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        sed = seq_scene(context).sequence_editor
        try:  # 素材はもう変えたので、立ち絵を作り直せなくても FINISHED にする（CANCELLED だと元に戻すの段が積まれない）
            for s in [s for s in sed.strips if s.get("tachie_still") == ch.name] if sed else []:
                remake_still(context, s)
            refresh_motion_now(seq_scene(context))
        except ERRORS as e:
            self.report({"WARNING"}, f"{ch.name}の素材を変えましたが、立ち絵を作り直せません: {e}")
            return {"FINISHED"}
        note = f"（素材に無いパーツ・レイヤー {removed} 個を既定に戻しました）" if removed else ""
        self.report({"WARNING"} if removed else {"INFO"}, f"{ch.name}の素材を変えました{note}")
        return {"FINISHED"}


def fit_to_material(item, material) -> int:
    """キャラクターか表情プリセットのパーツ・PSD のレイヤーのうち、素材に無いものを外す（既定に戻す）。外した数を返す。"""
    removed = 0
    for category, props in PART_PROPS.items():
        names = {} if material.is_psd else material.parts.get(category, {})
        for prop in props:
            if getattr(item, prop) and getattr(item, prop) not in names:
                setattr(item, prop, "")
                removed += 1
    layers = item_layers(item)
    if layers:
        keep = {p: v for p, v in layers.items() if material.is_psd and material.psd.find(p) is not None}
        removed += len(layers) - len(keep)
        set_item_layers(item, keep)
    return removed


def change_material(context, ch, source: str) -> int:
    """キャラクターの素材を `source` に変える。素材に無いパーツ・レイヤーは外し、その数を返す。"""
    material = load_material(source)
    ch.folder = source
    removed = fit_to_material(ch, material) + sum(fit_to_material(e, material) for e in ch.expressions)
    if material.is_psd and not item_layers(ch):
        psd_default_mouth(ch, material)
    return removed


def add_voice_character(context, name: str):
    """立ち絵の無いキャラクター（素材フォルダなし、声と字幕だけ）を追加する。(キャラクター, アドオン設定の
    初期値を使ったか) を返す。初期値は `name`（同じ名前があって番号を付けても、付ける前の名前）で探す。"""
    settings = seq_scene(context).tachie
    names = {c.name for c in settings.characters}
    base, i = name.strip() or "ナレーション", 2
    name = base
    while name in names:
        name, i = f"{base} {i}", i + 1
    saved = load_voice_defaults().get(base, {})
    try:
        config = parse_characters({name: merge_tables({"speaker": 3}, saved, {"folder": ""})}, Path("."),
                                  use_material=False)[name]
    except (*ERRORS, OSError) as e:
        raise ValueError(f"{base} の初期値を読めません: {e}") from None
    ch = settings.characters.add()
    try:
        apply_config(ch, config)
    except (TypeError, ValueError, KeyError) as e:  # 手で書いた値の型や範囲が合わない
        settings.characters.remove(len(settings.characters) - 1)
        raise ValueError(f"{base} の初期値を当てはめられません: {e}") from None
    lanes.ensure_layout(seq_scene(context))  # チャンネルはキャラ × 役割のレーンで決まる
    try:  # エンジンが止まっていても追加は待たせない
        ch.speaker_name = dict(client(context, ch, timeout=3.0).styles()).get(ch.speaker, "")
    except (VoicevoxError, OSError):
        pass
    settings.active_character = len(settings.characters) - 1
    return ch, bool(saved)


def _voice_default_names(self, context, edit_text):
    try:
        return [n for n in load_voice_defaults() if edit_text in n]
    except ValueError:
        return []


class TACHIE_OT_character_add_voice(bpy.types.Operator):
    """立ち絵の無いキャラクター（ナレーションなど、声と字幕だけ）を追加する"""

    bl_idname = "tachie.character_add_voice"
    bl_label = "立ち絵なしのキャラクターを追加"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(name="名前", default="ナレーション", search=_voice_default_names,
                                   description="アドオン設定に初期値を保存した名前なら、その設定で始める")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            ch, saved = add_voice_character(context, self.name)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        layout = next(label for key, label, *_ in LINE_LAYOUTS if key == line_layout(ch))
        note = "アドオン設定の初期値を使いました" if saved else f"セリフの形は「{layout}」"
        self.report({"INFO"}, f"{ch.name} を追加しました（{note}）")
        return {"FINISHED"}


def add_character(context, source: str):
    """素材フォルダか PSD からキャラクターを追加する。(キャラクター, 使った初期値の出どころ) を返す。"""
    settings = seq_scene(context).tachie
    try:
        character = load_material(source)
        key = material_key(source)
        material = load_material_defaults(character.path)
        saved = load_character_defaults().get(key, {})
        # 優先: アドオン設定 → 素材の tachie.toml → 素材の preset.ini → 固定値
        table = merge_tables({"speaker": 3}, material, saved)
        name = str(table.pop("name", "") or key)
        config = parse_characters({name: {**table, "folder": source}}, Path("."), use_material=False)[name]
    except (*ERRORS, OSError) as e:
        raise ValueError(str(e)) from None
    ch = settings.characters.add()
    try:
        apply_config(ch, config)
    except (TypeError, ValueError, KeyError) as e:  # tachie.toml の値の型や範囲が合わない
        settings.characters.remove(len(settings.characters) - 1)
        raise ValueError(f"初期値を当てはめられません: {e}") from None
    ch.folder = source
    if character.is_psd and not item_layers(ch):
        psd_default_mouth(ch, character)
    n = len(settings.characters)
    # 立ち位置が決まっていなければ、2 人目以降は左右を入れ替える
    if "position" not in config.layout and n > 1:
        ch.position_x = -0.3 if n % 2 == 0 else 0.3
    lanes.ensure_layout(seq_scene(context))  # チャンネルはキャラ × 役割のレーンで決まる
    try:  # エンジンが止まっていても追加は待たせない
        names = client(context, ch, timeout=3.0).styles()
        ch.speaker_name = dict(names).get(ch.speaker, "")
    except (VoicevoxError, OSError):
        pass
    settings.active_character = n - 1
    has_toml = (Path(character.path) / MATERIAL_CONFIG).is_file()
    has_ini = (Path(character.path) / PRESET_FILE).is_file()
    sources = [label for label, used in (("アドオン設定", bool(saved)), (MATERIAL_CONFIG, has_toml),
                                         (PRESET_FILE, has_ini)) if used]
    return ch, sources


class TACHIE_OT_save_defaults(bpy.types.Operator):
    """選択中のキャラクターの設定を、次にこの素材（立ち絵の無いキャラは同じ名前）を追加するときの初期値として保存する"""

    bl_idname = "tachie.save_defaults"
    bl_label = "初期値として保存"

    target: bpy.props.EnumProperty(items=[
        ("PREFS", "アドオン設定", "アドオン設定に保存する（全 .blend 共通。素材の tachie.toml より優先）"),
        ("MATERIAL", "素材フォルダ", "素材フォルダの tachie.toml に書く（素材と一緒に持ち運べる）"),
    ])

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        if not ch.folder:  # 立ち絵の無いキャラはキャラ名で、アドオン設定にだけ保存できる
            if self.target != "PREFS":
                self.report({"ERROR"}, "立ち絵の無いキャラクターには素材フォルダがありません。アドオン設定に保存してください")
                return {"CANCELLED"}
            try:
                texts = _defaults_texts()
                texts[(VOICE_DEFAULTS_TABLE, ch.name)] = dump_table(voice_defaults_config(ch), key=ch.name,
                                                                    parent=VOICE_DEFAULTS_TABLE)
                save_character_defaults(texts)
            except (ValueError, OSError) as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            self.report({"INFO"}, f"{ch.name} の初期値をアドオン設定に保存しました（同じ名前の立ち絵なしのキャラクターで使います）")
            return {"FINISHED"}
        config = config_of(ch)
        key = material_key(ch.folder)
        name = ch.name if ch.name != key else None
        try:
            if self.target == "PREFS":
                texts = _defaults_texts()
                texts[key] = dump_table(config, key=key, name=name)
                save_character_defaults(texts)
                where = "アドオン設定"
            else:
                folder = Path(bpy.path.abspath(ch.folder))
                if not folder.is_dir():  # PSD 立ち絵（folder が PSD ファイル）は素材の tachie.toml を読まない
                    self.report({"ERROR"}, "PSD 立ち絵の初期値はアドオン設定に保存してください")
                    return {"CANCELLED"}
                path = folder / MATERIAL_CONFIG
                try:
                    path.write_text(dump_table(config, name=name), encoding="utf-8")
                except OSError as e:
                    self.report({"ERROR"}, f"{path} に書けません（{e.strerror}）。アドオン設定に保存してください")
                    return {"CANCELLED"}
                where = str(path)
        except (ValueError, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{ch.name} の初期値を {where} に保存しました")
        return {"FINISHED"}


class TACHIE_OT_remove_defaults(bpy.types.Operator):
    """アドオン設定に保存したキャラクターの初期値を消す"""

    bl_idname = "tachie.remove_defaults"
    bl_label = "初期値を消す"

    key: bpy.props.StringProperty()
    # SKIP_SAVE: UI のボタンは指定しないプロパティに前回の値を入れるので、素材の初期値の × で voice=True が残らないように
    voice: bpy.props.BoolProperty(description="立ち絵の無いキャラクターの初期値（key はキャラ名）", options={"SKIP_SAVE"})

    def execute(self, context):
        try:
            texts = _defaults_texts()
            texts.pop((VOICE_DEFAULTS_TABLE, self.key) if self.voice else self.key, None)
            save_character_defaults(texts)
        except (ValueError, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class TACHIE_OT_character_remove(bpy.types.Operator):
    """選択中のキャラクターを一覧から外す（置いたストリップは消えない）"""

    bl_idname = "tachie.character_remove"
    bl_label = "キャラクターを削除"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def execute(self, context):
        settings = seq_scene(context).tachie
        settings.characters.remove(settings.active_character)
        settings.active_character = max(0, min(settings.active_character, len(settings.characters) - 1))
        for p in lanes.ensure_layout(seq_scene(context)):  # 残ったストリップは「未登録」のレーンへ
            self.report({"WARNING"}, p)
        return {"FINISHED"}


class TACHIE_OT_character_move(bpy.types.Operator):
    """選択中のキャラクターを一覧の上・下へ動かす（下のキャラほど立ち絵が手前に描かれる）"""

    bl_idname = "tachie.character_move"
    bl_label = "キャラクターを動かす"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(items=[("UP", "上へ", ""), ("DOWN", "下へ", "")])

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def execute(self, context):
        settings = seq_scene(context).tachie
        i = settings.active_character
        j = i - 1 if self.direction == "UP" else i + 1
        if not 0 <= j < len(settings.characters):
            return {"CANCELLED"}
        settings.characters.move(i, j)
        settings.active_character = j
        for p in lanes.ensure_layout(seq_scene(context)):
            self.report({"WARNING"}, p)
        return {"FINISHED"}


def _styles(self, context):
    return _style_items


class TACHIE_OT_pick_speaker(bpy.types.Operator):
    """VOICEVOX の話者一覧から選ぶ"""

    bl_idname = "tachie.pick_speaker"
    bl_label = "話者を選ぶ"
    bl_property = "style"
    bl_options = {"UNDO"}

    style: bpy.props.EnumProperty(items=_styles)
    target: bpy.props.EnumProperty(items=[
        ("CHARACTER", "キャラクター", "選択中のキャラクターの話者"),
        ("LINE", "選択中のセリフ", "選択中のセリフのスタイルを変えて作り直す"),
    ], options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None or active_line_id(context) is not None

    def invoke(self, context, event):
        global _style_items
        settings = seq_scene(context).tachie
        if self.target == "LINE" and active_line_id(context) is not None:
            sed = seq_scene(context).sequence_editor
            ch = next((c for c in settings.characters if c.name == sed.active_strip.get("tachie_character")), None)
        else:
            ch = settings.character()
        try:
            styles = client(context, ch).styles()
        except VoicevoxError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        _style_items = [(str(i), name, "", i) for i, name in styles]
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if self.target == "LINE":
            if active_line_id(context) is None:
                return {"CANCELLED"}
            return _regenerate_active(self, context, speaker=int(self.style))
        ch = seq_scene(context).tachie.character()
        if ch is None:
            return {"CANCELLED"}
        ch.speaker = int(self.style)
        ch.speaker_name = next((name for i, name, *_ in _style_items if i == self.style), "")
        return {"FINISHED"}


def _parts(self, context):
    return _part_items


class TACHIE_OT_pick_part(bpy.types.Operator):
    """素材のパーツ一覧から選ぶ"""

    bl_idname = "tachie.pick_part"
    bl_label = "パーツを選ぶ"
    bl_property = "part"
    bl_options = {"UNDO"}

    # 前に使ったときの値を覚えない（覚えると、立ち絵やプリセットのパーツを選んだあとに基本の表情のボタンを押しても、
    # 立ち絵・プリセットのほうを変えようとした）
    prop: bpy.props.StringProperty(options={"SKIP_SAVE"})
    preset: bpy.props.StringProperty(description="空ならキャラクターの基本の表情、指定するとその表情プリセット",
                                     options={"SKIP_SAVE"})
    still: bpy.props.BoolProperty(description="選択中の立ち絵のパーツ（その立ち絵だけ）", options={"SKIP_SAVE"})
    part: bpy.props.EnumProperty(items=_parts, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def _character(self, context):
        settings = seq_scene(context).tachie
        if self.still:
            return find_character(settings, seq_scene(context).sequence_editor.active_strip["tachie_still"])
        return settings.character()

    def invoke(self, context, event):
        global _part_items, _part_rows
        category = next(c for c, props in PART_PROPS.items() if self.prop in props)
        try:
            character = load_material(self._character(context).folder)
        except (*ERRORS, AttributeError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        parts = character.parts.get(category, {})
        names = sorted(parts)
        if self.still:
            empty = "（立ち絵の表情のまま）"
        elif self.preset:
            empty = "（基本の表情のまま）"
        else:
            empty = "（なし）" if category in SLOT_CATEGORIES else "（既定）"
        _part_items = [(NONE, empty, "")] + [(n, n, "") for n in names]
        # YMM4 と同じく、パーツの見本（写っている範囲を切り抜いた画像）を名前の左に並べた一覧
        wm = context.window_manager
        wm.progress_begin(0, len(names))
        try:
            _part_rows = [("", empty, 0)]
            for i, n in enumerate(names):
                _part_rows.append((n, n, part_icon(parts[n].path)))
                wm.progress_update(i + 1)
        finally:
            wm.progress_end()
        return wm.invoke_popup(self, width=360)

    def draw(self, context):
        current = self._current(context)
        col = self.layout.column(align=True)
        for value, label, icon in _part_rows:
            split = col.split(factor=0.22, align=True)
            if icon:
                split.template_icon(icon_value=icon, scale=PART_ICON_SCALE)
            else:
                split.label(text="")
            button = split.column(align=True)
            button.scale_y = PART_ICON_SCALE  # 見本と同じ高さのボタン
            op = button.operator("tachie.set_part", text=label, depress=value == current)
            op.prop, op.preset, op.still, op.part = self.prop, self.preset, self.still, value

    def _current(self, context) -> str:
        """今のパーツ（一覧で押されて見える）。"""
        try:
            if self.still:
                category = next(c for c, props in PART_PROPS.items() if self.prop in props)
                value = still_parts(seq_scene(context).sequence_editor.active_strip).get(category, "")
                if isinstance(value, list):
                    index = PART_PROPS[category].index(self.prop)
                    value = value[index] if index < len(value) else ""
                return value
            ch = self._character(context)
            return getattr(find_expression(ch, self.preset) if self.preset else ch, self.prop)
        except (*ERRORS, AttributeError):
            return ""

    def execute(self, context):
        return apply_part(self, context, "" if self.part == NONE else self.part)


def apply_part(op, context, value: str):
    """パーツを当てる（キャラの基本の表情・表情プリセット・選択中の立ち絵のパーツ）。"""
    if op.still:
        return _set_still_part(op, context, value)
    ch = seq_scene(context).tachie.character()
    target = find_expression(ch, op.preset) if op.preset else ch
    setattr(target, op.prop, value)
    return {"FINISHED"}


def _set_still_part(self, context, value: str):
    sed = seq_scene(context).sequence_editor
    strip = sed.active_strip
    if strip is None or not strip.get("tachie_still"):
        return {"CANCELLED"}
    category = next(c for c, props in PART_PROPS.items() if self.prop in props)
    parts = still_parts(strip)
    if category in SLOT_CATEGORIES:  # 他・後はスロットごと（"" のスロットは表情のまま）
        index = PART_PROPS[category].index(self.prop)
        slots = list(parts.get(category) or [])
        slots += [""] * (index + 1 - len(slots))
        slots[index] = value
        parts[category] = slots
    else:
        parts[category] = value
    set_still_parts(strip, parts)
    try:
        sed.active_strip = remake_still(context, strip)
        refresh_motion_now(seq_scene(context))
    except ERRORS as e:
        self.report({"ERROR"}, str(e))
        return {"CANCELLED"}
    return {"FINISHED"}


class TACHIE_OT_set_part(bpy.types.Operator):
    """このパーツにする"""

    bl_idname = "tachie.set_part"
    bl_label = "パーツを選ぶ"
    bl_options = {"UNDO", "INTERNAL"}

    prop: bpy.props.StringProperty(options={"SKIP_SAVE"})
    preset: bpy.props.StringProperty(options={"SKIP_SAVE"})
    still: bpy.props.BoolProperty(options={"SKIP_SAVE"})
    part: bpy.props.StringProperty(options={"SKIP_SAVE"}, description="空なら（なし）・（既定）・（表情のまま）")

    def execute(self, context):
        return apply_part(self, context, self.part)


PART_ICON_SCALE = 2.4  # パーツを選ぶ一覧の見本の大きさ（UI の単位）
PART_ICON_SIZE = 96  # 見本の画像のピクセル
_part_rows: list = []
_part_previews = None


def part_icon(path: Path) -> int:
    """パーツの見本のアイコン（作った見本はアドオンのユーザーフォルダの thumbs/ に残す）。"""
    global _part_previews
    from bpy.utils import previews

    from .tachie_studio.tachie.compose import part_thumbnail

    if _part_previews is None:
        _part_previews = previews.new()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return 0
    key = hashlib.sha1(f"{path}|{stamp}|{PART_ICON_SIZE}".encode("utf-8")).hexdigest()[:16]
    if key in _part_previews:
        return _part_previews[key].icon_id
    thumb = Path(bpy.utils.extension_path_user(__package__, path="thumbs", create=True)) / f"{key}.png"
    if not thumb.is_file():
        try:
            part_thumbnail(path, PART_ICON_SIZE).save(thumb)
        except Exception as e:  # 壊れた・大きすぎる画像（DecompressionBombError は OSError ではない）でも一覧は出す
            print("パーツの見本を作れません:", path, e)
            return 0
    return _part_previews.load(key, str(thumb), "IMAGE").icon_id


class TACHIE_OT_clear_still_parts(bpy.types.Operator):
    """選択中の立ち絵のパーツを元に戻す（表情プリセットの見た目にする）"""

    bl_idname = "tachie.clear_still_parts"
    bl_label = "パーツを元に戻す"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        sed = seq_scene(context).sequence_editor
        return bool(sed and sed.active_strip and sed.active_strip.get("tachie_parts"))

    def execute(self, context):
        sed = seq_scene(context).sequence_editor
        strip = sed.active_strip
        set_still_parts(strip, None)
        try:
            sed.active_strip = remake_still(context, strip)
            refresh_motion_now(seq_scene(context))
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


# ---- PSD 立ち絵の表情（表示するレイヤー） ----

_psd_option_items = []


def _psd_options(self, context):
    return _psd_option_items


def _psd_target(context, preset: str):
    ch = seq_scene(context).tachie.character()
    return ch, (find_expression(ch, preset) if preset else ch)


def psd_layer_state(ch, preset: str = ""):
    """(PsdDocument, 基本の表情の表示状態, この表情（プリセットなら重ねたもの）の表示状態)。"""
    from .tachie_studio.tachie.psd import apply_layers

    doc = load_material(ch.folder).psd
    base = item_layers(ch)
    layers = {**base, **(item_layers(find_expression(ch, preset)) if preset else {})}
    return doc, apply_layers(doc, base), apply_layers(doc, layers)


class TACHIE_OT_psd_choose(bpy.types.Operator):
    """PSD の * のフォルダから表示するものを 1 つ選ぶ"""

    bl_idname = "tachie.psd_choose"
    bl_label = "レイヤーを選ぶ"
    bl_property = "option"
    bl_options = {"UNDO"}

    group: bpy.props.StringProperty(description="* の選択肢を持つフォルダのパス")
    preset: bpy.props.StringProperty(description="空ならキャラクターの基本の表情、指定するとその表情プリセット")
    option: bpy.props.EnumProperty(items=_psd_options)

    def invoke(self, context, event):
        global _psd_option_items
        try:
            ch, _ = _psd_target(context, self.preset)
            doc = load_material(ch.folder).psd
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        group = doc.nodes().get(self.group)
        if group is None:
            return {"CANCELLED"}
        empty = [(NONE, "（基本の表情のまま）", "")] if self.preset else []
        _psd_option_items = empty + [(c.path(), c.label, "") for c in group.children if c.radio]
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        ch, target = _psd_target(context, self.preset)
        doc = load_material(ch.folder).psd
        group = doc.nodes().get(self.group)
        layers = item_layers(target)
        for c in group.children if group else []:
            if c.radio:
                layers.pop(c.path(), None)
        if self.option != NONE:
            layers[self.option] = True
        set_item_layers(target, layers)
        return {"FINISHED"}


class TACHIE_OT_psd_toggle(bpy.types.Operator):
    """PSD のレイヤー（フォルダ）の表示・非表示を切り替える。プリセットでは「基本のまま」にも戻せる"""

    bl_idname = "tachie.psd_toggle"
    bl_label = "表示を切り替える"
    bl_options = {"UNDO"}

    path: bpy.props.StringProperty()
    preset: bpy.props.StringProperty()
    mode: bpy.props.EnumProperty(items=[("TOGGLE", "切り替え", ""), ("RESET", "基本のまま", "")])

    def execute(self, context):
        from .tachie_studio.tachie.psd import is_on

        ch, target = _psd_target(context, self.preset)
        layers = item_layers(target)
        if self.mode == "RESET":
            layers.pop(self.path, None)
        else:
            doc, _, state = psd_layer_state(ch, self.preset)
            node = doc.nodes().get(self.path)
            if node is None:
                return {"CANCELLED"}
            layers[self.path] = not is_on(node, state)
        set_item_layers(target, layers)
        return {"FINISHED"}


def _fonts(self, context):
    return _font_items


class TACHIE_OT_pick_font(bpy.types.Operator):
    """インストールされているフォントから字幕のフォントを選ぶ"""

    bl_idname = "tachie.pick_font"
    bl_label = "フォントを選ぶ"
    bl_property = "font"
    bl_options = {"UNDO"}

    font: bpy.props.EnumProperty(items=_fonts)

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def invoke(self, context, event):
        global _font_items
        fonts = system_fonts()
        _font_paths.clear()
        _font_paths.update({str(i): path for i, (_, path) in enumerate(fonts)})
        _font_items = [(str(i), name, path) for i, (name, path) in enumerate(fonts)]
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        seq_scene(context).tachie.character().font_path = _font_paths[self.font]
        return {"FINISHED"}


class TACHIE_OT_reload_character(bpy.types.Operator):
    """素材フォルダを読み直し、合成済みの画像を作り直す（パーツを追加・変更したとき）"""

    bl_idname = "tachie.reload_character"
    bl_label = "素材を読み直す"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        _characters.clear()
        _caches.clear()
        load_image.cache_clear()
        shutil.rmtree(output_dir(context) / "cache", ignore_errors=True)
        # 立ち絵はキャッシュの画像を直接指しているので作り直す
        sed = seq_scene(context).sequence_editor
        try:
            if sed is not None:
                for s in [s for s in sed.strips if s.get("tachie_still")]:
                    remake_still(context, s)
            refresh_motion_now(seq_scene(context))
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class TACHIE_OT_import_characters(bpy.types.Operator, ImportHelper):
    """キャラクター設定を TOML から読み込む（同じ名前のキャラクターは上書き）"""

    bl_idname = "tachie.import_characters"
    bl_label = "キャラクター設定を読み込む"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".toml"
    filter_glob: bpy.props.StringProperty(default="*.toml", options={"HIDDEN"})

    def execute(self, context):
        settings = seq_scene(context).tachie
        try:
            configs = load_characters(self.filepath)
        except (OSError, ValueError) as e:
            self.report({"ERROR"}, f"読み込めませんでした: {e}")
            return {"CANCELLED"}
        names_by_engine: dict[str, dict] = {}
        for config in configs.values():
            existing = next((c for c in settings.characters if c.name == config.name), None)
            ch = existing or settings.characters.add()
            apply_config(ch, config)
            url = engine_url(context, ch)
            if url not in names_by_engine:
                try:
                    names_by_engine[url] = dict(client(context, ch, timeout=3.0).styles())
                except (VoicevoxError, OSError):
                    names_by_engine[url] = {}  # エンジンが動いていなければ話者名は空のまま（ID で表示）
            ch.speaker_name = names_by_engine[url].get(config.speaker, "")
        lanes.ensure_layout(seq_scene(context))
        self.report({"INFO"}, f"{len(configs)} 人のキャラクター設定を読み込みました")
        return {"FINISHED"}


def _is_psd_character(ch) -> bool:
    return ch is not None and ch.folder.lower().endswith(".psd")


class TACHIE_OT_import_presets(bpy.types.Operator, ImportHelper):
    """YMM4 の表情プリセット（素材フォルダの preset.ini、PSD 立ち絵は <PSD 名>-ymm.json）を、選択中のキャラクターの
    表情プリセットに読み込む（同じ名前は上書き）"""

    bl_idname = "tachie.import_presets"
    bl_label = "YMM4 の表情プリセットを読み込む"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: bpy.props.StringProperty(default="*.ini;*.json", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return ch is not None and bool(ch.folder)

    def invoke(self, context, event):
        ch = seq_scene(context).tachie.character()
        folder = bpy.path.abspath(ch.folder)
        self.filepath = str(animation_config_path(folder) if _is_psd_character(ch) else Path(folder) / PRESET_FILE)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        folder = bpy.path.abspath(ch.folder)
        try:
            if _is_psd_character(ch):
                # プリセットは基本の表情に重ねるので、基本の表情との違いにする（YMM4 のプリセットは表示全部を持つ）
                layers, warnings = load_ymm_presets(folder, load_material(ch.folder).psd, self.filepath,
                                                    base=item_layers(ch))
                presets = {name: {LAYERS_KEY: value} for name, value in layers.items()}
            else:
                presets, warnings = load_presets(folder, self.filepath)
        except (ValueError, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not presets:
            self.report({"WARNING"}, f"{self.filepath} に表情プリセットがありません")
            return {"CANCELLED"}
        for name, parts in presets.items():
            item = next((e for e in ch.expressions if e.name == name), None) or ch.expressions.add()
            item.name = name
            for props in PART_PROPS.values():
                for prop in props:
                    setattr(item, prop, "")
            set_item_layers(item, parts.get(LAYERS_KEY, {}))
            for category, value in parts.items():
                if category == LAYERS_KEY:
                    continue
                for prop, v in zip(PART_PROPS[category], [value] if isinstance(value, str) else value):
                    setattr(item, prop, v)
        message = f"{len(presets)} 個の表情プリセットを読み込みました"
        if warnings:
            message += f"（{len(warnings)} 件飛ばしました: {' / '.join(warnings[:3])}{' …' if len(warnings) > 3 else ''}）"
            print("\n".join(warnings))
        self.report({"WARNING"} if warnings else {"INFO"}, message)
        return {"FINISHED"}


class TACHIE_OT_export_ymm_presets(bpy.types.Operator):
    """PSD 立ち絵の表情プリセットを、YMM4 の表情プリセットとして <PSD 名>-ymm.json に書き出す
    （同じ名前のプリセットは置き換え、他のプリセットと口パク・まばたきの設定は残す）"""

    bl_idname = "tachie.export_ymm_presets"
    bl_label = "YMM4 の表情プリセットとして書き出す（PSD）"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return _is_psd_character(ch) and len(ch.expressions) > 0

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        try:
            doc = load_material(ch.folder).psd
            folder = bpy.path.abspath(ch.folder)
            ours = [ymm_preset(doc, e.name, apply_layers(doc, {**item_layers(ch), **item_layers(e)}))
                    for e in ch.expressions]
            names = {p["Name"] for p in ours}
            kept = [p for p in read_animation_config(folder).get("Presets") or [] if p.get("Name") not in names]
            path = write_animation_config(animation_config_path(folder), {"Presets": kept + ours})
        except (*ERRORS, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{len(ours)} 個の表情プリセットを {path.name} に書き出しました")
        return {"FINISHED"}


class TACHIE_OT_export_characters(bpy.types.Operator, ExportHelper):
    """キャラクター設定を TOML に書き出す（他のプロジェクトや python -m tachie_studio.pipeline で使える）"""

    bl_idname = "tachie.export_characters"
    bl_label = "キャラクター設定を書き出す"

    filename_ext = ".toml"
    filter_glob: bpy.props.StringProperty(default="*.toml", options={"HIDDEN"})

    def execute(self, context):
        configs = {ch.name: config_of(ch) for ch in seq_scene(context).tachie.characters}
        path = Path(self.filepath)
        path.write_text(dump_characters(configs, base=path.parent), encoding="utf-8")
        self.report({"INFO"}, f"{path} に書き出しました")
        return {"FINISHED"}


def subtitle_cues(scene) -> list:
    """置いてあるセリフの字幕（ミュートしたもの・書き出す範囲の外は除く）。時刻はシーンの開始フレームを 0 秒とする。

    テキストは置いたときのもの（パネルで編集して再生成していないテキストや「{発音}」は画面の字幕に無い）。
    """
    from .tachie_studio.srt import Cue

    sed = scene.sequence_editor
    if sed is None:
        return []
    fps = scene_fps(scene)
    first, last = scene.frame_start, scene.frame_end + 1
    cues = []
    for s in sed.strips:
        if (s.type != "TEXT" or not s.get("tachie_id") or s.get("tachie_role") == "name" or s.mute
                or sed.channels[s.channel].mute):
            continue
        start, end = max(s.left_handle, first), min(s.right_handle, last)
        if end <= start:
            continue
        text = breaks(s.get("tachie_placed_text", s.get("tachie_text", s.text)))
        cues.append(Cue((start - first) / fps, (end - first) / fps, text, s.get("tachie_character", "")))
    return cues


class TACHIE_OT_add_headline(bpy.types.Operator):
    """見出しテロップ（話題の切り替えなどに出す大きめの文字）を再生位置に置く。スタイルはシーンで共通"""

    bl_idname = "tachie.add_headline"
    bl_label = "見出しを置く"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        text = settings.headline_text.strip()
        if not text:
            self.report({"ERROR"}, "見出しの文字を入力してください")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        sed = scene.sequence_editor or scene.sequence_editor_create()
        lanes.ensure_layout(scene)  # 見出しのレーン（headline_channel）
        start = scene.frame_current
        frames = max(1, round(settings.headline_seconds * scene_fps(scene)))
        if _overlaps(sed, settings.headline_channel, start, start + frames):
            self.report({"ERROR"}, f"チャンネル {settings.headline_channel} の {start}〜{start + frames} フレームに"
                                   "他のストリップがあります")
            return {"CANCELLED"}
        strip = sed.strips.new_effect(name=f"見出し {text[:16]}", type="TEXT", channel=settings.headline_channel,
                                      frame_start=start, length=frames)
        strip["tachie_headline"] = True
        strip.text = text
        apply_headline(strip, settings)
        if strip.right_handle - 1 > scene.frame_end:
            scene.frame_end = strip.right_handle - 1
        for s in sed.strips_all:
            s.select = False
        strip.select = True
        sed.active_strip = strip
        return {"FINISHED"}


def _mark_ducking_applied(scene) -> None:
    """ダッキングだけを反映済みにする（表情・動きの反映待ちは残す）。"""
    from .ducking import ducking_signature

    faces, motion, _ = _applied_signatures.get(scene.name, ("", "", ""))
    _applied_signatures[scene.name] = (faces, motion, ducking_signature(scene))


class TACHIE_OT_set_bgm(bpy.types.Operator):
    """選択中の音声ストリップを BGM にする（セリフの音声が鳴っている間だけ音量を下げる）。今の音量が BGM の音量になる"""

    bl_idname = "tachie.set_bgm"
    bl_label = "選択中の音声を BGM にする"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from .ducking import refresh_ducking, set_bgm

        scene = seq_scene(context)
        sed = scene.sequence_editor
        sounds = [s for s in sed.strips if s.select and s.type == "SOUND" and not s.get("tachie_id")] \
            if sed else []
        if not sounds:
            self.report({"ERROR"}, "BGM にする音声ストリップを選んでください（セリフの音声は除く）")
            return {"CANCELLED"}
        for s in sounds:
            set_bgm(s, True)
        refresh_ducking(scene)
        _mark_ducking_applied(scene)
        self.report({"INFO"}, f"{len(sounds)} 本を BGM にしました")
        return {"FINISHED"}


class TACHIE_OT_unset_bgm(bpy.types.Operator):
    """BGM から外す（音量を BGM の音量に戻し、キーフレームを消す）"""

    bl_idname = "tachie.unset_bgm"
    bl_label = "BGM から外す"
    bl_options = {"REGISTER", "UNDO"}

    strip: bpy.props.StringProperty(name="ストリップ")

    def execute(self, context):
        from .ducking import set_bgm

        scene = seq_scene(context)
        sed = scene.sequence_editor
        strip = sed.strips.get(self.strip) if sed else None
        if strip is None or not strip.get("tachie_bgm"):
            self.report({"ERROR"}, f"BGM「{self.strip}」が見つかりません")
            return {"CANCELLED"}
        set_bgm(strip, False)
        _mark_ducking_applied(scene)
        return {"FINISHED"}


SOUND_GLOB = "*.wav;*.mp3;*.ogg;*.flac;*.m4a;*.aac;*.opus"


def _sound_error(path: Path) -> str | None:
    """音声として読めなければその理由。読めないファイルでも new_sound は 1 フレームの無音のストリップを作るので先に確かめる。"""
    if not path.is_file():
        return f"ファイルがありません: {path}"
    try:
        import aud

        aud.Sound(str(path)).length
    except Exception as e:
        return f"音声として読めません: {path.name}（{e}）"
    return None


class TACHIE_OT_register_sound_effect(bpy.types.Operator, ImportHelper):
    """効果音のファイルをアドオン設定に登録する（どの .blend でも使える）"""

    bl_idname = "tachie.register_sound_effect"
    bl_label = "効果音を登録"
    bl_options = {"REGISTER"}

    filter_glob: bpy.props.StringProperty(default=SOUND_GLOB, options={"HIDDEN"})

    def execute(self, context):
        path = Path(bpy.path.abspath(self.filepath))
        if error := _sound_error(path):
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        effects = prefs(context).sound_effects
        se = effects.add()
        se.name, se.filepath = path.stem, str(path)
        context.preferences.is_dirty = True
        self.report({"INFO"}, f"効果音「{se.name}」を登録しました")
        return {"FINISHED"}


class TACHIE_OT_remove_sound_effect(bpy.types.Operator):
    """効果音の登録を消す（置いてある効果音はそのまま）"""

    bl_idname = "tachie.remove_sound_effect"
    bl_label = "効果音の登録を消す"
    bl_options = {"REGISTER"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        effects = prefs(context).sound_effects
        if not 0 <= self.index < len(effects):
            return {"CANCELLED"}
        effects.remove(self.index)
        context.preferences.is_dirty = True
        return {"FINISHED"}


def _se_channel(sed, settings, start: int, end: int) -> int:
    """効果音を置くチャンネル: レーンより下（1 番から上）の空き。無ければレーンより上。"""
    return lanes.sound_channel(settings.id_data, start, end)


class TACHIE_OT_add_sound_effect(bpy.types.Operator):
    """登録した効果音を、再生位置か、選んでいるセリフそれぞれの頭に置く"""

    bl_idname = "tachie.add_sound_effect"
    bl_label = "効果音を置く"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        effects = prefs(context).sound_effects
        if not 0 <= self.index < len(effects):
            self.report({"ERROR"}, "効果音が登録されていません")
            return {"CANCELLED"}
        se = effects[self.index]
        path = Path(bpy.path.abspath(se.filepath))
        if error := _sound_error(path):
            self.report({"ERROR"}, f"効果音「{se.name}」: {error}")
            return {"CANCELLED"}
        scene = seq_scene(context)
        settings = scene.tachie
        ensure_sequencer_scene(context)
        sed = scene.sequence_editor or scene.sequence_editor_create()
        if settings.se_at_lines:
            ids = selected_targets(sed)[0]
            starts = [min(s.left_handle for s in line_strips(sed, i)) for i in ids]
            if not starts:
                self.report({"ERROR"}, "効果音を置くセリフを選んでください")
                return {"CANCELLED"}
        else:
            starts = [scene.frame_current]
        placed = []
        for start in starts:
            strip = sed.strips.new_sound(name=f"効果音 {se.name}", filepath=_path_for_blend(path),
                                         channel=128, frame_start=start)
            strip.channel = _se_channel(sed, settings, strip.left_handle, strip.right_handle)
            strip.volume = se.volume
            strip["tachie_se"] = se.name
            placed.append(strip)
        for s in sed.strips_all:
            s.select = False
        for s in placed:
            s.select = True
        sed.active_strip = placed[-1]
        scene.frame_end = max(scene.frame_end, max(s.right_handle for s in placed) - 1)
        self.report({"INFO"}, f"効果音「{se.name}」を {len(placed)} か所に置きました")
        return {"FINISHED"}


class TACHIE_OT_export_subtitles(bpy.types.Operator, ExportHelper):
    """置いてあるセリフの字幕を SRT / WebVTT で書き出す（YouTube などの字幕用）"""

    bl_idname = "tachie.export_subtitles"
    bl_label = "字幕を書き出す"

    filename_ext = ".srt"
    filter_glob: bpy.props.StringProperty(default="*.srt;*.vtt", options={"HIDDEN"})
    format: bpy.props.EnumProperty(name="形式", items=[("SRT", "SRT", "YouTube などで使える一般的な形式"),
                                                       ("VTT", "WebVTT", "Web 用の字幕形式")])
    with_speaker: bpy.props.BoolProperty(name="キャラ名を付ける", default=False,
                                         description="「キャラ名: セリフ」の形で書く")

    def check(self, context):
        # 形式に合わせて拡張子を変える
        ext = ".vtt" if self.format == "VTT" else ".srt"
        path = Path(self.filepath)
        if path.suffix.lower() != ext and path.name:
            self.filepath = str(path.with_suffix(ext))
            return True
        return False

    def execute(self, context):
        from .tachie_studio.srt import to_srt, to_vtt

        self.check(context)
        cues = subtitle_cues(seq_scene(context))
        if not cues:
            self.report({"WARNING"}, "書き出す字幕がありません")
            return {"CANCELLED"}
        text = (to_vtt if self.format == "VTT" else to_srt)(cues, self.with_speaker)
        Path(self.filepath).write_text(text, encoding="utf-8")
        self.report({"INFO"}, f"{len(cues)} 件の字幕を {self.filepath} に書き出しました")
        return {"FINISHED"}


# 動画の書き出し設定: 名前 → (表示名, 幅, 高さ, fps)
RENDER_PRESETS = {
    "FHD30": ("1920×1080 / 30fps（YouTube 標準）", 1920, 1080, 30),
    "FHD60": ("1920×1080 / 60fps", 1920, 1080, 60),
    "HD30": ("1280×720 / 30fps（軽い）", 1280, 720, 30),
    "UHD30": ("3840×2160 / 30fps（4K）", 3840, 2160, 30),
    "SHORT30": ("1080×1920 / 30fps（縦長・ショート）", 1080, 1920, 30),
}


def apply_render_preset(scene, preset: str) -> bool:
    """解像度・fps・MP4（H.264 / AAC）を設定する。解像度か fps が変わったら True。"""
    _, w, h, fps = RENDER_PRESETS[preset]
    r = scene.render
    changed = (r.resolution_x, r.resolution_y, r.resolution_percentage) != (w, h, 100) or scene_fps(scene) != fps
    r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
    r.fps, r.fps_base = fps, 1.0
    im = r.image_settings
    if hasattr(im, "media_type"):  # Blender 5: 動画は media_type を VIDEO にしてから
        im.media_type = "VIDEO"
    im.file_format = "FFMPEG"
    f = r.ffmpeg
    f.format, f.codec = "MPEG4", "H264"
    f.constant_rate_factor, f.ffmpeg_preset = "HIGH", "GOOD"
    f.audio_codec, f.audio_channels, f.audio_mixrate, f.audio_bitrate = "AAC", "STEREO", 48000, 192
    r.use_sequencer = True
    if bpy.data.filepath:
        r.filepath = f"//render/{Path(bpy.data.filepath).stem}_"
    return changed


class TACHIE_OT_render_preset(bpy.types.Operator):
    """動画の書き出し設定（解像度・フレームレート・MP4 / H.264 / AAC）をまとめて設定する"""

    bl_idname = "tachie.render_preset"
    bl_label = "書き出し設定"
    bl_options = {"REGISTER", "UNDO"}

    preset: bpy.props.EnumProperty(name="書き出し設定", items=[(k, v[0], "") for k, v in RENDER_PRESETS.items()])

    def execute(self, context):
        scene = seq_scene(context)
        changed = apply_render_preset(scene, self.preset)
        sed = scene.sequence_editor
        placed = sed is not None and any(s.get("tachie_id") or s.get("tachie_still") for s in sed.strips)
        if changed and placed:
            self.report({"WARNING"}, "解像度かフレームレートが変わりました。置いてあるセリフは「全セリフを再生成」で合わせてください")
        else:
            self.report({"INFO"}, f"書き出し設定: {RENDER_PRESETS[self.preset][0]}")
        return {"FINISHED"}


class TACHIE_OT_import_script(bpy.types.Operator, ImportHelper):
    """台本を読み、セリフを再生位置から順に並べる（1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」。
    YMM4 の台本（キャラ名「セリフ」、CSV・TSV）も読める）"""

    bl_idname = "tachie.import_script"
    bl_label = "台本を流し込む"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".txt"
    filter_glob: bpy.props.StringProperty(default="*.txt;*.csv;*.tsv", options={"HIDDEN"})
    use_text: bpy.props.BoolProperty(name="テキストエディタの台本を使う", default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        if self.use_text:
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        if self.use_text and settings.script is None:
            self.report({"ERROR"}, "台本のテキストを選んでください")
            return {"CANCELLED"}
        configs = {ch.name: config_of(ch) for ch in settings.characters}
        try:
            if self.use_text:
                # テキストの名前が .csv / .tsv なら（ファイルを開いたときなど）その形で読む
                lines = parse_script(settings.script.as_string(), configs, fmt=script_format(settings.script.name))
            else:
                lines = load_script(self.filepath, configs)
        except OSError as e:
            self.report({"ERROR"}, f"読み込めませんでした: {e}")
            return {"CANCELLED"}
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not lines:
            self.report({"ERROR"}, "セリフがありません")
            return {"CANCELLED"}

        ensure_sequencer_scene(context)
        gap = round(settings.line_gap * scene_fps(scene))
        cursor = scene.frame_current
        placed = 0
        wm = context.window_manager
        wm.progress_begin(0, len(lines))
        try:
            for line in lines:
                ch = find_character(settings, line.character)
                layout = line_layout(ch)
                clip, lines_dir = render_clip(context, ch, line.text, line.expression, kana=line.kana)
                channels = line_channels(context, ch, cursor, clip.frames, layout=layout)
                strips = place_strips(context, ch, clip, lines_dir, line.text, cursor, channels=channels,
                                      layout=layout)
                connect(context, strips)
                cursor = line_end(strips) + gap
                placed += 1
                wm.progress_update(placed)
        except ERRORS as e:
            self.report({"ERROR"}, f"{placed + 1} 個目のセリフ（{line.character}: {line.text}）で止まりました: {e}")
        finally:
            wm.progress_end()
            if placed:
                refresh_faces(context)
        if placed and settings.advance:
            scene.frame_current = cursor
        if placed == len(lines):
            self.report({"INFO"}, f"{placed} 個のセリフを並べました")
        return {"FINISHED"} if placed else {"CANCELLED"}


def _expressions(self, context):
    return _expression_items


class TACHIE_OT_pick_expression(bpy.types.Operator):
    """表情プリセットを選ぶ"""

    bl_idname = "tachie.pick_expression"
    bl_label = "表情を選ぶ"
    bl_property = "expression"
    bl_options = {"REGISTER", "UNDO"}

    target: bpy.props.EnumProperty(items=[("ADD", "追加するセリフ", ""), ("LINE", "選択中のセリフ", ""),
                                          ("STILL", "選択中の立ち絵", ""), ("FACE", "選択中の表情ストリップ", "")])
    expression: bpy.props.EnumProperty(items=_expressions)

    def _character(self, context):
        settings = seq_scene(context).tachie
        if self.target in ("LINE", "STILL", "FACE"):
            sed = seq_scene(context).sequence_editor
            key = "tachie_face" if self.target == "FACE" else "tachie_character"
            return find_character(settings, sed.active_strip[key])
        return settings.character()

    def invoke(self, context, event):
        global _expression_items
        try:
            ch = self._character(context)
        except (ValueError, KeyError, AttributeError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if ch is None:
            return {"CANCELLED"}
        # セリフの "" は重なる立ち絵の表情のまま
        _expression_items = [(NONE, "（立ち絵のまま）" if self.target == "LINE" else "（基本）", "")] + [
            (e.name, e.name, "") for e in ch.expressions]
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        value = "" if self.expression == NONE else self.expression
        if self.target == "ADD":
            seq_scene(context).tachie.expression = value
            return {"FINISHED"}
        sed = seq_scene(context).sequence_editor
        if self.target == "FACE":
            face = sed.active_strip
            face["tachie_expression"] = value
            face.name = _face_name(face["tachie_face"], value)
            return bpy.ops.tachie.apply_faces()
        if self.target == "STILL":
            try:
                strip = remake_still(context, sed.active_strip, value)
            except ERRORS as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            sed.active_strip = strip
            return {"FINISHED"}
        # 選択中のセリフの表情（音声は合成し直さない。重なる立ち絵をその間だけその表情にする）
        try:
            strips = set_line_expression(context, active_line_id(context), value)
            refresh_faces(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        seq_scene(context).sequence_editor.active_strip = line_focus(strips)
        connect(context, strips)
        return {"FINISHED"}


class TACHIE_OT_expression_add(bpy.types.Operator):
    """表情プリセットを追加する（変えたいパーツだけ指定する）"""

    bl_idname = "tachie.expression_add"
    bl_label = "表情を追加"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        names = {e.name for e in ch.expressions}
        item = ch.expressions.add()
        item.name = next(f"表情{i}" for i in range(1, len(names) + 2) if f"表情{i}" not in names)
        ch.active_expression = len(ch.expressions) - 1
        return {"FINISHED"}


class TACHIE_OT_expression_remove(bpy.types.Operator):
    """表情プリセットを削除する（このプリセットを使ったセリフは再生成できなくなる）"""

    bl_idname = "tachie.expression_remove"
    bl_label = "表情を削除"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return ch is not None and ch.expression() is not None

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        ch.expressions.remove(ch.active_expression)
        ch.active_expression = max(0, min(ch.active_expression, len(ch.expressions) - 1))
        return {"FINISHED"}


_classes = (
    TACHIE_OT_add_line,
    TACHIE_OT_add_still,
    TACHIE_OT_remake_still,
    TACHIE_OT_accent_join,
    TACHIE_OT_accent_split,
    TACHIE_OT_flip_still,
    TACHIE_OT_clear_still_parts,
    TACHIE_OT_set_part,
    TACHIE_OT_import_script,
    TACHIE_OT_pick_expression,
    TACHIE_OT_expression_add,
    TACHIE_OT_expression_remove,
    TACHIE_OT_regenerate,
    TACHIE_OT_regenerate_all,
    TACHIE_OT_character_add,
    TACHIE_OT_character_add_voice,
    TACHIE_OT_change_material,
    TACHIE_OT_export_ymm_presets,
    TACHIE_OT_pick_line_layout,
    TACHIE_OT_pick_line_character,
    TACHIE_OT_reset_line_voice,
    TACHIE_OT_reset_kana,
    TACHIE_OT_add_face,
    TACHIE_OT_batch_edit,
    TACHIE_OT_apply_faces,
    TACHIE_OT_preview_line,
    TACHIE_OT_edit_accent,
    TACHIE_OT_preview_accent,
    TACHIE_OT_reload_accent,
    TACHIE_OT_clear_accent,
    TACHIE_OT_dict_refresh,
    TACHIE_OT_dict_add,
    TACHIE_OT_dict_remove,
    TACHIE_OT_save_defaults,
    TACHIE_OT_remove_defaults,
    TACHIE_OT_character_remove,
    TACHIE_OT_character_move,
    TACHIE_OT_pick_speaker,
    TACHIE_OT_pick_part,
    TACHIE_OT_psd_choose,
    TACHIE_OT_psd_toggle,
    TACHIE_OT_pick_font,
    TACHIE_OT_reload_character,
    TACHIE_OT_import_characters,
    TACHIE_OT_import_presets,
    TACHIE_OT_export_subtitles,
    TACHIE_OT_add_headline,
    TACHIE_OT_set_bgm,
    TACHIE_OT_register_sound_effect,
    TACHIE_OT_remove_sound_effect,
    TACHIE_OT_add_sound_effect,
    TACHIE_OT_unset_bgm,
    TACHIE_OT_render_preset,
    TACHIE_OT_export_characters,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.handlers.save_pre.append(_move_temp_output)


def unregister():
    global _part_previews
    if _part_previews is not None:
        from bpy.utils import previews

        previews.remove(_part_previews)
        _part_previews = None
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _move_temp_output in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_move_temp_output)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
