"""場面転換の目印と「場面転換を外す」（blender-tools の押し出しと同じやり方）。

場面転換（`ymmp_media.transition_scenes`）を付けたら、その範囲に何も映らない（不透明度 0 の）カラーストリップ（目印、
`MARK` = {"token": 記録の番号}）を置き、付ける前の状態（前の場面・新しい場面の名前、打ったキーのフレームと元の値、伸ばした
お尻、移したチャンネル）をシーンの `REGISTRY`[token] に持つ。「場面転換を外す」（`tachie.remove_transition`）はそれで戻す。
目印を消しても外す: `depsgraph_update_post` で「記録はあるのに目印が無い」を見つけ、タイマー（`_sync`）で外す（ハンドラーの
中では書き換えない。画面の無いとき・再生中・書き出し中・リンクしたシーンでは外さない。smoke は `remove_orphans` を直接呼ぶ）。
前の場面・新しい場面が両方とも無い記録（切り取って貼り付ける途中など）は残す。外せなかったものは `_failed` に入れて繰り返さない。
付けたあとに場面を動かすと（Blender はキーも一緒に動かす）、片方だけなら場面転換が崩れ、目印は元の位置に残る。
「場面転換を付け直す」（`tachie.reapply_transition`、`reapply`）は外してから新しい場面の頭で同じ長さに付け直す。
"""

from __future__ import annotations

import uuid

import bpy
from bpy.app.handlers import persistent

from . import prefetch
from .motion import _channelbag
from .operators import _overlaps, seq_scene
from .ymmp_media import _fcurve, transition_label

MARK = "tachie_transition"  # 目印のストリップに持つ {"token": 記録の番号}
REGISTRY = "tachie_transitions"  # シーンに持つ場面転換の記録 {番号: 付ける前の状態}（目印を消しても残る）
TR_KEYS = ("type", "easing", "mode", "angle")  # 記録する場面転換の設定（テンプレートに登録するとき使う）


def _set(strip, attr: str, value: float) -> None:
    owner = strip.transform if attr.startswith("transform.") else strip
    setattr(owner, attr.split(".")[-1], value)


def _get(strip, attr: str) -> float:
    owner = strip.transform if attr.startswith("transform.") else strip
    return getattr(owner, attr.split(".")[-1])


def _point(point) -> dict:
    return {"co": list(point.co), "left": list(point.handle_left), "right": list(point.handle_right),
            "interpolation": point.interpolation, "easing": point.easing}


def _restore_point(point, saved, delta: float) -> None:
    point.co = (saved["co"][0] + delta, saved["co"][1])
    point.handle_left = (saved["left"][0] + delta, saved["left"][1])
    point.handle_right = (saved["right"][0] + delta, saved["right"][1])
    if "interpolation" in saved:
        point.interpolation, point.easing = saved["interpolation"], saved["easing"]


def _delta(strip, saved) -> float:
    """付けたあとにストリップを動かした量（Blender はストリップと一緒にキーも動かす）。"""
    origin = saved.get("origin")
    return strip.content_start - origin if origin is not None else 0.0


class Record:
    """付ける前の状態を、書き換える前に覚えていく（`transition_scenes` が書き換えるたびに呼ぶ）。"""

    def __init__(self, scene, tr: dict, before, after):
        self.scene = scene
        self.state = {"type": tr["type"], "tr": {k: tr[k] for k in TR_KEYS if tr.get(k) is not None},
                      "before": before.name,
                      "after": after.name if after is not None else "", "keys": [], "channels": {}, "ends": {},
                      "origins": {}, "speed": None}

    def keys(self, strip, attr: str, frames) -> None:
        """`strip` の `attr`（"blend_alpha"・"transform.offset_x"）の `frames` にキーを打つ前に呼ぶ（元の値と、そのフレームに
        もうあったキーを覚える。外すときに打ったキーを消して、元のキーを戻す）。"""
        frames = sorted(set(frames))
        fcurve = _fcurve(self.scene, strip, attr)
        old = [_point(p) for p in (fcurve.keyframe_points if fcurve is not None else ()) if round(p.co[0]) in frames]
        self.state["keys"].append({"strip": strip.name, "attr": attr, "frames": frames, "base": _get(strip, attr),
                                   "old": old, "origin": strip.content_start})

    def channel(self, strip) -> None:
        self.state["channels"].setdefault(strip.name, strip.channel)

    def end(self, strip) -> None:
        self.state["ends"].setdefault(strip.name, strip.right_handle)
        self.state["origins"].setdefault(strip.name, strip.content_start)

    def speed_key(self, strip, point) -> None:
        """fps 合わせの補正の終わりのキー（伸ばす前の位置）。"""
        self.state["speed"] = {"strip": strip.name, **_point(point), "origin": strip.content_start}

    def mark(self, start: int, end: int, label: str, avoid: list) -> None:
        """目印を置いて記録をシーンに入れる。"""
        scene = self.scene
        channel = _free_channel(scene, start, end, [s.channel for s in avoid])
        token = uuid.uuid4().hex[:12]
        marker = _new_marker(scene.sequence_editor, label, channel, start, end, token)
        if REGISTRY not in scene:
            scene[REGISTRY] = {}
        self.state.update(start=start, end=end, marker=marker.name)
        scene[REGISTRY][token] = self.state


