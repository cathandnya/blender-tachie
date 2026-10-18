"""シーケンサーのチャンネル: キャラ × 役割のレーン（`tachie_studio.channels`）をシーンに当てる。

レーンはキャラクター一覧（順・立ち絵の有無・キャラ名を出すか）と `channel_base` から毎回計算し、前に当てた
レイアウト（`applied_layout`）と違えば、印の付いたストリップをレーンへ動かしてチャンネルに名前を付ける
（`ensure_layout`）。`base` より下（動画・背景・効果音・BGM）は触らない。
"""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from .tachie_studio.channels import Item, LaneChar, Layout, assign, channel_layout, move_plan

DEFAULT_NAME = "Channel {}"


def overlaps(sed, channel: int, start: int, end: int, ignore=()) -> bool:
    """チャンネルの [start, end) に他のストリップがあるか（待機ストリップは作り直すので邪魔にならない）。"""
    return any(s.channel == channel and s.left_handle < end and start < s.right_handle and s not in ignore
               and not s.get("tachie_idle") for s in sed.strips)


def lane_key(strip) -> tuple[str, str | None] | None:
    """ストリップの印から、置くレーン (役割, キャラ)。アドオンが置いたものでなければ None（動かさない）。"""
    if strip.get("tachie_face"):
        return "face", strip["tachie_face"]
    if strip.get("tachie_headline"):
        return "headline", None
    if strip.get("tachie_id"):
        char = strip.get("tachie_character")
        if strip.type == "SOUND":
            return "audio", char
        if strip.type == "IMAGE":
            return "image", char
        if strip.type == "TEXT":
            return ("name" if strip.get("tachie_role") == "name" else "subtitle"), char
        return None
    if strip.type == "IMAGE" and (strip.get("tachie_idle") or strip.get("tachie_still")):
        return "image", strip.get("tachie_idle") or strip.get("tachie_still")
    return None


def scene_chars(scene) -> list[LaneChar]:
    """レーンを持つキャラ: キャラクター一覧の順。消した・名前を変えたキャラのストリップが残っていれば、後ろに
    「未登録」として足す（他のレーンとぶつからないように）。"""
    chars, known = [], set()
    for ch in scene.tachie.characters:  # 同じ名前のキャラ（追加して名前を変える途中など）は最初の 1 人だけ
        if ch.name not in known:
            chars.append(LaneChar(ch.name, bool(ch.folder), ch.show_name))
            known.add(ch.name)
    sed = scene.sequence_editor
    orphans: dict[str, set[str]] = {}
    for s in sed.strips if sed else ():
        key = lane_key(s)
        if key and key[1] and key[1] not in known and not s.get("tachie_idle"):  # 待機は作り直すので数えない
            orphans.setdefault(key[1], set()).add(key[0])
    for name, roles in orphans.items():
        chars.append(LaneChar(name, bool(roles & {"image", "face"}), "name" in roles, registered=False))
    return chars


def scene_layout(scene) -> Layout:
    return channel_layout(scene.tachie.channel_base, scene_chars(scene))


def lane(scene, role: str, char: str | None = None) -> int | None:
    return scene_layout(scene).channel(role, char)


def free_channel(sed, layout: Layout, start: int, end: int, first: int | None = None, taken=(), ignore=()) -> int:
    """レーンが埋まっているときの置き場所（あふれ）: `first`（既定は top の上）から上で、[start, end) が空いているところ。"""
    channel = max(first or 0, layout.top + 1)
    while channel in taken or (sed is not None and overlaps(sed, channel, start, end, ignore)):
        channel += 1
        if channel > 128:
            raise ValueError(f"{start}〜{end} フレームに空いているチャンネルがありません")
    return channel


def role_channel(scene, role: str, char: str | None, start: int, end: int, taken=(), ignore=(),
                 first: int | None = None) -> int:
    """役割のレーンが [start, end) で空いていればレーン、埋まっていればあふれ（`first` 以上）。"""
    layout = scene_layout(scene)
    sed = scene.sequence_editor
    channel = layout.channel(role, char)
    if channel is not None and channel not in taken and (first is None or channel >= first) and (
            sed is None or not overlaps(sed, channel, start, end, ignore)):
        return channel
    return free_channel(sed, layout, start, end, first, taken, ignore)


