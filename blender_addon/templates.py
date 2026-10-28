"""テンプレート: 選んだストリップを名前付きで登録し（アドオン設定、全 .blend 共通）、再生位置に置く。YMM4 のテンプレート
（ItemSettings.json）も読み込める。形式は `tachie_studio.templates`。

セリフは置くときに合成し直す（テキスト・キャラ・表情・声・発音・抑揚・形を持つ）。画像・動画・音声・テキスト・カラーは
YMM4 の読み込みと同じ `ymmp_media.MediaPlacer` で置く（座標は YMM4 と同じ形で持つ）。
"""

from __future__ import annotations

import math
import os
from collections import Counter
from pathlib import Path

import bpy
from bpy_extras.io_utils import ImportHelper

from .motion import _channelbag, jump_of
from .operators import (
    ERRORS, _applied_signatures, _still_overlaps, auto_signature, new_face, preset_parts, strip_parts,
    character_audio, character_voice, get_pose, place_still, item_still_pose, still_settings,
    ensure_sequencer_scene, line_accent, line_audio, line_channels, line_end, line_kana, line_layout,
    line_layout_of, line_sound, line_strips, line_subtitle, line_text, line_texts, line_voice, place_strips, prefs,
    refresh_faces, render_clip, scene_fps, seq_scene, set_line_text_pose,
)
from .properties import text_offset, text_style
from .tachie_studio.templates import (
    dumps, end_frame, load_ymm4_templates, loads, media_from_dict, media_to_dict, normalize,
)
from .tachie_studio.pipeline.config import merge_parts
from .tachie_studio.ymmp import Media, Project
from . import channels as lanes
from .ymmp_media import MediaFinder, MediaPlacer, box_height, media_root, place_transition
from .transitions import MARK, record_of

REPORT_TEXT = "YMM4 のテンプレートの読み込み結果"


# ---- 登録（ストリップ → テンプレート） ----

def _keys(scene, strip, attr: str) -> tuple[float, float] | None:
    """`attr` の F カーブの最初と最後のキーの値（2 つ以上あるとき）。"""
    bag = _channelbag(scene)
    if bag is None:
        return None
    path = strip.path_from_id(attr) if "." not in attr else strip.path_from_id() + "." + attr
    fcurve = next((f for f in bag.fcurves if f.data_path == path), None)
    if fcurve is None or len(fcurve.keyframe_points) < 2:
        return None
    return fcurve.keyframe_points[0].co[1], fcurve.keyframe_points[-1].co[1]


def _visual(scene, strip, m: Media) -> Media:
    """画像・動画（スピードコントロール）の transform・不透明度を YMM4 の座標にする（`MediaPlacer` の逆）。"""
    t = strip.transform
    m.x, m.y = t.offset_x, -t.offset_y
    m.zoom = t.scale_x * 100
    m.rotation = -math.degrees(t.rotation)
    m.opacity = strip.blend_alpha * 100
    for attr, key, conv in (("transform.offset_x", "x", lambda v: v), ("transform.offset_y", "y", lambda v: -v),
                            ("transform.scale_x", "zoom", lambda v: v * 100),
                            ("transform.rotation", "rotation", lambda v: -math.degrees(v)),
                            ("blend_alpha", "opacity", lambda v: v * 100)):
        keys = _keys(scene, strip, attr)
        if keys:
            m.anim[key] = (conv(keys[0]), conv(keys[1]), "直線移動")
    return m


def _base_point(strip) -> str:
    h = {"LEFT": "Left", "CENTER": "Center", "RIGHT": "Right"}.get(strip.anchor_x, "Left")
    v = {"TOP": "Top", "CENTER": "Center", "BOTTOM": "Bottom"}.get(strip.anchor_y, "Top")
    return h + v


def _font_path(font) -> str:
    """テキストのフォントのファイル（Blender の内蔵フォント "<builtin>" や未設定は ""）。"""
    if font is None or not font.filepath or font.filepath.startswith("<"):
        return ""
    return bpy.path.abspath(font.filepath)