def _new_marker(sed, name: str, channel: int, start: int, end: int, token: str):
    marker = sed.strips.new_effect(name, "COLOR", channel, start, length=max(1, end - start))
    marker.color = (0.3, 0.5, 0.9)
    marker.blend_alpha = 0.0
    marker.color_tag = "COLOR_05"
    marker[MARK] = {"token": token}
    return marker


def _free_channel(scene, start: int, end: int, used: list[int]) -> int:
    """目印のチャンネル: 場面のすぐ上から空いているところ（場面がアドオンのレーンより下ならレーンより下を先に）。"""
    sed = scene.sequence_editor
    base = scene.tachie.channel_base
    top = max(used)
    order = (list(range(top + 1, base)) + list(range(1, min(used)))) if top < base else []
    order += list(range(max(top, base) + 1, 129))
    for c in order:
        if not _overlaps(sed, c, start, end):
            return c
    return 128


def overlapping(scene, names: set[str], start: int, end: int) -> bool:
    """`names` の場面に、[start, end) と重なる場面転換がもうあるか。"""
    return any({state.get("before"), state.get("after")} & names
               and state.get("start", 0) < end and start < state.get("end", 0)
               for state in _registry(scene).values())


def _registry(scene):
    return scene.get(REGISTRY) or {}


def _is_marker(sed, token: str, state) -> bool:
    marker = sed.strips_all.get(state.get("marker", ""))
    return marker is not None and marker.get(MARK, {}).get("token") == token


def record_of(scene, strip):
    """目印のストリップの記録（無ければ None）。"""
    token = strip.get(MARK, {}).get("token")
    state = _registry(scene).get(token) if token else None
    return state if state is not None and state.get("marker") == strip.name else None


def selected(scene, sed) -> tuple[list[str], list[str]]:
    """選んでいる場面転換の記録の番号（目印か、前の場面・新しい場面を選んでいるもの。新しい順）と、記録の無い目印
    （複製・貼り付けしたもの）の名前。"""
    registry = _registry(scene)
    picked = [s for s in sed.strips_all if s.select]
    names = {s.name for s in picked}
    tokens, ghosts = set(), []
    for s in picked:
        token = s.get(MARK, {}).get("token")
        if token is None:
            continue
        if token in registry and registry[token].get("marker") == s.name:
            tokens.add(token)
        else:
            ghosts.append(s.name)
    tokens |= {t for t, state in registry.items() if state.get("before") in names or state.get("after") in names}
    return sorted(tokens, key=lambda t: registry[t].get("start", 0), reverse=True), ghosts