def face_channel(scene, char: str, start: int, end: int) -> int:
    """表情ストリップのチャンネル: そのキャラの表情のレーン。重なる表情があれば上（後から置いた表情ほど優先）。"""
    sed = scene.sequence_editor
    above = max((s.channel + 1 for s in sed.strips if s.get("tachie_face") == char
                 and s.left_handle < end and start < s.right_handle), default=None) if sed else None
    return role_channel(scene, "face", char, start, end, first=above)


def sound_channel(scene, start: int, end: int) -> int:
    """効果音・YMM4 の音声のチャンネル: レーンより下（1 番から上）の空き。無ければレーンより上。"""
    layout = scene_layout(scene)
    sed = scene.sequence_editor
    for channel in range(1, layout.base):
        if sed is None or not overlaps(sed, channel, start, end):
            return channel
    return free_channel(sed, layout, start, end)


# ---- レーンへ並べ直す ----

def _sync_cached(scene, layout: Layout) -> None:
    """キャラの立ち絵チャンネルと見出しのチャンネル（レイアウトから決まる値。待機・セリフなし・見出しが読む）。"""
    settings = scene.tachie
    for ch in settings.characters:
        channel = layout.channel("image", ch.name)
        if channel is not None and ch.channel != channel:
            ch.channel = channel
    if settings.headline_channel != layout.channel("headline"):
        settings.headline_channel = layout.channel("headline")


def ensure_layout(scene) -> list[str]:
    """レイアウトが前に当てたものと違えば並べ直す。問題（動かせなかったストリップなど）を返す。"""
    try:
        layout = scene_layout(scene)
    except ValueError as e:  # チャンネルが足りないなど
        return [str(e)]
    problems = []
    if scene.tachie.applied_layout != layout.to_json():
        problems = relayout(scene, layout)
    _sync_cached(scene, layout)
    return problems


def relayout(scene, layout: Layout | None = None, force: bool = False) -> list[str]:
    """印の付いたストリップをレーンへ動かし（埋まっていれば top より上）、チャンネルに名前を付ける。
    動かすのは前のレーンにちょうど乗っているものだけで、手で別のチャンネルに動かしたもの（とあふれたもの）はそのまま。
    `force`（ボタン「チャンネルを整理する」）と、前のレイアウトが無い（古い .blend）ときは全部をレーンへ。
    レーンより上の印の無いもの（YMM4 の手前の画像など）は top の増減の分まとめてずらす。"""
    settings = scene.tachie
    layout = layout or scene_layout(scene)
    old = Layout.from_json(settings.applied_layout)
    sed = scene.sequence_editor
    problems: list[str] = []
    if sed is not None:
        moved, problems = _move_strips(sed, layout, old, {ch.name for ch in settings.characters}, force)
        if not moved:  # 並べ替えられなかった: 当てたことにしない（パネルで「合っていません」と出し、次で直す）
            _sync_cached(scene, layout)
            return problems
        _rename_channels(sed, layout, old)
    settings.applied_layout = layout.to_json()
    settings.layout_version = 1
    _sync_cached(scene, layout)
    return problems


