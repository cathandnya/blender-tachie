"""YMM4 のプロジェクト（.ymmp）を読み込む: キャラクター・セリフ・表情を、このアドオンのセリフと表情ストリップにする。

セリフは YMM4 が保存した audio_query（発音・抑揚・話速など）で VOICEVOX から合成し直す。画像・動画・音声・テキストなどの
アイテムは読み込まず、読み込まなかったものは結果のテキスト（テキストエディタの「YMM4 の読み込み結果」）にまとめる。
"""

from __future__ import annotations

import math
import os
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import bpy
from bpy_extras.io_utils import ImportHelper

from .operators import (
    ERRORS, _applied_signatures, _face_name, _free_face_channel, _parts_dict, add_character, add_voice_character,
    auto_signature, character_voice, client, clip_job, connect, ensure_sequencer_scene, line_channels, line_end, line_layout,
    line_texts, load_material, place_still, place_strips, refresh_faces, remove_idle, render_clip, scene_fps, seq_scene,
    set_text_offset, subtitle_clashes,
)
from .tachie_studio.lipsync.timing import frame_count
from .tachie_studio.voicevox.client import VoicevoxError
from .properties import PART_PROPS, SLOT_CATEGORIES, VOICE_PROPS, place_text, system_fonts
from .tachie_studio.tachie.loader import CATEGORIES, _is_placeholder
from .ymmp_media import KIND_LABELS, MediaFinder, MediaPlacer
from .tachie_studio.ymmp import (Project, difference, find_by_tail, load_project, other_items_summary, overlay,
                                 preset_name, voice_wav)

REPORT_TEXT = "YMM4 の読み込み結果"
# YMM4 の音量（セリフ・キャラの既定は 50）→ Blender の音量。50 を等倍とみなす（YMM4 での意味は未確認）
VOLUME_BASE = 50.0
# 声の終わりよりこれ以上前から始まる別のキャラのセリフは、同時に喋るものとみなす（シーンのフレーム）
SIMULTANEOUS_FRAMES = 2


def material_problem(folder: str) -> str | None:
    """動く立ち絵の素材フォルダとして使えなければ、その理由（YMM4 から読み込むのは動く立ち絵だけ）。
    写真の入ったフォルダなどを素材にすると、シンプル立ち絵として大きな画像を合成し続けて固まったようになる。"""
    if not folder or not os.path.isdir(folder):
        return "フォルダがありません"
    if not any(os.path.isdir(os.path.join(folder, c)) for c in CATEGORIES):
        return "眉・目・口などのフォルダがありません（キャラクターの素材フォルダを選んでください）"
    return None


def find_material(root: str, folder_name: str) -> str | None:
    """`root` の下（2 階層まで）から、名前が `folder_name` の素材フォルダを探す。"""
    if not root or not folder_name:
        return None
    root = bpy.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return None
    want = unicodedata.normalize("NFC", folder_name)
    for depth_root, dirs, _ in os.walk(root):
        depth = Path(depth_root).relative_to(root).parts
        for d in dirs:
            if unicodedata.normalize("NFC", d) == want and material_problem(str(Path(depth_root) / d)) is None:
                return str(Path(depth_root) / d)
        if len(depth) >= 1:
            dirs[:] = []
    return None


def _abs(path: str) -> str:
    return bpy.path.abspath(os.path.expanduser(path)) if path else ""


def resolve_material(settings, c, ymmp: Path) -> str | None:
    """YMM4 のキャラクターの素材フォルダを探す: 元のパスの後ろの部分（.ymmp のフォルダとその上から）→
    素材・画像などを探す場所と .ymmp のフォルダの下（2 階層まで、同じ名前のフォルダ）。"""
    found = find_by_tail(c.folder, ymmp.parent, directory=True) if c.folder else None
    if found is not None and material_problem(str(found)) is None:
        return str(found)
    roots = (settings.ymmp_material_root, settings.ymmp_media_root, str(ymmp.parent))
    return next((f for r in dict.fromkeys(roots) if (f := find_material(r, c.folder_name))), None)