def _media_of(scene, strip, t0: int, fps: float, speed_of: dict) -> Media | None:
    """ストリップ → テンプレートの Media（対応しないものは None）。"""
    render = scene.render
    frame, length = strip.left_handle - t0, strip.right_handle - strip.left_handle
    layer = strip.channel
    if strip.type == "IMAGE":
        path = Path(bpy.path.abspath(strip.directory)) / strip.elements[0].filename
        return _visual(scene, strip, Media(kind="image", frame=frame, length=length, layer=layer, file=str(path)))
    if strip.type == "SPEED" and strip in speed_of.values():
        movie = strip.input_1
        src_fps = movie.fps or fps
        keys = _keys(scene, strip, "speed_frame_number")
        first, last = keys if keys else (strip.speed_frame_number, strip.speed_frame_number)
        seconds = max(1, length) / fps
        m = Media(kind="video", frame=frame, length=length, layer=layer, file=bpy.path.abspath(movie.filepath),
                  offset=first / src_fps, rate=(last - first) / src_fps / seconds * 100 if last != first else 100.0,
                  volume=0.0)
        return _visual(scene, strip, m)
    if strip.type == "MOVIE":
        if strip in speed_of:
            return None  # スピードコントロールの方で持つ
        src_fps = strip.fps or fps
        m = Media(kind="video", frame=frame, length=length, layer=layer, file=bpy.path.abspath(strip.filepath),
                  offset=(strip.left_handle - strip.frame_start) / src_fps, rate=fps / src_fps * 100, volume=0.0)
        return _visual(scene, strip, m)
    if strip.type == "SOUND":
        m = Media(kind="audio", frame=frame, length=length, layer=layer,
                  file=bpy.path.abspath(strip.sound.filepath), offset=(strip.left_handle - strip.frame_start) / fps,
                  volume=strip.volume * 50)
        keys = _keys(scene, strip, "volume")
        if keys:
            m.anim["volume"] = (keys[0] * 50, keys[1] * 50, "直線移動")
        return m
    if strip.type == "TEXT":
        m = Media(kind="text", frame=frame, length=length, layer=layer, text=strip.text,
                  font=_font_path(strip.font),
                  font_size=strip.font_size, font_color=tuple(strip.color), outline=strip.use_outline,
                  outline_color=tuple(strip.outline_color), bold=strip.use_bold, base_point=_base_point(strip),
                  x=(strip.location[0] - 0.5) * render.resolution_x, y=(0.5 - strip.location[1]) * render.resolution_y,
                  opacity=strip.blend_alpha * 100, line_spacing=strip.space_line)
        if strip.use_box:  # 背景の帯（置くときの逆）
            m.background = tuple(strip.box_color)
            m.background_margin = strip.box_margin * render.resolution_x
            m.background_round = strip.box_roundness * box_height(strip.font_size, strip.text, m.background_margin, strip.space_line)
        return m
    if strip.type == "COLOR":
        return Media(kind="color", frame=frame, length=length, layer=layer, color=(*strip.color, 1.0),
                     opacity=strip.blend_alpha * 100)
    return None


