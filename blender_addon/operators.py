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
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import channels as lanes
from .properties import AUDIO_PROPS, BAKED_AUDIO, ENGINE_PRESETS, LINE_LAYOUTS, apply_headline, apply_name, PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, apply_placement, apply_subtitle, place_text, system_fonts, text_offset
from .tachie_studio.pipeline import (CharacterConfig, FrameCache, Line, idle_frames, load_characters, load_script,
                                     parse_script, render_line)
from .tachie_studio.pipeline.render import LineMotion, blink_segment_frames, link_frames, segment_frames
from .tachie_studio.pipeline.script import script_format, split_kana
from .tachie_studio.pipeline.config import (LAYERS_KEY, MATERIAL_CONFIG, VOICE_DEFAULTS, VOICE_KEYS, dump_characters, dump_raw_table, dump_table,
                                            load_material_defaults, load_material_table, merge_tables,
                                            parse_characters)
from .tachie_studio.tachie import PRESET_FILE, Expression, load_character, load_presets
from .tachie_studio.tachie.compose import load_image
from .tachie_studio.tachie.psd import (animation_config_path, apply_layers, load_ymm_presets, read_animation_config,
                                       write_animation_config, ymm_preset)
from .tachie_studio.voicevox import VoicevoxClient, VoicevoxError

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


def client(context, ch=None, timeout: float = 60.0) -> VoicevoxClient:
    return VoicevoxClient(engine_url(context, ch), timeout=timeout)


def output_dir(context) -> Path:
    custom = prefs(context).output_dir
    if custom:
        return Path(bpy.path.abspath(custom))
    if bpy.data.filepath:
        return Path(bpy.path.abspath("//tachie"))
    return Path(tempfile.gettempdir()) / "blender_tachie"


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
        setattr(ch, prop, layout.get(key, ch.bl_rna.properties[prop].default))
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


def character_cache(context, ch, preset: str | None = None) -> FrameCache:
    """キャラクターの表情・大きさでの「目 × 口」合成キャッシュ。"""
    character = load_material(ch.folder)
    scale = seq_scene(context).render.resolution_y * ch.height / character.height
    return _cache(character, expression_of(ch, preset), output_dir(context) / "cache", scale)


def line_layout(ch, layout: str | None = None) -> str:
    """セリフの形（FULL / SUBTITLE / AUDIO）。None ならキャラの既定。素材の無いキャラは立ち絵を出せない。"""
    layout = layout or ch.line_layout
    return "SUBTITLE" if layout == "FULL" and not ch.folder else layout


def clip_job(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
             voice: dict | None = None, kana: str | None = None, accent: list | None = None,
             accent_kana: str | None = None, audio: dict | None = None, layout: str = "FULL",
             wav: bytes | None = None):
    """セリフの合成の準備（Blender に触る部分）だけをして、合成して書き出す関数を返す。返した関数は
    (RenderedClip, 書き出し先フォルダ) を返し、Blender に触らないので別スレッドで呼べる（音声合成を待つ間、画面を固めない）。

    `speaker`（スタイル）と `voice`（audio_query の値）はセリフごとの値。None ならキャラクターの既定値。
    `kana` は発音（AquesTalk 風記法）。None や空なら VOICEVOX が自動で付けた読み。
    `accent` は抑揚を編集した accent_phrases（kana より優先）。`accent_kana` は編集したときの発音で、
    今の発音（`kana` か自動の読み）と違えば `accent` は使わない（ユーザー辞書で自動の読みが変わったときなど）。
    `audio` はフェードと余韻（None ならキャラクターの既定値）。`layout` が FULL 以外なら立ち絵の連番は書かない。
    `wav` は合成済みの音声（YMM4 が保存した音声）。あれば VOICEVOX では合成しない（口パクの計算に audio_query は使う）。
    """
    audio = audio if audio is not None else character_audio(ch)
    if not ch.folder:
        preset = None  # 立ち絵の無いキャラに表情は無い（立ち絵のあるキャラで選んだ表情が残っていても使わない）
    if layout == "FULL":
        cache = character_cache(context, ch, preset)
        config = CharacterConfig(name=ch.name, folder=cache.character.path, speaker=ch.speaker,
                                 expression=cache.expression, mouth=ch.mouth)
    else:
        cache = None
        config = CharacterConfig(name=ch.name, folder=None, speaker=ch.speaker,
                                 expression=expression_of(ch, preset), mouth=ch.mouth)
    clip_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    lines_dir = output_dir(context) / "lines"
    line = Line(0, ch.name, text, preset or None, speaker, voice if voice is not None else character_voice(ch),
                kana or None, accent or None, accent_kana=accent_kana, fade_in=audio.get("fade_in", 0.0),
                fade_out=audio.get("fade_out", 0.0), after=audio.get("after", 0.0), wav=wav)
    engine, fps, character = client(context, ch), scene_fps(seq_scene(context)), cache and cache.character

    def run():
        clip = render_line(engine, config, character, line, lines_dir, fps=fps, cache=cache, stem=clip_id)
        return clip, lines_dir
    return run


