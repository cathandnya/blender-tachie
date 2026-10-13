"""YMM4 のプロジェクト（.ymmp）を読み込む: キャラクター・セリフ・表情を、このアドオンのセリフと表情ストリップにする。

セリフは YMM4 が保存した audio_query（発音・抑揚・話速など）で VOICEVOX から合成し直す。画像・動画・音声・テキストなどの
アイテムは読み込まず、読み込まなかったものは結果のテキスト（テキストエディタの「YMM4 の読み込み結果」）にまとめる。
"""

from __future__ import annotations

import math
import os
import unicodedata
from collections import Counter
from pathlib import Path

import bpy
from bpy_extras.io_utils import ImportHelper

from .operators import (
    ERRORS, _applied_signatures, _face_name, _free_face_channel, _parts_dict, add_character, add_voice_character,
    auto_signature, character_voice, client, connect, ensure_sequencer_scene, line_channels, line_end, line_layout,
    line_texts, load_material, place_strips, refresh_faces, remove_idle, render_clip, scene_fps, seq_scene,
    subtitle_clashes,
)
from .tachie_studio.lipsync.timing import frame_count
from .tachie_studio.voicevox.client import VoicevoxError
from .properties import PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, system_fonts
from .tachie_studio.tachie.loader import _is_placeholder
from .ymmp_media import KIND_LABELS, MediaFinder, MediaPlacer
from .tachie_studio.ymmp import Project, difference, load_project, other_items_summary, overlay, preset_name

REPORT_TEXT = "YMM4 の読み込み結果"
# YMM4 の音量（セリフ・キャラの既定は 50）→ Blender の音量。50 を等倍とみなす（YMM4 での意味は未確認）
VOLUME_BASE = 50.0
# 声の終わりよりこれ以上前から始まる別のキャラのセリフは、同時に喋るものとみなす（シーンのフレーム）
SIMULTANEOUS_FRAMES = 2


def find_material(root: str, folder_name: str) -> str | None:
    """`root` の下（2 階層まで）から、名前が `folder_name` の素材フォルダを探す。"""
    if not root or not folder_name:
        return None
    root = bpy.path.abspath(root)
    want = unicodedata.normalize("NFC", folder_name)
    for depth_root, dirs, _ in os.walk(root):
        depth = Path(depth_root).relative_to(root).parts
        for d in dirs:
            if unicodedata.normalize("NFC", d) == want:
                return str(Path(depth_root) / d)
        if len(depth) >= 1:
            dirs[:] = []
    return None


def _font_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).lower().replace(" ", "").replace("-", "")


def find_font(name: str) -> str | None:
    if not name:
        return None
    want = _font_key(name)
    for label, path in system_fonts():
        if _font_key(label) == want:
            return path
    return None


def _placeholder(material, category: str) -> str:
    """素材の空のパーツ（「(非表示)」「_」など）。無ければ ""。"""
    return next((p for p in sorted(material.parts.get(category, {})) if _is_placeholder(p)), "")


def _valid_parts(ch, parts: dict, problems: list[str], base: dict | None = None) -> dict:
    """素材に無いパーツを落とす（警告に書く）。`base`（キャラクターの基本の表情）があれば、落としたパーツは基本のまま。

    YMM4 で空のパーツ（顔色なし など）は、このアドオンでは素材の既定のパーツになるので、素材に
    「(非表示)」や「_」のような空のパーツがあればそれにする（他・後の空きスロットはそのまま空）。
    """
    material = load_material(ch.folder)
    out = {}
    for category, v in parts.items():
        if v == "" and category not in SLOT_CATEGORIES:
            out[category] = _placeholder(material, category)
            continue
        names = v if isinstance(v, list) else [v]
        kept = []
        fallback = (base or {}).get(category)
        fallback = fallback if isinstance(fallback, list) else [fallback or ""] * len(names)
        for i, name in enumerate(names):
            if name and name not in material.parts.get(category, {}):
                problems.append(f"{ch.name}: 素材に {category}/{name} がありません（基本のまま）")
                name = fallback[i] if i < len(fallback) else ""
            kept.append(name)
        out[category] = kept if isinstance(v, list) else kept[0]
    return out