def remove(scene, token: str, keep_channels: bool = False) -> list[str]:
    """場面転換を外して付ける前に戻す（打ったフレームのキーだけ消す・伸ばしたお尻・移したチャンネル）。目印が残っていれば
    消し、記録も消す。`keep_channels` なら移したチャンネルは戻さない（付け直すとき）。知らせることを返す。"""
    sed = scene.sequence_editor
    registry = scene[REGISTRY]
    state = registry[token]
    notes = []
    for key in state.get("keys", []):
        strip = sed.strips_all.get(key["strip"])
        if strip is None:
            notes.append(f"場面転換を付けた「{key['strip']}」が見つからないので、残った方だけ戻しました")
            continue
        fcurve = _fcurve(scene, strip, key["attr"])
        if fcurve is not None:
            delta = _delta(strip, key)
            frames = {round(f + delta) for f in key["frames"]}
            # 消すと後ろのキーの参照がずれるので、後ろから番号で引き直して消す
            for i in reversed([i for i, p in enumerate(fcurve.keyframe_points) if round(p.co[0]) in frames]):
                fcurve.keyframe_points.remove(fcurve.keyframe_points[i])
            for saved in key.get("old", []):  # 打つ前からあったキーを戻す
                _restore_point(fcurve.keyframe_points.insert(saved["co"][0] + delta, saved["co"][1]), saved, delta)
            if len(fcurve.keyframe_points):
                fcurve.update()
                continue  # ほかの動き（登場など）のキーが残っていれば、値はそのキーに従う
            _channelbag(scene).fcurves.remove(fcurve)
        _set(strip, key["attr"], key["base"])
    # 伸ばしたお尻を戻す（fps 合わせの補正は元の動画と、終わりのキー）
    speed = state.get("speed")
    if speed is not None:
        strip = sed.strips_all.get(speed["strip"])
        fcurve = _fcurve(scene, strip, "speed_frame_number") if strip is not None else None
        if fcurve is not None and len(fcurve.keyframe_points) == 2:
            k1 = max(fcurve.keyframe_points, key=lambda k: k.co[0])
            _restore_point(k1, speed, _delta(strip, speed))
            fcurve.update()
    origins = state.get("origins", {})
    for name, right in state.get("ends", {}).items():
        strip = sed.strips_all.get(name)
        if strip is None:
            continue
        right = round(right + _delta(strip, {"origin": origins.get(name)}))
        if strip.right_handle == right:
            continue
        if strip.right_handle > right or not _overlaps(sed, strip.channel, strip.right_handle, right, ignore=[strip]):
            strip.right_handle = right
        else:
            notes.append(f"「{name}」のお尻を元の位置まで戻せませんでした（他のストリップがあります）")
    # 上へ移したチャンネルを戻す（下へ動かすので下のものから。戻す先が埋まっていれば戻さない）
    moves = [] if keep_channels else [(sed.strips_all.get(n), c) for n, c in state.get("channels", {}).items()]
    for strip, channel in sorted(((s, c) for s, c in moves if s is not None and s.channel != c), key=lambda p: p[1]):
        if _overlaps(sed, channel, strip.left_handle, strip.right_handle, ignore=[strip]):
            notes.append(f"「{strip.name}」を元のチャンネル {channel} に戻せませんでした（他のストリップがあります）")
        else:
            strip.channel = channel
    if _is_marker(sed, token, state):
        marker = sed.strips_all[state["marker"]]
        meta = marker.parent_meta()
        (meta.strips if meta is not None else sed.strips).remove(marker)
    del registry[token]
    return notes


ANIMATED = ("blend_alpha", "transform.offset_x", "transform.offset_y", "speed_frame_number")  # 場面転換がキーを打つもの


def _moved(strip, state) -> float:
    """付けたあとに `strip` を動かした量。"""
    for key in state.get("keys", []):
        if key["strip"] == strip.name:
            return _delta(strip, key)
    return _delta(strip, {"origin": state.get("origins", {}).get(strip.name)})


def _snapshot(scene, state) -> dict:
    """付け直しに失敗したときに戻すための、外す前の場面（fps 合わせの補正は元の動画も）のお尻とキーと、目印。"""
    sed = scene.sequence_editor
    strips = {}
    for name in (state.get("before"), state.get("after")):
        strip = sed.strips_all.get(name or "")
        for s in ([strip] + ([strip.input_1] if strip.type == "SPEED" else [])) if strip is not None else ():
            curves = {}
            for attr in ANIMATED:
                if attr == "speed_frame_number" and s.type != "SPEED":
                    continue
                fcurve = _fcurve(scene, s, attr)
                curves[attr] = (_get(s, attr), [_point(p) for p in fcurve.keyframe_points] if fcurve else None)
            strips[s.name] = {"right": s.right_handle, "curves": curves}
    marker = sed.strips_all.get(state.get("marker", ""))
    return {"strips": strips, "marker": None if marker is None else
            {"name": marker.name, "channel": marker.channel, "start": marker.left_handle, "end": marker.right_handle}}