def render_clip(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
                voice: dict | None = None, kana: str | None = None, accent: list | None = None,
                accent_kana: str | None = None, audio: dict | None = None, layout: str = "FULL"):
    """セリフを合成して書き出す。(RenderedClip, 書き出し先フォルダ) を返す。引数は `clip_job` と同じ。"""
    return clip_job(context, ch, text, preset, speaker, voice, kana, accent, accent_kana, audio, layout)()


def line_channels(context, ch, start: int, frames: int, ignore=(), layout: str = "FULL",
                  prefer: dict | None = None) -> dict:
    """セリフを start に置くチャンネル {"audio", "image", "subtitle", "name"}（形 `layout` で置くものだけ）。

    どれもそのキャラのレーン（`channels`）。立ち絵は同じキャラの立ち絵のあるセリフと重ねられない。音声・字幕・
    キャラ名はレーンが埋まっていれば（同じキャラの立ち絵の無いセリフが重なるとき）レーンより上の空き。キャラ名は字幕より上。
    `prefer`（役割 → チャンネル。作り直すときの今のチャンネル）があり、そこが空いていればそこ（手で動かした位置を残す）。
    """
    prefer = prefer or {}
    scene = seq_scene(context)
    lanes.ensure_layout(scene)
    sed = scene.sequence_editor
    end = start + frames
    result = {}
    if layout == "FULL":
        image = lanes.lane(scene, "image", ch.name)
        if prefer.get("image") and (sed is None or not _overlaps(sed, prefer["image"], start, end, ignore)):
            image = prefer["image"]
        if image is None:
            raise ValueError(f"{ch.name}には立ち絵のチャンネルがありません（立ち絵の無いキャラクター）")
        if sed is not None and (_overlaps(sed, image, start, end, ignore) or _own_overlaps(sed, ch, start, end, ignore)):
            raise ValueError(f"{ch.name}の立ち絵（チャンネル {image}）の {start}〜{end} フレームに他のストリップが"
                             "あります（同じキャラのセリフは重ねられません）")
        result["image"] = image
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
                 layout: str = "FULL") -> list:
    """書き出したセリフを start に置く。置いたストリップ（音声・立ち絵・字幕・キャラ名のうち形 `layout` で置くもの）を返す。

    セリフごとの声（`voice`、None ならキャラクターの既定値）は音声ストリップの `tachie_voice_*` に持つ。
    `channels` は `line_channels` の結果（None なら設定のチャンネル）。
    """
    scene = seq_scene(context)
    settings = scene.tachie
    if channels is None:  # レーンのまま（重なりは確かめない）
        lanes.ensure_layout(scene)
        lay = lanes.scene_layout(scene)
        channels = {role: lay.channel(role, ch.name) for role in ("audio", "image", "subtitle", "name")}
        channels["name"] = channels["name"] or lanes.free_channel(None, lay, start, start + 1)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    if scene.view_settings.view_transform != "Standard":
        # 3D 用の AgX / Filmic のままだと立ち絵の色が淡くなる
        scene.view_settings.view_transform = "Standard"

    label = f"{ch.name}: " + (text if len(text) <= 16 else text[:15] + "…")
    sound = sed.strips.new_sound(name=f"音声 {label}", filepath=_path_for_blend(lines_dir / clip.wav),
                                 channel=channels["audio"], frame_start=start)
    strips = [sound]

    image = None
    if layout == "FULL":
        frames_dir = lines_dir / clip.frames_dir
        frame_files = sorted(os.listdir(frames_dir))
        image = sed.strips.new_image(name=f"立ち絵 {label}", filepath=_path_for_blend(frames_dir / frame_files[0]),
                                     channel=channels.get("image") or ch.channel, frame_start=start,
                                     fit_method="ORIGINAL")
        for name in frame_files[1:]:
            image.elements.append(name)
        image.blend_type = "ALPHA_OVER"
        set_pose(image, None, ch)
        strips.append(image)

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
    if image is not None:
        image["tachie_frames_dir"] = image.directory  # 表情ストリップが無いときの連番
        apply_faces(context, [image])
    # 発音は音声ストリップに。自動の読みと違えば「編集した発音」として再生成でも使う
    sound["tachie_kana"] = clip.extra.get("kana", "")
    sound["tachie_kana_auto"] = clip.extra.get("kana_auto", "")
    # 抑揚の編集は、そのときのテキスト・発音・スタイルでだけ使う（どれかが変われば取り直す）
    if accent and clip.extra.get("accent"):
        sound["tachie_accent"] = json.dumps(accent, ensure_ascii=False)
        sound["tachie_accent_key"] = json.dumps([text, clip.extra.get("kana", ""), clip.speaker], ensure_ascii=False)
    return strips