def _set_parts(item, parts: dict) -> None:
    for category, props in PART_PROPS.items():
        if category not in parts:
            continue
        values = parts[category] if category in SLOT_CATEGORIES else [parts[category]]
        for i, prop in enumerate(props):
            setattr(item, prop, values[i] if i < len(values) else "")


def _base_parts(ch) -> dict:
    """キャラクターの基本の表情（空のパーツも含む、他・後は 3 スロット）。"""
    parts = {}
    for category, props in PART_PROPS.items():
        values = [getattr(ch, p) for p in props]
        parts[category] = values if category in SLOT_CATEGORIES else values[0]
    return parts


class _Presets:
    """表情（基本の表情との差分）→ 表情プリセット名。同じ中身のプリセットがあれば使い、無ければ作る。"""

    def __init__(self, ch, problems: list[str]):
        self.ch = ch
        self.problems = problems
        self.known = {self._key(_parts_dict(e)): e.name for e in ch.expressions}
        self.created = 0

    @staticmethod
    def _key(diff: dict) -> str:
        return repr(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in diff.items() if k in PART_PROPS))

    def name_for(self, parts: dict) -> str:
        base = _base_parts(self.ch)
        diff = difference(base, parts)
        # 表情プリセットの "" は「基本のまま」なので、基本にあるパーツを消すには素材の空のパーツが要る
        material = load_material(self.ch.folder)
        for category, v in list(diff.items()):
            if not isinstance(v, list) and not v:
                del diff[category]
                self.problems.append(f"{self.ch.name}: {category} を消す表情は、素材に空のパーツが無いので基本のままにしました")
        for category in SLOT_CATEGORIES:
            b, v = base.get(category) or [], parts.get(category)
            if v is None:
                continue
            removed = [i for i, name in enumerate(b) if name and not (v[i] if i < len(v) else "")]
            empty = _placeholder(material, category)
            removed = [i for i in removed if b[i] != empty]
            if not removed:
                continue
            if not empty:
                self.problems.append(f"{self.ch.name}: {category} のスロットを消す表情は、素材に空のパーツが無いので"
                                     "基本のままにしました")
                continue
            slots = list(diff.get(category) or [])
            for i in removed:
                slots += [""] * (i + 1 - len(slots))
                slots[i] = empty
            diff[category] = slots
        if not diff:
            return ""
        key = self._key(diff)
        if key not in self.known:
            item = self.ch.expressions.add()
            item.name = preset_name(diff, {e.name for e in self.ch.expressions})
            _set_parts(item, diff)
            self.known[key] = item.name
            self.created += 1
        return self.known[key]


def _setup_character(context, ch, c, project: Project, tachie, problems: list[str]) -> None:
    """YMM4 のキャラクター設定（声・字幕・立ち絵の位置・基本の表情）を、追加したキャラクターに写す。"""
    if c.style_id is not None and c.style_id != ch.speaker:
        ch.speaker = c.style_id
        try:  # 一覧の表示名（追加したときの話者のまま残らないように）
            ch.speaker_name = dict(client(context, ch, timeout=3.0).styles()).get(ch.speaker, "")
        except (VoicevoxError, OSError):
            ch.speaker_name = ""
    for prop, (key, *_) in VOICE_PROPS.items():
        if key in c.voice:
            setattr(ch, prop, c.voice[key])
    ch.audio_volume = c.volume / VOLUME_BASE
    ch.audio_pan = c.pan / 100
    ch.audio_after = c.after
    ch.audio_fade_in, ch.audio_fade_out = c.fade_in, c.fade_out
    if c.font_size:
        ch.font_size = c.font_size
    font = find_font(c.font)
    if font:
        ch.font_path = font
    elif c.font:
        problems.append(f"{c.name}: 字幕のフォント「{c.font}」が見つかりません（今のフォントのまま）")
    ch.text_color = c.font_color
    ch.outline_color = c.outline_color
    if not c.outline:
        ch.outline_width = 0.0
    # YMM4 の字幕の Y は画面中央からの下向きのピクセル（下揃え）。このアドオンは画面の下からの割合
    ch.subtitle_y = min(1.0, max(0.0, 0.5 - c.subtitle_y / project.height))
    ch.show_idle = True
    if c.parts:
        _set_parts(ch, _valid_parts(ch, c.parts, problems))
    if tachie is not None:
        # YMM4 の立ち絵の X / Y は画面中央からの画像の中心（ピクセル、下向き正）、Zoom は素材の大きさに対する %
        render = seq_scene(context).render
        material = load_material(ch.folder)
        ch.height = min(3.0, max(0.1, material.height * tachie.zoom / 100 / project.height))
        ch.position_x = max(-1.0, min(1.0, tachie.x / project.width))
        ch.position_y = max(-1.0, min(1.0, -tachie.y / project.height))
        if render.resolution_y != project.height:
            problems.append("解像度が YMM4 と違うので、立ち絵の位置・大きさは割合で写しました")