def template_from_strips(context, strips) -> tuple[dict, bool, list[str]]:
    """選んだストリップ → (テンプレート, 立ち絵より奥か, 入れなかったもの)。"""
    scene = seq_scene(context)
    settings = scene.tachie
    sed = scene.sequence_editor
    fps = scene_fps(scene)
    t0 = min(s.left_handle for s in strips)
    # 写せるのはフレーム番号のスピードコントロール（YMM4 の読み込み・テンプレートで置くもの）だけ。他（伸縮・倍率など）は
    # 元の動画だけを入れる
    speed_of = {s.input_1: s for s in sed.strips if s.type == "SPEED" and s.input_1 is not None
                and s.input_1.type == "MOVIE" and s.speed_control == "FRAME_NUMBER"}
    items: list[dict] = []
    skipped: list[str] = []
    done_lines: set[str] = set()
    visual_channels = []
    for s in sorted(strips, key=lambda s: (s.left_handle, s.channel)):
        cid = s.get("tachie_id")
        if cid:
            if cid in done_lines:
                continue
            done_lines.add(cid)
            group = line_strips(sed, cid)
            ch = next((c for c in settings.characters if c.name == group[0].get("tachie_character")), None)
            if ch is None:
                skipped.append(f"キャラクターの無いセリフ「{group[0].get('tachie_text', '')[:20]}」")
                continue
            sound = line_sound(group) or group[0]
            text = line_text(group)
            kana = line_kana(group, text)
            speaker = int(sound.get("tachie_speaker", ch.speaker))
            accent = line_accent(group, text, kana, speaker)
            subtitle = line_subtitle(group)
            left = min(x.left_handle for x in group)
            items.append({
                "kind": "line", "frame": left - t0, "length": line_end(group) - left, "layer": s.channel,
                "character": ch.name, "text": text, "kana": kana, "accent": accent,
                "accent_kana": (kana or sound.get("tachie_kana_auto", "")) if accent else None, "style": speaker,
                "voice": line_voice(group, ch), "audio": {**line_audio(group, ch), "volume": sound.volume,
                                                          "pan": sound.pan},
                "expression": group[0].get("tachie_expression", ""), "look": strip_parts(sound, ch),
                "layout": line_layout_of(group),
                "subtitle_offset": list(text_offset(subtitle)) if subtitle is not None else None,
                "subtitle_style": list(text_style(subtitle)) if subtitle is not None else None,
                "subtitle_hidden": any(t.mute for t in line_texts(group)), "face_parts": None})
        elif s.get("tachie_face"):
            face_ch = next((c for c in settings.characters if c.name == s["tachie_face"]), None)
            items.append({"kind": "face", "frame": s.left_handle - t0, "length": s.right_handle - s.left_handle,
                          "layer": s.channel, "character": s["tachie_face"],
                          "expression": s.get("tachie_expression", ""), "parts": None,
                          **({"look": strip_parts(s, face_ch)} if face_ch is not None else {})})
        elif s.get("tachie_still"):
            still_ch = next((c for c in settings.characters if c.name == s["tachie_still"]), None)
            items.append({"kind": "still", "frame": s.left_handle - t0, "length": s.right_handle - s.left_handle,
                          "layer": s.channel, "character": s["tachie_still"],
                          "expression": s.get("tachie_expression", ""), "parts": None, "pose": get_pose(s),
                          "flip": s.get("tachie_flip"), "settings": still_settings(s), "jump": jump_of(s),
                          **({"look": strip_parts(s, still_ch)} if still_ch is not None else {})})
        elif s.get("tachie_idle"):
            skipped.append(f"前の版の待機の立ち絵（{s.name}）")
        elif s.get(MARK) is not None:  # 場面転換の目印 → 場面転換（付けた前の場面・新しい場面のキーは入れない）
            state = record_of(scene, s)
            if state is None:
                skipped.append(f"{s.name}（記録の無い場面転換の目印）")
                continue
            items.append({**state["tr"].to_dict(), "kind": "transition", "frame": s.left_handle - t0,
                          "length": s.right_handle - s.left_handle, "layer": s.channel})
        else:
            m = _media_of(scene, s, t0, fps, speed_of)
            if m is None:
                if s.type == "SPEED":
                    skipped.append(f"{s.name}（フレーム番号以外の速度は写せないので、元の動画を選んでください）")
                elif not (s.type == "MOVIE" and s in speed_of):
                    skipped.append(f"{s.name}（{s.type}）")
                continue
            if s.type == "IMAGE" and len(s.elements) > 1:
                skipped.append(f"{s.name} の連番（最初の 1 枚だけ入れました）")
            if s.get("tachie_bgm"):
                skipped.append(f"{s.name} の BGM の印（音声として入れました）")
            items.append({"kind": "media", "media": media_to_dict(m)})
            if m.kind in ("image", "video", "color"):
                visual_channels.append(s.channel)
    normalize(items)
    behind = bool(visual_channels) and max(visual_channels) < settings.channel_base  # レーンより下＝立ち絵より奥
    render = scene.render
    return {"fps": fps, "width": render.resolution_x, "height": render.resolution_y, "items": items}, behind, skipped


# ---- 置く ----