def _base_span(project: Project, name: str):
    """キャラクターの基本の表情・位置にする立ち絵アイテム（いちばん長く出ているもの）。無ければ None。
    YMM4 のキャラの既定のパーツ（TachieDefaultItemParameter）は新しく置くアイテムの初期値で、動画に出ている見た目とは
    限らない（口・体が空のまま、記号だけ、など）ので、基本の表情には実際に置かれていたアイテムを使う。"""
    # 立ち絵アイテムの parts は空のパーツも "" で持つので、どれかのパーツがあるものだけ
    spans = [s for s in project.tachies if s.character == name and not s.hidden and any(s.parts.values())]
    return max(spans, key=lambda s: s.length, default=None)


def _used_characters(project: Project) -> set[str]:
    return {v.character for v in project.voices} | {s.character for s in project.tachies + project.faces}


def _missing_media(settings, ymmp: Path, project: Project) -> tuple[int, list[str], bool]:
    """(画像・動画などのファイルの数, 見つからないファイル名, 探す場所が広すぎて途中までしか探していないか)。"""
    finder = MediaFinder(ymmp, settings.ymmp_media_root)
    files = sorted({m.file for m in project.media if m.file})
    missing = sorted({PureWindowsPath(f).name for f in files if finder.find(f) is None})
    return len(files), missing, finder.truncated


def _resolve_rows(settings, project: Project) -> list[tuple[str, str, str]]:
    """一覧に無い、立ち絵のあるキャラクターの (名前, YMM4 の素材フォルダの名前, 見つかった素材フォルダか "")。"""
    rows = []
    for name in sorted(_used_characters(project)):
        c = project.characters.get(name)
        if c is None or not c.has_tachie or any(ch.name == name for ch in settings.characters):
            continue  # 一覧にあるキャラはそのまま使う
        rows.append((name, c.folder_name, resolve_material(settings, c, project.path) or ""))
    return rows


def prepare_pending(settings, project: Project, match_video: bool, import_media: bool) -> bool:
    """見つからない素材フォルダ・ファイルがあれば、パネルで場所を指定するまで読み込みを待つ状態にして True。"""
    rows = _resolve_rows(settings, project)
    total, missing, wide = _missing_media(settings, project.path, project) if import_media else (0, [], False)
    if all(folder for *_, folder in rows) and not missing:
        return False
    cancel_pending(settings)
    settings.ymmp_match_video, settings.ymmp_import_media = match_video, import_media
    for name, folder_name, folder in rows:
        item = settings.ymmp_pending_chars.add()
        item.name, item.folder_name, item.folder = name, folder_name, folder
    settings.ymmp_pending_files, settings.ymmp_pending_missing = total, "\n".join(missing)
    settings.ymmp_pending_wide = wide
    settings.ymmp_pending = str(project.path)
    return True


def cancel_pending(settings) -> None:
    settings.ymmp_pending = ""
    settings.ymmp_pending_chars.clear()
    settings.ymmp_pending_files, settings.ymmp_pending_missing, settings.ymmp_pending_wide = 0, "", False


def rescan_pending_media(settings) -> None:
    """待っている読み込みの、見つからない素材フォルダ・ファイルを探し直す（探す場所を変えたとき）。"""
    try:
        project = load_project(settings.ymmp_pending)
    except ValueError:
        return
    found = {name: folder for name, _, folder in _resolve_rows(settings, project) if folder}
    for item in settings.ymmp_pending_chars:
        if material_problem(_abs(item.folder)) is not None and found.get(item.name):
            item.folder = found[item.name]
    total, missing, wide = (_missing_media(settings, project.path, project) if settings.ymmp_import_media
                            else (0, [], False))
    settings.ymmp_pending_files, settings.ymmp_pending_missing = total, "\n".join(missing)
    settings.ymmp_pending_wide = wide


_filling = False