def _report(text: str) -> None:
    t = bpy.data.texts.get(REPORT_TEXT) or bpy.data.texts.new(REPORT_TEXT)
    t.clear()
    t.write(text)


class TACHIE_OT_import_ymmp(bpy.types.Operator, ImportHelper):
    """YMM4 のプロジェクト（.ymmp）のキャラクター・セリフ・表情を読み込む（声は VOICEVOX で合成し直す）"""

    bl_idname = "tachie.import_ymmp"
    bl_label = "YMM4 のプロジェクトを読み込む"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".ymmp"
    filter_glob: bpy.props.StringProperty(default="*.ymmp", options={"HIDDEN"})
    match_video: bpy.props.BoolProperty(name="解像度・フレームレートを合わせる", default=True,
                                        description="シーンの解像度とフレームレートを YMM4 のプロジェクトに合わせる")
    import_media: bpy.props.BoolProperty(name="画像・動画・音声・テキストも読み込む", default=True,
                                         description="YMM4 のレイヤーの順で置く（立ち絵より奥のものは、アドオンの"
                                                     "チャンネルを上へずらしてその下に置く）")

    def draw(self, context):
        layout = self.layout
        settings = seq_scene(context).tachie
        layout.prop(settings, "ymmp_material_root")
        layout.label(text="無いキャラクターは、この中の同じ名前の素材フォルダから追加します")
        layout.prop(self, "match_video")
        layout.prop(self, "import_media")
        if self.import_media:
            layout.prop(settings, "ymmp_media_root")

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        try:
            project = load_project(self.filepath)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        problems: list[str] = []
        skipped: Counter = Counter()
        ensure_sequencer_scene(context)
        if self.match_video:
            render = scene.render
            before = (render.resolution_x, render.resolution_y, scene_fps(scene))
            render.resolution_x, render.resolution_y, render.resolution_percentage = (project.width, project.height,
                                                                                      100)
            render.fps, render.fps_base = round(project.fps), 1.0
            placed_before = scene.sequence_editor and any(
                s.get("tachie_id") or s.get("tachie_still") for s in scene.sequence_editor.strips)
            if placed_before and before != (render.resolution_x, render.resolution_y, scene_fps(scene)):
                problems.append("解像度・フレームレートを変えたので、前から置いてあったセリフ・立ち絵は"
                                "「作り直す」で全部作り直してください")
        fps = scene_fps(scene)

        def frame(f: int) -> int:  # YMM4 のフレーム（0 始まり）→ シーンのフレーム
            return scene.frame_start + round(f * fps / project.fps)

        # キャラクター: 同じ名前があればそのまま使い、無ければ素材フォルダから追加して YMM4 の設定を写す
        used = {v.character for v in project.voices} | {s.character for s in project.tachies + project.faces}
        characters = {}
        for name in sorted(used):
            c = project.characters.get(name)
            existing = next((ch for ch in settings.characters if ch.name == name), None)
            if existing is not None:
                characters[name] = existing
                continue
            if c is None:
                problems.append(f"{name}: キャラクターの設定がプロジェクトにありません")
                continue
            if not c.has_tachie:  # 立ち絵の無いキャラは声と字幕だけ
                ch = add_voice_character(context, name)
                ch.line_layout = "SUBTITLE" if c.subtitle_visible else "AUDIO"
                _setup_character(context, ch, c, project, None, problems)
                characters[name] = ch
                continue
            folder = find_material(settings.ymmp_material_root, c.folder_name)
            if folder is None:
                problems.append(f"{name}: 素材フォルダ「{c.folder_name}」が見つかりません"
                                "（「素材フォルダを探す場所」を指定するか、先にキャラクターを追加してください）")
                continue
            try:
                ch, _ = add_character(context, folder)
            except ValueError as e:
                problems.append(f"{name}: キャラクターを追加できません: {e}")
                continue
            ch.name = name
            tachie = next((s for s in project.tachies if s.character == name and not s.hidden), None)
            _setup_character(context, ch, c, project, tachie, problems)
            characters[name] = ch

        presets = {name: _Presets(ch, problems) for name, ch in characters.items()}

        # 画像・動画・音声・テキスト（キャラのチャンネルが決まってから、セリフを置く前に。チャンネルをずらすことがある）
        media = None
        if self.import_media and project.media:
            media = MediaPlacer(context, project, MediaFinder(Path(self.filepath), settings.ymmp_media_root), frame,
                                find_font)
            try:
                media.place()
            except ValueError as e:
                problems.append(f"画像・動画などを置けません: {e}")
            problems += media.problems

        def tachie_base(name: str, f: int) -> dict:
            """その時刻の立ち絵アイテムの基本の表情（無ければキャラクターの基本）。"""
            span = next((s for s in project.tachies
                         if s.character == name and not s.hidden and s.frame <= f < s.frame + s.length), None)
            base = project.characters[name].parts if name in project.characters else {}
            return span.parts if span is not None and span.parts else base

        def expression(name: str, f: int, face: dict) -> str:
            ch = characters[name]
            if not ch.folder:
                return ""
            parts = overlay(tachie_base(name, f), face)
            return presets[name].name_for(_valid_parts(ch, parts, problems, _base_parts(ch)))

        # セリフ
        sed = scene.sequence_editor or scene.sequence_editor_create()
        remove_idle(sed)
        voices = [v for v in project.voices if v.character in characters]
        skipped["キャラクターを読み込めなかったセリフ"] += len(project.voices) - len(voices)
        placed = 0
        wm = context.window_manager
        wm.progress_begin(0, len(voices))
        line_spans: dict[str, list[tuple[int, int]]] = {}
        try:
            for i, v in enumerate(voices):
                wm.progress_update(i)
                if v.hidden:
                    skipped["非表示のセリフ"] += 1
                    continue
                if not v.text.strip():
                    skipped["テキストの無いセリフ"] += 1
                    continue
                ch = characters[v.character]
                c = project.characters.get(v.character)
                start = frame(v.frame)
                # 余韻は YMM4 のアイテムの長さから。次のセリフに重ならないように縮める（シーンのフレームで数える。
                # fps が違うと YMM4 の 1 フレームでは足りない。合成し直した声はフレームの丸めで 1 フレーム長くなる
                # ことがあるので、その分あける）。声の途中から始まる別のキャラのセリフ（同時に喋る）は相手にしない
                after = max(0.0, v.length / project.fps - v.voice_seconds)
                voiced = frame_count(v.voice_seconds, fps)
                for w in voices[i + 1:]:
                    room = frame(w.frame) - start - voiced - 1
                    if w.frame > v.frame and (w.character == v.character or room >= -SIMULTANEOUS_FRAMES):
                        after = max(0.0, min(after, room / fps))
                        break
                audio = {"volume": v.volume / VOLUME_BASE, "pan": v.pan / 100, "fade_in": v.fade_in,
                         # 切り上げると余韻のフレームが 1 つ増えるので切り捨て
                         "fade_out": v.fade_out, "after": math.floor(after * 10000) / 10000}
                voice = {**character_voice(ch), **{k: x for k, x in v.voice.items() if k in character_voice(ch)}}
                hide_subtitle = v.subtitle == "Hidden" or (
                    v.subtitle == "UseCharacterSetting" and c and not c.subtitle_visible)
                layout = line_layout(ch)
                if not ch.folder:  # 立ち絵の無いキャラは、字幕を出すセリフは音声と字幕、出さないセリフは音声だけ
                    layout = "AUDIO" if hide_subtitle else "SUBTITLE"
                try:
                    preset = expression(v.character, v.frame, v.face)
                    clip, lines_dir = render_clip(context, ch, v.text, preset or None, speaker=v.style_id, voice=voice,
                                                  kana=v.kana or None, accent=v.accent, accent_kana=v.kana or None,
                                                  audio=audio, layout=layout)
                    channels = line_channels(context, ch, start, clip.frames, layout=layout)
                except ERRORS as e:
                    problems.append(f"{start} フレームの {v.character}「{v.text[:20]}」: {e}")
                    skipped["置けなかったセリフ"] += 1
                    continue
                strips = place_strips(context, ch, clip, lines_dir, v.text, start, voice=voice, accent=v.accent,
                                      audio=audio, channels=channels, layout=layout)
                sound = strips[0]
                sound.volume, sound.pan = audio["volume"], max(-1.0, min(1.0, audio["pan"]))
                if hide_subtitle:
                    for s in line_texts(strips):
                        s.mute = True
                else:
                    clashes = subtitle_clashes(sed, strips)
                    if clashes:
                        problems.append(f"{start} フレームの {v.character}「{v.text[:20]}」: 字幕が"
                                        f"{'・'.join(clashes)}の字幕と重なっています")
                connect(context, strips)
                line_spans.setdefault(v.character, []).append((start, line_end(strips)))
                placed += 1
        finally:
            wm.progress_end()

        # 表情: 立ち絵アイテムの基本の表情がキャラの基本と違う区間（セリフの間を除く）と、表情アイテム
        faces = 0

        def add_face(name: str, start: int, end: int, preset: str) -> None:
            nonlocal faces
            if end <= start:
                return
            face = sed.strips.new_effect(name=_face_name(name, preset), type="COLOR",
                                         channel=_free_face_channel(sed, settings, start, end),
                                         frame_start=start, length=end - start)
            face.color, face.color_tag, face.mute = (0.95, 0.6, 0.25), "COLOR_02", True
            face["tachie_face"] = name
            face["tachie_expression"] = preset
            faces += 1

        for span in project.tachies:
            if span.character not in characters or span.hidden or not characters[span.character].folder:
                continue
            ch = characters[span.character]
            preset = presets[span.character].name_for(
                _valid_parts(ch, span.parts, problems, _base_parts(ch))) if span.parts else ""
            if not preset:
                continue
            cursor, end = frame(span.frame), frame(span.frame + span.length)
            for a, b in sorted(line_spans.get(span.character, [])):  # セリフは自分の表情を持っている
                if b <= cursor or a >= end:
                    continue
                add_face(span.character, cursor, min(a, end), preset)
                cursor = max(cursor, b)
            add_face(span.character, cursor, end, preset)
        for span in project.faces:
            if span.character not in characters or span.hidden or not characters[span.character].folder:
                continue
            add_face(span.character, frame(span.frame), frame(span.frame + span.length),
                     expression(span.character, span.frame, span.parts))

        if frame(project.length) - 1 > scene.frame_end:
            scene.frame_end = frame(project.length) - 1
        problems_faces: list[str] = []
        refresh_faces(context, problems_faces)  # 待機の作り直しも（auto_idle のとき）
        _applied_signatures[scene.name] = auto_signature(scene)

        # 結果
        created = sum(p.created for p in presets.values())
        lines = [f"{Path(self.filepath).name}: セリフ {placed} / {len(project.voices)} 個、表情ストリップ {faces} 個、"
                 f"表情プリセットを {created} 個作りました"]
        if +skipped:
            lines += ["", "置かなかったセリフ:"] + [f"  {k}: {n}" for k, n in (+skipped).items()]
        media_lines = media.report() if media is not None else []
        if media_lines:
            lines += ["", *media_lines]
        others = other_items_summary(project)
        if not self.import_media:
            others.update(KIND_LABELS[m.kind] for m in project.media)
        if others:
            lines += ["", "読み込んでいないアイテム:"] + [f"  {k}: {n}" for k, n in others.most_common()]
        if project.unsupported:
            lines += ["", "写せなかった設定:"] + [f"  {k}: {n}" for k, n in project.unsupported.most_common()]
        if problems or problems_faces:
            lines += ["", "問題:"] + [f"  {p}" for p in dict.fromkeys(problems + problems_faces)]
        _report("\n".join(lines) + "\n")
        for line in lines:
            print(line)
        message = lines[0] + f"（詳しくはテキスト「{REPORT_TEXT}」）"
        if not placed:
            self.report({"ERROR"}, message)
            return {"CANCELLED"} if not characters else {"FINISHED"}
        self.report({"WARNING"} if problems or +skipped else {"INFO"}, message)
        return {"FINISHED"}


_classes = (TACHIE_OT_import_ymmp,)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