def line_image(strips):
    return next((s for s in strips if s.type == "IMAGE"), None)


def line_texts(strips) -> list:
    """字幕とキャラ名のストリップ。"""
    return [s for s in strips if s.type == "TEXT"]


def line_end(strips) -> int:
    """セリフの終わり（余韻を含む。音声だけのセリフは音声ストリップの終わりより後ろ）。"""
    start = min(s.left_handle for s in strips)
    return max(max(s.right_handle for s in strips), start + int(strips[0].get("tachie_frames", 0)))


def line_layout_of(strips) -> str:
    """置いてあるセリフの形（印の無い古いセリフは、立ち絵があれば FULL）。"""
    for s in strips:
        if s.get("tachie_layout"):
            return s["tachie_layout"]
    return "FULL" if line_image(strips) is not None else ("SUBTITLE" if line_texts(strips) else "AUDIO")


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
    if text != sound.get("tachie_placed_text", text):
        return None  # テキストが変わった（自動の読みも変わる）
    if auto is None:
        auto = sound.get("tachie_kana_auto", "")
    if key != [text, kana or auto, speaker]:
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
    if not kana or kana == sound.get("tachie_kana_auto") or text != sound.get("tachie_placed_text", text):
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
    targets = [s for s in sed.strips if s.left_handle >= frame and s not in ignore and not s.get("tachie_idle")]
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
    text, inline_kana = split_kana(text.strip())
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

    if layout is None:
        old_ch = next((c for c in settings.characters if c.name == old[0]["tachie_character"]), None)
        # 立ち絵の無いキャラの形は立ち絵を出せないためのもの。立ち絵のあるキャラに変えたらそのキャラの既定にする
        layout = (None if old_ch is not None and old_ch.name != ch.name and not old_ch.folder
                  else line_layout_of(old))
    layout = line_layout(ch, layout)

    clip, lines_dir = render_clip(context, ch, text, preset, speaker, voice, kana, accent, accent_kana, audio,
                                  layout)
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
        stay = [s for s in sed.strips if s not in old and s not in movers and not s.get("tachie_idle")
                and s.right_handle > old_end + delta]
        for m in movers:
            if m.get("tachie_idle"):
                continue
            for s in stay:
                if s.channel == m.channel:
                    shift = max(shift, s.right_handle - m.left_handle)

    old_image = line_image(old)
    pose = get_pose(old_image) if old_image is not None else None
    old_subtitle = line_subtitle(old)
    offset = text_offset(old_subtitle) if old_subtitle is not None else None
    for s in old:
        sed.strips.remove(s)
    remove_idle(sed)
    if settings.ripple and shift:
        shift_after(sed, old_end, shift)
    strips = place_strips(context, ch, clip, lines_dir, text, start, voice, accent, audio, channels, layout)
    image = line_image(strips)
    if pose is not None and image is not None:  # セリフごとの位置・大きさは作り直しても引き継ぐ
        set_pose(image, pose, ch)
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


def _split_by_faces(sed, ch, start: int, end: int) -> list[tuple[int, int, str]]:
    """[start, end) を表情ストリップの境目で分け、各区間の表情（基本は ""）を付ける。"""
    owner = [""] * (end - start)
    for a, b, preset in face_segments(sed, ch, start, end):
        owner[a:b] = [preset] * (b - a)
    out = []
    for i, preset in enumerate(owner):
        if out and out[-1][2] == preset:
            out[-1] = (out[-1][0], start + i + 1, preset)
        else:
            out.append((start + i, start + i + 1, preset))
    return out