def _item_look(ch, item: dict, preset: str, problems: list[str]) -> dict | None:
    """アイテムの表情のパーツ。登録したもの（`look`）はそのまま、YMM4 のパーツは `_ymm4_look`、前の版のテンプレートの
    立ち絵ごとのパーツ（`custom_parts`）はプリセットのパーツに重ねる。None ならプリセット `preset` のパーツ（置く側で写す）。"""
    if isinstance(item.get("look"), dict):
        return item["look"]
    ymm4 = _ymm4_look(ch, item, problems)
    if ymm4 is not None:
        return ymm4
    if item["kind"] == "still" and item.get("custom_parts"):
        return merge_parts(preset_parts(ch, preset), item["custom_parts"])
    return None


def _resolve_expression(ch, item: dict, problems: list[str]) -> str:
    """セリフ・表情ストリップ・立ち絵の表示用のプリセットの名前（キャラに無ければ ""）。"""
    preset = item.get("expression") or ""
    if preset and preset not in {e.name for e in ch.expressions}:
        if not isinstance(item.get("look"), dict):
            problems.append(f"{ch.name} に表情「{preset}」が無いので基本の表情にしました")
        return ""
    return preset


def _ymm4_look(ch, item: dict, problems: list[str]) -> dict | None:
    """YMM4 のテンプレートのパーツ（ボイス `face_parts`・表情アイテム・立ち絵アイテムの `parts`）→ アイテムのパーツ。
    立ち絵アイテムは全部のパーツなのでキャラの基本との差分に、ボイス・表情アイテムは変えるパーツだけ（重なる立ち絵の
    上に重なる。素材に無いパーツは落として立ち絵のまま）。パーツの指定が無いものは {}（立ち絵アイテムはキャラの基本の表情、
    表情アイテムは立ち絵のまま。.ymmp の読み込みと同じ）。YMM4 のアイテムでなければ None。"""
    parts = item.get("face_parts") if item["kind"] == "line" else item.get("parts")
    if not isinstance(parts, dict) or not ch.folder:
        return None
    if not any(parts.values()):
        return {}
    from .ymmp_import import _base_parts, _valid_parts, diff_parts

    if item["kind"] == "still":
        return diff_parts(ch, _valid_parts(ch, parts, problems, _base_parts(ch)), problems)
    return _valid_parts(ch, parts, problems)


def _still_pose(ch, item: dict, data: dict) -> dict | None:
    """立ち絵の位置・大きさ（立ち絵ごとの値）。登録した立ち絵は `pose`、YMM4 の立ち絵アイテムは X / Y / Zoom から。"""
    if item.get("pose"):
        return item["pose"]
    if "x" not in item:
        return None
    return item_still_pose(ch, item["x"], item["y"], item.get("zoom", 100.0),
                          float(data.get("width") or 1920), float(data.get("height") or 1080))


