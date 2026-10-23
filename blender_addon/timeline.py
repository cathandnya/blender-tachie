"""タイムラインの fps・解像度（Blender の出力タブ）を、置いたときから変えていないか見る。

fps・解像度は最初に決めるもの（セリフの口パクのコマ・立ち絵の大きさ・字幕の位置がこれで決まる）。Blender は fps を
変えると音声だけ秒の長さを保ち（位置はフレーム数のまま）、他のストリップ・キーフレームはフレーム数のまま、解像度を
変えても px の値（位置・文字の大きさ・拡大率）はそのままなので、置いたあとに変えるとずれる。置いてあるものを変換する
機能は作らない（再生速度を変えた（リタイミングした）ストリップのキーは、Blender 5.2.2 では API で動かすと落ちる）。
置いたときの fps・解像度はシーンの `timeline_fps` / `timeline_width` / `timeline_height` に覚え、出力タブで変えられたら
（`changed_from`）パネルで知らせて「元に戻す」を出す。
"""

from __future__ import annotations

import bpy

from .tachie_studio.pipeline.render import LineMotion

FPS_EPS = 1e-4  # fps の比較の許し（timeline_fps は 32bit の float なので 59.94fps は 1e-6 ほどずれる）


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
    """今の fps・解像度を置いたときのものとして覚える（何も置いていないとき・`force`。置いたあとは変えない）。"""
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
