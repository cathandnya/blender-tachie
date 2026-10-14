"""Blender Tachie: VOICEVOX の音声・口パクする動く立ち絵・字幕を Video Sequencer に並べる。"""

import bpy

from . import faceassign, operators, panels, properties, templates, ymmp_import

_modules = (properties, operators, ymmp_import, templates, faceassign, panels)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
