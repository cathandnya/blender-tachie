"""立ち絵の動き: 喋っている間に跳ねる・揺れる、キャラが現れる・消えるところの登場・退場（フェード・スライド）。

立ち絵ストリップ（セリフ・待機・セリフなし）の transform.offset_x / offset_y / rotation と blend_alpha に
キーフレームを打つ（連番には焼き込まない）。`refresh_motion` は管理しているキーフレームを全部消してから作り直すので、
何度呼んでもよい。喋っている区間は、セリフを書き出すときに保存した音量（lines/<id>.json）から取る。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import bpy

from .properties import apply_placement, place_text, text_offset

# 声が出ているとみなす音量（lipsync.mouth の無音のしきい値と同じ）
VOICED_RMS = 0.02
MANAGED = ("transform.offset_x", "transform.offset_y", "transform.rotation", "blend_alpha")
_PATH_RE = re.compile(r'^sequence_editor\.strips_all\["(?P<name>(?:[^"\\]|\\.)*)"\]\.(?P<attr>.+)$')


def owner(strip) -> str | None:
    """立ち絵ストリップのキャラ名（セリフ・待機・セリフなし）。立ち絵でなければ None。"""
    if strip.type != "IMAGE":
        return None
    return strip.get("tachie_character") or strip.get("tachie_idle") or strip.get("tachie_still")


def _channelbag(scene, create: bool = False):
    ad = scene.animation_data
    if ad is None:
        if not create:
            return None
        ad = scene.animation_data_create()
    if ad.action is None:
        if not create:
            return None
        ad.action = bpy.data.actions.new(f"{scene.name}Action")
    from bpy_extras import anim_utils

    slot = ad.action_slot
    if slot is None:
        return None
    return anim_utils.action_get_channelbag_for_slot(ad.action, slot)


def clear_motion(scene, names: set[str]) -> None:
    """管理しているキーフレーム（立ち絵ストリップ `names` と、もう無いストリップの分）を消す。"""
    bag = _channelbag(scene)
    if bag is None:
        return
    existing = {s.name for s in scene.sequence_editor.strips_all} if scene.sequence_editor else set()
    for fcurve in list(bag.fcurves):
        m = _PATH_RE.match(fcurve.data_path)
        if m is None or m["attr"] not in MANAGED:
            continue
        name = m["name"].replace('\\"', '"').replace("\\\\", "\\")
        if name in names or name not in existing:
            bag.fcurves.remove(fcurve)


def voiced_segments(rms: list[float], min_gap: int) -> list[tuple[int, int]]:
    """声が出ている区間 [(開始, 終わり)]（セリフの先頭からのフレーム）。`min_gap` より短い切れ目はつなぐ。"""
    segments: list[list[int]] = []
    for i, r in enumerate(rms):
        if r < VOICED_RMS:
            continue
        if segments and i - segments[-1][1] <= min_gap:
            segments[-1][1] = i + 1
        else:
            segments.append([i, i + 1])
    return [(a, b) for a, b in segments if b - a >= 2]


def _key(strip, path: str, frame: int, value: float) -> None:
    target = strip.transform if path.startswith("transform.") else strip
    attr = path.split(".")[-1]
    setattr(target, attr, value)
    target.keyframe_insert(attr, frame=frame)


def _talk(strip, ch, motion_path: Path, fps: float, height: int) -> None:
    """喋っている間、半周期ごとに跳ねる（上下）・揺れる（回転）キーフレームを打つ。"""
    if not motion_path.is_file():
        return
    from .tachie_studio.pipeline.render import LineMotion

    motion = LineMotion.load(motion_path)
    half = max(1, round(ch.talk_period * fps / 2))
    amp_y = ch.talk_bounce * height
    amp_r = math.radians(ch.talk_sway)
    base_y, start = strip.transform.offset_y, int(strip.content_start)
    for a, b in voiced_segments(motion.rms[:motion.voice_frames], half):
        frames = list(range(a, b, half)) + [b]
        for k, f in enumerate(frames):
            # 端（喋り始め・終わり）は元の位置、間は上と下・右と左を交互に
            edge = k in (0, len(frames) - 1)
            if amp_y:
                _key(strip, "transform.offset_y", start + f, base_y + (0 if edge else amp_y * (k % 2)))
            if amp_r:
                _key(strip, "transform.rotation", start + f, 0 if edge else amp_r * (1 if k % 2 else -1))


def spans(strips) -> list[list]:
    """続いて並んでいる立ち絵ストリップのまとまり（キャラが画面にいる区間）。"""
    out: list[list] = []
    for s in sorted(strips, key=lambda s: s.left_handle):
        if out and s.left_handle <= out[-1][-1].right_handle:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def _edge(strip, ch, effect: str, frame_from: int, frame_to: int, appearing: bool, width: int) -> None:
    """登場（appearing）・退場のキーフレーム。frame_from → frame_to の間に見えない状態と元の状態を行き来する。"""
    hidden_first = appearing
    if effect == "FADE":
        _key(strip, "blend_alpha", frame_from, 0.0 if hidden_first else 1.0)
        _key(strip, "blend_alpha", frame_to, 1.0 if hidden_first else 0.0)
    elif effect == "SLIDE":
        base = strip.transform.offset_x
        # 近い方の画面の端から入る（出る）。左右反転したストリップは offset の向きも逆
        visual = -base if strip.use_flip_x else base
        side = 1 if (visual > 0 or (visual == 0 and ch.position_x >= 0)) else -1
        off_visual = visual + side * width
        off = -off_visual if strip.use_flip_x else off_visual
        _key(strip, "transform.offset_x", frame_from, off if hidden_first else base)
        _key(strip, "transform.offset_x", frame_to, base if hidden_first else off)


def refresh_motion(scene, lines_dir: Path) -> int:
    """立ち絵の動きのキーフレームを作り直す。動きを付けたストリップの数を返す。"""
    sed = scene.sequence_editor
    if sed is None:
        return 0
    characters = {c.name: c for c in scene.tachie.characters}
    # 消した（名前を変えた）キャラの立ち絵に付けた動きも消す（作り直さないので残ったままになる）
    # 字幕とキャラ名のうち、フェードするものと前にフェードを打ったもの（tachie_fade_keyed）だけを触る
    # （フェードしない字幕に手で付けた不透明度やキーフレームは消さない）
    def fade_of(t) -> float:
        ch = characters.get(t.get("tachie_character"))
        return ch.subtitle_fade if ch is not None else 0.0

    texts = [s for s in sed.strips if s.type == "TEXT" and s.get("tachie_id")
             and (fade_of(s) > 0 or s.get("tachie_fade_keyed"))]
    clear_motion(scene, {s.name for s in sed.strips if owner(s) is not None} | {s.name for s in texts})
    images = [s for s in sed.strips if owner(s) in characters]
    fps = scene.render.fps / scene.render.fps_base
    width, height = scene.render.resolution_x, scene.render.resolution_y
    for s in images:  # キーフレームの無い元の状態
        apply_placement(s, characters[owner(s)])
        s.transform.rotation = 0.0
        s.blend_alpha = 1.0
    # セリフごとの字幕の位置（字幕ストリップでずらした量をキャラ名にも写す）。tachie_sub_x を持たない古いセリフの
    # 字幕は触らない（手で動かした location を、動きの作り直しのたびに既定の位置へ戻さないため）
    offsets = {s["tachie_id"]: text_offset(s) for s in sed.strips
               if s.type == "TEXT" and s.get("tachie_id") and s.get("tachie_role") != "name" and "tachie_sub_x" in s}
    for t in sed.strips:
        ch = characters.get(t.get("tachie_character"))
        if t.type != "TEXT" or ch is None or t.get("tachie_id") not in offsets:
            continue
        if t.get("tachie_role") == "name":
            t["tachie_sub_x"], t["tachie_sub_y"] = offsets[t["tachie_id"]]
        place_text(t, ch, t.get("tachie_placed_text", t.get("tachie_text", "")))
    count = 0
    for t in texts:  # 字幕（とキャラ名）のフェードイン
        t.blend_alpha = 1.0
        fade = fade_of(t)
        if fade > 0:
            end = min(t.left_handle + max(1, round(fade * fps)), t.right_handle - 1)
            _key(t, "blend_alpha", t.left_handle, 0.0)
            _key(t, "blend_alpha", max(end, t.left_handle + 1), 1.0)
            t["tachie_fade_keyed"] = True
            count += 1
        elif "tachie_fade_keyed" in t:
            del t["tachie_fade_keyed"]
    for ch in characters.values():
        mine = [s for s in images if owner(s) == ch.name and s.channel == ch.channel]
        if ch.talk_bounce > 0 or ch.talk_sway > 0:
            for s in mine:
                if s.get("tachie_id"):
                    _talk(s, ch, lines_dir / f"{s['tachie_id']}.json", fps, height)
                    count += 1
        duration = max(1, round(ch.effect_seconds * fps))
        for group in spans(mine):
            first, last = group[0], group[-1]
            d = duration
            if len(group) == 1 and ch.enter_effect != "NONE" and ch.exit_effect != "NONE":
                # 1 本だけのまとまりで登場と退場が重なると、同じフレームのキーが上書きされて片方が消える
                d = min(d, max(1, (first.right_handle - 1 - first.left_handle) // 2))
            if ch.enter_effect != "NONE":
                end = min(first.left_handle + d, first.right_handle - 1)
                _edge(first, ch, ch.enter_effect, first.left_handle, end, True, width)
                count += 1
            if ch.exit_effect != "NONE":
                begin = max(last.right_handle - 1 - d, last.left_handle)
                _edge(last, ch, ch.exit_effect, begin, last.right_handle - 1, False, width)
                count += 1
    return count


def motion_signature(scene) -> str:
    """動きに関わる値（立ち絵ストリップの位置・長さ、セリフごとの位置・大きさ・字幕の位置、キャラの動きの設定）。"""
    sed = scene.sequence_editor
    if sed is None:
        return ""
    chars = scene.tachie.characters
    settings = [(c.name, c.channel, round(c.talk_bounce, 4), round(c.talk_sway, 3), round(c.talk_period, 3),
                 c.enter_effect, c.exit_effect, round(c.effect_seconds, 3), round(c.subtitle_fade, 3)) for c in chars]
    active = any(s[2] or s[3] or s[5] != "NONE" or s[6] != "NONE" or s[8] for s in settings)
    items = []
    for s in sed.strips:
        if s.type == "TEXT" and s.get("tachie_id"):  # 字幕のフェードは字幕の位置で決まる
            offset = () if s.get("tachie_role") == "name" else tuple(round(v, 4) for v in text_offset(s))
            active = active or offset not in ((), (0.0, 0.0))
            items.append((s.name, s.channel, int(s.content_start), s.left_handle, s.right_handle, offset))
            continue
        if owner(s) is None:
            continue
        pose = (round(s.get("tachie_pos_x", 0.0), 4), round(s.get("tachie_pos_y", 0.0), 4),
                round(s.get("tachie_scale", 1.0), 4))
        active = active or pose != (0.0, 0.0, 1.0)
        items.append((s.name, s.channel, int(s.content_start), s.left_handle, s.right_handle, pose))
    if not active:
        return ""
    return repr((settings, sorted(items)))
