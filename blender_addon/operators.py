import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .properties import PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, apply_placement, apply_subtitle, system_fonts
from .tachie_studio.pipeline import CharacterConfig, FrameCache, Line, idle_frames, load_characters, parse_script, render_line
from .tachie_studio.pipeline.config import (MATERIAL_CONFIG, VOICE_DEFAULTS, VOICE_KEYS, dump_characters, dump_raw_table, dump_table,
                                            load_material_table, merge_tables, parse_characters)
from .tachie_studio.tachie import Expression, load_character
from .tachie_studio.tachie.compose import load_image
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


def client(context) -> VoicevoxClient:
    return VoicevoxClient(prefs(context).voicevox_url)


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
    """キャラクターか表情プリセットのパーツ指定を {フォルダ名: 値} にする（空は省く）。"""
    parts = {}
    for category, props in PART_PROPS.items():
        values = [v for p in props if (v := getattr(item, p))]
        if values:
            parts[category] = values if category in SLOT_CATEGORIES else values[0]
    return parts


def find_expression(ch, preset: str):
    for e in ch.expressions:
        if e.name == preset:
            return e
    raise ValueError(f"{ch.name} に表情 {preset!r} がありません")


def config_of(ch) -> CharacterConfig:
    base = parse_characters({ch.name: {"folder": "", "speaker": ch.speaker, "parts": _parts_dict(ch)}}, Path("."),
                            use_material=False)
    return CharacterConfig(
        name=ch.name, folder=Path(bpy.path.abspath(ch.folder)), speaker=ch.speaker,
        expression=base[ch.name].expression, mouth=ch.mouth,
        voice=_config_voice(ch),
        expressions={e.name: _parts_dict(e) for e in ch.expressions},
        layout={"position": ch.position_x, "flip": ch.flip_x, "height": ch.height, "channel": ch.channel},
        subtitle={"font": ch.font_path, "size": ch.font_size, "color": list(ch.text_color),
                  "outline_color": list(ch.outline_color), "outline_width": ch.outline_width,
                  "y": ch.subtitle_y},
    )


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
    ch.folder = str(config.folder)
    ch.speaker = config.speaker
    ch.mouth = config.mouth
    for prop, (key, *_) in VOICE_PROPS.items():
        setattr(ch, prop, config.voice.get(key, VOICE_DEFAULTS[key]))
    e = config.expression
    for prop, value in (("part_eyebrow", e.eyebrow), ("part_eye", e.eye), ("part_mouth", e.mouth),
                        ("part_hair", e.hair), ("part_complexion", e.complexion), ("part_body", e.body)):
        setattr(ch, prop, value or "")
    for category, values in (("他", e.others), ("後", e.backs)):
        for i, prop in enumerate(PART_PROPS[category]):
            setattr(ch, prop, values[i] if i < len(values) else "")
    ch.expressions.clear()
    for preset, parts in config.expressions.items():
        item = ch.expressions.add()
        item.name = preset
        for category, value in parts.items():
            values = [value] if isinstance(value, str) else list(value)
            for prop, v in zip(PART_PROPS[category], values):
                setattr(item, prop, v)
    layout, sub = config.layout, config.subtitle
    for prop, key in (("position_x", "position"), ("flip_x", "flip"), ("height", "height"), ("channel", "channel")):
        if key in layout:
            setattr(ch, prop, layout[key])
    for prop, key in (("font_path", "font"), ("font_size", "size"), ("text_color", "color"),
                      ("outline_color", "outline_color"), ("outline_width", "outline_width"),
                      ("subtitle_y", "y")):
        if key in sub:
            setattr(ch, prop, sub[key])


# ---- キャラクター追加の初期値 ----

def character_defaults_path() -> Path:
    """アドオン設定のキャラクターごとの既定値（全 .blend 共通）。キーは素材フォルダ名。"""
    return Path(bpy.utils.extension_path_user(__package__, create=True)) / "character_defaults.toml"


def load_character_defaults() -> dict:
    """{素材フォルダ名: テーブル}。テーブルでない値（手で書いた誤り）は無視する。"""
    import tomllib

    path = character_defaults_path()
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path} を読めません: {e}") from None
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_character_defaults(tables: dict) -> None:
    """{素材フォルダ名: dump_table の TOML 文字列} を書く。"""
    character_defaults_path().write_text("\n".join(tables.values()), encoding="utf-8")