def _rollback(scene, token: str, state: dict, snap: dict) -> None:
    """`remove(keep_channels=True)` で外した場面転換を `_snapshot` の状態に戻す（付け直しを断ったとき）。"""
    sed = scene.sequence_editor
    bag = _channelbag(scene, create=True)
    for name, saved in snap["strips"].items():
        strip = sed.strips_all.get(name)
        if strip is None:
            continue
        if strip.type != "SPEED":  # 補正の長さは元の動画に従う
            strip.right_handle = saved["right"]
        for attr, (value, points) in saved["curves"].items():
            fcurve = _fcurve(scene, strip, attr)
            if points is None:
                if fcurve is not None:
                    bag.fcurves.remove(fcurve)
                _set(strip, attr, value)
                continue
            if fcurve is None:
                fcurve = bag.fcurves.new(f"{strip.path_from_id()}.{attr}")
            fcurve.keyframe_points.clear()
            fcurve.keyframe_points.add(len(points))
            for point, p in zip(fcurve.keyframe_points, points):
                _restore_point(point, p, 0.0)
            fcurve.update()
    m = snap["marker"]
    if m is not None:
        marker = _new_marker(sed, m["name"], m["channel"], m["start"], m["end"], token)
        state["marker"] = marker.name
    scene[REGISTRY][token] = state


def reapply(scene, token: str) -> list[str]:
    """場面を動かしたあとの場面転換を付け直す: 外してから、新しい場面の頭（新しい場面が無ければ前の場面を動かした分ずらした
    位置）で同じ長さ・同じ設定で付け直し、目印も置き直す。移したチャンネルは戻さずに、記録の元のチャンネルを引き継ぐ。
    付けられなければ何も変えずに理由を投げる（`ValueError`）。知らせることを返す。"""
    from .ymmp_media import transition_scenes

    sed = scene.sequence_editor
    registry = scene[REGISTRY]
    state = registry[token].to_dict()
    label = transition_label(state.get("type"))
    before = sed.strips_all.get(state.get("before", ""))
    after = sed.strips_all.get(state["after"]) if state.get("after") else None
    if before is None or (state.get("after") and after is None):
        raise ValueError(f"{label}: 前の場面か新しい場面が見つからないので付け直せませんでした")
    start = after.left_handle if after is not None else round(state["start"] + _moved(before, state))
    if start <= before.left_handle:
        where = f"新しい場面「{after.name}」" if after is not None else "場面転換の位置"
        raise ValueError(f"{label}: {where}が前の場面「{before.name}」より前から始まっているので付け直せませんでした")
    snap = _snapshot(scene, state)
    tokens = set(registry.keys())
    notes = remove(scene, token, keep_channels=True)
    problems = []
    tr = {"type": state["type"], **state.get("tr", {})}
    try:
        ok = transition_scenes(scene, before, after, tr, start, start + state["end"] - state["start"], problems)
    except Exception as e:  # 外したまま残さない
        problems.append(f"{label}: 付け直せませんでした: {e}")
        ok = False
    if not ok:
        for t in set(registry.keys()) - tokens:  # 途中で入った記録と目印は捨てる
            if _is_marker(sed, t, registry[t]):
                sed.strips.remove(sed.strips_all[registry[t]["marker"]])
            del registry[t]
        _rollback(scene, token, state, snap)
        raise ValueError("\n".join(problems) or f"{label}: 付け直せませんでした")
    new, = set(registry.keys()) - tokens
    # 移したチャンネルは付ける前のもの（今回移したものより前の記録を優先）
    registry[new]["channels"] = {**registry[new]["channels"].to_dict(),
                                 **{n: c for n, c in state.get("channels", {}).items() if n in sed.strips_all}}
    return notes + problems


def _orphans(scene) -> list[str]:
    """目印を消した場面転換の記録の番号（新しい順）。前の場面・新しい場面が両方とも無いもの（切り取って貼り付ける途中
    など）は外さずに残す。書き換えられないシーン（リンクしたものなど）は見ない。"""
    sed = scene.sequence_editor
    registry = scene.get(REGISTRY)
    if sed is None or not registry or scene.library is not None:
        return []
    out = []
    for token, state in registry.items():
        if token in _failed or _is_marker(sed, token, state):
            continue
        if sed.strips_all.get(state.get("before", "")) is None and sed.strips_all.get(state.get("after", "")) is None:
            continue
        out.append(token)
    return sorted(out, key=lambda t: registry[t].get("start", 0), reverse=True)


