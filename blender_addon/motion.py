"""立ち絵の動き: 喋っている間に跳ねる・揺れる、キャラが現れる・消えるところの登場・退場（フェード・スライド）。

立ち絵ストリップ（立ち絵と、前の版で置いたセリフ・待機の立ち絵）の transform.offset_x / offset_y / rotation と
blend_alpha にキーフレームを打つ（連番には焼き込まない）。`refresh_motion` は管理しているキーフレームを全部消してから
作り直すので、何度呼んでもよい。喋っている区間は、立ち絵に重なるそのキャラのセリフを書き出すときに保存した音量
（lines/<id>.json）から取る。
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import bpy

from .properties import apply_placement, apply_text_size, lay, name_height, place_text, text_offset, text_style
from .tachie_studio.ymmp import jump_keys

# 声が出ているとみなす音量（lipsync.mouth の無音のしきい値と同じ）
VOICED_RMS = 0.02
MANAGED = ("transform.offset_x", "transform.offset_y", "transform.rotation", "blend_alpha",
           "transform.scale_x", "transform.scale_y")
_PATH_RE = re.compile(r'^sequence_editor\.strips_all\["(?P<name>(?:[^"\\]|\\.)*)"\]\.(?P<attr>.+)$')
# 立ち絵ごとの位置・大きさ（ユーザーがキーフレームを打てる。アドオンは打たない）と既定値
POSE_KEYS = {"tachie_pos_x": 0.0, "tachie_pos_y": 0.0, "tachie_scale": 1.0}
_POSE_RE = re.compile(r'^sequence_editor\.strips_all\["(?P<name>(?:[^"\\]|\\.)*)"\]\["(?P<key>%s)"\]$'
                      % "|".join(POSE_KEYS))


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


def pose_curves(scene) -> dict[str, dict]:
    """立ち絵ストリップの名前 → {位置・大きさのキー（`POSE_KEYS`）: そのキーフレームの F カーブ}。"""
    bag = _channelbag(scene)
    out: dict[str, dict] = {}
    for fcurve in bag.fcurves if bag is not None else ():
        m = _POSE_RE.match(fcurve.data_path)
        if m is not None and len(fcurve.keyframe_points):
            name = m["name"].replace('\\"', '"').replace("\\\\", "\\")
            out.setdefault(name, {})[m["key"]] = fcurve
    return out


LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
_POINT_ATTRS = ("interpolation", "easing", "handle_left_type", "handle_right_type")


def save_pose_keys(scene, strip) -> list:
    """`strip` の位置・大きさのキーフレーム（ストリップを消すと Blender が F カーブも消すので、作り直す前に覚える）。"""
    return [(key, fcurve.extrapolation,
             [(tuple(p.co), tuple(p.handle_left), tuple(p.handle_right), *(getattr(p, a) for a in _POINT_ATTRS))
              for p in fcurve.keyframe_points])
            for key, fcurve in pose_curves(scene).get(strip.name, {}).items()]


def restore_pose_keys(scene, strip, saved: list) -> None:
    """`save_pose_keys` で覚えたキーフレームを `strip` に打ち直す（もう F カーブがあるもの＝同じストリップは触らない）。"""
    if not saved:
        return
    have = pose_curves(scene).get(strip.name, {})
    bag = _channelbag(scene, create=True)
    for key, extrapolation, points in saved:
        if key in have or bag is None:
            continue
        path = strip.path_from_id(f'["{key}"]')
        fcurve = bag.fcurves.find(path) or bag.fcurves.new(path)  # キーの無い F カーブが残っていればそれに打つ
        fcurve.keyframe_points.clear()
        fcurve.extrapolation = extrapolation
        fcurve.keyframe_points.add(len(points))
        for p, (co, left, right, *attrs) in zip(fcurve.keyframe_points, points):
            p.co, p.handle_left, p.handle_right = co, left, right
            for a, v in zip(_POINT_ATTRS, attrs):
                setattr(p, a, v)
        fcurve.update()


def _pose_signature(curves: dict) -> tuple:
    """位置・大きさのキーフレーム（打ち直したら動きを作り直す）。外挿・モディファイアも区間の値を変える。"""
    return tuple((key, f.extrapolation, tuple(m.type for m in f.modifiers),
                  tuple((tuple(round(v, 4) for v in (*p.co, *p.handle_left, *p.handle_right)), p.interpolation, p.easing)
                        for p in f.keyframe_points))
                 for key, f in sorted(curves.items()))


def _apply_pose_curves(scene, strip, curves: dict, width: int, height: int) -> None:
    """位置・大きさにキーフレームを打った立ち絵: 打ったキーの値（の今のフレームの値 = apply_placement が当てた元の位置）
    からの差を、アドオンの動き（跳ねる・登場・退場など）に重ねて、立ち絵の区間の毎フレームに打ち直す。"""
    bag = _channelbag(scene, create=True)
    frames = range(strip.left_handle, strip.right_handle)
    static = {k: float(strip.get(k, d)) for k, d in POSE_KEYS.items()}

    def pose(key: str) -> list[float]:
        fcurve = curves.get(key)
        return [fcurve.evaluate(f) if fcurve else static[key] for f in frames]

    flip = -1 if strip.use_flip_x else 1  # 反転したストリップは offset の向きも逆
    changes = []  # (transform の属性, フレームごとの値, 掛けるか（でなければ足す）)
    if "tachie_pos_x" in curves:
        changes.append(("offset_x", [flip * (v - static["tachie_pos_x"]) * width for v in pose("tachie_pos_x")], False))
    if "tachie_pos_y" in curves:
        changes.append(("offset_y", [(v - static["tachie_pos_y"]) * height for v in pose("tachie_pos_y")], False))
    if "tachie_scale" in curves:
        base = max(static["tachie_scale"], 1e-3)
        ratios = [max(v, 0.0) / base for v in pose("tachie_scale")]
        changes += [("scale_x", ratios, True), ("scale_y", ratios, True)]
    for attr, values, multiply in changes:
        path = strip.transform.path_from_id(attr)
        fcurve = bag.fcurves.find(path)
        motion = [fcurve.evaluate(f) for f in frames] if fcurve else [getattr(strip.transform, attr)] * len(frames)
        if fcurve is None:
            fcurve = bag.fcurves.new(path)
        else:
            fcurve.keyframe_points.clear()
        fcurve.keyframe_points.add(len(frames))
        fcurve.keyframe_points.foreach_set(
            "co", [x for f, m, v in zip(frames, motion, values) for x in (f, m * v if multiply else m + v)])
        fcurve.keyframe_points.foreach_set("interpolation", [LINEAR] * len(frames))
        fcurve.update()


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


def _talk(strip, ch, motion_path: Path, fps: float, height: int, start: int | None = None) -> None:
    """喋っている間、半周期ごとに跳ねる（上下）・揺れる（回転）キーフレームを打つ。

    `start` はセリフの動きの頭のフレーム（None ならストリップの中身の頭。前の版のセリフの立ち絵）。
    声の区間はストリップの区間で切る。"""
    if not motion_path.is_file():
        return
    from .tachie_studio.pipeline.render import LineMotion

    motion = LineMotion.load(motion_path)
    half = max(1, round(ch.talk_period * fps / 2))
    amp_y = ch.talk_bounce * height
    amp_r = math.radians(ch.talk_sway)
    base_r = base_rotation(strip)
    base_y = strip.transform.offset_y
    start = int(strip.content_start) if start is None else start
    lo, hi = strip.left_handle - start, strip.right_handle - 1 - start
    for a, b in voiced_segments(motion.rms[:motion.voice_frames], half):
        a, b = max(a, lo), min(b, hi)
        if b - a < 2:
            continue
        frames = list(range(a, b, half)) + [b]
        for k, f in enumerate(frames):
            # 端（喋り始め・終わり）は元の位置、間は上と下・右と左を交互に
            edge = k in (0, len(frames) - 1)
            if amp_y:
                _key(strip, "transform.offset_y", start + f, base_y + (0 if edge else amp_y * (k % 2)))
            if amp_r:
                _key(strip, "transform.rotation", start + f,
                     base_r + (0 if edge else amp_r * (1 if k % 2 else -1)))


def motion_paths(sed, lines_dir: Path):
    """セリフの口の動き（lines/<id>.json）のパスを返す関数。音声ストリップの wav の隣を先に見る（未保存のときに一時
    フォルダへ書き出したセリフは、保存して出力フォルダが変わっても wav の隣にある）。無ければ出力フォルダの lines。
    音声ストリップは最初に 1 回だけ集める（セリフ × 立ち絵の数だけ全ストリップを見ないように）。"""
    sounds = {}
    for s in sed.strips:
        if s.type == "SOUND" and s.sound and (clip_id := s.get("tachie_id")) and clip_id not in sounds:
            sounds[clip_id] = s.sound.filepath

    def find(clip_id: str) -> Path:
        if clip_id in sounds:
            beside = Path(bpy.path.abspath(sounds[clip_id])).with_suffix(".json")
            if beside.is_file():
                return beside
        return lines_dir / f"{clip_id}.json"

    return find


def _line_heads(sed, name: str) -> list[tuple[str, int, int]]:
    """キャラ `name` のセリフ（ミュートは除く）の [(id, 動きの頭, 終わり)]。動きの頭は音声ストリップの中身の頭。"""
    out = []
    for s in sed.strips:
        if s.type == "SOUND" and s.get("tachie_id") and s.get("tachie_character") == name and not s.mute:
            head = int(s.content_start)
            out.append((s["tachie_id"], head, head + (int(s.get("tachie_frames", 0)) or s.right_handle - head)))
    return out


def base_rotation(strip) -> float:
    """立ち絵ごとの回転角（`tachie_rotation`、度、YMM4 と同じく時計回りが正）→ transform.rotation（ラジアン、反時計回りが正）。"""
    return -math.radians(float(strip.get("tachie_rotation", 0.0)))


def base_opacity(strip) -> float:
    return float(strip.get("tachie_opacity", 1.0))


def _appears(fade: float, rise: float, grow: float, seconds: float) -> bool:
    """字幕の出し方があるか（`refresh_motion` の appear_of の値）。"""
    return fade > 0 or rise > 0 or grow < 1


def _interpolate(scene, owner, attr: str, interpolation: str, easing: str | None = None) -> None:
    """`owner`（ストリップか transform）の `attr` の F カーブのキーの補間を変える。"""
    bag = _channelbag(scene)
    path = owner.path_from_id(attr)  # 名前の " や \ もエスケープされた F カーブのパス
    fcurve = next((f for f in bag.fcurves if f.data_path == path), None) if bag else None
    for kp in fcurve.keyframe_points if fcurve else ():
        kp.interpolation = interpolation
        if easing:
            kp.easing = easing


def _ease_out(scene, owner, attr: str) -> None:
    """速く動き始めてゆっくり止まるようにする（字幕の浮き上がる・拡大）。"""
    _interpolate(scene, owner, attr, "CUBIC", "EASE_OUT")


def _constant(scene, strip, attr: str) -> None:
    """なめらかにつながず切り替える（喋る時のみ表示）。"""
    _interpolate(scene, strip, attr, "CONSTANT")


def _visibility(scene, strip, fps: float, talks: list[tuple[str, int, int]]) -> set[str]:
    """立ち絵ごとのフェード（`tachie_still_fade_in` / `_out`）と喋る時のみ表示（`tachie_speech_only`）の不透明度のキー。
    打った側（"in" / "out"）を返す（その側にはキャラのフェードの登場・退場を打たない）。"""
    opacity = base_opacity(strip)
    left, last = strip.left_handle, strip.right_handle - 1
    if strip.get("tachie_speech_only"):
        # 重なるそのキャラのセリフの間だけ見せる（セリフの区間の境目で切り替える）。重なる・続くセリフはつなぐ
        # （同じキャラのセリフも重ねられるので、先のセリフの終わりで消さない）
        shown: list[list[int]] = []
        for a, b in sorted((max(left, a), min(last + 1, b)) for _, a, b in talks if a < last + 1 and left < b):
            if shown and a <= shown[-1][1]:
                shown[-1][1] = max(shown[-1][1], b)
            else:
                shown.append([a, b])
        _key(strip, "blend_alpha", left, 0.0)
        for a, b in shown:
            _key(strip, "blend_alpha", a, opacity)
            if b <= last:
                _key(strip, "blend_alpha", b, 0.0)
        _constant(scene, strip, "blend_alpha")
        return {"in", "out"}
    fade_in = round(float(strip.get("tachie_still_fade_in", 0.0)) * fps)
    fade_out = round(float(strip.get("tachie_still_fade_out", 0.0)) * fps)
    if not (fade_in or fade_out):
        return set()
    if fade_in and fade_out:  # 重なれば半分ずつ
        fade_in, fade_out = min(fade_in, (last - left) // 2), min(fade_out, (last - left) // 2)
    if fade_in:
        _key(strip, "blend_alpha", left, 0.0)
        _key(strip, "blend_alpha", min(last, left + max(1, fade_in)), opacity)
    if fade_out:
        _key(strip, "blend_alpha", max(left, last - max(1, fade_out)), opacity)
        _key(strip, "blend_alpha", last, 0.0)
    return {side for side, n in (("in", fade_in), ("out", fade_out)) if n}


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
        opacity = base_opacity(strip)
        _key(strip, "blend_alpha", frame_from, 0.0 if hidden_first else opacity)
        _key(strip, "blend_alpha", frame_to, opacity if hidden_first else 0.0)
    elif effect == "SLIDE":
        base = strip.transform.offset_x
        # 近い方の画面の端から入る（出る）。左右反転したストリップは offset の向きも逆
        visual = -base if strip.use_flip_x else base
        side = 1 if (visual > 0 or (visual == 0 and lay(ch, "position_x") >= 0)) else -1
        off_visual = visual + side * width
        off = -off_visual if strip.use_flip_x else off_visual
        _key(strip, "transform.offset_x", frame_from, off if hidden_first else base)
        _key(strip, "transform.offset_x", frame_to, base if hidden_first else off)


def jump_of(strip) -> dict | None:
    """立ち絵の跳ねながら登場退場（`tachie_jump`。YMM4 の立ち絵アイテムの映像エフェクトから）。"""
    try:
        return json.loads(strip["tachie_jump"]) if strip.get("tachie_jump") else None
    except ValueError:
        return None


def _jump(strip, jump: dict, fps: float, width: int, height: int, appearing: bool, limit: int) -> None:
    """跳ねながら登場（appearing）・退場のキーフレーム（`jump_keys`）。退場は登場を逆に再生する（反転（退場時）が
    オフならずれの向きも逆）。`limit` はキーを打てるフレーム数（登場と退場が重ならないように）。収まらなければ
    動き全体を `limit` に縮める（途中で切ると、ずれたまま・跳ねたままの値が残るため）。"""
    sign = (-1 if jump.get("in_reverse") else 1) if appearing else (1 if jump.get("out_reverse") else -1)
    flip = -1 if strip.use_flip_x else 1  # 反転したストリップは offset の向きも逆
    # 基準はキーの無い元の位置・大きさ（apply_placement が当てた値。前のキーで変わった値を使わない）
    base_x, base_y = strip.transform.offset_x, strip.transform.offset_y
    base_s = float(strip.get("tachie_scale", 1.0))
    dx, dy = sign * jump["x"] * width * flip, sign * jump["y"] * height
    keys = jump_keys(jump, fps)
    limit = max(0, limit)
    shrink = min(1.0, limit / keys[-1][0]) if keys[-1][0] else 1.0
    for f, rest, h, sy in keys:
        f = round(f * shrink)
        frame = strip.left_handle + f if appearing else strip.right_handle - 1 - f
        _key(strip, "transform.offset_x", frame, base_x + dx * rest)
        _key(strip, "transform.offset_y", frame, base_y + dy * rest + h * height)
        _key(strip, "transform.scale_y", frame, base_s * sy)
        _key(strip, "transform.scale_x", frame, base_s * (2 - sy))


def refresh_motion(scene, lines_dir: Path) -> int:
    """立ち絵の動きのキーフレームを作り直す（テンプレートなどの登場・退場の動きはストリップの端に合わせる）。
    動きを付けたストリップの数を返す。"""
    from .ymmp_media import sync_edges

    sed = scene.sequence_editor
    if sed is None:
        return 0
    sync_edges(scene)
    characters = {c.name: c for c in scene.tachie.characters}
    # 消した（名前を変えた）キャラの立ち絵に付けた動きも消す（作り直さないので残ったままになる）
    # 字幕とキャラ名のうち、出し方（フェード・浮き上がる・拡大）のあるものと前にキーを打ったもの（tachie_fade_keyed）
    # だけを触る（出し方の無い字幕に手で付けた不透明度・変形やキーフレームは消さない）
    def appear_of(t) -> tuple[float, float, float, float]:
        """(フェードの秒数, 浮き上がる量, 始めの大きさ, 動く秒数)"""
        ch = characters.get(t.get("tachie_character"))
        if ch is None:
            return 0.0, 0.0, 1.0, 0.0
        return ch.subtitle_fade, ch.subtitle_rise, ch.subtitle_grow, ch.subtitle_motion_seconds

    texts = [s for s in sed.strips if s.type == "TEXT" and s.get("tachie_id")
             and (_appears(*appear_of(s)) or s.get("tachie_fade_keyed"))]
    clear_motion(scene, {s.name for s in sed.strips if owner(s) is not None} | {s.name for s in texts})
    images = [s for s in sed.strips if owner(s) in characters]
    fps = scene.render.fps / scene.render.fps_base
    width, height = scene.render.resolution_x, scene.render.resolution_y
    for s in images:  # キーフレームの無い元の状態
        apply_placement(s, characters[owner(s)])
        s.transform.rotation = base_rotation(s)
        s.blend_alpha = base_opacity(s)
    # セリフごとの字幕の位置（字幕ストリップでずらした量をキャラ名にも写す）。tachie_sub_x を持たない古いセリフの
    # 字幕は触らない（手で動かした location を、動きの作り直しのたびに既定の位置へ戻さないため）
    offsets = {s["tachie_id"]: (text_offset(s), text_style(s)) for s in sed.strips
               if s.type == "TEXT" and s.get("tachie_id") and s.get("tachie_role") != "name" and "tachie_sub_x" in s}
    for t in sed.strips:
        ch = characters.get(t.get("tachie_character"))
        if t.type != "TEXT" or ch is None or t.get("tachie_id") not in offsets:
            continue
        text = t.get("tachie_placed_text", t.get("tachie_text", ""))
        if t.get("tachie_role") == "name":
            (t["tachie_sub_x"], t["tachie_sub_y"]), (t["tachie_sub_size"], t["tachie_sub_width"]) = offsets[t["tachie_id"]]
            size = lay(ch, "font_size") * 0.6  # キャラ名の大きさ（apply_name と同じ。縦長用の配置で変わる）
            if abs(t.font_size - size) > 1e-6:
                t.font_size = size
        else:
            apply_text_size(t, ch, text)  # セリフごとの文字の大きさ・折り返し幅
        place_text(t, ch, text)
    count = 0
    for t in texts:  # 字幕（とキャラ名）の出し方: フェードイン・下から浮き上がる・拡大
        fade, rise, grow, seconds = appear_of(t)
        t.blend_alpha = 1.0
        moves = rise > 0 or grow < 1
        if moves or "tachie_rise_keyed" in t:  # フェードだけの字幕に手で付けた変形は戻さない
            t.transform.offset_x = t.transform.offset_y = 0.0
            t.transform.scale_x = t.transform.scale_y = 1.0
        if moves:
            t["tachie_rise_keyed"] = True
        elif "tachie_rise_keyed" in t:
            del t["tachie_rise_keyed"]
        if not _appears(fade, rise, grow, seconds):
            if "tachie_fade_keyed" in t:
                del t["tachie_fade_keyed"]
            continue
        left = t.left_handle

        def end_of(sec: float) -> int:
            return max(left + 1, min(left + max(1, round(sec * fps)), t.right_handle - 1))

        if fade > 0:
            _key(t, "blend_alpha", left, 0.0)
            _key(t, "blend_alpha", end_of(fade), 1.0)
        if moves:
            # TEXT ストリップの拡大の中心は origin が (0.5, 0.5) のとき画面の中央（ほかの値では字幕の大きさで変わる）。
            # 字幕の下端の中央（location、anchor_y = BOTTOM）が動かないように、縮めた分を offset でずらす。
            # 浮き上がる・拡大は同じ長さ・同じイージングなので、途中のコマでもずらす量が合う
            t.transform.origin = (0.5, 0.5)
            end = end_of(seconds)
            shrink = 1.0 - grow
            start = ((t.location[0] - 0.5) * width * shrink, (t.location[1] - 0.5) * height * shrink - rise * height)
            for axis, value in zip(("offset_x", "offset_y"), start):
                if value:
                    _key(t, f"transform.{axis}", left, value)
                    _key(t, f"transform.{axis}", end, 0.0)
                    _ease_out(scene, t.transform, axis)
            if grow < 1:
                for axis in ("scale_x", "scale_y"):
                    _key(t, f"transform.{axis}", left, grow)
                    _key(t, f"transform.{axis}", end, 1.0)
                    _ease_out(scene, t.transform, axis)
        t["tachie_fade_keyed"] = True
        count += 1
    motion_path = motion_paths(sed, lines_dir)
    for ch in characters.values():
        mine = [s for s in images if owner(s) == ch.name]  # チャンネルでは絞らない（手で動かした立ち絵も）
        talks = _line_heads(sed, ch.name)
        if ch.talk_bounce > 0 or ch.talk_sway > 0:
            for s in mine:
                if s.get("tachie_id"):  # 前の版のセリフの立ち絵
                    _talk(s, ch, motion_path(s["tachie_id"]), fps, height)
                    count += 1
                elif s.get("tachie_still"):  # 重なるセリフの間
                    for clip_id, head, end in talks:
                        if head < s.right_handle and s.left_handle < end:
                            _talk(s, ch, motion_path(clip_id), fps, height, head)
                            count += 1
        # 跳ねながら登場退場（YMM4 の立ち絵アイテムごとの映像エフェクト）。付いている側はキャラの登場・退場の代わり
        jumps = {s.name: j for s in mine if (j := jump_of(s))}
        for s in mine:
            j = jumps.get(s.name)
            if not j:
                continue
            frames = s.right_handle - 1 - s.left_handle
            both = j.get("in") and j.get("out")
            for appearing in (True, False):
                if j.get("in" if appearing else "out"):
                    # 登場と退場の両方なら半分ずつ（同じフレームのキーで上書きし合わないように）
                    _jump(s, j, fps, width, height, appearing, frames // 2 - (0 if appearing else 1) if both else frames)
                    count += 1
        # 立ち絵ごとのフェード・喋る時のみ表示。打った側にはキャラのフェードの登場・退場を重ねない
        faded = {s.name: sides for s in mine if s.get("tachie_still") and (sides := _visibility(scene, s, fps, talks))}
        count += len(faded)
        duration = max(1, round(ch.effect_seconds * fps))
        for group in spans(mine):
            first, last = group[0], group[-1]
            d = duration
            if len(group) == 1 and ch.enter_effect != "NONE" and ch.exit_effect != "NONE":
                # 1 本だけのまとまりで登場と退場が重なると、同じフレームのキーが上書きされて片方が消える
                d = min(d, max(1, (first.right_handle - 1 - first.left_handle) // 2))
            if ch.enter_effect != "NONE" and not (jumps.get(first.name) or {}).get("in") and not (
                    ch.enter_effect == "FADE" and "in" in faded.get(first.name, ())):
                end = min(first.left_handle + d, first.right_handle - 1)
                _edge(first, ch, ch.enter_effect, first.left_handle, end, True, width)
                count += 1
            if ch.exit_effect != "NONE" and not (jumps.get(last.name) or {}).get("out") and not (
                    ch.exit_effect == "FADE" and "out" in faded.get(last.name, ())):
                begin = max(last.right_handle - 1 - d, last.left_handle)
                _edge(last, ch, ch.exit_effect, begin, last.right_handle - 1, False, width)
                count += 1
    # 位置・大きさのキーフレーム（ストリップタブの欄に打ったもの）を、ここまでの動きに重ねる
    curves = pose_curves(scene)
    for s in images:
        if s.name in curves:
            _apply_pose_curves(scene, s, curves[s.name], width, height)
            count += 1
    return count


def _movable_texts(sed):
    """プレビューで動かせる字幕・キャラ名（tachie_sub_x を持つセリフのもの）。"""
    subtitles = {s["tachie_id"] for s in sed.strips
                 if s.type == "TEXT" and s.get("tachie_id") and s.get("tachie_role") != "name" and "tachie_sub_x" in s}
    return [s for s in sed.strips if s.type == "TEXT" and s.get("tachie_id") in subtitles]


def moved_texts(scene) -> list:
    """プレビューで動かした字幕・キャラ名（`place_text` が置いた位置 `tachie_placed_loc` から動いたもの）。
    置いた位置を持たない古いものは動かしていないとみなす。"""
    sed = scene.sequence_editor
    if sed is None:
        return []
    return [s for s in _movable_texts(sed) if "tachie_placed_loc" in s
            and any(abs(a - b) > 1e-5 for a, b in zip(s.location, s["tachie_placed_loc"]))]


def sync_text_offsets(scene) -> int:
    """プレビューで動かした字幕・キャラ名の位置を、セリフごとのずらす量（字幕ストリップの tachie_sub_x /
    tachie_sub_y）に書き戻し、そのセリフの字幕とキャラ名を置き直す。書き戻したセリフの数を返す。

    キャラ名はキャラ名の基準の位置からの差にする。字幕とキャラ名を一緒に動かしたら字幕を使う。
    動かしたかは置いた位置と比べる（解像度を変えてキャラ名の基準の位置がずれただけのものを、動かしたと
    取り違えないため。undo で動かしたあとの段に戻ったときも、その段の置いた位置と比べられる）。
    """
    sed = scene.sequence_editor
    characters = {c.name: c for c in scene.tachie.characters}
    moved = [s for s in moved_texts(scene) if s.get("tachie_character") in characters]
    if not moved:
        return 0
    offsets: dict[str, tuple[float, float]] = {}
    for s in sorted(moved, key=lambda s: s.get("tachie_role") == "name"):  # 字幕を先に
        if s["tachie_id"] in offsets:
            continue
        ch = characters[s["tachie_character"]]
        if s.get("tachie_role") == "name":
            base = name_height(ch, s.get("tachie_placed_text", s.get("tachie_text", "")), s)
        else:
            base = lay(ch, "subtitle_y")
        offset = (s.location[0] - 0.5 - lay(ch, "subtitle_x"), s.location[1] - base)
        offsets[s["tachie_id"]] = tuple(round(max(-1.0, min(1.0, v)), 5) for v in offset)
    for t in _movable_texts(sed):
        if t["tachie_id"] in offsets and t.get("tachie_character") in characters:
            t["tachie_sub_x"], t["tachie_sub_y"] = offsets[t["tachie_id"]]
            place_text(t, characters[t["tachie_character"]], t.get("tachie_placed_text", t.get("tachie_text", "")))
    return len(offsets)


def motion_signature(scene) -> str:
    """動きに関わる値（立ち絵ストリップの位置・長さ、セリフの位置、立ち絵ごとの位置・大きさ、字幕の位置、
    キャラの動きの設定）。"""
    sed = scene.sequence_editor
    if sed is None:
        return ""
    chars = scene.tachie.characters
    settings = [(c.name, round(c.talk_bounce, 4), round(c.talk_sway, 3), round(c.talk_period, 3),
                 c.enter_effect, c.exit_effect, round(c.effect_seconds, 3), round(c.subtitle_fade, 3),
                 round(c.subtitle_rise, 4), round(c.subtitle_grow, 4), round(c.subtitle_motion_seconds, 3)) for c in chars]
    active = any(s[1] or s[2] or s[4] != "NONE" or s[5] != "NONE" or _appears(*s[7:]) for s in settings)
    # 縦長用の配置を使うキャラがいれば、解像度を変えたとき（下の resolution）に置き直す
    active = active or any(c.use_portrait for c in chars)
    curves = pose_curves(scene)
    items = []
    for s in sed.strips:
        if s.type == "SOUND" and s.get("tachie_id"):  # 立ち絵が跳ねる・揺れる区間
            items.append((s.name, int(s.content_start), s.left_handle, s.right_handle, s.mute,
                          int(s.get("tachie_frames", 0))))
            continue
        if s.type == "TEXT" and s.get("tachie_id"):  # 字幕のフェードは字幕の位置で決まる
            offset = () if s.get("tachie_role") == "name" else tuple(round(v, 4) for v in text_offset(s))
            active = active or offset not in ((), (0.0, 0.0))
            if s.get("tachie_role") != "name":
                style = tuple(round(v, 2) for v in text_style(s))
                active = active or style != (0.0, 0.0)
                offset += style
            # 拡大の始めのずらす量は字幕の位置（キャラの字幕の位置・行数で変わる）と解像度で決まる
            loc = tuple(round(v, 4) for v in s.location)
            items.append((s.name, int(s.content_start), s.left_handle, s.right_handle, offset, loc))
            continue
        if owner(s) is None:
            continue
        look = tuple(round(float(s.get(k, d)), 4) for k, d in (
            ("tachie_opacity", 1.0), ("tachie_rotation", 0.0), ("tachie_still_fade_in", 0.0),
            ("tachie_still_fade_out", 0.0), ("tachie_speech_only", 0.0)))
        keyed = curves.get(s.name)
        if keyed:  # キーフレームを打った値は今のフレームで変わるので、キーで比べる
            pose = (_pose_signature(keyed), s.get("tachie_flip"), s.get("tachie_jump", ""), look)
        else:
            pose = (round(s.get("tachie_pos_x", 0.0), 4), round(s.get("tachie_pos_y", 0.0), 4),
                    round(s.get("tachie_scale", 1.0), 4), s.get("tachie_flip"), s.get("tachie_jump", ""), look)
        active = active or bool(keyed) or pose != (0.0, 0.0, 1.0, None, "", (1.0, 0.0, 0.0, 0.0, 0.0))
        items.append((s.name, int(s.content_start), s.left_handle, s.right_handle, pose))
    from .ymmp_media import edges_signature

    edges = edges_signature(sed)  # テンプレートなどの登場・退場の動きは、ストリップの長さに合わせる
    if not active and not edges:
        return ""
    resolution = (scene.render.resolution_x, scene.render.resolution_y)
    return repr((settings, sorted(items), sorted(edges), resolution))
