"""場面転換の目印と記録（blender-tools の押し出しと同じ形）。

場面転換（`ymmp_media.transition_scenes`）を付けたら、その範囲に何も映らない（不透明度 0 の）カラーストリップ（目印、
`MARK` = {"token": 記録の番号}）を置き、付ける前の状態をシーンの `REGISTRY`[token] に持つ。どちらも blender-tools の
押し出しと同じ名前・同じ形なので、「付け直す」「外す」・目印を消したときに外すのは blender-tools が行う（このアドオンには
置かない）。記録の欄（blender-tools と決めたもの）:

- a / b: 前の場面・新しい場面のストリップの名前（新しい場面が無ければ ""）。left / right: 範囲 [left, right)。marker: 目印の名前
- a_right: 前の場面の伸ばす前のお尻。b_left: 新しい場面の頭。b_channel: 新しい場面の移す前のチャンネル
  （b_channels: 移したストリップごとの元のチャンネル。fps 合わせの補正は元の動画も移す）。crops: {}（画面外は切らない）
- kind: "push"（既定）/ "fade"
- プッシュ: attr（"offset_x" / "offset_y"）と base_a / base_b（キーの元の位置）。キーは前の場面 (left, base_a) → (right-1,
  base_a+距離)、新しい場面 (left, base_b-距離) → (right-1, base_b)、イージングは動き始めのキー。斜めは axes
  （{attr: {"base_a", "base_b"}}、attr / base_* は最初の軸）。新しい場面が動いていて前の場面だけ押したときは b_keys = False。
  前の場面が新しい場面の下で続く（a_right > right）ときは、前の場面に right で元の位置に戻すキー（a_return = True）
- フェード: target（"a" / "b"、上にある方）と full（不透明度）。キーは blend_alpha の (left, ·) → (right-1, ·)
  （前の場面は full → 0、新しい場面は 0 → full）
- speed_a: 前の場面が fps 合わせの補正（印 `tachie_fps_speed` の SPEED、フレーム番号の 2 つの直線のキー）のとき、
  {"movie": 元の動画の名前, "right": 伸ばす前のお尻, "end_key": 伸ばす前の終わりのキー [フレーム, 値]}。伸ばすときは元の動画の
  お尻と終わりのキーを同じ傾きで延ばす（`ymmp_media._extend`）
- tr: {type, easing, mode, angle}（このアドオンでテンプレートに登録するとき使う）
"""

from __future__ import annotations

import uuid

MARK = "vtools_push"  # 目印のストリップに持つ {"token": 記録の番号}（blender-tools と同じ）
REGISTRY = "vtools_pushes"  # シーンに持つ記録 {番号: 付ける前の状態}（blender-tools と同じ）
TR_KEYS = ("type", "easing", "mode", "angle")  # 記録する場面転換の設定（テンプレートに登録するとき使う）


class Record:
    """付ける前の状態を、書き換える前に覚えていく（`transition_scenes` が書き換えるたびに呼ぶ）。"""

    def __init__(self, scene, tr: dict, before, after):
        self.scene = scene
        self.before, self.after = before, after
        self.state = {"kind": "fade" if tr["type"] == "fade" else "push",
                      "tr": {k: tr[k] for k in TR_KEYS if tr.get(k) is not None},
                      "a": before.name, "b": after.name if after is not None else "",
                      "a_right": before.right_handle,
                      "b_left": after.left_handle if after is not None else 0,
                      "b_channel": after.channel if after is not None else 0,
                      "b_channels": {}, "crops": {}}
        self.bases: dict[str, dict[str, float]] = {}  # プッシュの軸 → {"a" / "b": 元の位置}

    def keys(self, strip, attr: str, value: float, back: bool = False) -> None:
        """`strip` の `attr`（"blend_alpha"・"transform.offset_x"）にキーを打つ前に呼ぶ（`value` はキーの元の値。`back` は
        前の場面を範囲の終わりで元の位置に戻すキーも打つとき）。"""
        role = "a" if strip == self.before else "b"
        if attr == "blend_alpha":
            self.state.update(target=role, full=value)
            return
        self.bases.setdefault(attr.split(".")[-1], {})[role] = value
        if back:
            self.state["a_return"] = True

    def channel(self, strip) -> None:
        self.state["b_channels"].setdefault(strip.name, strip.channel)

    def speed(self, movie, point) -> None:
        """前の場面が fps 合わせの補正のとき、伸ばす前の元の動画のお尻と終わりのキー。"""
        self.state["speed_a"] = {"movie": movie.name, "right": movie.right_handle, "end_key": list(point.co)}

    def mark(self, start: int, end: int, label: str, avoid: list) -> None:
        """目印を置いて記録をシーンに入れる。"""
        scene = self.scene
        if self.state["kind"] == "push" and self.bases:
            def base(attr: str, role: str) -> float:
                # キーを打たなかった方（動いている新しい場面）は今の位置（0 にすると外すときにその位置へ動かされる）
                if role in self.bases[attr]:
                    return self.bases[attr][role]
                strip = self.before if role == "a" else self.after
                return getattr(strip.transform, attr) if strip is not None else 0.0

            attrs = list(self.bases)
            self.state.update(attr=attrs[0], base_a=base(attrs[0], "a"), base_b=base(attrs[0], "b"))
            if "b" not in self.bases[attrs[0]]:
                self.state["b_keys"] = False
            if len(attrs) > 1:
                self.state["axes"] = {a: {"base_a": base(a, "a"), "base_b": base(a, "b")} for a in attrs}
        sed = scene.sequence_editor
        channel = _free_channel(scene, start, end, [s.channel for s in avoid])
        marker = sed.strips.new_effect(label, "COLOR", channel, start, length=max(1, end - start))
        marker.color = (0.3, 0.5, 0.9)
        marker.blend_alpha = 0.0
        marker.color_tag = "COLOR_05"
        token = uuid.uuid4().hex[:12]
        marker[MARK] = {"token": token}
        if REGISTRY not in scene:
            scene[REGISTRY] = {}
        self.state.update(left=start, right=end, marker=marker.name)
        scene[REGISTRY][token] = self.state


def _free_channel(scene, start: int, end: int, used: list[int]) -> int:
    """目印のチャンネル: 場面のすぐ上から空いているところ（場面がアドオンのレーンより下ならレーンより下を先に）。"""
    from .operators import _overlaps

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
    """`names` の場面に、[start, end) と重なる場面転換（blender-tools で付けた押し出しも）がもうあるか。"""
    return any({state.get("a"), state.get("b")} & names
               and state.get("left", 0) < end and start < state.get("right", 0)
               for state in (scene.get(REGISTRY) or {}).values())


def record_of(scene, strip):
    """目印のストリップの記録（無ければ None）。"""
    token = strip.get(MARK, {}).get("token")
    state = (scene.get(REGISTRY) or {}).get(token) if token else None
    return state if state is not None and state.get("marker") == strip.name else None
