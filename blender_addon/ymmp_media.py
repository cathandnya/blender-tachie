"""YMM4 のプロジェクトの画像・動画・音声・テキストのアイテムをストリップにする（#15 段階 2）。

座標: YMM4 の X / Y は画面中央からのピクセル（下が正）、Zoom は %（100 で素材の 1 ピクセルが出力の 1 ピクセル）。
Blender の画像・動画は `fit_method="ORIGINAL"` で置き、transform の offset（ピクセル）・scale・rotation（反時計回り）に写す。

チャンネル: YMM4 で立ち絵より奥のレイヤー（`front_layer` より下）の画像・動画は、アドオンのチャンネル（効果音・音声・
立ち絵・字幕・キャラ名・見出し）より下に、レイヤーの順で 1 本ずつ置く。下に空きが足りなければ、アドオンのチャンネルと
そこから上のストリップをまとめて上へずらす（`shift_addon_channels`）。手前のレイヤーは見出しなどより上、音声は
効果音と同じく空いているチャンネル。

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

from .motion import _channelbag, _key
from .operators import _overlaps, _path_for_blend, _sound_error, seq_scene
from .properties import default_font, load_font
from .tachie_studio.ymmp import Media, Project, front_layer

# YMM4 の音量（50 が等倍）
VOLUME_BASE = 50.0
# 探す深さ（.ymmp のフォルダ・指定した場所からの階層）
SEARCH_DEPTH = {"project": 3, "root": 4}
KIND_LABELS = {"image": "画像", "video": "動画", "audio": "音声", "text": "テキスト", "color": "カラー"}


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


class MediaFinder:
    """YMM4 のパス（Windows）のファイルを、元のパス → .ymmp と同じフォルダ → その下 → `root` の下 の順で探す。"""

    def __init__(self, ymmp: Path | None, root: str = ""):
        """`ymmp` が None なら元のパスと `root` の下だけを探す（テンプレートを未保存の .blend に置くとき）。"""
        self.base = ymmp.parent if ymmp is not None else None
        self.roots = [(self.base, SEARCH_DEPTH["project"])] if self.base is not None else []
        if root:
            self.roots.append((Path(bpy.path.abspath(os.path.expanduser(root))), SEARCH_DEPTH["root"]))
        self._index: dict[str, Path] | None = None

    def _build(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for base, depth in self.roots:
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
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
        name = PureWindowsPath(file).name
        if self.base is not None and (self.base / name).is_file():
            return self.base / name
        if self._index is None:
            self._index = self._build()
        return self._index.get(_nfc(name))


def addon_channels(settings) -> list[int]:
    return [settings.se_channel, settings.audio_channel, settings.subtitle_channel, settings.name_channel,
            settings.headline_channel, *(c.channel for c in settings.characters)]


def shift_addon_channels(scene, n: int) -> None:
    """アドオンのチャンネル（設定と各キャラ）と、その一番下から上にあるストリップを `n` 上へずらす。"""
    if n <= 0:
        return
    settings = scene.tachie
    base = min(addon_channels(settings))
    sed = scene.sequence_editor
    strips = [s for s in sed.strips if s.channel >= base] if sed else []
    top = max([s.channel for s in strips] + addon_channels(settings))
    if top + n > 128:
        raise ValueError(f"チャンネルが足りません（{n} 本ずらすと 128 を超えます）")
    for s in sorted(strips, key=lambda s: -s.channel):  # 上から動かす（重なると Blender が別のチャンネルへ逃がす）
        s.channel += n
    for prop in ("se_channel", "audio_channel", "subtitle_channel", "name_channel", "headline_channel"):
        setattr(settings, prop, getattr(settings, prop) + n)
    for c in settings.characters:
        c.channel += n


def _linear(scene, strip, attr: str) -> None:
    """`attr` の F カーブを直線にする（YMM4 の直線移動）。"""
    bag = _channelbag(scene)
    if bag is None:
        return
    path = strip.path_from_id(attr)
    for fcurve in bag.fcurves:
        if fcurve.data_path == path:
            for k in fcurve.keyframe_points:
                k.interpolation = "LINEAR"


class MediaPlacer:
    def __init__(self, context, project: Project, finder: MediaFinder, frame: Callable[[int], int],
                 find_font: Callable[[str], str | None], behind: bool | None = None):
        """`behind` は映像を全部立ち絵より奥（True）・手前（False）に置く（テンプレート）。None なら YMM4 のレイヤーで決める。"""
        self.scene = seq_scene(context)
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
        self._movie_fps: dict[Path, float] = {}

    # ---- 準備 ----

    def _path(self, m: Media) -> Path | None:
        if m.file not in self._paths:
            self._paths[m.file] = self.finder.find(m.file)
        path = self._paths[m.file]
        if path is None:
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
            if m.kind in ("text", "color"):
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
        lanes, cursor = {}, start
        sed = self.scene.sequence_editor
        for layer in sorted(layers):
            width = max(w for _, _, w in layers[layer])
            spans = [self._span(m) for m, _, _ in layers[layer]]
            while not fixed and any(_overlaps(sed, c, a, b) for c in range(cursor, cursor + width) for a, b in spans):
                cursor += 1
            if cursor + width - 1 > 128:
                raise ValueError("チャンネルが足りません")
            lanes[layer] = cursor
            cursor += width
        return lanes

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
        settings = self.scene.tachie
        reserved = set(addon_channels(settings))
        channel = settings.se_channel
        while channel < 128 and (channel in reserved or _overlaps(self.scene.sequence_editor, channel, a, b)):
            channel += 1
        return channel

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
        if back:
            need = sum(max(w for _, _, w in v) for v in back.values())
            settings = self.scene.tachie
            base = min(addon_channels(settings))
            a, b = self.frame(0), self.frame(project.length) + 1
            shift_addon_channels(self.scene, max(0, need - self._free_below(base, a, b)))
            base = min(addon_channels(settings))
            back_lanes = self._lanes(back, base - need, fixed=True)
        else:
            back_lanes = {}
        settings = self.scene.tachie
        top = max(settings.subtitle_channel, settings.name_channel, settings.headline_channel,
                  *(c.channel for c in settings.characters))
        fore_lanes = self._lanes(fore, top + 1, fixed=False)
        for lanes, groups in ((back_lanes, back), (fore_lanes, fore)):
            for layer, entries in groups.items():
                for m, path, _ in entries:
                    try:
                        self._place_visual(sed, m, path, lanes[layer])
                    except (RuntimeError, ValueError, OSError) as e:
                        self.problems.append(f"{KIND_LABELS[m.kind]} {PureWindowsPath(m.file).name or m.text[:20]}: {e}")
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
        elif m.kind == "image":
            strip = sed.strips.new_image(name=f"YMM4 {name}", filepath=_path_for_blend(path), channel=channel,
                                         frame_start=start, fit_method="ORIGINAL")
            strip.right_handle = end
        else:
            strip = self._movie(sed, m, path, channel, start, end)
        self._mark(strip, m)
        if m.kind not in ("text", "color"):
            t = strip.transform
            t.scale_x = t.scale_y = m.zoom / 100 * self.sy
            t.offset_x, t.offset_y = m.x * self.sx, -m.y * self.sy
            t.rotation = -math.radians(m.rotation)
            strip.blend_type = "ALPHA_OVER"
        strip.blend_alpha = max(0.0, min(1.0, m.opacity / 100))
        self._animate(strip, m, start, end)
        self.placed[m.kind] += 1

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
        strip.wrap_width = 0.0
        horizontal = next((h for h in ("Left", "Center", "Right") if m.base_point.startswith(h)), "Left")
        vertical = next((v for v in ("Top", "Center", "Bottom") if m.base_point.endswith(v)), "Top")
        strip.anchor_x = strip.alignment_x = horizontal.upper()
        strip.anchor_y = vertical.upper()
        strip.location = self._location(m.x, m.y)
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
        else:
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
        if "opacity" in anim:
            a, b, _ = anim["opacity"]
            _key(strip, "blend_alpha", start, a / 100)
            _key(strip, "blend_alpha", last, b / 100)
            _linear(self.scene, strip, "blend_alpha")
        elif m.fade_in > 0 or m.fade_out > 0:
            self._fade(strip, "blend_alpha", strip.blend_alpha, m, start, last)

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
        if self.missing:
            names = sorted(self.missing)
            lines += ["", f"見つからなかったファイル（{len(names)} 個。「画像・動画などを探す場所」で指定できます）:"]
            lines += [f"  {n}（{self.missing[n]} か所）" for n in names[:30]]
            if len(names) > 30:
                lines.append(f"  ほか {len(names) - 30} 個")
        return lines