def insert_template(context, data: dict, behind: bool) -> tuple[Counter, list[str]]:
    """テンプレートを再生位置に置く。(置いた数, 問題) を返す。"""
    scene = seq_scene(context)
    settings = scene.tachie
    ensure_sequencer_scene(context)
    sed = scene.sequence_editor or scene.sequence_editor_create()
    fps = scene_fps(scene)
    src_fps = float(data.get("fps") or fps)
    start = scene.frame_current

    def frame(f: int) -> int:
        return start + round(f * fps / src_fps)

    placed: Counter = Counter()
    problems: list[str] = []
    items = data.get("items", [])
    characters = {c.name: c for c in settings.characters}
    # 映像・音声（先に置く。立ち絵より奥のものはアドオンのチャンネルをずらすことがある）
    media = [media_from_dict(i["media"]) for i in items if i["kind"] == "media"]
    if media:
        project = Project(path=Path("."), fps=src_fps, width=int(data.get("width") or scene.render.resolution_x),
                          height=int(data.get("height") or scene.render.resolution_y), length=end_frame(data),
                          media=media)
        # 元のパスに無ければ「画像・動画などを探す場所」→ .blend のフォルダの下。未保存なら探す場所だけ
        # （bpy.path.abspath("//") は "" になり、カレントディレクトリ（/ のこともある）の下を探してしまう）
        root = media_root(settings)
        finder = MediaFinder(Path(bpy.path.abspath("//")) / "_" if bpy.data.filepath else None, root)
        from .ymmp_import import find_font

        placer = MediaPlacer(context, project, finder, frame, find_font, behind=behind)
        try:
            placer.place()
        except ValueError as e:
            problems.append(str(e))
        placed.update(placer.placed)
        problems += placer.problems + [f"見つからないファイル: {n}" for n in placer.missing]
        if placer.missing:
            problems.append("素材のある場所をテンプレートの「素材を探す場所」に指定すると、そこから探します")
    # 場面転換（テンプレートの映像を置いたあとで、境目で終わる・始まる画像・動画を探す）
    for item in (i for i in items if i["kind"] == "transition"):
        a = frame(item["frame"])
        if place_transition(context, item, a, max(a + 1, frame(item["frame"] + item["length"])), problems):
            placed["transition"] += 1
    # セリフ
    lines = sorted((i for i in items if i["kind"] == "line"), key=lambda i: i["frame"])
    for n, item in enumerate(lines):
        ch = characters.get(item["character"])
        if ch is None:
            problems.append(f"キャラクター「{item['character']}」が無いので「{item['text'][:20]}」を置きませんでした")
            continue
        at = frame(item["frame"])
        layout = line_layout(ch, item.get("layout"))
        audio = {**character_audio(ch), **(item.get("audio") or {})}  # 無い値（余韻など）はキャラの既定値
        voice = {**character_voice(ch), **{k: v for k, v in (item.get("voice") or {}).items()
                                           if k in character_voice(ch)}}
        try:
            preset = _resolve_expression(ch, item, problems)
            clip, lines_dir = render_clip(context, ch, item["text"], preset or None, speaker=item.get("style"),
                                          voice=voice, kana=item.get("kana") or None, accent=item.get("accent"),
                                          accent_kana=item.get("accent_kana"), audio=audio)
            # 同じキャラの次のセリフに重ならないように余韻を縮める（合成し直した声の長さや fps の丸めで伸びることがある）
            nxt = next((w for w in lines[n + 1:] if w["character"] == item["character"] and w["frame"] > item["frame"]),
                       None)
            excess = at + clip.frames - frame(nxt["frame"]) if nxt else 0
            if excess > 0 and audio.get("after", 0.0) > 0:
                audio["after"] = math.floor(max(0.0, audio["after"] - excess / fps) * 10000) / 10000
                clip, lines_dir = render_clip(context, ch, item["text"], preset or None, speaker=item.get("style"),
                                              voice=voice, kana=item.get("kana") or None, accent=item.get("accent"),
                                              accent_kana=item.get("accent_kana"), audio=audio)
            channels = line_channels(context, ch, at, clip.frames, layout=layout)
        except ERRORS as e:
            problems.append(f"「{item['text'][:20]}」: {e}")
            continue
        strips = place_strips(context, ch, clip, lines_dir, item["text"], at, voice=voice, accent=item.get("accent"),
                              audio=audio, channels=channels, layout=layout,
                              parts=_item_look(ch, item, preset, problems) if ch.folder else None)
        sound = strips[0]
        sound.volume = audio.get("volume", sound.volume)
        sound.pan = max(-1.0, min(1.0, audio.get("pan", sound.pan)))
        if item.get("subtitle_offset") or item.get("subtitle_style"):  # 位置・文字の大きさ・折り返し幅
            set_line_text_pose(strips, ch, item["text"], item.get("subtitle_offset"), item.get("subtitle_style"))
        for t in line_texts(strips):
            t.mute = bool(item.get("subtitle_hidden"))
        placed["line"] += 1
    # 表情ストリップ
    for item in (i for i in items if i["kind"] == "face"):
        ch = characters.get(item["character"])
        if ch is None or not ch.folder:
            problems.append(f"キャラクター「{item['character']}」（立ち絵あり）が無いので表情ストリップを置きませんでした")
            continue
        try:
            preset = _resolve_expression(ch, item, problems)
        except ERRORS as e:
            problems.append(f"{ch.name} の表情ストリップ: {e}")
            continue
        a, b = frame(item["frame"]), max(frame(item["frame"]) + 1, frame(item["frame"] + item["length"]))
        new_face(sed, settings, ch.name, preset, a, b, _item_look(ch, item, preset, problems))
        placed["face"] += 1
    # 立ち絵（同じキャラの立ち絵に重なるものは置かない）
    for item in (i for i in items if i["kind"] == "still"):
        ch = characters.get(item["character"])
        if ch is None or not ch.folder:
            problems.append(f"キャラクター「{item['character']}」（立ち絵あり）が無いので立ち絵を置きませんでした")
            continue
        a, b = frame(item["frame"]), max(frame(item["frame"]) + 1, frame(item["frame"] + item["length"]))
        if _still_overlaps(sed, ch, a, b):
            problems.append(f"{ch.name} の立ち絵（{a}〜{b} フレーム）は、置いてある立ち絵と重なるので置きませんでした")
            continue
        try:
            preset = _resolve_expression(ch, item, problems)
            flip = item.get("flip")  # YMM4 と同じく立ち絵ごと。キャラの設定と同じならキャラに従う
            place_still(context, ch, preset, a, b - a, _still_pose(ch, item, data), problems=problems,
                        flip=None if flip is None or bool(flip) == ch.flip_x else bool(flip), jump=item.get("jump"),
                        settings=item.get("settings"), parts=_item_look(ch, item, preset, problems))
        except ERRORS as e:
            problems.append(f"{ch.name} の立ち絵: {e}")
            continue
        placed["still"] += 1
    end = frame(end_frame(data))
    if end - 1 > scene.frame_end:
        scene.frame_end = end - 1
    from . import operators

    operators._applying_faces = True
    try:
        refresh_faces(context, problems)  # 重なる立ち絵の口パク・動き・ダッキングも作り直す
    finally:
        operators._applying_faces = False
        _applied_signatures[scene.name] = auto_signature(scene)
    return placed, problems


