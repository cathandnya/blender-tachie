"""タイムラインの fps・解像度を変える。

fps・解像度は最初に決めるもの（セリフの口パクのコマ・立ち絵の大きさ・字幕の位置がこれで決まる）。置いたあとに
変えるときは、置いてあるものを時間（秒）と画面に対する位置・大きさを保って変換する（`convert`）。
Blender は fps を変えると音声だけ秒の長さを保ち（位置はフレーム数のまま、長さと頭の切り取りは秒）、他のストリップ・
キーフレームはフレーム数のまま、解像度を変えても px の値（位置・文字の大きさ・拡大率）はそのまま。
置いたときの fps・解像度はシーンの `timeline_fps` / `timeline_width` / `timeline_height` に覚え、出力タブで直接
変えられたら（`changed_from`）パネルで「合わせる」「戻す」を出す。
"""

from __future__ import annotations

import re
import bpy

from .tachie_studio.pipeline.render import LineMotion

FPS_CHOICES = (24, 25, 30, 50, 60)
SIZE_CHOICES = (720, 1080, 1440, 2160)  # 短い辺（1920×1080 なら 1080、1080×1920 も 1080）
SHAPES = {"WIDE": ("横長 16:9", 16, 9), "TALL": ("縦長 9:16（ショート）", 9, 16), "SQUARE": ("正方形 1:1", 1, 1)}
FPS_EPS = 1e-4  # fps の比較の許し（timeline_fps は 32bit の float なので 59.94fps は 1e-6 ほどずれる）
_STRIP_PATH = re.compile(r'sequence_editor\.strips_all\["((?:[^"\\]|\\.)*)"\]\.(.+)')


def current(scene) -> tuple[float, int, int]:
    r = scene.render
    return r.fps / r.fps_base, r.resolution_x, r.resolution_y


def _has_strips(scene) -> bool:
    return scene.sequence_editor is not None and len(scene.sequence_editor.strips) > 0


def _line_fps(scene) -> float | None:
    """置いてあるセリフの動き（lines/<id>.json）の fps。前の版の .blend（置いたときの fps を覚えていない）用。"""
    from .motion import motion_paths
    from .operators import output_dir

    sed = scene.sequence_editor
    find = motion_paths(sed, output_dir(bpy.context) / "lines")
    for s in sed.strips:
        if s.type == "SOUND" and s.get("tachie_id"):
            path = find(s["tachie_id"])
            if path.is_file():
                try:
                    return LineMotion.load(path).fps
                except (OSError, ValueError, TypeError):
                    continue
    return None


def baseline(scene) -> tuple[float, int, int] | None:
    """置いたときの fps・解像度。何も置いていなければ None。覚えていなければ（前の版）セリフの動きの fps と今の解像度。"""
    if not _has_strips(scene):
        return None
    s = scene.tachie
    if s.timeline_fps > 0:
        return s.timeline_fps, s.timeline_width, s.timeline_height
    fps, w, h = current(scene)
    return _line_fps(scene) or fps, w, h


def remember(scene, force: bool = False) -> None:
    """今の fps・解像度を置いたときのものとして覚える（何も置いていないとき・変換したとき。置いたあとは変えない）。"""
    s = scene.tachie
    if force or not _has_strips(scene):
        fps, w, h = current(scene)
    elif s.timeline_fps > 0:
        return
    else:
        fps, w, h = baseline(scene)
    s.timeline_fps, s.timeline_width, s.timeline_height = fps, w, h


def changed_from(scene) -> tuple[float, int, int] | None:
    """出力タブなどで fps・解像度が置いたときから変わっていれば、置いたときの値。"""
    base = baseline(scene)
    if base is None:
        return None
    fps, w, h = current(scene)
    if abs(base[0] - fps) < FPS_EPS and (base[1], base[2]) == (w, h):
        return None
    return base


