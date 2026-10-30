"""YMM4 のプロジェクトの画像・動画・音声・テキストのアイテムをストリップにする（#15 段階 2）。

座標: YMM4 の X / Y は画面中央からのピクセル（下が正）、Zoom は %（100 で素材の 1 ピクセルが出力の 1 ピクセル）。
Blender の画像・動画は `fit_method="ORIGINAL"` で置き、transform の offset（ピクセル）・scale・rotation（反時計回り）に写す。

チャンネル: YMM4 で立ち絵より奥のレイヤー（`front_layer` より下）の画像・動画は、アドオンのレーン（`channels`、
`channel_base` から上）より下に、レイヤーの順で 1 本ずつ置く。下に空きが足りなければ、レーンとそこから上のストリップを
まとめて上へずらす（`shift_layout`）。手前のレイヤーはレーンより上、音声は効果音と同じくレーンより下の空き。

動画: Blender の動画ストリップは素材の 1 フレームをシーンの 1 フレームで再生する（フレームレートを合わせない）。
素材とシーンのフレームレートが違うか、再生速度が 100% でなければ、スピードコントロールのストリップ
（`FRAME_NUMBER`、素材のフレーム番号に直線のキーフレーム）を上に重ねて時間を合わせる（retiming キーを API で
動かすと、他のストリップがあるシーンで Blender 5.2 が落ちるため）。
"""

from __future__ import annotations

import json
import math
import os
import unicodedata
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import bpy

from . import channels as lanes
from .motion import _channelbag, _key
from .operators import _overlaps, _path_for_blend, _sound_error, output_dir, seq_scene
from .tachie_studio.media_effects import outline_image
from .properties import LINE_PITCH, default_font, load_font
from .tachie_studio.ymmp import Media, Project, find_by_tail, front_layer

# YMM4 の音量（50 が等倍）
VOLUME_BASE = 50.0
# 探す深さ（.ymmp のフォルダ・指定した場所からの階層）
SEARCH_DEPTH = {"project": 3, "root": 4}
# 名前で探すときに見るフォルダの数の上限（ホームフォルダなど広すぎる場所を指定されても固まらないように）
SEARCH_MAX_DIRS = 3000
KIND_LABELS = {"image": "画像", "video": "動画", "audio": "音声", "text": "テキスト", "color": "カラー", "mosaic": "モザイク",
               "transition": "場面転換"}


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def media_root(settings) -> str:
    """画像・動画などを探す場所: シーンの指定（YMM4 の読み込みのときに指定したもの）→ アドオン設定（全プロジェクト共通）。"""
    return settings.ymmp_media_root or bpy.context.preferences.addons[__package__].preferences.media_root


