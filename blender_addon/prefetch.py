"""シーケンサーのプリフェッチ（止まっている間に先のフレームを裏で描く）を、ストリップを書き換える間だけ止める。

Blender 自身の操作はストリップを消す・作る前にプリフェッチを止めるが、Python の API（strips.remove / new_* / channel・
elements・F カーブの書き換え）では止まらない。裏で描いている途中のストリップを消すと、消えたストリップを読みに行って
Blender ごと落ちる（ストリップをドラッグした直後の自動の反映で起きた）。`use_prefetch` を切ると Blender は裏の処理を
止めるので、書き換えの前に切り、終わったら戻す。
"""

from __future__ import annotations

import functools
import time
from contextlib import contextmanager

import bpy
from bpy.app.handlers import persistent

_depth = 0
_pending: set[str] = set()  # 書き換えたあと、少し待ってからプリフェッチを戻すシーン（名前。undo で ID が作り直されるため）
_wanted: set[str] = set()  # アドオンが止めたことのあるシーン（undo / redo で止めた状態に戻ったら戻す）
_resume_at = 0.0  # 最後に書き換え終えてから RESUME_SECONDS 後（time.monotonic）
RESUME_SECONDS = 0.5


@contextmanager
def paused():
    """全シーンのプリフェッチを止める（入れ子にしてよい。いちばん外側を抜けたら戻す）。

    画面があるときは、戻すのを少し待つ（`RESUME_SECONDS`）。書き換えた直後に戻すと、Blender がシーケンサーの中身を
    更新し終える前の古い状態（消したストリップ）のままプリフェッチが描き始め、落ちることがあった（立ち絵を分割して
    ドラッグしたあとの自動の反映で起きた）。画面の無いとき（smoke）はタイマーが回らないので、すぐ戻す。"""
    global _depth
    stopped = []
    if _depth == 0:
        for scene in bpy.data.scenes:
            sed = scene.sequence_editor
            if sed is not None and sed.use_prefetch:
                sed.use_prefetch = False
                stopped.append(scene.name)
                _wanted.add(scene.name)
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        if bpy.app.background:
            _restore(stopped)
        else:
            _pending.update(stopped)
            if _depth == 0 and _pending:
                _schedule()  # 続けて書き換えたら、最後から数え直す


def _restore(names) -> None:
    for name in names:
        scene = bpy.data.scenes.get(name)  # 途中で消えた・名前を変えたシーンは戻せない
        sed = scene.sequence_editor if scene is not None else None
        if sed is not None:
            sed.use_prefetch = True


def _schedule() -> None:
    global _resume_at
    _resume_at = time.monotonic() + RESUME_SECONDS
    if not bpy.app.timers.is_registered(_resume):
        bpy.app.timers.register(_resume, first_interval=RESUME_SECONDS)


def _resume():
    if _depth > 0:  # まだ書き換えている
        return RESUME_SECONDS
    wait = _resume_at - time.monotonic()
    if wait > 0.01:  # 待っている間にまた書き換えた
        return wait
    resume_now()
    return None


def resume_now() -> None:
    """待っているシーンのプリフェッチをすぐ戻す。"""
    scenes = list(_pending)
    _pending.clear()
    _restore(scenes)


@persistent
def _on_save_pre(*_args):
    # 待っている間に保存すると、プリフェッチを切ったまま .blend に残る
    if _depth == 0:
        resume_now()


@persistent
def _on_undo(*_args):
    # 操作の undo の段には止めていた use_prefetch = False が残るので、その段へ戻ったら（少し待って）戻す
    if _depth == 0 and not bpy.app.background:
        for name in _wanted:
            scene = bpy.data.scenes.get(name)
            sed = scene.sequence_editor if scene is not None else None
            if sed is not None and not sed.use_prefetch:
                _pending.add(name)
        if _pending:
            _schedule()


@persistent
def _on_load_pre(*_args):
    _pending.clear()  # 前のファイルのシーン
    _wanted.clear()


def register() -> None:
    bpy.app.handlers.save_pre.append(_on_save_pre)
    bpy.app.handlers.load_pre.append(_on_load_pre)
    bpy.app.handlers.undo_post.append(_on_undo)
    bpy.app.handlers.redo_post.append(_on_undo)


def unregister() -> None:
    for handlers, func in ((bpy.app.handlers.save_pre, _on_save_pre), (bpy.app.handlers.load_pre, _on_load_pre),
                           (bpy.app.handlers.undo_post, _on_undo), (bpy.app.handlers.redo_post, _on_undo)):
        if func in handlers:
            handlers.remove(func)
    if bpy.app.timers.is_registered(_resume):
        bpy.app.timers.unregister(_resume)
    resume_now()  # 無効にしたあとはタイマーで戻せない


def guarded(func):
    """関数の実行中はプリフェッチを止める。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with paused():
            return func(*args, **kwargs)
    return wrapper


def _guard_execute(func):
    @functools.wraps(func)
    def execute(self, context):
        with paused():
            return func(self, context)
    execute._prefetch_guarded = True
    return execute


def _guard_modal(func):
    @functools.wraps(func)
    def modal(self, context, event):
        with paused():
            return func(self, context, event)
    modal._prefetch_guarded = True
    return modal


def guard_class(cls, modal: bool = True) -> None:
    """オペレーターの execute（と modal）の間はプリフェッチを止める（登録する前に呼ぶ）。
    Blender は登録のときに引数の数を確かめるので、元と同じ引数で包む。"""
    func = cls.__dict__.get("execute")
    if func is not None and not getattr(func, "_prefetch_guarded", False):
        cls.execute = _guard_execute(func)
    func = cls.__dict__.get("modal")
    if modal and func is not None and not getattr(func, "_prefetch_guarded", False):
        cls.modal = _guard_modal(func)