def _defaults_texts() -> dict:
    """今の既定値ファイルを {キー: TOML 文字列} にする（1 キャラ分だけ書き換えるため）。

    他のキャラの分は解釈せずにそのまま書き戻す（speaker や mouth を足すと素材の tachie.toml より優先されてしまい、
    検査で落ちる項目があると消すこともできなくなるため）。
    """
    return {key: dump_raw_table(table, key) for key, table in load_character_defaults().items()}


def material_key(folder: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", os.path.basename(os.path.normpath(bpy.path.abspath(folder))))


def _free_channel(settings, ch, wanted: int | None = None) -> int:
    """他のキャラ・音声・字幕と重ならないチャンネル。`wanted` が空いていればそれ。"""
    used = {c.channel for c in settings.characters if c != ch}
    used |= {settings.audio_channel, settings.subtitle_channel}
    if wanted is not None and wanted not in used:
        return wanted
    return next(c for c in range(min(used | {wanted or 3, 3}), 129) if c not in used)


def find_character(settings, name: str):
    for ch in settings.characters:
        if ch.name == name:
            return ch
    raise ValueError(f"キャラクター {name!r} が設定にありません")


# ---- ストリップ ----

def _overlaps(sed, channel: int, start: int, end: int, ignore=()) -> bool:
    # 待機ストリップは作り直すので邪魔にならない
    return any(s.channel == channel and s.left_handle < end and start < s.right_handle and s not in ignore
               and not s.get("tachie_idle") for s in sed.strips)


def _path_for_blend(path: Path) -> str:
    return bpy.path.relpath(str(path)) if bpy.data.filepath else str(path)


def line_strips(sed, clip_id: str) -> list:
    return [s for s in sed.strips if s.get("tachie_id") == clip_id]


def character_cache(context, ch, preset: str | None = None) -> FrameCache:
    """キャラクターの表情・大きさでの「目 × 口」合成キャッシュ。"""
    character = load_material(ch.folder)
    base = character.default("体") or next(p for parts in character.parts.values() for p in parts.values())
    _, image_h = load_image(base.path).size
    scale = seq_scene(context).render.resolution_y * ch.height / image_h
    return _cache(character, expression_of(ch, preset), output_dir(context) / "cache", scale)


def render_clip(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
                voice: dict | None = None):
    """セリフを合成して書き出す。(RenderedClip, 書き出し先フォルダ) を返す。

    `speaker`（スタイル）と `voice`（audio_query の値）はセリフごとの値。None ならキャラクターの既定値。
    """
    cache = character_cache(context, ch, preset)
    config = CharacterConfig(name=ch.name, folder=cache.character.path, speaker=ch.speaker,
                             expression=cache.expression, mouth=ch.mouth)
    clip_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    lines_dir = output_dir(context) / "lines"
    clip = render_line(client(context), config, cache.character, Line(0, ch.name, text, preset or None, speaker,
                                                                    voice if voice is not None else character_voice(ch)),
                       lines_dir,
                       fps=scene_fps(seq_scene(context)), cache=cache, stem=clip_id)
    return clip, lines_dir


def check_free(context, ch, start: int, frames: int, ignore=()) -> None:
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    channels = (settings.audio_channel, ch.channel, settings.subtitle_channel)
    if len(set(channels)) < 3:
        raise ValueError("音声・立ち絵・字幕のチャンネルは別々にしてください")
    if sed is None:
        return
    busy = [c for c in channels if _overlaps(sed, c, start, start + frames, ignore)]
    if busy:
        raise ValueError(f"チャンネル {', '.join(map(str, busy))} の {start}〜{start + frames} フレームに"
                         "他のストリップがあります")


def place_strips(context, ch, clip, lines_dir: Path, text: str, start: int, voice: dict | None = None) -> list:
    """書き出したセリフを start に置く。置いたストリップ（音声・立ち絵・字幕）を返す。

    セリフごとの声（`voice`、None ならキャラクターの既定値）は音声ストリップの `tachie_voice_*` に持つ。
    """
    scene = seq_scene(context)
    settings = scene.tachie
    sed = scene.sequence_editor or scene.sequence_editor_create()
    if scene.view_settings.view_transform != "Standard":
        # 3D 用の AgX / Filmic のままだと立ち絵の色が淡くなる
        scene.view_settings.view_transform = "Standard"

    frames_dir = lines_dir / clip.frames_dir
    frame_files = sorted(os.listdir(frames_dir))
    label = f"{ch.name}: " + (text if len(text) <= 16 else text[:15] + "…")

    sound = sed.strips.new_sound(name=f"音声 {label}", filepath=_path_for_blend(lines_dir / clip.wav),
                                 channel=settings.audio_channel, frame_start=start)

    image = sed.strips.new_image(name=f"立ち絵 {label}", filepath=_path_for_blend(frames_dir / frame_files[0]),
                                 channel=ch.channel, frame_start=start, fit_method="ORIGINAL")
    for name in frame_files[1:]:
        image.elements.append(name)
    image.blend_type = "ALPHA_OVER"
    apply_placement(image, ch)

    subtitle = sed.strips.new_effect(name=f"字幕 {label}", type="TEXT", channel=settings.subtitle_channel,
                                     frame_start=start, length=clip.frames)
    apply_subtitle(subtitle, ch, text)

    if subtitle.right_handle - 1 > scene.frame_end:
        # 再生・レンダリングがセリフの途中で切れないように
        scene.frame_end = subtitle.right_handle - 1

    strips = [sound, image, subtitle]
    for s in strips:
        s["tachie_id"] = clip.frames_dir
        s["tachie_text"] = text
        s["tachie_placed_text"] = text  # 置いたときのテキスト（パネルで編集されたストリップを見分ける）
        s["tachie_character"] = ch.name
        s["tachie_speaker"] = clip.speaker
        s["tachie_folder"] = ch.folder
        s["tachie_mouth"] = clip.extra.get("mouth_mode", "")
        s["tachie_expression"] = clip.extra.get("expression", "")
    set_line_voice(sound, voice if voice is not None else character_voice(ch))
    return strips


def set_line_voice(sound, voice: dict) -> None:
    for prop, (key, label, lo, hi, desc) in VOICE_PROPS.items():
        name = f"tachie_voice_{prop}"
        sound[name] = float(voice.get(key, VOICE_DEFAULTS[key]))
        sound.id_properties_ui(name).update(min=lo, max=hi, soft_min=lo, soft_max=hi, step=1, precision=2,
                                            description=desc)


def line_sound(strips):
    return next((s for s in strips if s.type == "SOUND"), None)


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
               speaker: int | None = None, voice: dict | None = None, character: str | None = None) -> list:
    """セリフを作り直して同じ位置に置き直す。新しいストリップを返す。

    指定しなかった値は置いてあるセリフから取る（声とスタイルは音声ストリップ）。`character` で別のキャラにすると、
    スタイルと声はそのキャラの既定値になり、そのキャラに無い表情プリセットは基本の表情に戻る。
    """
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    old = line_strips(sed, clip_id)
    if not old:
        raise ValueError("セリフのストリップが見つかりません")
    text = text if text is not None else line_text(old)
    preset = preset if preset is not None else old[0].get("tachie_expression", "")
    sound = line_sound(old) or old[0]
    if character is not None and character != old[0]["tachie_character"]:
        ch = find_character(settings, character)
        if preset and preset not in {e.name for e in ch.expressions}:
            preset = ""
        speaker = ch.speaker if speaker is None else speaker
        voice = character_voice(ch) if voice is None else voice
    else:
        ch = find_character(settings, old[0]["tachie_character"])
        speaker = int(sound.get("tachie_speaker", ch.speaker)) if speaker is None else speaker
        voice = line_voice(old, ch) if voice is None else voice
    start = min(s.left_handle for s in old)
    old_end = max(s.right_handle for s in old)

    clip, lines_dir = render_clip(context, ch, text, preset, speaker, voice)
    delta = start + clip.frames - old_end
    # 消す前に置けるか確かめる（ずらすストリップは邪魔にならない）
    movers = [s for s in sed.strips if settings.ripple and delta and s.left_handle >= old_end]
    check_free(context, ch, start, clip.frames, ignore=old + movers)

    for s in old:
        sed.strips.remove(s)
    remove_idle(sed)
    if settings.ripple and delta:
        shift_after(sed, old_end, delta)
    return place_strips(context, ch, clip, lines_dir, text, start, voice)