# ---- オペレーター ----

LABELS = {"line": "セリフ", "face": "表情", "still": "立ち絵", "image": "画像", "video": "動画", "audio": "音声", "text": "テキスト",
          "color": "カラー", "video_sound": "動画の音", "transition": "場面転換"}


class TACHIE_OT_register_template(bpy.types.Operator):
    """選択中のストリップ（セリフ・表情・立ち絵・画像・動画・音声・テキスト・カラー）をテンプレートとして登録する（全 .blend 共通）"""

    bl_idname = "tachie.register_template"
    bl_label = "選択中のストリップを登録"
    bl_options = {"REGISTER"}

    name: bpy.props.StringProperty(name="名前", default="")

    @classmethod
    def poll(cls, context):
        sed = seq_scene(context).sequence_editor
        return sed is not None and any(s.select for s in sed.strips)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        name = self.name.strip()
        if not name:
            self.report({"ERROR"}, "名前を入れてください")
            return {"CANCELLED"}
        strips = [s for s in seq_scene(context).sequence_editor.strips if s.select]
        data, behind, skipped = template_from_strips(context, strips)
        if not data["items"]:
            self.report({"ERROR"}, "登録できるストリップがありません" + (f"（{skipped[0]}）" if skipped else ""))
            return {"CANCELLED"}
        templates = prefs(context).templates
        item = next((t for t in templates if t.name == name), None)
        replaced = item is not None
        item = item or templates.add()
        item.name, item.data, item.behind = name, dumps(data), behind
        context.preferences.is_dirty = True
        message = f"テンプレート「{name}」を{'上書き' if replaced else '登録'}しました（{len(data['items'])} 個）"
        if skipped:
            message += f"。入れなかったもの: {' / '.join(skipped[:3])}{' …' if len(skipped) > 3 else ''}"
        self.report({"WARNING"} if skipped else {"INFO"}, message)
        return {"FINISHED"}


