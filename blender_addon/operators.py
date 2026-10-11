import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .properties import PART_PROPS, SLOT_CATEGORIES, apply_placement, apply_subtitle, system_fonts
from .tachie_studio.pipeline import CharacterConfig, FrameCache, Line, idle_frames, load_characters, parse_script, render_line
from .tachie_studio.pipeline.config import dump_characters, parse_characters
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
    base = parse_characters({ch.name: {"folder": "", "speaker": ch.speaker, "parts": _parts_dict(ch)}}, Path("."))
    return CharacterConfig(
        name=ch.name, folder=Path(bpy.path.abspath(ch.folder)), speaker=ch.speaker,
        expression=base[ch.name].expression, mouth=ch.mouth,
        expressions={e.name: _parts_dict(e) for e in ch.expressions},
        layout={"position": ch.position_x, "flip": ch.flip_x, "height": ch.height, "channel": ch.channel},
        subtitle={"font": ch.font_path, "size": ch.font_size, "color": list(ch.text_color),
                  "outline_color": list(ch.outline_color), "outline_width": ch.outline_width,
                  "y": ch.subtitle_y},
    )


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


def render_clip(context, ch, text: str, preset: str | None = None):
    """セリフを合成して書き出す。(RenderedClip, 書き出し先フォルダ) を返す。"""
    cache = character_cache(context, ch, preset)
    config = CharacterConfig(name=ch.name, folder=cache.character.path, speaker=ch.speaker,
                             expression=cache.expression, mouth=ch.mouth)
    clip_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    lines_dir = output_dir(context) / "lines"
    clip = render_line(client(context), config, cache.character, Line(0, ch.name, text, preset or None), lines_dir,
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


def place_strips(context, ch, clip, lines_dir: Path, text: str, start: int) -> list:
    """書き出したセリフを start に置く。置いたストリップ（音声・立ち絵・字幕）を返す。"""
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
        s["tachie_character"] = ch.name
        s["tachie_speaker"] = ch.speaker
        s["tachie_folder"] = ch.folder
        s["tachie_mouth"] = clip.extra.get("mouth_mode", "")
        s["tachie_expression"] = clip.extra.get("expression", "")
    return strips


def shift_after(sed, frame: int, delta: int, ignore=()) -> None:
    """frame 以降に始まるストリップを delta フレームずらす。重ならない順に動かす。"""
    targets = [s for s in sed.strips if s.left_handle >= frame and s not in ignore and not s.get("tachie_idle")]
    targets.sort(key=lambda s: s.left_handle, reverse=delta > 0)
    for s in targets:
        s.frame_start += delta


def regenerate(context, clip_id: str, text: str | None = None, preset: str | None = None) -> list:
    """セリフを作り直して同じ位置に置き直す。新しいストリップを返す。"""
    settings = seq_scene(context).tachie
    sed = seq_scene(context).sequence_editor
    old = line_strips(sed, clip_id)
    if not old:
        raise ValueError("セリフのストリップが見つかりません")
    text = text if text is not None else old[0]["tachie_text"]
    preset = preset if preset is not None else old[0].get("tachie_expression", "")
    ch = find_character(settings, old[0]["tachie_character"])
    start = min(s.left_handle for s in old)
    old_end = max(s.right_handle for s in old)

    clip, lines_dir = render_clip(context, ch, text, preset)
    delta = start + clip.frames - old_end
    # 消す前に置けるか確かめる（ずらすストリップは邪魔にならない）
    movers = [s for s in sed.strips if settings.ripple and delta and s.left_handle >= old_end]
    check_free(context, ch, start, clip.frames, ignore=old + movers)

    for s in old:
        sed.strips.remove(s)
    remove_idle(sed)
    if settings.ripple and delta:
        shift_after(sed, old_end, delta)
    return place_strips(context, ch, clip, lines_dir, text, start)


def remove_idle(sed) -> None:
    for s in [s for s in sed.strips if s.get("tachie_idle")]:
        sed.strips.remove(s)


def refresh_idle(context) -> int:
    """待機ストリップを作り直す。各キャラの立ち絵チャンネルの隙間を、シーンの開始から最後のセリフの
    終わりまで、まばたきだけの立ち絵で埋める。置いた本数を返す。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor
    if sed is None:
        return 0
    remove_idle(sed)
    line_ends = [s.right_handle for s in sed.strips if s.get("tachie_id")]
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
            paths = idle_frames(cache, b - a, fps, seed=(ch.name, a))
            strip = sed.strips.new_image(name=f"待機 {ch.name}", filepath=_path_for_blend(paths[0]),
                                         channel=ch.channel, frame_start=a, fit_method="ORIGINAL")
            for p in paths[1:]:
                strip.elements.append(p.name)
            strip.blend_type = "ALPHA_OVER"
            apply_placement(strip, ch)
            strip["tachie_idle"] = ch.name
            count += 1
    return count


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


class TACHIE_OT_regenerate_all(bpy.types.Operator):
    """すべてのセリフを、今のキャラクター設定で作り直す（表情や話者を変えたとき）"""

    bl_idname = "tachie.regenerate_all"
    bl_label = "全セリフを再生成"
    bl_options = {"REGISTER", "UNDO"}

    character_only: bpy.props.BoolProperty(name="選択中のキャラクターだけ", default=False)

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
        # 後ろから作り直すと、長さが変わってずらしても未処理のセリフの位置を追いかけなくてよい
        count = 0
        wm = context.window_manager
        wm.progress_begin(0, len(firsts))
        try:
            for cid in sorted(firsts, key=firsts.get, reverse=True):
                connect(context, regenerate(context, cid))
                count += 1
                wm.progress_update(count)
        except ERRORS as e:
            self.report({"ERROR"}, f"{count} 件作り直したところで失敗しました: {e}")
            return {"FINISHED"} if count else {"CANCELLED"}
        finally:
            wm.progress_end()
            if count and settings.auto_idle:
                refresh_idle(context)
        self.report({"INFO"}, f"{count} 件のセリフを作り直しました")
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
        except ERRORS as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        ch = settings.characters.add()
        ch.name = character.name
        ch.folder = self.directory
        # 2 人目以降は左右を入れ替え、別のチャンネルに置く
        n = len(settings.characters)
        if n > 1:
            ch.position_x = -0.3 if n % 2 == 0 else 0.3
            used = {c.channel for c in settings.characters if c != ch}
            used |= {settings.audio_channel, settings.subtitle_channel}
            ch.channel = next(c for c in range(min(used | {3}), 129) if c not in used)
        settings.active_character = n - 1
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

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

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
        ch = seq_scene(context).tachie.character()
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

    def execute(self, context):
        _characters.clear()
        _caches.clear()
        load_image.cache_clear()
        shutil.rmtree(output_dir(context) / "cache", ignore_errors=True)
        # 待機ストリップはキャッシュの画像を直接指しているので作り直す
        try:
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
            if placed and settings.auto_idle:
                refresh_idle(context)
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

    target: bpy.props.EnumProperty(items=[("ADD", "追加するセリフ", ""), ("LINE", "選択中のセリフ", "")])
    expression: bpy.props.EnumProperty(items=_expressions)

    def _character(self, context):
        settings = seq_scene(context).tachie
        if self.target == "LINE":
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
        # 選択中のセリフはその表情で作り直す
        try:
            strips = regenerate(context, active_line_id(context), preset=value)
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
    TACHIE_OT_import_script,
    TACHIE_OT_pick_expression,
    TACHIE_OT_expression_add,
    TACHIE_OT_expression_remove,
    TACHIE_OT_refresh_idle,
    TACHIE_OT_regenerate,
    TACHIE_OT_regenerate_all,
    TACHIE_OT_character_add,
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