def same_shape(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """縦横比が同じか（偶数に丸めた分の 1px のずれは同じとみなす）。"""
    return abs(a[0] * b[1] - b[0] * a[1]) <= max(a[0], a[1], b[0], b[1])


def size_for(width: int, height: int, short: int) -> tuple[int, int]:
    """縦横比を保って短い辺を `short` にした大きさ（動画の書き出しのため偶数）。"""
    s = short / min(width, height)
    return max(2, round(width * s / 2) * 2), max(2, round(height * s / 2) * 2)


def convert(context, old: tuple[float, int, int], problems: list[str]) -> None:
    """fps・解像度を `old` から今の値に変えたシーンの、置いてあるものを変換する（今の値はもう入っていること）。
    立ち絵・字幕の作り直しは呼んだ側（`operators.refresh_faces` など）。"""
    from .operators import seq_scene

    scene = seq_scene(context)
    fps, w, h = current(scene)
    if _has_strips(scene) and not same_shape((old[1], old[2]), (w, h)):  # 何も置いていなければ形も選べる
        raise ValueError(f"縦横比が違うので変換できません（{old[1]}×{old[2]} → {w}×{h}）")
    sed = scene.sequence_editor
    if sed is not None:
        if abs(fps - old[0]) > FPS_EPS:
            _retime(context, scene, sed, old[0], fps / old[0], problems)
        scale = min(w, h) / min(old[1], old[2])
        if abs(scale - 1) > 1e-9:
            _rescale(scene, sed, scale)
    remember(scene, force=True)


def _retime(context, scene, sed, old_fps: float, k: float, problems: list[str]) -> None:
    """fps を k 倍にしたとき、時間（秒）を保つようにストリップ・セリフの動き・キーフレーム・マーカーを動かす。"""
    from .motion import load_motion, motion_paths
    from .operators import output_dir

    origin = scene.frame_start

    def m(f: float) -> int:
        return origin + round((f - origin) * k)

    # セリフの長さ（余韻を含む）を新しい fps で。動きの json は書き換えず、読むときに今の fps で数え直す
    # （`motion.load_motion`。ファイルは元に戻す（undo）で戻らないので、書き換えると口パクと長さが食い違う）
    find = motion_paths(sed, output_dir(context) / "lines")
    frames: dict[str, int] = {}
    for s in sed.strips:
        clip_id = s.get("tachie_id")
        if s.type != "SOUND" or not clip_id or clip_id in frames:
            continue
        try:
            frames[clip_id] = load_motion(find(clip_id), k * old_fps).frames
        except (OSError, ValueError, TypeError):
            frames[clip_id] = round(int(s.get("tachie_frames", 0)) * k)

    # 速度の補正（SPEED）の元の動画はそのまま（キーフレームを動かせば合う）
    sped = {s.input_1.name for s in sed.strips if s.type == "SPEED" and getattr(s, "input_1", None)}
    plan = []
    for s in sed.strips:
        if getattr(s, "input_count", 0):
            continue  # 効果のストリップは元のストリップに付いて動く
        if s.type == "META":
            problems.append(f"メタストリップ「{s.name}」の中は変換していません")
        left, right = s.left_handle, s.right_handle
        if s.type == "SOUND":
            # 音声は Blender が長さと頭の切り取りを秒のまま数え直している（位置だけフレーム数のまま）
            head = left - s.frame_offset_start / k
            plan.append((s, left, m(left), None, m(head)))
            continue
        new_right = m(right)
        clip_id = s.get("tachie_id")
        if clip_id in frames and right - left == int(s.get("tachie_frames", 0)):
            new_right = m(left) + frames[clip_id]  # 字幕・キャラ名はセリフ（余韻を含む）と同じ長さ
        plan.append((s, left, m(left), new_right, None))
    # 重ねずに動かす（Blender は重なる位置へ動かすと別のチャンネルへ逃がす）: 伸ばすときは後ろから、縮めるときは前から
    plan.sort(key=lambda p: p[1], reverse=k > 1)
    movies = []
    for s, left, new_left, new_right, head in plan:
        if head is not None:
            s.frame_start = head
            continue
        old_head = s.frame_start
        s.frame_start += new_left - left
        if new_right is not None and s.right_handle != new_right:
            s.right_handle = new_right
        if s.left_handle != new_left:
            s.left_handle = new_left
        if s.type == "MOVIE" and s.name not in sped:
            movies.append((s, left - old_head, right - old_head))
    for s in sed.strips:  # セリフの長さ（余韻を含む）
        if s.get("tachie_id") in frames and "tachie_frames" in s:
            s["tachie_frames"] = frames[s["tachie_id"]]
    _scale_keys(scene, lambda x: origin + (x - origin) * k)
    for movie, first, last in movies:  # キーフレームを写したあとに打つ
        _speed(sed, movie, first, last, problems)
    for marker in scene.timeline_markers:
        marker.frame = m(marker.frame)
    scene.frame_end = m(scene.frame_end + 1) - 1
    scene.frame_current = m(scene.frame_current)
    if scene.use_preview_range:
        scene.frame_preview_start = m(scene.frame_preview_start)
        scene.frame_preview_end = m(scene.frame_preview_end + 1) - 1


def _speed(sed, movie, first: float, last: float, problems: list[str]) -> None:
    """動画は素材の 1 コマをシーンの 1 コマとして再生するので、fps を変えたら速度の補正（SPEED のフレーム番号）を重ねて
    前と同じ速さにする（`ymmp_media._movie` と同じ）。`first` / `last` は前の左右の端の素材のコマ。"""
    from .operators import _overlaps

    if len(movie.retiming_keys):
        problems.append(f"動画「{movie.name}」は再生速度を変えてあるので、速さを合わせていません")
        return
    start, end = movie.left_handle, movie.right_handle
    channel = movie.channel + 1
    if channel > 128 or _overlaps(sed, channel, start, end):
        problems.append(f"動画「{movie.name}」のすぐ上のチャンネルが空いていないので、速さを合わせていません")
        return
    speed = sed.strips.new_effect(name=f"速度 {movie.name}", type="SPEED", channel=channel, frame_start=start,
                                  input1=movie)
    speed.speed_control = "FRAME_NUMBER"
    for frame, value in ((start, first), (end, last)):
        speed.speed_frame_number = value
        speed.keyframe_insert("speed_frame_number", frame=frame)
    from .ymmp_media import _linear

    _linear(sed.id_data, speed, "speed_frame_number")


def _fcurves(scene):
    ad = scene.animation_data
    if ad is None or ad.action is None:
        return
    for layer in ad.action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield from bag.fcurves


def _scale_keys(scene, fx=None, fy=None, paths=None) -> None:
    """シーンのキーフレームの時刻を fx、値を fy で写す（`paths(data_path)` が True の F カーブだけ）。"""
    for fc in _fcurves(scene):
        if paths is not None and not paths(fc.data_path):
            continue
        for kp in fc.keyframe_points:
            for point in (kp.co, kp.handle_left, kp.handle_right):
                if fx is not None:
                    point.x = fx(point.x)
                if fy is not None:
                    point.y = fy(point.y)
        fc.update()


def _rescale(scene, sed, s: float) -> None:
    """解像度を s 倍にしたとき（縦横比は同じ）、px で持っている値を s 倍にする。アドオンの立ち絵は作り直すので触らない
    （合成キャッシュの大きさ・立ち位置は画面に対する割合）。字幕・キャラ名・見出しはキャラ・シーンの設定から当て直す。"""
    moved, sized = set(), set()  # 位置（px）を写すストリップ、大きさ（拡大率・文字の大きさ）も写すストリップ
    for x in sed.strips_all:
        for key in ("tachie_sub_size", "tachie_sub_width"):
            if x.get(key):
                x[key] = float(x[key]) * s
        if x.get("tachie_still") or x.get("tachie_character") or x.get("tachie_idle") or x.get("tachie_headline"):
            continue
        if x.type == "TEXT":
            x.font_size *= s
            sized.add(x.name)
        elif hasattr(x, "transform"):
            t = x.transform
            t.offset_x, t.offset_y = t.offset_x * s, t.offset_y * s
            moved.add(x.name)
            # 画像・動画は素材の px に拡大率を掛けた大きさ。カラー・調整レイヤー・シーンは画面いっぱいのまま
            if x.type in ("IMAGE", "MOVIE", "MOVIECLIP", "MASK"):
                t.scale_x, t.scale_y = t.scale_x * s, t.scale_y * s
                sized.add(x.name)

    def scaled(path: str) -> bool:
        found = _STRIP_PATH.match(path)
        if found is None:
            return False
        name, prop = found[1].replace('\\"', '"'), found[2]
        if prop in ("transform.offset_x", "transform.offset_y"):
            return name in moved
        return name in sized and prop in ("transform.scale_x", "transform.scale_y", "font_size")

    _scale_keys(scene, fy=lambda y: y * s, paths=scaled)
    settings = scene.tachie
    for ch in settings.characters:  # update で置いてある字幕・キャラ名に当て直す
        if ch.subtitle_width:
            ch.subtitle_width *= s
        ch.font_size = min(ch.font_size * s, ch.bl_rna.properties["font_size"].hard_max)
    settings.headline_size = min(settings.headline_size * s, settings.bl_rna.properties["headline_size"].hard_max)