def _segments_key(segments, base: str) -> str:
    return json.dumps([base, segments], ensure_ascii=False)


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
    """表情ストリップに合わせて立ち絵（セリフ・セリフなし）を描き直す。描き直した本数を返す。

    セリフは保存した動き（lines/<id>.json）から連番を並べ直してフォルダを差し替える（音声は合成し直さない）。
    セリフなしの立ち絵は作り直す。待機は refresh_idle が表情ストリップの境目で分けて置く。
    描き直せなかったもの（表情ストリップより前に作ったセリフなど）は `problems` に理由を足す。
    """
    scene = seq_scene(context)
    sed = scene.sequence_editor
    if sed is None:
        return 0
    settings = scene.tachie
    if strips is None:
        strips = [s for s in sed.strips if s.type == "IMAGE" and (s.get("tachie_id") or s.get("tachie_still"))]
    count = 0
    for strip in list(strips):
        name = strip.get("tachie_still") or strip.get("tachie_character")
        ch = next((c for c in settings.characters if c.name == name), None)
        if ch is None:
            continue
        preset = strip.get("tachie_expression", "")
        start = int(strip.content_start)
        segments = face_segments(sed, ch, start, start + len(strip.elements))
        key = _segments_key(segments, preset) if segments else ""
        if strip.get("tachie_faced", "") == key:
            continue
        try:
            if strip.get("tachie_still"):
                remake_still(context, strip)
            else:
                _relayout_line(context, strip, ch, preset, segments, key)
        except ERRORS as e:
            if problems is not None:
                problems.append(str(e))
            continue
        count += 1
    return count


def _relayout_line(context, strip, ch, preset: str, segments, key: str) -> None:
    original = strip.get("tachie_frames_dir", strip.directory)
    if not segments:
        strip.directory = original
        strip["tachie_faced"] = ""
        return
    motion_path = output_dir(context) / "lines" / f"{strip['tachie_id']}.json"
    if not motion_path.is_file():
        raise ValueError(f"{strip.name} は表情ストリップに対応する前に作ったセリフです（再生成すると使えます）")
    motion = LineMotion.load(motion_path)
    if motion.frames != len(strip.elements):
        raise ValueError(f"{strip.name} の長さが書き出したときと違います（再生成してください）")
    base = character_cache(context, ch, preset)
    caches = {p: character_cache(context, ch, p) for _, _, p in segments}
    paths = segment_frames(motion, [(0, motion.frames, base)] + [(a, b, caches[p]) for a, b, p in segments])
    directory = output_dir(context) / "lines" / f"{strip['tachie_id']}_f{hashlib.sha1(key.encode()).hexdigest()[:10]}"
    link_frames(paths, directory)
    strip.directory = _path_for_blend(directory) + "/"
    strip["tachie_faced"] = key


def set_line_expression(context, clip_id: str, preset: str) -> list:
    """セリフの表情を変える。保存した動き（lines/<id>.json）から立ち絵の連番を並べ直すので、音声は合成し直さない。

    動きの無い古いセリフ・長さが書き出したときと違うセリフは作り直す（`regenerate`）。表情ストリップは呼び出し側で
    `refresh_faces` する（ここでは立ち絵の元の連番を差し替えて、表情ストリップの区間を当て直すだけ）。
    """
    sed = seq_scene(context).sequence_editor
    strips = line_strips(sed, clip_id)
    if not strips:
        raise ValueError("セリフのストリップが見つかりません")
    ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
    if preset and preset not in {e.name for e in ch.expressions}:
        raise ValueError(f"{ch.name} に表情 {preset!r} がありません")
    image = line_image(strips)
    if image is not None:
        motion_path = output_dir(context) / "lines" / f"{clip_id}.json"
        motion = LineMotion.load(motion_path) if motion_path.is_file() else None
        if motion is None or motion.frames != len(image.elements):
            return regenerate(context, clip_id, preset=preset)
        paths = segment_frames(motion, [(0, motion.frames, character_cache(context, ch, preset))])
        key = hashlib.sha1(json.dumps(["expression", preset], ensure_ascii=False).encode()).hexdigest()[:10]
        directory = output_dir(context) / "lines" / f"{clip_id}_e{key}"
        link_frames(paths, directory)
        image.directory = _path_for_blend(directory) + "/"
        image["tachie_frames_dir"] = image.directory
        image["tachie_faced"] = ""  # 表情ストリップの区間は新しい連番の上に当て直す
    for s in strips:
        s["tachie_expression"] = preset
    if image is not None:
        apply_faces(context, [image])
    return strips