def remove_idle(sed) -> None:
    for s in [s for s in sed.strips if s.get("tachie_idle")]:
        sed.strips.remove(s)


def refresh_idle(context) -> int:
    """待機ストリップを作り直す。各キャラの立ち絵チャンネルの隙間を、シーンの開始から最後のセリフ
    （か手動の立ち絵）の終わりまで、まばたきだけの立ち絵で埋める。置いた本数を返す。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor
    if sed is None:
        return 0
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
        busy = sorted((s.left_handle, s.right_handle) for s in sed.strips if s.channel == ch.channel)
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
        cache = character_cache(context, ch)
        for a, b in gaps:
            strip = _blink_strip(sed, ch, cache, f"待機 {ch.name}", a, b - a, fps)
            strip["tachie_idle"] = ch.name
            count += 1
    return count


def _blink_strip(sed, ch, cache: FrameCache, name: str, start: int, frames: int, fps: float):
    """まばたきだけの立ち絵（口は閉じ）のストリップを置く。"""
    paths = idle_frames(cache, frames, fps, seed=(ch.name, start))
    strip = sed.strips.new_image(name=name, filepath=_path_for_blend(paths[0]),
                                 channel=ch.channel, frame_start=start, fit_method="ORIGINAL")
    for p in paths[1:]:
        strip.elements.append(p.name)
    strip.blend_type = "ALPHA_OVER"
    apply_placement(strip, ch)
    return strip


def place_still(context, ch, preset: str, start: int, frames: int):
    """セリフなしの立ち絵（まばたきのみ）を置く。待機ストリップと違い、作り直しでは消えない。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    if scene.view_settings.view_transform != "Standard":
        scene.view_settings.view_transform = "Standard"
    cache = character_cache(context, ch, preset)
    strip = _blink_strip(sed, ch, cache, f"立ち絵 {ch.name}" + (f"({preset})" if preset else ""),
                         start, frames, scene_fps(scene))
    strip["tachie_still"] = ch.name
    strip["tachie_character"] = ch.name
    strip["tachie_expression"] = preset or ""
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
    sed.strips.remove(strip)
    return place_still(context, ch, preset, start, frames)


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
        text = settings.text.strip()
        if ch is None:
            self.report({"ERROR"}, "キャラクターを追加してください")
            return {"CANCELLED"}
        if not text:
            self.report({"ERROR"}, "セリフを入力してください")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        start = seq_scene(context).frame_current
        try:
            clip, lines_dir = render_clip(context, ch, text, settings.expression)
            check_free(context, ch, start, clip.frames)
            if seq_scene(context).sequence_editor:
                remove_idle(seq_scene(context).sequence_editor)
            strips = place_strips(context, ch, clip, lines_dir, text, start)
            if settings.auto_idle:
                refresh_idle(context)
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        connect(context, strips)
        if settings.advance:
            seq_scene(context).frame_current = strips[1].right_handle
        self.report({"INFO"}, f"{strips[1].duration} フレームのセリフを追加しました")
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
        ensure_sequencer_scene(context)
        start = scene.frame_current
        frames = max(1, round(settings.still_seconds * scene_fps(scene)))
        sed = scene.sequence_editor
        if sed and _overlaps(sed, ch.channel, start, start + frames):
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
        sed.active_strip = strips[2]
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
    sed.active_strip = strips[2]
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