class MediaFinder:
    """YMM4 のパス（Windows）のファイルを、元のパス → 元のパスの後ろの部分（.ymmp のフォルダとその上から）
    → .ymmp と同じフォルダ → その下 → `root` の下 の順で探す。"""

    def __init__(self, ymmp: Path | None, root: str = ""):
        """`ymmp` が None なら元のパスと `root` の下だけを探す（テンプレートを未保存の .blend に置くとき）。"""
        self.base = ymmp.parent if ymmp is not None else None
        self.roots = [(self.base, SEARCH_DEPTH["project"])] if self.base is not None else []
        if root:
            self.roots.append((Path(bpy.path.abspath(os.path.expanduser(root))), SEARCH_DEPTH["root"]))
        self._index: dict[str, Path] | None = None
        self.truncated = False  # 広すぎて途中までしか探していない

    def _build(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        visited = 0
        for base, depth in self.roots:
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                visited += 1
                if visited > SEARCH_MAX_DIRS:
                    self.truncated = True
                    return index
                if len(Path(dirpath).relative_to(base).parts) >= depth:
                    dirnames[:] = []
                dirnames.sort()
                for f in sorted(filenames):
                    index.setdefault(_nfc(f), Path(dirpath) / f)  # 浅いところ・先に探す場所が優先
        return index

    def find(self, file: str) -> Path | None:
        if not file:
            return None
        windows = file.startswith("\\\\") or ":" in file[:3]
        if (os.name == "nt" or not windows) and Path(file).is_file():
            return Path(file)  # 元のパス（Windows のパスは Windows でだけ）
        if self.base is not None and (found := find_by_tail(file, self.base)) is not None:
            return found  # Windows と同じ並びで持ってきたとき
        for base, _ in self.roots[1 if self.base is not None else 0:]:  # 指定した場所の直下に同じ並びで
            if (found := find_by_tail(file, base, levels=0)) is not None:
                return found
        name = PureWindowsPath(file).name
        if self.base is not None and (self.base / name).is_file():
            return self.base / name
        if self._index is None:
            self._index = self._build()
        return self._index.get(_nfc(name))


def shift_layout(scene, n: int) -> None:
    """レーン（`channel_base` から上）と、そこから上にあるストリップ・チャンネルの名前などを `n` 上へずらす。"""
    if n <= 0:
        return
    settings = scene.tachie
    lanes.ensure_layout(scene)
    base = settings.channel_base
    sed = scene.sequence_editor
    strips = [s for s in sed.strips if s.channel >= base] if sed else []
    top = max([s.channel for s in strips] + [lanes.scene_layout(scene).top])
    if top + n > 128:
        raise ValueError(f"チャンネルが足りません（{n} 本ずらすと 128 を超えます）")
    for s in sorted(strips, key=lambda s: -s.channel):  # 上から動かす（重なると Blender が別のチャンネルへ逃がす）
        s.channel += n
    if sed is not None:  # チャンネルの名前・ミュート・ロックも一緒に
        channels = sed.channels
        for c in range(top, base - 1, -1):
            src, dst = channels[c], channels[c + n]
            dst.name, dst.mute, dst.lock = src.name, src.mute, src.lock
        for c in range(base, base + n):
            channels[c].name, channels[c].mute, channels[c].lock = lanes.DEFAULT_NAME.format(c), False, False
    # ずらし終わったレイアウトを当てたことにしてから base を変える（update で並べ直さない）
    settings.applied_layout = lanes.channel_layout(base + n, lanes.scene_chars(scene)).to_json()
    settings.channel_base = base + n


def _keyframes(scene, strip, attr: str):
    """`attr` の F カーブ（配列なら全要素）のキーフレーム。"""
    bag = _channelbag(scene)
    if bag is None:
        return
    path = strip.path_from_id(attr)
    for fcurve in bag.fcurves:
        if fcurve.data_path == path:
            yield from fcurve.keyframe_points


def _linear(scene, strip, attr: str) -> None:
    """`attr` の F カーブを直線にする（YMM4 の直線移動）。"""
    for k in _keyframes(scene, strip, attr):
        k.interpolation = "LINEAR"


# YMM4 のイージング → Blender のキーフレームの補間・イージング
EASING_TYPES = {"Sine": "SINE", "Quad": "QUAD", "Cubic": "CUBIC", "Quart": "QUART", "Quint": "QUINT", "Expo": "EXPO",
                "Circ": "CIRC", "Back": "BACK", "Elastic": "ELASTIC", "Bounce": "BOUNCE"}
EASING_MODES = {"In": "EASE_IN", "Out": "EASE_OUT", "InOut": "EASE_IN_OUT"}
# 退場は登場を逆に再生した動き（Out の減速は、退場では加速になる）
REVERSED_MODES = {"EASE_IN": "EASE_OUT", "EASE_OUT": "EASE_IN", "EASE_IN_OUT": "EASE_IN_OUT"}
# 画面外へずらす向き（画面の幅・高さの何倍か。Blender の y は上が正）
SLIDE_DIRECTIONS = {"Left": (-1.0, 0.0), "Right": (1.0, 0.0), "Top": (0.0, 1.0), "Bottom": (0.0, -1.0)}


def _ease(scene, strip, attr: str, frame: int, slide: dict, out: bool) -> None:
    """`frame` から始まる区間の補間を YMM4 のイージングにする（`out` は退場で、イージングを逆向きに）。"""
    interpolation = EASING_TYPES.get(slide["easing"], "LINEAR")
    easing = EASING_MODES.get(slide["mode"], "EASE_IN")
    if out:
        easing = REVERSED_MODES[easing]
    for k in _keyframes(scene, strip, attr):
        if round(k.co[0]) == frame:
            k.interpolation = interpolation
            k.easing = easing


def _scene_strips(sed) -> list:
    """場面（場面転換の前・新しい場面）になれるストリップ: 画像・動画（速度の補正などの効果をかけた動画はその効果の
    ストリップ）で、アドオンの立ち絵などでないもの。"""
    inputs = {e.input_1.name for e in sed.strips if getattr(e, "input_1", None) is not None}
    return [s for s in sed.strips
            if (s.type in ("IMAGE", "MOVIE") or (getattr(s, "input_1", None) is not None and s.input_1.type == "MOVIE"))
            and not s.mute and lanes.lane_key(s) is None and s.name not in inputs]


def _fcurve(scene, strip, attr: str):
    """`attr`（"blend_alpha"・"transform.offset_x" など）の F カーブ（無ければ None）。"""
    bag = _channelbag(scene)
    path = f"{strip.path_from_id()}.{attr}"
    return next((f for f in bag.fcurves if f.data_path == path), None) if bag is not None else None


def _keyed_between(scene, owner, attrs, lo: int, hi: int) -> bool:
    """`owner`（ストリップか transform）の `attrs` に [lo, hi] のキーがあるか。"""
    bag = _channelbag(scene)
    if bag is None:
        return False
    paths = {f"{owner.path_from_id()}.{a}" for a in attrs}
    return any(lo <= k.co[0] <= hi for f in bag.fcurves if f.data_path in paths for k in f.keyframe_points)


def _full_alpha(scene, strip) -> float:
    """不透明度のキーフレームのいちばん濃い値（キーが無ければ今の値）。フェードアウトした場面の終わりの値（0）や、
    フェードインの途中の値で場面転換のキーを打つと、場面転換の間ずっと見えなくなるため。"""
    fcurve = _fcurve(scene, strip, "blend_alpha")
    if fcurve is not None and len(fcurve.keyframe_points):
        return max(k.co[1] for k in fcurve.keyframe_points)
    return strip.blend_alpha


def _room_after(strip) -> float:
    """お尻を後ろへ伸ばせる素材の残り（フレーム。画像 1 枚なら無限）。"""
    if strip.type == "MOVIE" or (strip.type == "IMAGE" and len(strip.elements) > 1):
        return max(0, int(strip.content_end) - strip.right_handle)
    return math.inf


FPS_SPEED = "tachie_fps_speed"  # アドオンが付けた fps 合わせの速度の補正（`MediaPlacer._movie`）


def _fps_speed(scene, strip) -> bool:
    """アドオンが付けた fps 合わせの速度の補正か（フレーム番号の 2 つの直線のキー、元はリタイミングしていない動画）。"""
    if strip.type != "SPEED" or not strip.get(FPS_SPEED) or strip.speed_control != "FRAME_NUMBER":
        return False
    movie = strip.input_1
    curve = _fcurve(scene, strip, "speed_frame_number")
    return (movie is not None and movie.type == "MOVIE" and not len(movie.retiming_keys) and curve is not None
            and len(curve.keyframe_points) == 2)


def _extend(scene, strip, end: int) -> bool:
    """お尻を `end` まで伸ばす。fps 合わせの速度の補正は元の動画を伸ばし、終わりのフレーム番号を同じ傾きで延ばす。
    伸ばした先に素材が無い（最後のコマで止まった絵になる）なら True。"""
    if strip.type != "SPEED":
        frozen = end - strip.right_handle > _room_after(strip)
        strip.right_handle = end
        return frozen
    movie = strip.input_1
    curve = _fcurve(scene, strip, "speed_frame_number")
    k0, k1 = sorted(curve.keyframe_points, key=lambda k: k.co[0])
    slope = (k1.co[1] - k0.co[1]) / (k1.co[0] - k0.co[0]) if k1.co[0] != k0.co[0] else 0.0
    value = k0.co[1] + slope * (end - k0.co[0])
    movie.right_handle = end
    for point in (k1.co, k1.handle_left, k1.handle_right):
        point.x, point.y = end, value
    curve.update()
    return value > movie.frame_duration - 1


def _with_input(strip) -> list:
    """場面のストリップと、fps 合わせの補正なら元の動画（どちらもチャンネルを使う）。"""
    return [strip] + ([strip.input_1] if strip.type == "SPEED" else [])


def transition_label(kind: str) -> str:
    return "場面転換（フェード）" if kind == "fade" else "場面転換（プッシュ）"


def transition_scenes(scene, before, after, tr: dict, start: int, end: int, problems: list[str]) -> bool:
    """場面転換（`tr`: type = fade / push、easing・mode・angle）を [start, end) に付ける。前の場面 `before` のお尻を `end` まで
    伸ばし、その区間を前の場面自身で動かす（残像を作らず、前の場面も再生したまま。blender-tools の押し出しと同じやり方で、
    時間の位置は YMM4 と同じく境目から後ろ）。プッシュは前の場面を押し出して新しい場面 `after` を反対側から入れ
    （角度 0 で左へ、画面 1 つ分）、フェードは上にある方の不透明度を変える。速度の補正などの効果をかけた動画は動かせないので
    断る（blender-tools と同じ）。伸ばした先に素材が無ければ最後のコマで止まった絵になる（知らせる）。
    範囲に目印のストリップを置き、付ける前の状態を記録する（`transitions`。blender-tools の押し出しと同じ形で、
    付け直す・外すのは blender-tools）。
    付けられなければ理由を `problems` に足して False。"""
    from . import transitions

    sed = scene.sequence_editor
    label = transition_label(tr["type"])
    if transitions.overlapping(scene, {before.name} | ({after.name} if after is not None else set()), start, end):
        problems.append(f"{label}: 「{before.name}」にはもう場面転換が付いているので付けませんでした（先に外してください）")
        return False
    # 速度の補正などの効果をかけた動画は断る（blender-tools と同じ）。アドオンが付けた fps 合わせの補正だけは扱う
    inputs = {e.input_1 for e in sed.strips_all if getattr(e, "input_1", None) is not None}
    for s in (before, after):
        if s is not None and (s in inputs or (getattr(s, "input_1", None) is not None and not _fps_speed(scene, s))):
            problems.append(f"{label}: 「{s.name}」は速度の補正などの効果をかけてあるので付けませんでした")
            return False
    # 前の場面が使うチャンネル（fps 合わせの補正は元の動画と補正の 2 本）
    own = _with_input(before)
    after_own = _with_input(after) if after is not None else []
    if before.channel >= scene.tachie.channel_base or (after is not None and after.channel >= scene.tachie.channel_base):
        limit = 129
    else:  # アドオンのレーンより下の場面なら、新しい場面を移すときもレーンより下に収める（キャラを隠さない）
        limit = scene.tachie.channel_base
    # 断るかどうかを全部確かめてから書き換える（途中で断ったときに、新しい場面を移した・前の場面を伸ばしたままにしない）
    moving = ("offset_x", "offset_y")
    if tr["type"] == "push" and _keyed_between(scene, before.transform, moving, start, end):
        problems.append(f"{label}: 前の場面「{before.name}」が動いているので付けませんでした")
        return False
    move_to = None
    if before.right_handle < end:
        if after is not None and {s.channel for s in after_own} & {s.channel for s in own}:
            # 新しい場面を前の場面のすぐ上のチャンネルへ（元のチャンネルは前の場面が伸びる。fps 合わせの補正は元の動画と
            # 補正の 2 本を並べたまま）。さらに上へ移すと、間のチャンネルで重なっているストリップ（上のレイヤーの画像など）を
            # 新しい場面の区間ずっと隠すので、すぐ上が空いているときだけ
            low = before.channel + 1
            move_to = [(s, low + k) for k, s in enumerate(sorted(after_own, key=lambda s: s.channel))]
            if low + len(after_own) - 1 >= limit or any(
                    _overlaps(sed, c, after.left_handle, after.right_handle, ignore=after_own) for _, c in move_to):
                problems.append(f"{label}: 新しい場面「{after.name}」のすぐ上のチャンネルが空いていないので付けませんでした")
                return False
        if any(_overlaps(sed, s.channel, before.right_handle, end, ignore=(*own, *after_own)) for s in own):
            problems.append(f"{label}: 前の場面「{before.name}」を伸ばす先に他のストリップがあるので付けませんでした")
            return False
    record = transitions.Record(scene, tr, before, after)
    scenes = [*own, *after_own]  # 目印はこれらと重ならないチャンネルに置く
    if before.right_handle < end:
        if move_to is not None:  # 重ねずに動かす（上へ動かすので上のものから。Blender は重なる位置へ動かすと逃がす）
            for strip, channel in sorted(move_to, key=lambda p: -p[1]):
                record.channel(strip)
                strip.channel = channel
        if before.type == "SPEED":
            record.speed(before.input_1, max(_fcurve(scene, before, "speed_frame_number").keyframe_points,
                                             key=lambda k: k.co[0]))
        if _extend(scene, before, end):
            problems.append(f"{label}: 「{before.name}」は素材が足りないので、最後のコマで止まった絵が入ります")
    last = max(start, end - 1)
    if tr["type"] == "fade":
        # 上にある方の不透明度を変える（前の場面が上なら薄くし、新しい場面が上なら濃くする）
        target, frm, to = ((after, 0.0, None) if after is not None and after.channel > before.channel
                           else (before, None, 0.0))
        full = _full_alpha(scene, target)
        record.keys(target, "blend_alpha", full)
        _key(target, "blend_alpha", start, full if frm is None else frm)
        _key(target, "blend_alpha", last, full if to is None else to)
        target.blend_alpha = full
        _ease(scene, target, "blend_alpha", start, tr, out=False)
        record.mark(start, end, label, scenes)
        return True
    render = scene.render
    angle = math.radians(tr.get("angle") or 0.0)
    dx, dy = -math.cos(angle) * render.resolution_x, math.sin(angle) * render.resolution_y
    # 前の場面が新しい場面の上で続くときは、押し出したあと戻さない（戻すと新しい場面を隠す）
    covers = after is not None and before.channel > after.channel
    if after is not None and _keyed_between(scene, after.transform, moving, start, end):
        problems.append(f"{label}: 新しい場面「{after.name}」が動いているので、押し出しは前の場面だけにしました")
        after = None
    for target, (frm, to) in ((before, (0.0, 1.0)), (after, (-1.0, 0.0))):
        if target is None:
            continue
        t = target.transform
        for attr, d in (("offset_x", dx), ("offset_y", dy)):
            if abs(d) < 1e-6:
                continue
            base = getattr(t, attr)
            back = target is before and before.right_handle > end and not covers
            record.keys(target, f"transform.{attr}", base, back)
            _key(target, f"transform.{attr}", start, base + d * frm)
            _key(target, f"transform.{attr}", last, base + d * to)
            if back:
                # 新しい場面の下で続いている前の場面は、押し出したあと元の位置に戻す（新しい場面が先に終わったときに
                # 押し出したままだと何も映らない）
                _key(target, f"transform.{attr}", end, base)
            _ease(scene, t, attr, start, tr, out=False)
            setattr(t, attr, base)
    record.mark(start, end, label, scenes)
    return True


def _selected_scenes(scenes: list, start: int) -> tuple:
    """選んである場面のストリップから (前の場面, 新しい場面)。`start` から始まるものが新しい場面、`start` で終わる・
    `start` をまたいで続くものが前の場面で、両方 1 本ずつ選んであるときだけ（Blender は選択を覚えたままなので、前に選んだ
    背景などが残っていても拾わないように）。決まらなければ (None, None)。"""
    chosen = [s for s in scenes if s.select]
    afters = [s for s in chosen if abs(s.left_handle - start) <= 1]
    befores = [s for s in chosen if s not in afters and s.left_handle < start - 1 and s.right_handle >= start - 1]
    return (befores[0], afters[0]) if len(befores) == 1 and len(afters) == 1 else (None, None)


EDGES = "tachie_edges"  # 登場・退場の動き（画面外から移動・フェード）を付けたパス [{"path", "in", "out"}]
EDGES_SPAN = "tachie_edges_span"  # 登場・退場のキーを最後に合わせたときのストリップの [頭, お尻]


def record_edges(strip, path: str, enter: bool, leave: bool) -> None:
    """`strip` の `path` に登場（頭の 2 つのキー）・退場（お尻の 2 つのキー）の動きを付けたことを覚える
    （ストリップの長さを変えたら `sync_edges` がキーを動かす）。"""
    if not (enter or leave):
        return
    edges = [e for e in json.loads(strip.get(EDGES) or "[]") if e.get("path") != path]
    edges.append({"path": path, "in": bool(enter), "out": bool(leave)})
    strip[EDGES] = json.dumps(edges)
    strip[EDGES_SPAN] = [strip.left_handle, strip.right_handle]


def edges_signature(sed) -> list:
    """登場・退場の動きを付けたストリップの位置（`motion_signature` に入れる）。"""
    return [(s.name, s.left_handle, s.right_handle) for s in sed.strips_all if s.get(EDGES)]


def sync_edges(scene) -> int:
    """ストリップの長さを変えたら、登場・退場の動きのキーを端と一緒に動かす（テンプレートを置いたあとに縮めると、
    退場のキーが元のお尻に残って動きが見えない）。前に合わせたとき（`EDGES_SPAN`）から頭が動いた分だけ登場の 2 つの
    キーを、お尻が動いた分だけ退場の 2 つのキーを動かす（手で動かしたキーの位置は保つ）。頭とお尻が同じだけ動いたのは
    ストリップごと動かしたもので、キーは Blender が一緒に動かしている。キーの数が付けたときと違う（手で足した・消した）
    パスは触らない。キーを動かしたストリップの数を返す。"""
    bag = _channelbag(scene)
    sed = scene.sequence_editor
    if bag is None or sed is None:
        return 0
    strips = [s for s in sed.strips_all if s.get(EDGES)]
    if not strips:
        return 0
    curves: dict[str, list] = {}  # F カーブを 1 回だけ見て、パスごとに引く（ストリップ × F カーブで回さない）
    for f in bag.fcurves:
        curves.setdefault(f.data_path, []).append(f)
    count = 0
    for strip in strips:
        span = list(strip.get(EDGES_SPAN) or ())
        strip[EDGES_SPAN] = [strip.left_handle, strip.right_handle]
        if len(span) != 2:
            continue
        dl, dr = strip.left_handle - span[0], strip.right_handle - span[1]
        if dl == dr:
            continue
        try:
            edges = json.loads(strip[EDGES])
        except ValueError:
            continue
        moved = False
        for edge in edges:
            path = f"{strip.path_from_id()}.{edge.get('path', '')}"
            want = 2 * bool(edge.get("in")) + 2 * bool(edge.get("out"))
            for fcurve in curves.get(path, ()):
                keys = sorted(fcurve.keyframe_points, key=lambda k: k.co[0])
                if len(keys) != want:
                    continue
                shifts = []
                if edge.get("in"):
                    shifts.append((keys[:2], dl))
                if edge.get("out"):
                    # 短くしても退場が登場の終わり（登場が無ければストリップの頭）より前に入らないように
                    floor = keys[1].co[0] + dl if edge.get("in") else strip.left_handle
                    shifts.append((keys[-2:], max(dr, floor - keys[-2].co[0])))
                changed = False
                for pair, d in shifts:
                    if d:
                        for k in pair:
                            k.co[0] += d
                            k.handle_left[0] += d
                            k.handle_right[0] += d
                        changed = True
                if changed:
                    fcurve.update()
                    moved = True
        count += moved
    return count


def place_transition(context, tr: dict, start: int, end: int, problems: list[str]) -> bool:
    """テンプレートの場面転換を置く。`start` を前の場面（そこで終わる画像・動画のいちばん上）と新しい場面（そこから
    始まる画像・動画のいちばん上）の境目にして `transition_scenes` で付ける。画像・動画を選んであれば、それを前の場面・
    新しい場面にする（`_selected_scenes`。境目で終わる重ねた画像などが選ばれないように）。そこで終わるものが無ければ、新しい場面の
    下で続いている画像・動画のいちばん上を前の場面にする（新しい場面を上に重ねて置いたとき。上のものは重ねた画像など。場面転換の途中で
    終わるときだけ終わりまで伸ばす）。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor
    scenes = _scene_strips(sed) if sed is not None else []
    before, after = _selected_scenes(scenes, start)
    if before is None:
        before = max((s for s in scenes if abs(s.right_handle - start) <= 1), key=lambda s: s.channel, default=None)
        after = max((s for s in scenes if abs(s.left_handle - start) <= 1 and s != before), key=lambda s: s.channel,
                    default=None)
    if before is None and after is not None:
        before = max((s for s in scenes if s.left_handle < start - 1 and s.right_handle > start + 1
                      and s.channel < after.channel), key=lambda s: s.channel, default=None)
    if before is None:
        label = transition_label(tr["type"])
        problems.append(f"{label}: 再生位置で終わる、または新しい場面の下で続いている画像・動画（前の場面）が無いので"
                        "置きませんでした")
        return False
    return transition_scenes(scene, before, after, tr, start, end, problems)


def box_height(font_size: float, text: str, margin: float, line_spacing: float = 1.0) -> float:
    """テキストの背景の帯の高さの見積もり（ピクセル。角の丸みを帯の高さに対する割合にするのに使う）。
    2 行目からは行の間隔（行間 1 で文字の大きさの約 1.16 倍）。"""
    return font_size * (1 + text.count("\n") * LINE_PITCH * line_spacing) + 2 * margin


class MediaPlacer:
    def __init__(self, context, project: Project, finder: MediaFinder, frame: Callable[[int], int],
                 find_font: Callable[[str], str | None], behind: bool | None = None):
        """`behind` は映像を全部立ち絵より奥（True）・手前（False）に置く（テンプレート）。None なら YMM4 のレイヤーで決める。"""
        self.scene = seq_scene(context)
        from . import timeline

        timeline.remember(self.scene)
        self.effects_dir = output_dir(context) / "media"  # 縁取りなどを焼き込んだ画像
        self.behind = behind
        self.project = project
        self.finder = finder
        self.frame = frame
        self.find_font = find_font
        self.fps = self.scene.render.fps / self.scene.render.fps_base
        render = self.scene.render
        self.sx, self.sy = render.resolution_x / project.width, render.resolution_y / project.height
        self.placed: Counter = Counter()
        self.missing: dict[str, int] = {}
        self.problems: list[str] = []
        self._paths: dict[str, Path | None] = {}
        self._strips: dict[int, object] = {}  # id(Media) → 置いたストリップ（場面転換で新しい場面も動かす）
        self._movie_fps: dict[Path, float] = {}

    # ---- 準備 ----

    def _path(self, m: Media) -> Path | None:
        if m.file not in self._paths:
            self._paths[m.file] = self.finder.find(m.file)
        path = self._paths[m.file]
        if path is None and m.transition is None:  # 場面転換の残像は前の場面と同じファイルなので数えない
            name = PureWindowsPath(m.file).name or "（ファイル名なし）"
            self.missing[name] = self.missing.get(name, 0) + 1
        return path

    def _span(self, m: Media) -> tuple[int, int]:
        start = self.frame(m.frame)
        return start, max(start + 1, self.frame(m.frame + m.length))

    def _movie_fps_of(self, path: Path) -> float:
        """動画のフレームレート（読めなければ 0。new_movie は読めないファイルでも 1 フレームのストリップを作る）。"""
        if path not in self._movie_fps:
            sed = self.scene.sequence_editor
            probe = sed.strips.new_movie(name="probe", filepath=str(path), channel=128, frame_start=1)
            self._movie_fps[path] = probe.fps
            sed.strips.remove(probe)
        return self._movie_fps[path]

    def _needs_speed(self, m: Media, path: Path) -> bool:
        """動画を素材のフレームレート・再生速度に合わせるのにスピードコントロールが要るか。"""
        return abs(self._movie_fps_of(path) * m.rate / 100 - self.fps) > 1e-3

    def _visuals(self, items: list[Media]) -> list[tuple[Media, Path | None, int]]:
        """置く画像・動画・テキストと、使うチャンネルの本数（スピードコントロールの分）。"""
        out = []
        for m in items:
            if m.kind in ("text", "color", "mosaic"):
                out.append((m, None, 1))
                continue
            path = self._path(m)
            if path is None:
                continue
            ext = path.suffix.lower()
            ok = bpy.path.extensions_image if m.kind == "image" else bpy.path.extensions_movie
            if ext not in ok:
                self.problems.append(f"{path.name}: Blender で読めない形式です（{ext}）")
                continue
            if m.kind == "video" and not self._movie_fps_of(path):
                self.problems.append(f"{path.name}: 動画として読めません")
                continue
            out.append((m, path, 2 if m.kind == "video" and self._needs_speed(m, path) else 1))
        return out

    # ---- チャンネル ----

    def _lanes(self, layers: dict[int, list], start: int, fixed: bool) -> dict[int, int]:
        """レイヤー → チャンネル（下から順）。`fixed` なら start から詰めて、そうでなければ空いているところ。"""
        lane_of, cursor = {}, start
        sed = self.scene.sequence_editor
        for layer in sorted(layers):
            width = max(w for _, _, w in layers[layer])
            spans = [self._span(m) for m, _, _ in layers[layer]]
            while not fixed and any(_overlaps(sed, c, a, b) for c in range(cursor, cursor + width) for a, b in spans):
                cursor += 1
            if cursor + width - 1 > 128:
                raise ValueError("チャンネルが足りません")
            lane_of[layer] = cursor
            cursor += width
        return lane_of

    def _free_below(self, base: int, a: int, b: int) -> int:
        """`base` のすぐ下から、[a, b) にストリップの無いチャンネルが続く本数。"""
        sed = self.scene.sequence_editor
        n = 0
        for c in range(base - 1, 0, -1):
            if _overlaps(sed, c, a, b):
                break
            n += 1
        return n

    def _sound_channel(self, a: int, b: int) -> int:
        return lanes.sound_channel(self.scene, a, b)

    # ---- 置く ----

    def place(self) -> None:
        project = self.project
        sed = self.scene.sequence_editor or self.scene.sequence_editor_create()
        items = [m for m in project.media if not m.hidden and m.transition is None]
        transitions = [m for m in project.media if not m.hidden and m.transition is not None]
        visuals = self._visuals([m for m in items if m.kind != "audio"])
        front = front_layer(project)
        back: dict[int, list] = {}
        fore: dict[int, list] = {}
        for entry in visuals:
            m = entry[0]
            # テンプレートの「立ち絵より奥」は画像・動画・カラーだけ（テキストは見出しなどなので手前に置く）
            behind = m.layer < front if self.behind is None else (self.behind and m.kind != "text")
            (back if behind else fore).setdefault(m.layer, []).append(entry)
        settings = self.scene.tachie
        lanes.ensure_layout(self.scene)
        if back:
            need = sum(max(w for _, _, w in v) for v in back.values())
            a, b = self.frame(0), self.frame(project.length) + 1
            shift_layout(self.scene, max(0, need - self._free_below(settings.channel_base, a, b)))
            back_lanes = self._lanes(back, settings.channel_base - need, fixed=True)
        else:
            back_lanes = {}
        fore_lanes = self._lanes(fore, lanes.scene_layout(self.scene).top + 1, fixed=False)
        for lane_of, groups in ((back_lanes, back), (fore_lanes, fore)):
            for layer, entries in groups.items():
                for m, path, _ in entries:
                    try:
                        self._place_visual(sed, m, path, lane_of[layer])
                    except (RuntimeError, ValueError, OSError) as e:
                        self.problems.append(f"{KIND_LABELS[m.kind]} {PureWindowsPath(m.file).name or m.text[:20]}: {e}")
        for m in transitions:  # 場面転換は前の場面のストリップ自身を伸ばして動かす
            tr = m.transition
            before = self._strips.get(id(tr.get("before")))
            after = self._strips.get(id(tr["after"])) if tr.get("after") is not None else None
            if before is None:
                self.problems.append(f"場面転換: 前の場面 {PureWindowsPath(m.file).name} を置けなかったので付けませんでした")
                continue
            start, end = self._span(m)
            try:
                if transition_scenes(self.scene, before, after, tr, start, end, self.problems):
                    self.placed["transition"] += 1
            except (RuntimeError, ValueError) as e:
                self.problems.append(f"場面転換 {PureWindowsPath(m.file).name}: {e}")
        for m in items:
            if m.kind == "audio":
                path = self._path(m)
                if path is not None:
                    if abs(m.rate - 100) > 1e-6:
                        self.problems.append(f"{path.name}: 音声の再生速度（{m.rate:g}%）は写していません（等速で置きました）")
                    self._place_sound(sed, m, path, m.volume, "audio")
            elif m.kind == "video" and (m.volume > 0 or "volume" in m.anim):
                path = self._paths.get(m.file)
                if path is None or path.suffix.lower() not in bpy.path.extensions_movie or _sound_error(path):
                    continue  # 音の無い動画
                if abs(m.rate - 100) > 1e-6:
                    self.problems.append(f"{path.name}: 再生速度を変えた動画の音は置いていません")
                    continue
                self._place_sound(sed, m, path, m.volume, "video_sound")

    def _mark(self, strip, m: Media) -> None:
        strip["tachie_ymmp"] = m.kind

    def _place_visual(self, sed, m: Media, path: Path | None, channel: int) -> None:
        start, end = self._span(m)
        name = (path.name if path else m.text.replace("\n", " ")[:20]) or KIND_LABELS[m.kind]
        if m.kind == "text":
            strip = sed.strips.new_effect(name=f"YMM4 {name}", type="TEXT", channel=channel, frame_start=start,
                                          length=end - start)
            self._text(strip, m)
        elif m.kind == "color":
            strip = sed.strips.new_effect(name="カラー", type="COLOR", channel=channel, frame_start=start,
                                          length=end - start)
            strip.color = m.color[:3]
        elif m.kind == "mosaic":
            # 調整レイヤー + コンポジターの補正（範囲の中だけピクセル化）。下のチャンネル全部にかかる
            strip = sed.strips.new_effect(name="YMM4 モザイク", type="ADJUSTMENT", channel=channel,
                                          frame_start=start, length=end - start)
            modifier = strip.modifiers.new(name="モザイク", type="COMPOSITOR")
            modifier.node_group = self._mosaic_tree(m)
        elif m.kind == "image":
            if m.stroke is not None:  # 縁取りは画像に焼き込む（縁の分だけ大きくなるが、中心は同じ）
                s = m.stroke
                try:
                    path = outline_image(path, self.effects_dir, s["thickness"], s["color"], angular=s["angular"],
                                         blur=s["blur"], only=s["only"])
                except OSError as e:
                    self.problems.append(f"{path.name} の縁取りを作れません: {e}")
            strip = sed.strips.new_image(name=f"YMM4 {name}", filepath=_path_for_blend(path), channel=channel,
                                         frame_start=start, fit_method="ORIGINAL")
            strip.right_handle = end
        else:
            strip = self._movie(sed, m, path, channel, start, end)
        self._mark(strip, m)
        if m.kind not in ("text", "color", "mosaic"):
            t = strip.transform
            t.scale_x = t.scale_y = m.zoom / 100 * self.sy
            t.offset_x, t.offset_y = m.x * self.sx, -m.y * self.sy
            t.rotation = -math.radians(m.rotation)
            strip.blend_type = "ALPHA_OVER"
        strip.blend_alpha = max(0.0, min(1.0, m.opacity / 100))
        self._animate(strip, m, start, end)
        self._strips[id(m)] = strip
        self.placed[m.kind] += 1

    def _mosaic_tree(self, m: Media):
        """範囲（四角いマスク）の中だけピクセル化するコンポジターのノードグループ。"""
        mo = m.mosaic
        tree = bpy.data.node_groups.new("YMM4 モザイク", "CompositorNodeTree")
        tree.interface.new_socket("Image", in_out="INPUT", socket_type="NodeSocketColor")
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        nodes, links = tree.nodes, tree.links
        group_in, group_out = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
        group_in.location, group_out.location = (-400, 0), (400, 0)
        pixelate = nodes.new("CompositorNodePixelate")
        pixelate.inputs["Size"].default_value = max(1, round(mo["size"] * self.sy))
        pixelate.location = (-150, 150)
        box = nodes.new("CompositorNodeBoxMask")  # 位置は画面の割合（下が 0）、大きさは幅・高さに対する割合
        box.inputs["Position"].default_value = self._location(m.x, m.y)
        # Box Mask の大きさは縦も横も画面の幅に対する割合（16:9 で (0.5, 0.5) は正方形）
        zoom = m.zoom / 100
        box.inputs["Size"].default_value = (mo["width"] * zoom / self.project.width,
                                            mo["height"] * zoom / self.project.width)
        box.inputs["Rotation"].default_value = -math.radians(m.rotation)
        box.location = (-150, -150)
        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.location = (150, 0)

        def socket(sockets, name, kind):
            return next(s for s in sockets if s.name == name and s.type == kind)
        plain, pixel = socket(mix.inputs, "A", "RGBA"), socket(mix.inputs, "B", "RGBA")
        if mo["invert"]:  # 範囲の外にかける
            plain, pixel = pixel, plain
        links.new(group_in.outputs[0], pixelate.inputs["Color"])
        links.new(group_in.outputs[0], plain)
        links.new(pixelate.outputs[0], pixel)
        links.new(box.outputs["Mask"], socket(mix.inputs, "Factor", "VALUE"))
        links.new(socket(mix.outputs, "Result", "RGBA"), group_out.inputs[0])
        return tree

    def _movie(self, sed, m: Media, path: Path, channel: int, start: int, end: int):
        movie = sed.strips.new_movie(name=f"YMM4 {path.name}", filepath=_path_for_blend(path), channel=channel,
                                     frame_start=start, fit_method="ORIGINAL")
        src_fps = self._movie_fps_of(path) or self.fps
        rate = m.rate / 100
        if abs(src_fps * rate - self.fps) <= 1e-3:
            # 素材の 1 フレーム = シーンの 1 フレーム: 頭の切り取りだけ
            movie.frame_start = start - round(m.offset * src_fps)
            movie.left_handle, movie.right_handle = start, end
            return movie
        movie.left_handle, movie.right_handle = start, end
        self._mark(movie, m)
        speed = sed.strips.new_effect(name=f"YMM4 速度 {path.name}", type="SPEED", channel=channel + 1,
                                      frame_start=start, input1=movie)
        speed.speed_control = "FRAME_NUMBER"
        speed[FPS_SPEED] = True  # 場面転換で伸ばせる（フレーム番号の直線の 2 つのキー）
        first = m.offset * src_fps
        _key(speed, "speed_frame_number", start, first)
        _key(speed, "speed_frame_number", end, first + (end - start) / self.fps * rate * src_fps)
        _linear(self.scene, speed, "speed_frame_number")
        return speed

    def _text(self, strip, m: Media) -> None:
        strip.text = m.text
        size = m.font_size * m.zoom / 100 * self.sy
        strip.font_size = max(1.0, size)
        # フォントは名前（YMM4）か、ファイルのパス（アドオンのテンプレート）
        font_path = m.font if m.font and os.path.isfile(bpy.path.abspath(m.font)) else self.find_font(m.font)
        if m.font and not font_path:
            self.problems.append(f"テキストのフォント「{m.font}」が見つかりません（字幕の既定のフォント）")
        font_path = font_path or default_font()  # Blender の既定のフォントは日本語と記号（★ など）が小さい
        if font_path:
            try:
                strip.font = load_font(font_path)
            except RuntimeError:
                pass
        strip.color = m.font_color
        strip.use_outline = m.outline
        strip.outline_color = m.outline_color
        strip.outline_width = 0.1
        strip.use_bold = m.bold
        strip.space_line = max(0.0, m.line_spacing)
        strip.wrap_width = 0.0
        horizontal = next((h for h in ("Left", "Center", "Right") if m.base_point.startswith(h)), "Left")
        vertical = next((v for v in ("Top", "Center", "Bottom") if m.base_point.endswith(v)), "Top")
        strip.anchor_x = strip.alignment_x = horizontal.upper()
        strip.anchor_y = vertical.upper()
        strip.location = self._location(m.x, m.y)
        if m.background is not None:
            # 背景の帯。Blender の余白は出力の幅に対する割合、角の丸みは帯の高さに対する割合
            margin = m.background_margin * m.zoom / 100 * self.sx
            height = box_height(size, m.text, margin, m.line_spacing)
            strip.use_box = True
            strip.box_color = m.background
            strip.box_margin = min(1.0, margin / self.scene.render.resolution_x)
            strip.box_roundness = min(1.0, m.background_round * m.zoom / 100 * self.sy / height) if height else 0.0
        if m.rotation:
            self.problems.append(f"テキスト「{m.text[:20]}」の回転は写していません")

    def _location(self, x: float, y: float) -> tuple[float, float]:
        return 0.5 + x / self.project.width, 0.5 - y / self.project.height

    def _animate(self, strip, m: Media, start: int, end: int) -> None:
        """2 値のアニメーション（始め → 終わりの直線）とフェードイン・アウトのキーフレーム。"""
        last = max(start, end - 1)
        anim = m.anim
        if m.kind == "text":
            if "x" in anim or "y" in anim:
                (x0, x1), (y0, y1) = (anim.get("x", (m.x, m.x))[:2], anim.get("y", (m.y, m.y))[:2])
                for frame, x, y in ((start, x0, y0), (last, x1, y1)):
                    strip.location = self._location(x, y)
                    strip.keyframe_insert("location", frame=frame)
                _linear(self.scene, strip, "location")
        elif m.kind != "mosaic":  # モザイクのアニメーションは読み込みで「写せなかった設定」に出している
            moves = {"x": ("transform.offset_x", lambda v: v * self.sx),
                     "y": ("transform.offset_y", lambda v: -v * self.sy),
                     "rotation": ("transform.rotation", lambda v: -math.radians(v)),
                     "zoom": ("transform.scale_x", lambda v: v / 100 * self.sy)}
            for attr, (path, conv) in moves.items():
                if attr in anim:
                    a, b, _ = anim[attr]
                    _key(strip, path, start, conv(a))
                    _key(strip, path, last, conv(b))
                    _linear(self.scene, strip.transform, path.split(".")[-1])
                    if attr == "zoom":
                        _key(strip, "transform.scale_y", start, conv(a))
                        _key(strip, "transform.scale_y", last, conv(b))
                        _linear(self.scene, strip.transform, "scale_y")
        if m.slide is not None:
            self._slide(strip, m, start, last)
        if "opacity" in anim:
            a, b, _ = anim["opacity"]
            _key(strip, "blend_alpha", start, a / 100)
            _key(strip, "blend_alpha", last, b / 100)
            _linear(self.scene, strip, "blend_alpha")
        elif m.fade_in > 0 or m.fade_out > 0:
            self._fade(strip, "blend_alpha", strip.blend_alpha, m, start, last)

    def _slide(self, strip, m: Media, start: int, last: int) -> None:
        """画面外から移動して登場・退場（画面 1 つ分ずらした位置から、YMM4 のイージングで動かす）。"""
        anim, slide = m.anim, m.slide
        dx, dy = SLIDE_DIRECTIONS.get(slide["direction"], (-1.0, 0.0))
        n = max(1, round(slide["seconds"] * self.fps))
        if m.kind == "text":
            if "x" in anim or "y" in anim:
                self.problems.append(f"テキスト「{m.text[:20]}」は移動のアニメーションがあるので、画面外からの登場は写していません")
                return
            target, attr = strip, "location"
            x, y = strip.location
            outside, inside = (x + dx, y + dy), (x, y)
            keys = lambda pos, frame: (setattr(strip, "location", pos), strip.keyframe_insert("location", frame=frame))
        else:
            if ("x" if dx else "y") in anim:  # 動かす向きの軸だけがぶつかる
                self.problems.append(f"{KIND_LABELS[m.kind]}は移動のアニメーションがあるので、画面外からの登場は写していません")
                return
            render = self.scene.render
            target, attr = strip.transform, "offset_x" if dx else "offset_y"
            base = getattr(target, attr)
            shift = dx * render.resolution_x if dx else dy * render.resolution_y
            outside, inside = base + shift, base
            keys = lambda v, frame: _key(strip, f"transform.{attr}", frame, v)
        if slide["in"]:
            keys(outside, start)
            keys(inside, min(last, start + n))
        if slide["out"]:
            keys(inside, max(start, last - n))
            keys(outside, last)
        record_edges(strip, "location" if m.kind == "text" else f"transform.{attr}", slide["in"], slide["out"])
        if slide["in"]:
            _ease(self.scene, target, attr, start, slide, out=False)
        if slide["out"]:
            _ease(self.scene, target, attr, max(start, last - n), slide, out=True)
        # 位置を置いた場所に戻す（キーフレームがあるのでフレームごとに上書きされる）
        if m.kind == "text":
            strip.location = inside
        else:
            setattr(target, attr, inside)

    def _fade(self, strip, attr: str, full: float, m: Media, start: int, last: int) -> None:
        if m.fade_in > 0:
            _key(strip, attr, start, 0.0)
            _key(strip, attr, min(last, start + max(1, round(m.fade_in * self.fps))), full)
        if m.fade_out > 0:
            _key(strip, attr, max(start, last - max(1, round(m.fade_out * self.fps))), full)
            _key(strip, attr, last, 0.0)
        _linear(self.scene, strip, attr)
        record_edges(strip, attr, m.fade_in > 0, m.fade_out > 0)

    def _place_sound(self, sed, m: Media, path: Path, volume: float, kind: str) -> None:
        error = _sound_error(path)
        if error:
            self.problems.append(error)
            return
        start, end = self._span(m)
        strip = sed.strips.new_sound(name=f"YMM4 {path.name}", filepath=_path_for_blend(path), channel=128,
                                     frame_start=start - round(m.offset * self.fps))
        strip.left_handle, strip.right_handle = start, end
        strip.channel = self._sound_channel(start, end)
        strip["tachie_ymmp"] = kind
        strip.volume = volume / VOLUME_BASE
        last = max(start, end - 1)
        if "volume" in m.anim:
            a, b, _ = m.anim["volume"]
            _key(strip, "volume", start, a / VOLUME_BASE)
            _key(strip, "volume", last, b / VOLUME_BASE)
            _linear(self.scene, strip, "volume")
        elif m.fade_in > 0 or m.fade_out > 0:
            self._fade(strip, "volume", strip.volume, m, start, last)
        self.placed["video_sound" if kind == "video_sound" else "audio"] += 1

    # ---- 報告 ----

    def report(self) -> list[str]:
        labels = {**KIND_LABELS, "video_sound": "動画の音"}
        lines = ["置いたアイテム: " + "、".join(f"{labels[k]} {n}" for k, n in self.placed.items())] if self.placed else []
        if self.finder.truncated:
            lines += ["", f"「画像・動画などを探す場所」が広すぎるので、{SEARCH_MAX_DIRS} フォルダまでしか探していません"]
        if self.missing:
            names = sorted(self.missing)
            lines += ["", f"見つからなかったファイル（{len(names)} 個。「画像・動画などを探す場所」で指定できます）:"]
            lines += [f"  {n}（{self.missing[n]} か所）" for n in names[:30]]
            if len(names) > 30:
                lines.append(f"  ほか {len(names) - 30} 個")
        return lines