def refresh_faces(context, problems: list | None = None) -> int:
    """全体を表情ストリップに合わせる（立ち絵の描き直しと待機の作り直し）。"""
    count = apply_faces(context, problems=problems)
    if seq_scene(context).tachie.auto_idle:
        refresh_idle(context)  # 最後に動きとダッキングも作り直す
    else:
        # 呼び出し側は反映済みとして署名を記録するので、待機を作らないときも動きとダッキングは作り直す
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
    """表情ストリップと、それが効く立ち絵の位置・表情。変わったら描き直す（待機は作り直すので含めない）。"""
    sed = scene.sequence_editor
    if sed is None:
        return ""
    items = []
    for s in sed.strips:
        if s.get("tachie_face"):
            items.append(("face", s.name, s.channel, s.left_handle, s.right_handle, s["tachie_face"],
                          s.get("tachie_expression", "")))
        elif s.type == "IMAGE" and (s.get("tachie_id") or s.get("tachie_still")):
            items.append(("image", s.name, int(s.content_start), len(s.elements), s.get("tachie_faced", "")))
    if not any(i[0] == "face" for i in items) and not any(
            s.get("tachie_faced") for s in sed.strips if s.type == "IMAGE"):
        return ""  # 表情ストリップが無く、描き直した立ち絵も無い
    return json.dumps(sorted(items), ensure_ascii=False)


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
    with bpy.context.temp_override(window=window) if window else _nullcontext():
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


def remove_idle(sed) -> None:
    for s in [s for s in sed.strips if s.get("tachie_idle")]:
        sed.strips.remove(s)