class TACHIE_OT_insert_template(bpy.types.Operator):
    """テンプレートを再生位置に置く"""

    bl_idname = "tachie.insert_template"
    bl_label = "テンプレートを置く"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        templates = prefs(context).templates
        if not 0 <= self.index < len(templates):
            return {"CANCELLED"}
        template = templates[self.index]
        try:
            placed, problems = insert_template(context, loads(template.data), template.behind)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        summary = "、".join(f"{LABELS.get(k, k)} {n}" for k, n in placed.items()) or "何も置けませんでした"
        if problems:
            print("\n".join(problems))
            self.report({"WARNING"}, f"「{template.name}」: {summary}。{problems[0]}"
                                     + (f"（ほか {len(problems) - 1} 件）" if len(problems) > 1 else ""))
        else:
            self.report({"INFO"}, f"「{template.name}」を置きました（{summary}）")
        return {"FINISHED"} if placed else {"CANCELLED"}


class TACHIE_OT_remove_template(bpy.types.Operator):
    """テンプレートを消す"""

    bl_idname = "tachie.remove_template"
    bl_label = "テンプレートを消す"
    bl_options = {"REGISTER"}

    index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        templates = prefs(context).templates
        if not 0 <= self.index < len(templates):
            return {"CANCELLED"}
        templates.remove(self.index)
        context.preferences.is_dirty = True
        return {"FINISHED"}


class TACHIE_OT_import_ymm4_templates(bpy.types.Operator, ImportHelper):
    """YMM4 のテンプレート（user/setting/<版>/YukkuriMovieMaker.Settings.ItemSettings.json）を読み込む（同じ名前は上書き）"""

    bl_idname = "tachie.import_ymm4_templates"
    bl_label = "YMM4 のテンプレートを読み込む"
    bl_options = {"REGISTER"}

    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            loaded = load_ymm4_templates(self.filepath)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        templates = prefs(context).templates
        characters = {c.name for c in seq_scene(context).tachie.characters}
        lines = [""]
        added = 0
        for name, data, behind, skipped in loaded:
            if not data["items"]:
                lines.append(f"{name}: 読めるアイテムが無いので登録しませんでした（"
                             + "、".join(f"{k} {n}" for k, n in skipped.items()) + "）")
                continue
            added += 1
            item = next((t for t in templates if t.name == name), None) or templates.add()
            item.name, item.data, item.behind = name, dumps(data), behind
            counts = Counter(LABELS[i["kind"]] if i["kind"] != "media" else LABELS[i["media"]["kind"]]
                             for i in data["items"])
            missing = sorted({i["character"] for i in data["items"]
                              if i["kind"] in ("line", "face", "still") and i["character"] not in characters})
            note = "、".join(f"{k} {n}" for k, n in counts.items()) or "空"
            lines.append(f"{name}: {note}" + ("（立ち絵より奥）" if behind else ""))
            if skipped:
                lines.append("  読まなかったもの: " + "、".join(f"{k} {n}" for k, n in skipped.items()))
            if missing:
                lines.append("  置くときに要るキャラクター（今は無い）: " + "、".join(missing))
        lines[0] = f"{os.path.basename(self.filepath)}: {len(loaded)} 個のうち {added} 個のテンプレートを読み込みました"
        lines.insert(1, "")
        context.preferences.is_dirty = True
        text = bpy.data.texts.get(REPORT_TEXT) or bpy.data.texts.new(REPORT_TEXT)
        text.clear()
        text.write("\n".join(lines) + "\n")
        print("\n".join(lines))
        self.report({"INFO"}, lines[0] + f"（詳しくはテキスト「{REPORT_TEXT}」）")
        return {"FINISHED"}


def draw_templates(layout, preferences, editable: bool = False) -> None:
    """テンプレートの一覧。`editable` なら名前・立ち絵より奥・削除も出す（アドオン設定）。"""
    for i, t in enumerate(preferences.templates):
        row = layout.row(align=True)
        if editable:
            row.prop(t, "name", text="")
            row.prop(t, "behind", text="", icon="IMAGE_BACKGROUND")
            row.operator("tachie.remove_template", text="", icon="X").index = i
        else:
            row.operator("tachie.insert_template", text=t.name, icon="FILE_TICK").index = i
    if not preferences.templates:
        layout.label(text="なし")


_classes = (TACHIE_OT_register_template, TACHIE_OT_insert_template, TACHIE_OT_remove_template,
            TACHIE_OT_import_ymm4_templates)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
