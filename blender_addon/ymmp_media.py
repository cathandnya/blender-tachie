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


def animate_transition(scene, top, hold, after, tr: dict, start: int, end: int) -> None:
    """場面転換（`tr`: type = fade / push、easing・mode・angle）のキーフレームを打つ。フェードは残像（いちばん上で重ねる
    ストリップ `top`）を薄くし、プッシュは残像（位置を動かすストリップ `hold`。動画の残像は速度の補正の元の動画）を
    押し出して新しい場面 `after` を反対側から入れる（角度 0 で左へ、画面 1 つ分）。"""
    last = max(start, end - 1)
    if tr["type"] == "fade":
        _key(top, "blend_alpha", start, top.blend_alpha)
        _key(top, "blend_alpha", last, 0.0)
        _ease(scene, top, "blend_alpha", start, tr, out=False)
        return
    render = scene.render
    angle = math.radians(tr.get("angle") or 0.0)
    dx, dy = -math.cos(angle) * render.resolution_x, math.sin(angle) * render.resolution_y
    for target, (frm, to) in ((hold, (0.0, 1.0)), (after, (-1.0, 0.0))):
        if target is None:
            continue
        t = target.transform
        base_x, base_y = t.offset_x, t.offset_y
        for attr, base, d in (("offset_x", base_x, dx), ("offset_y", base_y, dy)):
            if abs(d) < 1e-6:
                continue
            _key(target, f"transform.{attr}", start, base + d * frm)
            _key(target, f"transform.{attr}", last, base + d * to)
            _ease(scene, t, attr, start, tr, out=False)
            setattr(t, attr, base)


def _scene_strips(sed) -> list:
    """場面（場面転換の前・新しい場面）になれるストリップ: 画像・動画（速度の補正をかけた動画はその補正のストリップ）で、
    アドオンの立ち絵などでも、効果の元でもないもの。"""
    inputs = {e.input_1.name for e in sed.strips if getattr(e, "input_1", None) is not None}
    return [s for s in sed.strips
            if (s.type in ("IMAGE", "MOVIE") or (s.type == "SPEED" and s.input_1 is not None and s.input_1.type == "MOVIE"))
            and not s.mute and lanes.lane_key(s) is None and not s.get("tachie_transition") and s.name not in inputs]


def _fcurve(scene, strip, attr: str):
    """`attr`（"blend_alpha"・"transform.offset_x" など）の F カーブ（無ければ None）。"""
    bag = _channelbag(scene)
    path = f"{strip.path_from_id()}.{attr}"
    return next((f for f in bag.fcurves if f.data_path == path), None) if bag is not None else None


def _value_at(scene, strip, attr: str, frame: int, default: float) -> float:
    """`attr` の `frame` での値（キーフレームがあればその値）。"""
    fcurve = _fcurve(scene, strip, attr)
    return fcurve.evaluate(frame) if fcurve is not None else default


def _keyed(scene, strip, attrs) -> bool:
    return any(_fcurve(scene, strip, a) is not None for a in attrs)


def _copy_look(scene, dst, src, frame: int) -> None:
    """変形（`frame` での値。動いていた場面は終わりの位置で止める）・切り抜き・左右反転を写す。"""
    t, b = dst.transform, src.transform
    for attr in ("offset_x", "offset_y", "scale_x", "scale_y", "rotation"):
        setattr(t, attr, _value_at(scene, src, f"transform.{attr}", frame, getattr(b, attr)))
    t.origin = b.origin
    dst.crop.min_x, dst.crop.max_x, dst.crop.min_y, dst.crop.max_y = (src.crop.min_x, src.crop.max_x, src.crop.min_y,
                                                                      src.crop.max_y)
    dst.use_flip_x, dst.use_flip_y = src.use_flip_x, src.use_flip_y