def refresh_idle(context) -> int:
    """待機ストリップを作り直す。各キャラの立ち絵チャンネルの隙間を、シーンの開始から最後のセリフ
    （か手動の立ち絵）の終わりまで、まばたきだけの立ち絵で埋める。置いた本数を返す。

    そのキャラの立ち絵の無いセリフ（音声と字幕・音声だけ）の間は埋めない（立ち絵を消す）。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor
    if sed is None:
        return 0
    lanes.ensure_layout(scene)  # 待機は立ち絵のレーン（ch.channel）に置く
    remove_idle(sed)
    line_ends = [s.right_handle for s in sed.strips if s.get("tachie_id") or s.get("tachie_still")]
    if not line_ends:
        return 0
    start, end = scene.frame_start, max(line_ends)
    fps = scene_fps(scene)
    count = 0
    for ch in scene.tachie.characters:
        if not ch.show_idle or not ch.folder:
            continue
        # そのキャラの立ち絵（手で別のチャンネルに動かしたものも）と、立ち絵のチャンネルに実際にあるもの
        # （新しく作るストリップは重なっても Blender が逃がさないので、そこには置かない）
        busy = [(s.left_handle, s.right_handle) for s in sed.strips
                if s.channel == ch.channel or _own_image(s, ch.name)]
        busy += [(s.left_handle, max(s.right_handle, s.left_handle + int(s.get("tachie_frames", 0))))
                 for s in sed.strips  # 余韻の分まで（音声だけのセリフは音声ストリップより長い）
                 if s.get("tachie_character") == ch.name and s.get("tachie_id")
                 and s.get("tachie_layout", "FULL") != "FULL"]
        busy.sort()
        gaps, cursor = [], start
        for left, right in busy:
            if left > cursor:
                gaps.append((cursor, min(left, end)))
            cursor = max(cursor, right)
        if cursor < end:
            gaps.append((cursor, end))
        gaps = [(a, b) for a, b in gaps if b > a]
        if not gaps:
            continue
        caches = {}
        for gap_a, gap_b in gaps:
            # 表情ストリップの境目でも分け、その区間の表情で置く
            for a, b, preset in _split_by_faces(sed, ch, gap_a, gap_b):
                if preset not in caches:
                    caches[preset] = character_cache(context, ch, preset)
                strip = _blink_strip(sed, ch, caches[preset], f"待機 {ch.name}", a, b - a, fps)
                strip["tachie_idle"] = ch.name
                count += 1
    refresh_motion_now(scene)
    return count


def _own_image(strip, name: str) -> bool:
    """キャラ `name` のセリフかセリフなしの立ち絵（待機は除く）。置いてあるチャンネルは問わない。"""
    from .motion import owner

    return not strip.get("tachie_idle") and owner(strip) == name


def _own_overlaps(sed, ch, start: int, end: int, ignore=()) -> bool:
    """キャラ `ch` の立ち絵（手で別のチャンネルに動かしたものも）が [start, end) に重なるか。"""
    return any(_own_image(s, ch.name) and s.left_handle < end and start < s.right_handle and s not in ignore
               for s in sed.strips)


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
                channel: int | None = None):
    """セリフなしの立ち絵（まばたきのみ）を置く。待機ストリップと違い、作り直しでは消えない。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    lanes.ensure_layout(scene)  # 立ち絵のレーン（ch.channel）に置く
    if scene.view_settings.view_transform != "Standard":
        scene.view_settings.view_transform = "Standard"
    cache = character_cache(context, ch, preset)
    name = f"立ち絵 {ch.name}" + (f"({preset})" if preset else "")
    if not channel or _overlaps(sed, channel, start, start + frames):  # 作り直す前のチャンネルが埋まっていればレーン
        channel = ch.channel
    segments = face_segments(sed, ch, start, start + frames)
    if segments:
        # 表情ストリップと重なる: 区間ごとに表情を変えた連番を faces/ に並べる
        caches = {p: character_cache(context, ch, p) for _, _, p in segments}
        paths = blink_segment_frames([(0, frames, cache)] + [(a, b, caches[p]) for a, b, p in segments],
                                     frames, scene_fps(scene), seed=(ch.name, start))
        # フォルダはキャラ・位置・長さごと（同じ区間の別の立ち絵と連番を取り合わないように）
        strip = _strip_from_paths(context, sed, ch, name, start, paths, "faces",
                                  json.dumps([ch.name, start, frames, _segments_key(segments, preset)],
                                             ensure_ascii=False), channel)
    else:
        strip = _blink_strip(sed, ch, cache, name, start, frames, scene_fps(scene), channel)
    strip["tachie_faced"] = _segments_key(segments, preset) if segments else ""
    strip["tachie_still"] = ch.name
    strip["tachie_character"] = ch.name
    strip["tachie_expression"] = preset or ""
    set_pose(strip, pose, ch)
    if strip.right_handle - 1 > scene.frame_end:
        scene.frame_end = strip.right_handle - 1
    return strip


def remake_still(context, strip, preset: str | None = None):
    """手動の立ち絵を今の位置・長さ・キャラクター設定で作り直す。新しいストリップを返す。"""
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    ch = find_character(settings, strip["tachie_still"])
    preset = preset if preset is not None else strip.get("tachie_expression", "")
    if preset:
        find_expression(ch, preset)
    start, frames = strip.left_handle, strip.right_handle - strip.left_handle
    character_cache(context, ch, preset)  # 素材が読めないときは消す前に失敗させる
    pose = get_pose(strip)
    channel = strip.channel  # 今のチャンネル（空いていれば、そこに置き直す）
    sed.strips.remove(strip)
    return place_still(context, ch, preset, start, frames, pose, channel)


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