class TACHIE_OT_reset_line_voice(bpy.types.Operator):
    """選択中のセリフの声とスタイルを、キャラクターの今の既定値に戻して作り直す"""

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
        return _regenerate_active(self, context, speaker=ch.speaker, voice=character_voice(ch))


class TACHIE_OT_regenerate_all(bpy.types.Operator):
    """すべてのセリフを、今のキャラクター設定で作り直す（表情を変えたとき。声とスタイルはセリフごとの値のまま）"""

    bl_idname = "tachie.regenerate_all"
    bl_label = "全セリフを再生成"
    bl_options = {"REGISTER", "UNDO"}

    character_only: bpy.props.BoolProperty(name="選択中のキャラクターだけ", default=False)
    use_character_voice: bpy.props.BoolProperty(
        name="キャラクターの声を当て直す", default=False,
        description="セリフごとの声とスタイルを、キャラクターの今の既定値に置き換える",
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
                    kwargs = {"speaker": ch.speaker, "voice": character_voice(ch)}
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
    """動く立ち絵の素材フォルダを選んでキャラクターを追加する"""

    bl_idname = "tachie.character_add"
    bl_label = "キャラクターを追加"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        settings = seq_scene(context).tachie
        try:
            character = load_material(self.directory)
            key = material_key(self.directory)
            material = load_material_table(character.path)
            saved = load_character_defaults().get(key, {})
            # 優先: アドオン設定 → 素材の tachie.toml → 固定値
            table = merge_tables({"speaker": 3}, material, saved)
            name = str(table.pop("name", "") or key)
            config = parse_characters({name: {**table, "folder": self.directory}}, Path("."), use_material=False)[name]
        except (*ERRORS, OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        ch = settings.characters.add()
        try:
            apply_config(ch, config)
        except (TypeError, ValueError, KeyError) as e:  # tachie.toml の値の型や範囲が合わない
            settings.characters.remove(len(settings.characters) - 1)
            self.report({"ERROR"}, f"初期値を当てはめられません: {e}")
            return {"CANCELLED"}
        ch.folder = self.directory
        n = len(settings.characters)
        # 立ち位置が決まっていなければ、2 人目以降は左右を入れ替える
        if "position" not in config.layout and n > 1:
            ch.position_x = -0.3 if n % 2 == 0 else 0.3
        # チャンネルは他のキャラと重なるなら空いている番号へ
        if n > 1 or "channel" in config.layout:
            ch.channel = _free_channel(settings, ch, config.layout.get("channel"))
        try:  # エンジンが止まっていても追加は待たせない
            names = VoicevoxClient(prefs(context).voicevox_url, timeout=3.0).styles()
            ch.speaker_name = dict(names).get(ch.speaker, "")
        except (VoicevoxError, OSError):
            pass
        settings.active_character = n - 1
        sources = [label for label, t in (("アドオン設定", saved), (MATERIAL_CONFIG, material)) if t]
        if sources:
            self.report({"INFO"}, f"{ch.name} を {'と'.join(sources)} の初期値で追加しました")
        return {"FINISHED"}


class TACHIE_OT_save_defaults(bpy.types.Operator):
    """選択中のキャラクターの設定を、次にこの素材を追加するときの初期値として保存する"""

    bl_idname = "tachie.save_defaults"
    bl_label = "初期値として保存"

    target: bpy.props.EnumProperty(items=[
        ("PREFS", "アドオン設定", "アドオン設定に保存する（全 .blend 共通。素材の tachie.toml より優先）"),
        ("MATERIAL", "素材フォルダ", "素材フォルダの tachie.toml に書く（素材と一緒に持ち運べる）"),
    ])

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return ch is not None and bool(ch.folder)

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
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

    def execute(self, context):
        try:
            texts = _defaults_texts()
            texts.pop(self.key, None)
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
        try:
            styles = client(context).styles()
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
        try:
            names = dict(client(context).styles())
        except VoicevoxError:
            names = {}  # エンジンが動いていなければ話者名は空のまま（ID で表示）
        for config in configs.values():
            existing = next((c for c in settings.characters if c.name == config.name), None)
            ch = existing or settings.characters.add()
            apply_config(ch, config)
            ch.speaker_name = names.get(config.speaker, "")
        self.report({"INFO"}, f"{len(configs)} 人のキャラクター設定を読み込みました")
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


class TACHIE_OT_import_script(bpy.types.Operator, ImportHelper):
    """台本を読み、セリフを再生位置から順に並べる（1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」）"""

    bl_idname = "tachie.import_script"
    bl_label = "台本を流し込む"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".txt"
    filter_glob: bpy.props.StringProperty(default="*.txt", options={"HIDDEN"})
    use_text: bpy.props.BoolProperty(name="テキストエディタの台本を使う", default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        if self.use_text:
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        if self.use_text:
            if settings.script is None:
                self.report({"ERROR"}, "台本のテキストを選んでください")
                return {"CANCELLED"}
            source = settings.script.as_string()
        else:
            try:
                source = Path(self.filepath).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as e:
                self.report({"ERROR"}, f"読み込めませんでした: {e}")
                return {"CANCELLED"}
        try:
            lines = parse_script(source, {ch.name: config_of(ch) for ch in settings.characters})
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
                clip, lines_dir = render_clip(context, ch, line.text, line.expression)
                check_free(context, ch, cursor, clip.frames)
                strips = place_strips(context, ch, clip, lines_dir, line.text, cursor)
                connect(context, strips)
                cursor = strips[1].right_handle + gap
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
                                          ("STILL", "選択中の立ち絵", "")])
    expression: bpy.props.EnumProperty(items=_expressions)

    def _character(self, context):
        settings = seq_scene(context).tachie
        if self.target in ("LINE", "STILL"):
            sed = seq_scene(context).sequence_editor
            return find_character(settings, sed.active_strip["tachie_character"])
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
        seq_scene(context).sequence_editor.active_strip = strips[2]
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
    TACHIE_OT_pick_line_character,
    TACHIE_OT_reset_line_voice,
    TACHIE_OT_save_defaults,
    TACHIE_OT_remove_defaults,
    TACHIE_OT_character_remove,
    TACHIE_OT_pick_speaker,
    TACHIE_OT_pick_part,
    TACHIE_OT_pick_font,
    TACHIE_OT_reload_character,
    TACHIE_OT_import_characters,
    TACHIE_OT_export_characters,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
