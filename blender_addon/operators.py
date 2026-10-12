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

from .properties import AUDIO_PROPS, BAKED_AUDIO, PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, apply_placement, apply_subtitle, system_fonts
from .tachie_studio.pipeline import CharacterConfig, FrameCache, Line, idle_frames, load_characters, parse_script, render_line
from .tachie_studio.pipeline.render import LineMotion, blink_segment_frames, link_frames, segment_frames
from .tachie_studio.pipeline.script import split_kana
from .tachie_studio.pipeline.config import (MATERIAL_CONFIG, VOICE_DEFAULTS, VOICE_KEYS, dump_characters, dump_raw_table, dump_table,
                                            load_material_defaults, load_material_table, merge_tables,
                                            parse_characters)
from .tachie_studio.tachie import PRESET_FILE, Expression, load_character, load_presets
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
        audio=character_audio(ch),
    )


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
    ch.folder = str(config.folder)
    ch.speaker = config.speaker
    ch.mouth = config.mouth
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
    base = character.default("体") or next(p for parts in character.parts.values() for p in parts.values())
    _, image_h = load_image(base.path).size
    scale = seq_scene(context).render.resolution_y * ch.height / image_h
    return _cache(character, expression_of(ch, preset), output_dir(context) / "cache", scale)


def render_clip(context, ch, text: str, preset: str | None = None, speaker: int | None = None,
                voice: dict | None = None, kana: str | None = None, accent: list | None = None,
                accent_kana: str | None = None, audio: dict | None = None):
    """セリフを合成して書き出す。(RenderedClip, 書き出し先フォルダ) を返す。

    `speaker`（スタイル）と `voice`（audio_query の値）はセリフごとの値。None ならキャラクターの既定値。
    `kana` は発音（AquesTalk 風記法）。None や空なら VOICEVOX が自動で付けた読み。
    `accent` は抑揚を編集した accent_phrases（kana より優先）。`accent_kana` は編集したときの発音で、
    今の発音（`kana` か自動の読み）と違えば `accent` は使わない（ユーザー辞書で自動の読みが変わったときなど）。
    `audio` はフェードと余韻（None ならキャラクターの既定値）。
    """
    audio = audio if audio is not None else character_audio(ch)
    cache = character_cache(context, ch, preset)
    config = CharacterConfig(name=ch.name, folder=cache.character.path, speaker=ch.speaker,
                             expression=cache.expression, mouth=ch.mouth)
    clip_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    lines_dir = output_dir(context) / "lines"
    clip = render_line(client(context), config, cache.character, Line(0, ch.name, text, preset or None, speaker,
                                                                    voice if voice is not None else character_voice(ch),
                                                                    kana or None, accent or None,
                                                                    accent_kana=accent_kana,
                                                                    fade_in=audio.get("fade_in", 0.0),
                                                                    fade_out=audio.get("fade_out", 0.0),
                                                                    after=audio.get("after", 0.0)),
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


def place_strips(context, ch, clip, lines_dir: Path, text: str, start: int, voice: dict | None = None,
                 accent: list | None = None, audio: dict | None = None) -> list:
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
    set_line_audio(sound, audio if audio is not None else character_audio(ch))
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
               kana: str | None = None, accent: list | None = None, audio: dict | None = None) -> list:
    """セリフを作り直して同じ位置に置き直す。新しいストリップを返す。

    指定しなかった値は置いてあるセリフから取る（声とスタイルは音声ストリップ）。`character` で別のキャラにすると、
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
    old_end = max(s.right_handle for s in old)

    clip, lines_dir = render_clip(context, ch, text, preset, speaker, voice, kana, accent, accent_kana, audio)
    delta = start + clip.frames - old_end
    # 消す前に置けるか確かめる（ずらすストリップは邪魔にならない）
    movers = [s for s in sed.strips if settings.ripple and delta and s.left_handle >= old_end]
    check_free(context, ch, start, clip.frames, ignore=old + movers)

    for s in old:
        sed.strips.remove(s)
    remove_idle(sed)
    if settings.ripple and delta:
        shift_after(sed, old_end, delta)
    return place_strips(context, ch, clip, lines_dir, text, start, voice, accent, audio)


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


def _strip_from_paths(context, sed, ch, name: str, start: int, paths: list, folder: str, key: str):
    """画像のパスの列をハードリンクで 1 つのフォルダに並べ、立ち絵ストリップにする。"""
    directory = output_dir(context) / folder / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    link_frames(paths, directory)
    files = sorted(os.listdir(directory))
    strip = sed.strips.new_image(name=name, filepath=_path_for_blend(directory / files[0]),
                                 channel=ch.channel, frame_start=start, fit_method="ORIGINAL")
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


def refresh_faces(context, problems: list | None = None) -> int:
    """全体を表情ストリップに合わせる（立ち絵の描き直しと待機の作り直し）。"""
    count = apply_faces(context, problems=problems)
    if seq_scene(context).tachie.auto_idle:
        refresh_idle(context)
    return count


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
            items.append(("image", s.name, s.channel, int(s.content_start), len(s.elements),
                          s.get("tachie_faced", "")))
    if not any(i[0] == "face" for i in items) and not any(
            s.get("tachie_faced") for s in sed.strips if s.type == "IMAGE"):
        return ""  # 表情ストリップが無く、描き直した立ち絵も無い
    return json.dumps(sorted(items), ensure_ascii=False)


_applied_signatures: dict[str, str] = {}
_face_timer_running = False
_applying_faces = False


@persistent
def _on_depsgraph_update(scene, depsgraph):
    global _face_timer_running
    if _applying_faces or _face_timer_running:
        return
    # 引数はウィンドウのシーン。シーケンサーが別のシーンを映していればそちらも見る
    scenes = {scene, getattr(bpy.context, "sequencer_scene", None) or scene}
    if any(faces_signature(sc) != _applied_signatures.get(sc.name, "") for sc in scenes):
        _face_timer_running = True
        bpy.app.timers.register(_apply_faces_later, first_interval=0.3)


def _apply_faces_later():
    """ドラッグなどが終わってから描き直す（自動の反映）。"""
    global _face_timer_running, _applying_faces
    wm = bpy.context.window_manager
    if wm and any(getattr(w, "modal_operators", None) for w in wm.windows):
        return 0.3  # まだ動かしている
    _face_timer_running = False
    window = wm.windows[0] if wm and wm.windows else None
    with bpy.context.temp_override(window=window) if window else _nullcontext():
        # refresh_faces は seq_scene(context) を描き直すので、そのシーンだけを見る（他のシーンは
        # シーケンサーに映ったときに反映する）
        scene = seq_scene(bpy.context)
        if faces_signature(scene) == _applied_signatures.get(scene.name, ""):
            return None
        _applying_faces = True
        try:
            problems = []
            refresh_faces(bpy.context, problems)
            for p in dict.fromkeys(problems):
                print("表情を反映できません:", p)
            # undo_push はしない: 直前の操作（ドラッグなど）の段は反映前の状態なので、元に戻すと署名が
            # 変わってまた描き直される。ここで段を積むと 1 回目の取り消しが反映だけを戻し、表情ストリップは
            # 動かしたまま立ち絵だけ古い表情になる
        except ERRORS as e:
            print("表情の反映に失敗しました:", e)
        finally:
            _applying_faces = False
            _applied_signatures[scene.name] = faces_signature(scene)
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
        caches = {}
        for gap_a, gap_b in gaps:
            # 表情ストリップの境目でも分け、その区間の表情で置く
            for a, b, preset in _split_by_faces(sed, ch, gap_a, gap_b):
                if preset not in caches:
                    caches[preset] = character_cache(context, ch, preset)
                strip = _blink_strip(sed, ch, caches[preset], f"待機 {ch.name}", a, b - a, fps)
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
    name = f"立ち絵 {ch.name}" + (f"({preset})" if preset else "")
    segments = face_segments(sed, ch, start, start + frames)
    if segments:
        # 表情ストリップと重なる: 区間ごとに表情を変えた連番を faces/ に並べる
        caches = {p: character_cache(context, ch, p) for _, _, p in segments}
        paths = blink_segment_frames([(0, frames, cache)] + [(a, b, caches[p]) for a, b, p in segments],
                                     frames, scene_fps(scene), seed=(ch.name, start))
        # フォルダはキャラ・位置・長さごと（同じ区間の別の立ち絵と連番を取り合わないように）
        strip = _strip_from_paths(context, sed, ch, name, start, paths, "faces",
                                  json.dumps([ch.name, start, frames, _segments_key(segments, preset)],
                                             ensure_ascii=False))
    else:
        strip = _blink_strip(sed, ch, cache, name, start, frames, scene_fps(scene))
    strip["tachie_faced"] = _segments_key(segments, preset) if segments else ""
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
        text, kana = split_kana(settings.text.strip())
        if ch is None:
            self.report({"ERROR"}, "キャラクターを追加してください")
            return {"CANCELLED"}
        if not text:
            self.report({"ERROR"}, "セリフを入力してください")
            return {"CANCELLED"}
        ensure_sequencer_scene(context)
        start = seq_scene(context).frame_current
        try:
            clip, lines_dir = render_clip(context, ch, text, settings.expression, kana=kana)
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
            c = client(context)
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
        play_preview(context, wav)
        return {"FINISHED"}


def play_preview(context, wav: bytes) -> None:
    """合成した wav を鳴らす（前の試聴は止める）。"""
    global _preview
    import aud

    path = output_dir(context) / "preview.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    if _preview is not None:
        _preview.stop()
    path.write_bytes(wav)
    _preview = aud.Device().play(aud.Sound(str(path)).cache())


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


def _fresh_accent(context, text: str, speaker: int, kana: str | None) -> list:
    c = client(context)
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
                phrases = line_accent(strips, text, kana, speaker) or _fresh_accent(context, text, speaker, kana)
            else:  # 自動の読みは今のもので確かめる（ユーザー辞書で変わることがある）
                query = client(context).audio_query(text, speaker)
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
            c = client(context)
            query = c.audio_query(edit.text, edit.speaker)
            query.update(line_voice(strips, ch))
            query["accent_phrases"] = accent_from_edit(edit)
            wav = c.synthesis(query, edit.speaker)
        except (*ERRORS, IndexError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        play_preview(context, wav)
        return {"FINISHED"}


class TACHIE_OT_reload_accent(bpy.types.Operator):
    """編集中の抑揚を、発音から自動で付けたものに戻す"""

    bl_idname = "tachie.reload_accent"
    bl_label = "抑揚を取り直す"

    def execute(self, context):
        edit = context.window_manager.tachie_accent
        try:
            load_accent_edit(edit, _fresh_accent(context, edit.text, edit.speaker, edit.kana or None))
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


def _free_face_channel(sed, settings, start: int, end: int) -> int:
    channel = settings.subtitle_channel + 1
    while channel < 128 and any(s.channel == channel and s.left_handle < end and start < s.right_handle
                                for s in sed.strips):
        channel += 1
    return channel


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
        ensure_sequencer_scene(context)
        sed = scene.sequence_editor or scene.sequence_editor_create()
        start = scene.frame_current
        # 再生位置にこのキャラのセリフがあれば、その終わりまで
        line = next((s for s in sed.strips if s.type == "IMAGE" and s.get("tachie_id")
                     and s.get("tachie_character") == ch.name and s.left_handle <= start < s.right_handle), None)
        end = line.right_handle if line else start + max(1, round(settings.face_seconds * scene_fps(scene)))
        preset = settings.expression
        face = sed.strips.new_effect(name=_face_name(ch.name, preset), type="COLOR",
                                     channel=_free_face_channel(sed, settings, start, end),
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
            _applied_signatures[scene.name] = faces_signature(scene)
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
            _applied_signatures[seq_scene(context).name] = faces_signature(seq_scene(context))
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

    @classmethod
    def poll(cls, context):
        ids, stills, faces = selected_targets(seq_scene(context).sequence_editor)
        return len(ids) + len(stills) + len(faces) > 0

    def invoke(self, context, event):
        global _batch_styles
        sed = seq_scene(context).sequence_editor
        ids, stills, faces = selected_targets(sed)
        _batch_mixed.clear()
        if ids:
            try:
                _batch_styles = [(str(i), name, "", i) for i, name in
                                 VoicevoxClient(prefs(context).voicevox_url, timeout=3.0).styles()]
            except (VoicevoxError, OSError):
                _batch_styles = []
            settings = seq_scene(context).tachie
            values = []
            for cid in ids:
                strips = line_strips(sed, cid)
                ch = next((c for c in settings.characters if c.name == strips[0]["tachie_character"]), None)
                if ch is None:
                    continue  # キャラクターを消したセリフ（execute で「変えられなかった」に数える）
                sound = line_sound(strips) or strips[0]
                v = {"character": ch.name, "speaker": str(sound.get("tachie_speaker", ch.speaker)),
                     "expression": strips[0].get("tachie_expression", "") or NONE}
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
        for name in ["expression", "character", "speaker", *VOICE_PROPS, *(f"audio_{k}" for k in AUDIO_PROPS)]:
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
        if preset is None and not (voice_changes or audio_changes or self.use_character or use_speaker):
            self.report({"WARNING"}, "変える項目がありません（値を変えるか、左のチェックを入れてください）")
            return {"CANCELLED"}
        mix_only = set(audio_changes) <= {"volume", "pan"}
        regen = preset is not None or self.use_character or use_speaker or bool(voice_changes) or not mix_only
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
                try:
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
            _applied_signatures[scene.name] = faces_signature(scene)
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
            material = load_material_defaults(character.path)
            saved = load_character_defaults().get(key, {})
            # 優先: アドオン設定 → 素材の tachie.toml → 素材の preset.ini → 固定値
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
        has_toml = (Path(character.path) / MATERIAL_CONFIG).is_file()
        has_ini = (Path(character.path) / PRESET_FILE).is_file()
        sources = [label for label, used in (("アドオン設定", bool(saved)), (MATERIAL_CONFIG, has_toml),
                                             (PRESET_FILE, has_ini)) if used]
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


class TACHIE_OT_import_presets(bpy.types.Operator, ImportHelper):
    """YMM4 の表情プリセット（素材フォルダの preset.ini）を、選択中のキャラクターの表情プリセットに読み込む
    （同じ名前は上書き）"""

    bl_idname = "tachie.import_presets"
    bl_label = "YMM4 の表情プリセットを読み込む"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".ini"
    filter_glob: bpy.props.StringProperty(default="*.ini", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return ch is not None and bool(ch.folder)

    def invoke(self, context, event):
        ch = seq_scene(context).tachie.character()
        self.filepath = str(Path(bpy.path.abspath(ch.folder)) / PRESET_FILE)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        ch = seq_scene(context).tachie.character()
        try:
            presets, warnings = load_presets(bpy.path.abspath(ch.folder), self.filepath)
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
            for category, value in parts.items():
                for prop, v in zip(PART_PROPS[category], [value] if isinstance(value, str) else value):
                    setattr(item, prop, v)
        message = f"{len(presets)} 個の表情プリセットを読み込みました"
        if warnings:
            message += f"（{len(warnings)} 件飛ばしました: {' / '.join(warnings[:3])}{' …' if len(warnings) > 3 else ''}）"
            print("\n".join(warnings))
        self.report({"WARNING"} if warnings else {"INFO"}, message)
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
                clip, lines_dir = render_clip(context, ch, line.text, line.expression, kana=line.kana)
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
    TACHIE_OT_pick_speaker,
    TACHIE_OT_pick_part,
    TACHIE_OT_pick_font,
    TACHIE_OT_reload_character,
    TACHIE_OT_import_characters,
    TACHIE_OT_import_presets,
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
