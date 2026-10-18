"""シーケンサーのプリフェッチ（止まっている間に先のフレームを裏で描く）を、ストリップを書き換える間だけ止める。

Blender 自身の操作はストリップを消す・作る前にプリフェッチを止めるが、Python の API（strips.remove / new_* / channel・
elements・F カーブの書き換え）では止まらない。裏で描いている途中のストリップを消すと、消えたストリップを読みに行って
Blender ごと落ちる（ストリップをドラッグした直後の自動の反映で起きた）。`use_prefetch` を切ると Blender は裏の処理を
止めるので、書き換えの前に切り、終わったら戻す。
"""

from __future__ import annotations

import functools
from contextlib import contextmanager

import bpy

_depth = 0


@contextmanager
def paused():
    """全シーンのプリフェッチを止める（入れ子にしてよい。いちばん外側で戻す）。"""
    global _depth
    stopped = []
    if _depth == 0:
        for scene in bpy.data.scenes:
            sed = scene.sequence_editor
            if sed is not None and sed.use_prefetch:
                sed.use_prefetch = False
                stopped.append(scene)
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        for scene in stopped:  # 名前ではなく ID で持つ（途中でシーンの名前が変わっても戻す）
            try:
                sed = scene.sequence_editor
            except ReferenceError:  # 途中で消えたシーン
                continue
            if sed is not None:
                sed.use_prefetch = True


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