def fill_pending_folders(settings, changed) -> None:
    """素材フォルダを指定したら、その隣（同じ親フォルダの下）から、まだ見つかっていない他のキャラの素材も探す。"""
    global _filling
    folder = _abs(changed.folder)
    if _filling or not settings.ymmp_pending or material_problem(folder) is not None:
        return
    _filling = True
    try:
        parent = os.path.dirname(folder.rstrip("/\\"))
        for item in settings.ymmp_pending_chars:
            if item.name != changed.name and material_problem(_abs(item.folder)) is not None:
                found = find_material(parent, item.folder_name)
                if found:
                    item.folder = found
    finally:
        _filling = False


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
    ch.line_spacing = c.line_height / 100  # 範囲の外はプロパティの min / max に丸められる
    ch.outline_color = c.outline_color
    if not c.outline:
        ch.outline_width = 0.0
    # YMM4 の字幕の Y は画面中央からの下向きのピクセル（下揃え）。このアドオンは画面の下からの割合
    ch.subtitle_y = min(1.0, max(0.0, 0.5 - c.subtitle_y / project.height))
    ch.show_idle = True
    base = tachie.parts if tachie is not None and any(tachie.parts.values()) else c.parts  # 立ち絵アイテムの見た目を優先
    if base:
        _set_parts(ch, _valid_parts(ch, base, problems))
    if tachie is not None:
        # YMM4 の立ち絵の X / Y は画面中央からの画像の中心（ピクセル、下向き正）、Zoom は素材の大きさに対する %
        render = seq_scene(context).render
        material = load_material(ch.folder)
        ch.height = min(3.0, max(0.1, material.height * tachie.zoom / 100 / project.height))
        ch.position_x = max(-1.0, min(1.0, tachie.x / project.width))
        ch.position_y = max(-1.0, min(1.0, -tachie.y / project.height))
        # 左右反転は立ち絵アイテムに付く（キャラクターの TachieIsInverted は新しいアイテムの初期値）。位置は
        # 画面上の位置のまま（反転したキャラの offset の符号は apply_placement が直す）
        ch.flip_x = tachie.inverted
        if render.resolution_y != project.height:
            problems.append("解像度が YMM4 と違うので、立ち絵の位置・大きさは割合で写しました")


def _report(text: str) -> None:
    t = bpy.data.texts.get(REPORT_TEXT) or bpy.data.texts.new(REPORT_TEXT)
    t.clear()
    t.write(text)


@dataclass
class _LineJob:
    """合成を待っているセリフ。`run` は Blender に触らずに合成する関数（`operators.clip_job`）。"""
    voice_item: object  # ymmp.Voice
    ch: object  # TachieCharacter
    start: int
    audio: dict
    voice: dict
    hide_subtitle: bool
    layout: str
    run: object


def _run_job(job: _LineJob):
    """合成して (RenderedClip, フォルダ) を返す。失敗したら例外を返す（別スレッドから持ち帰るため）。"""
    try:
        return job.run()
    except Exception as e:  # noqa: BLE001
        return e


