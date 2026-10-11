"""Blender を画面ありで起動して、立ち絵パネルを開いたシーケンサーのスクリーンショットを撮る。

    BLENDER_USER_RESOURCES=out/smoke/blender_user blender --window-geometry 0 0 1600 1100 \
        out/smoke/smoke.blend --python scripts/blender_screenshot.py -- out/screenshots

scripts/smoke.sh で拡張機能を入れた一時ユーザーフォルダと smoke.blend を使う。
サイドバーのタブは API で切り替えられないので、撮影用にシーケンサーのサイドバーの他のパネルを外して
立ち絵タブだけにし、閉じているパネルも開いた状態で登録し直す（この Blender を終了すれば元に戻る）。
"""

import sys
from pathlib import Path

import addon_utils
import bpy

out = Path(sys.argv[sys.argv.index("--") + 1])
out.mkdir(parents=True, exist_ok=True)
addon_utils.enable("bl_ext.user_default.blender_tachie", default_set=True)
bpy.context.preferences.view.show_splash = False


def _depth(cls) -> int:
    parent = getattr(cls, "bl_parent_id", "")
    return 0 if not parent else 1 + _depth(getattr(bpy.types, parent, type("", (), {})))


def isolate_tachie_panels():
    panels = [c for c in bpy.types.Panel.__subclasses__()
              if getattr(c, "bl_space_type", "") == "SEQUENCE_EDITOR" and getattr(c, "bl_region_type", "") == "UI"
              and getattr(c, "is_registered", False)]
    ours = [c for c in panels if c.__module__.startswith("bl_ext.")]
    for c in sorted(panels, key=_depth, reverse=True):
        bpy.utils.unregister_class(c)
    for c in sorted(ours, key=_depth):
        c.bl_options = set(getattr(c, "bl_options", set())) - {"DEFAULT_CLOSED"}
        bpy.utils.register_class(c)


def setup():
    isolate_tachie_panels()
    window = bpy.context.window_manager.windows[0]
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    # Blender 5 のシーケンサーはワークスペースのシーケンサー用シーンを表示する
    window.workspace.sequencer_scene = bpy.context.scene
    area.type = "SEQUENCE_EDITOR"
    space = area.spaces.active
    space.view_type = "SEQUENCER_PREVIEW"
    space.show_region_ui = True

    scene = bpy.context.scene
    sed = scene.sequence_editor
    lines = {}
    for st in sed.strips:
        if st.get("tachie_id"):
            lines.setdefault(st["tachie_id"], []).append(st)
    third = sorted(lines.values(), key=lambda sts: min(s.left_handle for s in sts))[2]
    text = next(s for s in third if s.type == "TEXT")
    for st in sed.strips_all:
        st.select = False
    text.select = True
    sed.active_strip = text
    scene.frame_set((text.left_handle + text.right_handle) // 2)
    scene.tachie.active_character = 0
    return None


def fit():
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        if area.type != "SEQUENCE_EDITOR":
            continue
        for region in area.regions:
            if region.type in ("WINDOW", "PREVIEW"):
                with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region,
                                               space_data=area.spaces.active):
                    try:
                        if region.type == "PREVIEW":
                            bpy.ops.sequencer.view_all_preview()
                        else:
                            bpy.ops.sequencer.view_all()
                    except RuntimeError as e:
                        print("全体表示できません:", region.type, e)
    return None


def shoot():
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        path = out / "sequencer.png"
        bpy.ops.screen.screenshot(filepath=str(path))
    print("撮影:", path)
    return None


def quit_blender():
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(setup, first_interval=2.0)
bpy.app.timers.register(fit, first_interval=3.5)
bpy.app.timers.register(shoot, first_interval=6.0)
bpy.app.timers.register(quit_blender, first_interval=7.0)
