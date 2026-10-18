"""Blender Tachie: VOICEVOX の音声・口パクする動く立ち絵・字幕を Video Sequencer に並べる。"""

import bpy

from . import channels, faceassign, operators, panels, prefetch, properties, templates, ymmp_import

_modules = (properties, operators, channels, ymmp_import, templates, faceassign, panels)
# ストリップを書き換えるオペレーター。実行中はシーケンサーのプリフェッチを止める（止めないと Blender が落ちることがある）。
# 待っている間もタイマーで呼ばれる modal（YMM4 の読み込み・AI での表情の割り当て）は、書き換えるところだけ止める
_GUARDED = (operators, channels, ymmp_import, templates, faceassign)
_MODAL_SELF_GUARDED = {"TACHIE_OT_import_ymmp", "TACHIE_OT_assign_faces"}


def register():
    for m in _GUARDED:
        for cls in getattr(m, "_classes", ()):
            if issubclass(cls, bpy.types.Operator):
                prefetch.guard_class(cls, modal=cls.__name__ not in _MODAL_SELF_GUARDED)
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