_running = False  # 読み込み中（合成を待っている間に、もう 1 つ始めない）


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
    # ファイル選択から読み込むとき、見つからない素材・ファイルがあればパネルで場所を指定するまで待つ
    confirm: bpy.props.BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    # パネルの「読み込む」（待っている読み込みを、指定した素材フォルダで続ける）
    pending: bpy.props.BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    _state = None
    _timer = None

    @classmethod
    def poll(cls, context):
        if _running:
            cls.poll_message_set("YMM4 のプロジェクトを読み込んでいる途中です（Esc でやめる）")
            return False
        return True

    def invoke(self, context, event):
        if self.pending:
            return self.execute(context)
        self.confirm = True
        return ImportHelper.invoke(self, context, event)

    def _start(self, context, scene_name: str, jobs: list[_LineJob], place, finish):
        """別スレッドで合成を始める。合成し終わったものはタイマー（modal）で順に置き、最後に finish を呼ぶ。"""
        global _running
        state = {"jobs": jobs, "results": [], "next": 0, "cancel": False, "place": place, "finish": finish,
                 "scene": scene_name, "workspace": context.workspace and context.workspace.name}

        def work():
            for job in jobs:
                if state["cancel"]:
                    return
                state["results"].append(_run_job(job))  # list.append はスレッドをまたいでも安全

        self._state = state
        state["thread"] = threading.Thread(target=work, daemon=True)
        state["thread"].start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        _running = True
        self._status()
        return {"RUNNING_MODAL"}

    def _status(self, text: str | None = "") -> None:
        """ステータスバーに進み具合を出す（None で消す）。始めたときのワークスペースに出し、そこから消す。"""
        state = self._state
        workspace = bpy.data.workspaces.get(state["workspace"] or "")
        if workspace is None:
            return
        if text == "":
            text = (f"YMM4 のプロジェクトを読み込んでいます: セリフを合成中 {state['next']} / {len(state['jobs'])} 本"
                    "（Esc でやめる。置いたセリフは残ります）")
        workspace.status_text_set(text)

    def _cleanup(self, context) -> None:
        global _running
        _running = False
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._status(None)

    def cancel(self, context):
        # ファイルを開き直したときなど、modal の途中で外されたとき
        self._state["cancel"] = True
        self._cleanup(context)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            self._state["cancel"] = True
            self._cleanup(context)
            raise

    def _modal(self, context, event):
        state = self._state
        if event.type == "ESC" and event.value == "PRESS":
            state["cancel"] = True
        elif event.type != "TIMER":
            return {"PASS_THROUGH"}
        if state["cancel"]:
            if state["thread"].is_alive():
                # 合成中の 1 本が終わるまで待つ（結果は捨てる）。キャッシュの画像を同時に書かないように、
                # 終わるまで表情の反映も次の読み込みも始めない
                self._status("YMM4 のプロジェクトの読み込みをやめています…")
                return {"RUNNING_MODAL"}
        elif seq_scene(context).name != state["scene"]:
            # シーケンサーのシーンが変わったら、元のシーンに戻るまで置かない（置き先がずれる）
            self._status(f"YMM4 のプロジェクトを読み込んでいます: シーン「{state['scene']}」に戻すと続けます"
                         "（Esc でやめる）")
            return {"RUNNING_MODAL"}
        else:
            # 合成し終わったものを順に置く（置くのはメインスレッドだけ）
            while state["next"] < len(state["results"]):
                state["place"](state["jobs"][state["next"]], state["results"][state["next"]])
                state["next"] += 1
            if state["next"] < len(state["jobs"]):
                self._status()
                for area in context.screen.areas if context.screen else []:
                    if area.type == "SEQUENCE_EDITOR":
                        area.tag_redraw()
                return {"RUNNING_MODAL"}
        self._cleanup(context)
        return state["finish"](cancelled=len(state["jobs"]) - state["next"])

    def draw(self, context):
        self.layout.prop(self, "match_video")
        self.layout.prop(self, "import_media")
        self.layout.label(text="素材・画像などは自動で探します（見つからなければ、あとで指定します）")

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.tachie
        folders: dict[str, str] = {}  # キャラ名 → 指定された素材フォルダ
        if self.pending:
            if not settings.ymmp_pending:
                self.report({"ERROR"}, "読み込みを待っている YMM4 のプロジェクトがありません")
                return {"CANCELLED"}
            self.filepath = settings.ymmp_pending
            self.match_video, self.import_media = settings.ymmp_match_video, settings.ymmp_import_media
            folders = {c.name: _abs(c.folder) for c in settings.ymmp_pending_chars if c.folder}
        try:
            project = load_project(self.filepath)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not self.pending and self.confirm and prepare_pending(settings, project, self.match_video,
                                                                 self.import_media):
            self.report({"WARNING"}, "見つからない素材・ファイルがあります。「台本の流し込み」で場所を指定して"
                                     "「読み込む」を押してください")
            return {"FINISHED"}
        if self.pending or self.confirm:
            cancel_pending(settings)
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
        used = _used_characters(project)
        characters = {}
        added_folders: dict[str, str] = {}
        for name in sorted(used):
            c = project.characters.get(name)
            existing = next((ch for ch in settings.characters if ch.name == name), None)
            if existing is not None:
                characters[name] = existing
                continue
            if c is None:
                problems.append(f"{name}: キャラクターの設定がプロジェクトにありません")
                continue
            if not c.has_tachie:  # 立ち絵の無いキャラは声と字幕だけ（アドオン設定の初期値に YMM4 の設定を重ねる）
                try:
                    ch, _ = add_voice_character(context, name)
                except ValueError as e:
                    problems.append(f"{name}: キャラクターを追加できません: {e}")
                    continue
                ch.line_layout = "SUBTITLE" if c.subtitle_visible else "AUDIO"
                _setup_character(context, ch, c, project, None, problems)
                characters[name] = ch
                continue
            folder = folders.get(name) or resolve_material(settings, c, Path(self.filepath))
            if not folder:
                problems.append(f"{name}: 素材フォルダ「{c.folder_name}」が見つかりません"
                                "（読み込むときに指定するか、先にキャラクターを追加してください）")
                continue
            if (problem := material_problem(folder)) is not None:  # 指定されたフォルダが素材でない
                problems.append(f"{name}: {folder}: {problem}")
                continue
            try:
                ch, _ = add_character(context, folder)
            except ValueError as e:
                problems.append(f"{name}: キャラクターを追加できません: {e}")
                continue
            ch.name = name
            tachie = _base_span(project, name) or next(
                (s for s in project.tachies if s.character == name and not s.hidden), None)
            _setup_character(context, ch, c, project, tachie, problems)
            characters[name] = ch
            added_folders[name] = folder

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

        base_spans = {name: _base_span(project, name) for name in characters}

        def tachie_base(name: str, f: int) -> dict:
            """その時刻の立ち絵アイテムの基本の表情（無ければ基本の表情にしたアイテム、それも無ければキャラクターの基本）。"""
            span = next((s for s in project.tachies
                         if s.character == name and not s.hidden and s.frame <= f < s.frame + s.length), None)
            if span is not None and span.parts:
                return span.parts
            fallback = base_spans.get(name)  # 立ち絵アイテムの外は、基本の表情にしたアイテム
            if fallback is not None:
                return fallback.parts
            return project.characters[name].parts if name in project.characters else {}

        def expression(name: str, f: int, face: dict) -> str:
            ch = characters[name]
            if not ch.folder:
                return ""
            parts = overlay(tachie_base(name, f), face)
            return presets[name].name_for(_valid_parts(ch, parts, problems, _base_parts(ch)))

        # セリフ: 準備（Blender に触る部分）を全部してから、音声合成（時間がかかる）を順に進め、できたものから置く
        sed = scene.sequence_editor or scene.sequence_editor_create()
        remove_idle(sed)
        voices = [v for v in project.voices if v.character in characters]
        skipped["キャラクターを読み込めなかったセリフ"] += len(project.voices) - len(voices)
        placed = 0
        reused = 0  # YMM4 が保存した音声を使ったセリフ（VOICEVOX で合成しない）
        line_spans: dict[str, list[tuple[int, int]]] = {}
        jobs: list[_LineJob] = []
        for i, v in enumerate(voices):
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
            wav = voice_wav(v)
            try:
                preset = expression(v.character, v.frame, v.face)
                run = clip_job(context, ch, v.text, preset or None, speaker=v.style_id, voice=voice,
                               kana=v.kana or None, accent=v.accent, accent_kana=v.kana or None,
                               audio=audio, layout=layout, wav=wav)
            except ERRORS as e:
                problems.append(f"{start} フレームの {v.character}「{v.text[:20]}」: {e}")
                skipped["置けなかったセリフ"] += 1
                continue
            jobs.append(_LineJob(v, ch, start, audio, voice, hide_subtitle, layout, run))
            reused += wav is not None

        def place(job: _LineJob, result) -> None:
            """合成し終わったセリフを置く（result は (RenderedClip, フォルダ) か、合成で出た例外）。"""
            nonlocal placed
            v = job.voice_item
            where = f"{job.start} フレームの {v.character}「{v.text[:20]}」"
            try:
                if isinstance(result, Exception):
                    raise result
                clip, lines_dir = result
                channels = line_channels(context, job.ch, job.start, clip.frames, layout=job.layout)
            except ERRORS as e:
                problems.append(f"{where}: {e}")
                skipped["置けなかったセリフ"] += 1
                return
            strips = place_strips(context, job.ch, clip, lines_dir, v.text, job.start, voice=job.voice,
                                  accent=v.accent, audio=job.audio, channels=channels, layout=job.layout)
            sound = strips[0]
            sound.volume, sound.pan = job.audio["volume"], max(-1.0, min(1.0, job.audio["pan"]))
            if job.hide_subtitle:
                for s in line_texts(strips):
                    s.mute = True
            else:
                if v.subtitle == "Custom":
                    # ボイスごとの字幕の位置（YMM4 の X / Y は画面中央からの下向きのピクセル、字幕の下端）を、
                    # キャラの字幕の位置からずらす量（画面に対する割合）にする
                    offset = (v.sub_x / project.width, 0.5 - v.sub_y / project.height - job.ch.subtitle_y)
                    for s in line_texts(strips):
                        set_text_offset(s, offset)
                        place_text(s, job.ch, v.text)
                clashes = subtitle_clashes(sed, strips)
                if clashes:
                    problems.append(f"{where}: 字幕が{'・'.join(clashes)}の字幕と重なっています")
            connect(context, strips)
            line_spans.setdefault(v.character, []).append((job.start, line_end(strips)))
            placed += 1

        def finish(cancelled: int = 0):
            """セリフを置き終わったあと（`cancelled` は途中でやめて置かなかったセリフの数）。"""
            if cancelled:
                skipped["途中でやめたので置かなかったセリフ"] += cancelled
            # 最後のセリフより後ろの立ち絵アイテムは、セリフなしの立ち絵にする（待機は最後のセリフの終わりまでしか
            # 埋めないので、セリフの無いプロジェクトやエンディングで立ち絵が消える）。表情は下の表情ストリップで付く
            stills = 0
            last_line = max((b for spans in line_spans.values() for _, b in spans), default=scene.frame_start)
            for name in characters:
                ch = characters[name]
                if not ch.folder:
                    continue
                cursor = last_line
                for span in sorted((s for s in project.tachies if s.character == name and not s.hidden),
                                   key=lambda s: s.frame):
                    a, b = max(cursor, frame(span.frame)), frame(span.frame + span.length)
                    if b <= a:
                        continue
                    try:
                        place_still(context, ch, "", a, b - a)
                    except ERRORS as e:
                        problems.append(f"{a} フレームの {name} の立ち絵: {e}")
                        continue
                    stills += 1
                    cursor = b

            # 表情: 立ち絵アイテムの基本の表情がキャラの基本と違う区間（セリフの間を除く）と、表情アイテム
            faces = 0

            def add_face(name: str, start: int, end: int, preset: str) -> None:
                nonlocal faces
                if end <= start:
                    return
                face = sed.strips.new_effect(name=_face_name(name, preset), type="COLOR",
                                             channel=_free_face_channel(sed, settings, start, end, name),
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
            lines = [f"{Path(self.filepath).name}: セリフ {placed} / {len(project.voices)} 個、"
                     + (f"セリフなしの立ち絵 {stills} 個、" if stills else "")
                     + f"表情ストリップ {faces} 個、表情プリセットを {created} 個作りました"]
            if jobs:
                lines += ["", f"音声: YMM4 が保存した音声を使ったセリフ {reused} 本、VOICEVOX で合成したセリフ "
                              f"{len(jobs) - reused} 本"]
            if added_folders:
                lines += ["", "追加したキャラクターの素材フォルダ:"] + [f"  {n}: {f}" for n, f in added_folders.items()]
            if +skipped:
                lines += ["", "置かなかったセリフ:"] + [f"  {k}: {n}" for k, n in (+skipped).items()]
            media_lines = media.report() if media is not None else []
            if media_lines:
                lines += ["", *media_lines]
            others = other_items_summary(project)
            if not self.import_media:
                others.update(KIND_LABELS["transition" if m.transition else m.kind] for m in project.media)
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
            if not placed and not stills:
                self.report({"ERROR"}, message)
                return {"CANCELLED"} if not characters else {"FINISHED"}
            self.report({"WARNING"} if problems or +skipped else {"INFO"}, message)
            return {"FINISHED"}

        if bpy.app.background or context.window is None or not jobs:
            # 画面が無い（スモークテストなど）ときは、合成し終わるまで待つ
            for job in jobs:
                place(job, _run_job(job))
            return finish()
        # 画面があれば別スレッドで合成し、タイマーで進み具合を出しながら置いていく（Esc でやめる）
        return self._start(context, scene.name, jobs, place, finish)


class TACHIE_OT_import_ymmp_cancel(bpy.types.Operator):
    """待っている YMM4 のプロジェクトの読み込みをやめる"""

    bl_idname = "tachie.import_ymmp_cancel"
    bl_label = "やめる"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        cancel_pending(seq_scene(context).tachie)
        return {"FINISHED"}


_classes = (TACHIE_OT_import_ymmp, TACHIE_OT_import_ymmp_cancel)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