def place_transition(context, tr: dict, start: int, end: int, problems: list[str]) -> bool:
    """テンプレートの場面転換を置く。`start` を前の場面（そこで終わる画像・動画のいちばん上）と新しい場面（そこから
    始まる画像・動画）の境目として、前の場面の最後のコマで止めた残像をすぐ上に作り、`animate_transition` で動かす
    （.ymmp の読み込みの場面転換（`ymmp._transitions`）と同じ）。置けなければ理由を `problems` に足して False。"""
    scene = seq_scene(context)
    sed = scene.sequence_editor
    label = "場面転換（フェード）" if tr["type"] == "fade" else "場面転換（プッシュ）"
    scenes = _scene_strips(sed) if sed is not None else []
    before = max((s for s in scenes if abs(s.right_handle - start) <= 1), key=lambda s: s.channel, default=None)
    if before is None:
        problems.append(f"{label}: 再生位置で終わる画像・動画（前の場面）が無いので置きませんでした")
        return False
    source = before.input_1 if before.type == "SPEED" else before
    if (source.type == "MOVIE" and len(source.retiming_keys)) or (
            before.type == "SPEED" and before.speed_control != "FRAME_NUMBER"):
        problems.append(f"{label}: 前の場面の動画「{source.name}」は再生速度を変えてあるので置きませんでした")
        return False
    after = max((s for s in scenes if abs(s.left_handle - start) <= 1 and s != before), key=lambda s: s.channel,
                default=None)
    # 残像は前と新しい場面のすぐ上（アドオンのレーンより下の場面なら、レーンより下に収める。キャラを隠さない）
    need = 1 if before.type == "IMAGE" else 2
    lowest = max(before.channel, after.channel if after is not None else 0) + 1
    limit = scene.tachie.channel_base if before.channel < scene.tachie.channel_base else 129
    channel = next((c for c in range(lowest, limit - need + 1)
                    if not any(_overlaps(sed, c + k, start, end) for k in range(need))), None)
    if channel is None:
        problems.append(f"{label}: 前の場面「{before.name}」のすぐ上に空いているチャンネルが無いので置きませんでした")
        return False
    last = before.right_handle - 1
    name = f"場面転換 {before.name}"
    if before.type == "IMAGE":
        elem = before.strip_elem_from_frame(last) or before.elements[-1]
        path = os.path.join(bpy.path.abspath(before.directory), elem.filename)
        hold = sed.strips.new_image(name=name, filepath=path, channel=channel, frame_start=start, fit_method="ORIGINAL")
        hold.right_handle = end
        top = hold
    else:
        # 動画は素材の最後に映っていたコマで止める（速度の補正のフレーム番号を固定。位置は元の動画の変形で動かす）。
        # 速度の補正をかけた動画は、補正のフレーム番号の最後のコマ
        movie = before.input_1 if before.type == "SPEED" else before
        if before.type == "SPEED":
            frame_number = _value_at(scene, before, "speed_frame_number", last, before.speed_frame_number)
        else:
            frame_number = last - movie.content_start
        hold = sed.strips.new_movie(name=name, filepath=bpy.path.abspath(movie.filepath), channel=channel,
                                    frame_start=start, fit_method="ORIGINAL")
        hold.left_handle, hold.right_handle = start, end
        top = sed.strips.new_effect(name=f"{name} 止め", type="SPEED", channel=channel + 1, frame_start=start,
                                    input1=hold)
        top.speed_control = "FRAME_NUMBER"
        # 画面から追加した動画は素材の fps に合わせて伸び縮みする（24fps のシーンに 30fps の素材なら 0.8 倍の長さ）が、
        # API で作った動画は素材の 1 コマ = 1 フレーム。フレーム番号は素材の中の位置（長さの比）で写す
        scale = hold.frame_duration / max(1, movie.frame_duration)
        top.speed_frame_number = min(max(0, hold.frame_duration - 1), max(0, round(frame_number * scale)))
    # 見えている場面の変形（速度の補正をかけた動画は補正のストリップが持つ。YMM4 の読み込みと同じ）を残像に写す
    _copy_look(scene, hold, before, last)
    top.blend_type = before.blend_type
    # 不透明度はキーフレームのいちばん濃い値（前の場面のフェードアウトで 0 になった終わりの値を使うと、残像が初めから見えない）
    alpha_curve = _fcurve(scene, before, "blend_alpha")
    top.blend_alpha = (max(k.co[1] for k in alpha_curve.keyframe_points)
                       if alpha_curve is not None and len(alpha_curve.keyframe_points) else before.blend_alpha)
    for s in {hold, top}:
        s["tachie_transition"] = tr["type"]
    if after is not None and tr["type"] == "push" and _keyed(scene, after, ("transform.offset_x", "transform.offset_y")):
        problems.append(f"{label}: 新しい場面「{after.name}」が動いているので、押し出しは前の場面だけにしました")
        after = None
    animate_transition(scene, top, hold, after, tr, start, end)
    return True


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
        items = [m for m in project.media if not m.hidden]
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
        for m in items:
            if m.transition is not None and id(m) in self._strips:
                try:
                    self._transition(m, self._strips[id(m)])
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
        self.placed["transition" if m.transition else m.kind] += 1

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
        if slide["in"]:
            _ease(self.scene, target, attr, start, slide, out=False)
        if slide["out"]:
            _ease(self.scene, target, attr, max(start, last - n), slide, out=True)
        # 位置を置いた場所に戻す（キーフレームがあるのでフレームごとに上書きされる）
        if m.kind == "text":
            strip.location = inside
        else:
            setattr(target, attr, inside)

    def _transition(self, m: Media, strip) -> None:
        """場面転換の残像を動かす（`animate_transition`）。"""
        tr = m.transition
        start, end = self._span(m)
        after = self._strips.get(id(tr["after"])) if tr["after"] is not None else None
        if after is not None and (tr["after"].slide is not None or any(k in tr["after"].anim for k in ("x", "y"))):
            self.problems.append("場面転換の新しい場面が動いているので、押し出しは前の場面だけにしました")
            after = None
        animate_transition(self.scene, strip, strip, after, tr, start, end)

    def _fade(self, strip, attr: str, full: float, m: Media, start: int, last: int) -> None:
        if m.fade_in > 0:
            _key(strip, attr, start, 0.0)
            _key(strip, attr, min(last, start + max(1, round(m.fade_in * self.fps))), full)
        if m.fade_out > 0:
            _key(strip, attr, max(start, last - max(1, round(m.fade_out * self.fps))), full)
            _key(strip, attr, last, 0.0)
        _linear(self.scene, strip, attr)

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
