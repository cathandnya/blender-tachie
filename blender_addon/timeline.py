"""タイムラインの fps（Blender の出力タブ）を、置いたときから変えていないか見る。

fps は最初に決めるもの（セリフの口パクのコマがこれで決まる）。Blender は fps を変えると音声だけ秒の長さを保ち
（位置はフレーム数のまま）、他のストリップ・キーフレームはフレーム数のままなので、置いたあとに変えるとずれる。置いてある
ものを変換する機能は作らない（再生速度を変えた（リタイミングした）ストリップのキーは、Blender 5.2.2 では API で動かすと
落ちる）。置いたときの fps はシーンの `timeline_fps` に覚え、出力タブで変えられたら（`changed_from`）パネルで知らせて
「元に戻す」を出す。解像度は知らせない（横長と縦長を切り替えて作る。立ち絵・字幕は自動の反映が新しい解像度で置き直す）。
"""

from __future__ import annotations

import bpy

from .tachie_studio.pipeline.render import LineMotion

FPS_EPS = 1e-4  # fps の比較の許し（timeline_fps は 32bit の float なので 59.94fps は 1e-6 ほどずれる）


def current(scene) -> float:
    r = scene.render
    return r.fps / r.fps_base


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


def baseline(scene) -> float | None:
    """置いたときの fps。何も置いていなければ None。覚えていなければ（前の版）セリフの動きの fps。"""
    if not _has_strips(scene):
        return None
    if scene.tachie.timeline_fps > 0:
        return scene.tachie.timeline_fps
    return _line_fps(scene) or current(scene)


def remember(scene, force: bool = False) -> None:
    """今の fps を置いたときのものとして覚える（何も置いていないとき・`force`。置いたあとは変えない）。"""
    s = scene.tachie
    if force or not _has_strips(scene):
        s.timeline_fps = current(scene)
    elif s.timeline_fps <= 0:
        s.timeline_fps = baseline(scene)


def changed_from(scene) -> float | None:
    """出力タブなどで fps が置いたときから変わっていれば、置いたときの fps。"""
    base = baseline(scene)
    if base is None or abs(base - current(scene)) < FPS_EPS:
        return None
    return base