class TACHIE_OT_add_line(bpy.types.Operator):
    """選択中のキャラクターでセリフを合成し、音声・口パク立ち絵・字幕を再生位置に置く"""

    bl_idname = "tachie.add_line"
    bl_label = "セリフを追加"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = seq_scene(context).tachie
        ch = settings.character()
        text, kana = split_kana(settings.text.strip())
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
            clip, lines_dir = render_clip(context, ch, text, settings.expression, kana=kana, layout=layout)
            channels = line_channels(context, ch, start, clip.frames, layout=layout)
            if seq_scene(context).sequence_editor:
                remove_idle(seq_scene(context).sequence_editor)
            strips = place_strips(context, ch, clip, lines_dir, text, start, channels=channels, layout=layout)
            if settings.auto_idle:
                refresh_idle(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        connect(context, strips)
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
    """選択中のキャラクターの立ち絵（まばたきのみ）を、セリフなしで再生位置に置く"""

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
                    or _own_overlaps(sed, ch, start, start + frames)):
            self.report({"ERROR"}, f"チャンネル {ch.channel} の {start}〜{start + frames} フレームに"
                                   "他のストリップがあります")
            return {"CANCELLED"}
        try:
            character_cache(context, ch, settings.expression)  # 待機を消す前に素材を確かめる
            if sed:
                remove_idle(sed)
            strip = place_still(context, ch, settings.expression, start, frames)
            if settings.auto_idle:
                refresh_idle(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        scene.sequence_editor.active_strip = strip
        if settings.advance:
            scene.frame_current = strip.right_handle
        self.report({"INFO"}, f"{ch.name}の立ち絵を {frames} フレーム置きました")
        return {"FINISHED"}


class TACHIE_OT_remake_still(bpy.types.Operator):
    """選択中の立ち絵を、今の位置・長さ・キャラクター設定で作り直す（伸ばしたあとなど）"""

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
            strip = sed.active_strip
            ch = find_character(seq_scene(context).tachie, strip["tachie_still"])
            character_cache(context, ch, strip.get("tachie_expression", ""))  # 待機を消す前に素材を確かめる
            remove_idle(sed)
            strip = remake_still(context, strip)
            if seq_scene(context).tachie.auto_idle:
                refresh_idle(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        sed.active_strip = strip
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
            if seq_scene(context).tachie.auto_idle:
                refresh_idle(context)
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
        if seq_scene(context).tachie.auto_idle:
            refresh_idle(context)
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
        text, kana = split_kana(sed.active_strip["tachie_text"].strip())
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
    text, kana = split_kana(sed.active_strip["tachie_text"].strip())
    ch = find_character(seq_scene(context).tachie, strips[0]["tachie_character"])
    speaker = int((sound or strips[0]).get("tachie_speaker", ch.speaker))
    return strips, text, ch, speaker, kana or line_kana(strips, text)


def load_accent_edit(edit, phrases: list) -> None:
    edit.phrases.clear()
    for ap in phrases:
        item = edit.phrases.add()
        item.rest = json.dumps({k: v for k, v in ap.items() if k != "moras"}, ensure_ascii=False)
        for mora in ap["moras"]:
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
        load_accent_edit(edit, phrases)
        edit.clip_id, edit.text, edit.kana, edit.speaker = active_line_id(context), text, kana or "", speaker
        widest = max((len(p["moras"]) for p in phrases), default=1)
        return context.window_manager.invoke_props_dialog(self, width=max(420, min(1400, 64 * widest + 80)),
                                                          confirm_text="反映して作り直す")

    def draw(self, context):
        edit = context.window_manager.tachie_accent
        layout = self.layout
        row = layout.row(align=True)
        row.prop(edit, "tab", expand=True)
        row.operator("tachie.preview_accent", text="", icon="PLAY")
        row.operator("tachie.reload_accent", text="", icon="FILE_REFRESH")
        for item in edit.phrases:
            box = layout.box()
            if edit.tab == "PITCH":
                box.prop(item, "shift", slider=True)
            row = box.row(align=True)
            for m in item.moras:
                col = row.column(align=True)
                col.label(text=m.name)
                if edit.tab == "PITCH":
                    sub = col.column(align=True)
                    sub.enabled = m.voiced
                    sub.prop(m, "pitch", text="")
                else:
                    sub = col.column(align=True)
                    sub.enabled = bool(m.consonant)
                    sub.prop(m, "consonant_length", text="")
                    col.prop(m, "vowel_length", text="")
        if edit.tab == "LENGTH":
            layout.label(text="上が子音、下が母音の長さ（秒）")

    def execute(self, context):
        edit = context.window_manager.tachie_accent
        if active_line_id(context) != edit.clip_id:
            self.report({"ERROR"}, "編集中に選択中のセリフが変わりました")
            return {"CANCELLED"}
        return _regenerate_active(self, context, kana=edit.kana or None, accent=accent_from_edit(edit))


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
        line = next((s for s in sed.strips if s.type == "IMAGE" and s.get("tachie_id")
                     and s.get("tachie_character") == ch.name and s.left_handle <= start < s.right_handle), None)
        end = line.right_handle if line else start + max(1, round(settings.face_seconds * scene_fps(scene)))
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
            # 手動の立ち絵はずらし終わった位置で作り直す
            remove_idle(sed)
            for s in stills:
                remake_still(context, s)
        except ERRORS as e:
            self.report({"ERROR"}, f"{count} 件作り直したところで失敗しました: {e}")
            return {"FINISHED"} if count else {"CANCELLED"}
        finally:
            wm.progress_end()
            if (count or stills) and settings.auto_idle:
                refresh_idle(context)
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
        try:  # 素材はもう変えたので、待機を作り直せなくても FINISHED にする（CANCELLED だと元に戻すの段が積まれない）
            if seq_scene(context).tachie.auto_idle:
                refresh_idle(context)
        except ERRORS as e:
            self.report({"WARNING"}, f"{ch.name}の素材を変えましたが、待機の立ち絵を作り直せません: {e}")
            return {"FINISHED"}
        note = f"（素材に無いパーツ・レイヤー {removed} 個を既定に戻しました）" if removed else ""
        self.report({"WARNING"} if removed else {"INFO"},
                    f"{ch.name}の素材を変えました{note}。置いてあるセリフは「全セリフを再生成」で反映されます")
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
    had_material = bool(ch.folder)
    ch.folder = source
    removed = fit_to_material(ch, material) + sum(fit_to_material(e, material) for e in ch.expressions)
    if material.is_psd and not item_layers(ch):
        psd_default_mouth(ch, material)
    if not had_material and ch.line_layout == "SUBTITLE":
        ch.line_layout = "FULL"  # 立ち絵の無いキャラに素材を付けたら、新しいセリフは立ち絵ありに
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
    if ch.line_layout == "FULL":  # 立ち絵が無いので、保存していなければ「音声と字幕」
        ch.line_layout = "SUBTITLE"
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
        layout = next(label for key, label, *_ in LINE_LAYOUTS if key == ch.line_layout)
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

    prop: bpy.props.StringProperty()
    preset: bpy.props.StringProperty(description="空ならキャラクターの基本の表情、指定するとその表情プリセット")
    part: bpy.props.EnumProperty(items=_parts)

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def invoke(self, context, event):
        global _part_items
        category = next(c for c, props in PART_PROPS.items() if self.prop in props)
        try:
            character = load_material(seq_scene(context).tachie.character().folder)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        names = sorted(character.parts.get(category, {}))
        if self.preset:
            empty = "（基本の表情のまま）"
        else:
            empty = "（なし）" if category in SLOT_CATEGORIES else "（既定）"
        _part_items = [(NONE, empty, "")] + [(n, n, "") for n in names]
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        target = find_expression(ch, self.preset) if self.preset else ch
        setattr(target, self.prop, "" if self.part == NONE else self.part)
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
        # 待機と手動の立ち絵はキャッシュの画像を直接指しているので作り直す
        sed = seq_scene(context).sequence_editor
        try:
            if sed is not None:
                for s in [s for s in sed.strips if s.get("tachie_still")]:
                    remake_still(context, s)
            refresh_idle(context)
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
        text = s.get("tachie_placed_text", s.get("tachie_text", s.text))
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
        if scene.sequence_editor:
            remove_idle(scene.sequence_editor)
        try:
            for line in lines:
                ch = find_character(settings, line.character)
                layout = line_layout(ch)
                clip, lines_dir = render_clip(context, ch, line.text, line.expression, kana=line.kana, layout=layout)
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
            if settings.auto_idle:
                refresh_idle(context)  # 1 本も置けなくても、消した待機を戻す
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
        _expression_items = [(NONE, "（基本）", "")] + [(e.name, e.name, "") for e in ch.expressions]
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
        # 選択中のセリフはその表情で作り直す
        try:
            strips = regenerate(context, active_line_id(context), sed.active_strip["tachie_text"], preset=value)
            if seq_scene(context).tachie.auto_idle:
                refresh_idle(context)
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


class TACHIE_OT_refresh_idle(bpy.types.Operator):
    """喋っていない間の立ち絵（まばたきのみ）を作り直す。ストリップを手で動かしたあとに使う"""

    bl_idname = "tachie.refresh_idle"
    bl_label = "待機を更新"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            count = refresh_idle(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"待機ストリップを {count} 本置きました")
        return {"FINISHED"}


_classes = (
    TACHIE_OT_add_line,
    TACHIE_OT_add_still,
    TACHIE_OT_remake_still,
    TACHIE_OT_import_script,
    TACHIE_OT_pick_expression,
    TACHIE_OT_expression_add,
    TACHIE_OT_expression_remove,
    TACHIE_OT_refresh_idle,
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


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