def _move_strips(sed, layout: Layout, old: Layout | None, registered: set[str],
                 force: bool = False) -> tuple[bool, list[str]]:
    """(動かせたか, 問題)。動かせなかった（行き先が決まらない）ときは何も動かさない。"""
    # 一覧にいないキャラの待機は消す（待機は作り直すもので、消したキャラの分は要らない）
    for s in [s for s in sed.strips if s.get("tachie_idle") and s["tachie_idle"] not in registered]:
        sed.strips.remove(s)
    strips = list(sed.strips)
    tagged = [(s, k) for s in strips if (k := lane_key(s)) is not None]
    tagged_set = {s for s, _ in tagged}
    kept, above = [], set()
    if not force and old is not None:
        # 手で動かしたもの（前のレーンにいないもの）は動かさない。前にレーンが無かった役割（キャラ名を出すようにした・
        # 素材を付けたなど）はレーンへ。前の top より上（あふれ）にいるものは、印の無いものと一緒にずらす
        # （レーンが増えたときにレーンの中に取り残されないように）
        kept = [s for s, k in tagged if (c := old.channel(*k)) is not None and c != s.channel]
        above = {s for s in kept if s.channel > old.top}
        kept_set = set(kept)
        tagged = [(s, k) for s, k in tagged if s not in kept_set]
    # 前のいちばん下より下にあった印の無いもの（背景など）は、いちばん下を下げても動かさない（手前へ飛ばさない）
    below = max(layout.base, old.base) if old is not None else layout.base
    front = [s for s in strips if (s not in tagged_set or s in above) and s.channel >= below]
    fixed = [(s.channel, s.left_handle, s.right_handle) for s in strips
             if s not in tagged_set and s.channel < below]
    fixed += [(s.channel, s.left_handle, s.right_handle) for s in kept if s not in above]
    # レーンより上の印の無いものは、レーンと重ならないように（減ったときは詰めて）まとめてずらす
    delta = 0
    if front:
        low = min(s.channel for s in front)
        if low <= layout.top:
            delta = layout.top + 1 - low
        elif old is not None and low > old.top and layout.top < old.top:
            delta = max(layout.top + 1 - low, layout.top - old.top)
    targets: dict = {}
    problems = []
    for s in front:
        target = s.channel + delta
        if target > 128:
            problems.append(f"{s.name}: チャンネルが足りないので動かしていません")
            target = s.channel
        targets[s.name] = target
    obstacles = fixed + [(targets[s.name], s.left_handle, s.right_handle) for s in front]
    items = []
    for s, (role, char) in tagged:
        # 表情は今の優先（チャンネルが上・後から始まるほど優先）を保つ
        priority = (s.channel, s.left_handle) if role == "face" else (0, s.left_handle)
        items.append(Item(s.name, role, char, s.left_handle, s.right_handle, priority))
    try:
        targets.update(assign(items, layout, obstacles))
        current = {s.name: (s.channel, s.left_handle, s.right_handle) for s in strips if s.name in targets}
        steps = move_plan(current, targets, fixed)
    except ValueError as e:
        return False, problems + [str(e)]
    by_name = {s.name: s for s in strips}
    for name, channel in steps:
        by_name[name].channel = channel
    for name, channel in targets.items():
        if by_name[name].channel != channel:
            problems.append(f"{name}: チャンネル {channel} に動かせませんでした（{by_name[name].channel} にあります）")
    return True, problems


def _rename_channels(sed, layout: Layout, old: Layout | None) -> None:
    """レーンに名前を付け、ミュート・ロックをレーンと一緒に移す。空いたチャンネルは、アドオンが付けた名前なら戻す。"""
    channels = sed.channels
    states = {}
    if old is not None:
        for lane in old.lanes:
            c = channels[lane.channel]
            states[(lane.role, lane.char)] = (c.mute, c.lock)
    new = {lane.channel for lane in layout.lanes}
    if old is not None:
        for lane in old.lanes:
            c = channels[lane.channel]
            if lane.channel not in new:
                if c.name == lane.label:
                    c.name = DEFAULT_NAME.format(lane.channel)
                c.mute = c.lock = False
    for lane in layout.lanes:
        c = channels[lane.channel]
        c.name = lane.label
        if old is not None:
            c.mute, c.lock = states.get((lane.role, lane.char), (False, False))


class TACHIE_OT_relayout(bpy.types.Operator):
    """アドオンが置いたストリップを、キャラ × 役割のチャンネル（レーン）に並べ直し、チャンネルに名前を付ける"""

    bl_idname = "tachie.relayout"
    bl_label = "チャンネルを整理する"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from .operators import seq_scene

        scene = seq_scene(context)
        try:
            problems = relayout(scene, force=True)
        except ValueError as e:  # チャンネルが足りない（キャラが多すぎる・いちばん下が高すぎる）
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        for p in problems:
            self.report({"WARNING"}, p)
        if not problems:
            self.report({"INFO"}, f"チャンネルを {scene.tachie.channel_base} 番から並べ直しました")
        return {"FINISHED"}


@persistent
def _on_load(_):
    """.blend を開いたら、キャラのいるシーンのチャンネルをレーンに合わせる（古い .blend の移行も）。"""
    for scene in bpy.data.scenes:
        if len(scene.tachie.characters):
            for p in ensure_layout(scene):
                print("チャンネルを整理できません:", p)


_classes = (TACHIE_OT_relayout,)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