def remove_orphans(scene) -> list[str]:
    """目印を消した場面転換を外す。外せなかったものは繰り返さない（`_failed`）。知らせることを返す。"""
    orphaned = _orphans(scene)
    notes = []
    if orphaned:
        with prefetch.paused():
            for token in orphaned:
                try:
                    notes += remove(scene, token)
                except Exception as e:  # 同じ失敗をシーンの更新のたびに繰り返さないように
                    _failed.add(token)
                    notes.append(f"場面転換を外せませんでした: {e}")
    return notes


_failed: set[str] = set()  # 外せなかった場面転換の記録の番号（ファイルを開き直すまで試さない）


def _sync():
    """タイマー: 目印を消した場面転換を全シーンで外し、戻せなかったところがあれば画面に知らせる。"""
    if bpy.app.is_job_running("RENDER"):
        return 0.5  # 書き出しの最中は待つ
    notes = []
    for scene in bpy.data.scenes:
        notes += remove_orphans(scene)
    if notes:
        wm = bpy.context.window_manager
        if wm.windows:
            def draw(self, _context):
                for note in notes:
                    self.layout.label(text=note)

            with bpy.context.temp_override(window=wm.windows[0]):
                wm.popup_menu(draw, title="目印を消した場面転換を外しました", icon="INFO")
    return None


@persistent
def _on_depsgraph_update(_scene, _depsgraph=None) -> None:
    """目印が消えたら場面転換を外す（ハンドラーの中では書き換えず、タイマーで）。画面の無いときと再生中は見ない。"""
    if bpy.app.background or bpy.app.timers.is_registered(_sync):
        return
    screen = getattr(bpy.context, "screen", None)
    if screen is not None and screen.is_animation_playing:
        return
    if any(_orphans(scene) for scene in bpy.data.scenes if REGISTRY in scene):
        bpy.app.timers.register(_sync, first_interval=0.0)


@persistent
def _on_load_post(*_args) -> None:
    _failed.clear()


class TACHIE_OT_remove_transition(bpy.types.Operator):
    """場面転換を外して、付ける前に戻す（目印か、前の場面・新しい場面を選んで。目印を消しても外れます）"""
    bl_idname = "tachie.remove_transition"
    bl_label = "場面転換を外す"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = seq_scene(context)
        sed = scene.sequence_editor
        tokens, ghosts = selected(scene, sed) if sed is not None else ([], [])
        if not tokens:
            if ghosts:
                self.report({"ERROR"}, f"「{ghosts[0]}」には場面転換の記録がありません（複製・貼り付けした目印です）。"
                                       "目印を消すだけで構いません")
            else:
                self.report({"ERROR"}, "場面転換の目印か、場面転換を付けた画像・動画を選んでください")
            return {"CANCELLED"}
        for token in tokens:
            for note in remove(scene, token):
                self.report({"WARNING"}, note)
        self.report({"INFO"}, f"{len(tokens)} か所の場面転換を外しました")
        return {"FINISHED"}


class TACHIE_OT_reapply_transition(bpy.types.Operator):
    """場面を動かしたあとの場面転換を、新しい場面の頭で付け直す（目印か、前の場面・新しい場面を選んで）"""
    bl_idname = "tachie.reapply_transition"
    bl_label = "付け直す"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = seq_scene(context)
        sed = scene.sequence_editor
        tokens, _ghosts = selected(scene, sed) if sed is not None else ([], [])
        if not tokens:
            self.report({"ERROR"}, "場面転換の目印か、場面転換を付けた画像・動画を選んでください")
            return {"CANCELLED"}
        done = 0
        for token in reversed(tokens):  # 前から順に
            try:
                notes = reapply(scene, token)
            except ValueError as e:
                self.report({"WARNING"}, str(e))
                continue
            done += 1
            for note in notes:
                self.report({"WARNING"}, note)
        if not done:
            self.report({"ERROR"}, "場面転換を付け直せませんでした")
            return {"CANCELLED"}
        self.report({"INFO"}, f"{done} か所の場面転換を付け直しました")
        return {"FINISHED"}


_classes = (TACHIE_OT_remove_transition, TACHIE_OT_reapply_transition)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    for handlers, func in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
                           (bpy.app.handlers.load_post, _on_load_post)):
        if func in handlers:
            handlers.remove(func)
    if bpy.app.timers.is_registered(_sync):
        bpy.app.timers.unregister(_sync)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
