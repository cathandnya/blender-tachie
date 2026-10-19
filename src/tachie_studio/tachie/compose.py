"""動く立ち絵のパーツを重ねて 1 枚の画像にする。"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops

from .loader import Character, Part

# 0通常 1加算 2減算 3乗算 4スクリーン 5オーバーレイ 6比較(明) 7比較(暗) 8輝度 9色差 10陰影 11明暗 12差分
BLEND_NORMAL = 0
BLEND_MULTIPLY = 3
SUPPORTED_BLENDS = (BLEND_NORMAL, BLEND_MULTIPLY)

SLOT_COUNT = 3  # 他1〜3、後1〜3


@dataclass
class Expression:
    """表情（どのパーツを使うか）と、目・口のフレーム。

    パーツ名が None のカテゴリは素材の最初のパーツを使う。`others` / `backs` は
    他1, 他2, 他3 / 後1, 後2, 後3 の順（手前から）。"" は空きスロット。

    `eye_frame` / `mouth_frame` は `Part.frames` の添字（0 = 閉じ、-1 = 開き）。
    None は開き。`mouth_vowel` に "a"〜"o" を入れると、あいうえお口パクの画像を使う
    （その母音の画像が無いパーツでは開きの画像になる）。
    """

    eyebrow: str | None = None
    eye: str | None = None
    mouth: str | None = None
    hair: str | None = None
    complexion: str | None = None
    body: str | None = None
    others: list[str] = field(default_factory=list)
    backs: list[str] = field(default_factory=list)
    layers: dict[str, bool] = field(default_factory=dict)  # PSD 立ち絵: 表示するレイヤーの上書き {パス: 表示するか}
    eye_frame: int | None = None
    mouth_frame: int | None = None
    mouth_vowel: str | None = None


@dataclass
class Layer:
    part: Part
    image_path: Path
    opacity: float


@lru_cache(maxsize=256)
def load_image(path: Path) -> Image.Image:
    """パーツ画像を RGBA で読む。呼び出し側で書き換えないこと。"""
    with Image.open(path) as im:
        if getattr(im, "is_animated", False):
            warnings.warn(f"動画パーツは最初のフレームだけを使います: {path}", stacklevel=2)
        return im.convert("RGBA")


def _frame(part: Part, index: int | None) -> Path:
    if index is None:
        return part.path
    return part.frames[max(-len(part.frames), min(index, len(part.frames) - 1))]


def _pick(character: Character, category: str, name: str | None) -> Part | None:
    if name is None:
        return character.default(category)
    return character.part(category, name)


def build_layers(character: Character, expr: Expression) -> list[Layer]:
    """奥から手前の順にレイヤーを並べる。

    公式の重ね順（手前から）: 他1→他2→他3→髪（半透明）→眉→目→口→髪→顔色→体→後1→後2→後3。
    髪は 2 回描く。奥の髪は不透明で、眉より手前の髪は ini の opacity で描く
    （opacity を下げると前髪越しに眉や目が見える）。
    合成モードが通常以外のパーツは、他のパーツより手前に描く。
    """
    if character.simple:
        return _simple_layers(character, expr)
    if len(expr.others) > SLOT_COUNT or len(expr.backs) > SLOT_COUNT:
        raise ValueError(f"他・後はそれぞれ {SLOT_COUNT} つまでです")

    def layer(part: Part | None, image_path: Path | None = None, opacity: float | None = None) -> list[Layer]:
        if part is None:
            return []
        return [Layer(part, image_path or part.path, part.opacity if opacity is None else opacity)]

    eye = _pick(character, "目", expr.eye)
    mouth = _pick(character, "口", expr.mouth)
    hair = _pick(character, "髪", expr.hair)

    mouth_path = None
    if mouth is not None:
        if expr.mouth_vowel is not None:
            mouth_path = mouth.vowels.get(expr.mouth_vowel, mouth.path)
        else:
            mouth_path = _frame(mouth, expr.mouth_frame)

    layers: list[Layer] = []
    for name in reversed(expr.backs):
        if name:  # "" は空きスロット
            layers += layer(character.part("後", name))
    layers += layer(_pick(character, "体", expr.body))
    layers += layer(_pick(character, "顔色", expr.complexion))
    layers += layer(hair, opacity=1.0)
    layers += layer(mouth, mouth_path)
    layers += layer(eye, _frame(eye, expr.eye_frame) if eye else None)
    layers += layer(_pick(character, "眉", expr.eyebrow))
    layers += layer(hair)
    for name in reversed(expr.others):
        if name:
            layers += layer(character.part("他", name))

    # 通常以外の合成モードは手前へ（相対順は保つ）
    return [l for l in layers if l.part.blend == BLEND_NORMAL] + [
        l for l in layers if l.part.blend != BLEND_NORMAL
    ]


def simple_mouth(character: Character, expr: Expression) -> Part | None:
    """シンプル立ち絵の口パク用の口: 閉じ = 体の画像、開き = 口の画像の 2 段階。"""
    closed, opened = _pick(character, "体", expr.body), _pick(character, "口", expr.mouth)
    if closed is None or opened is None:
        return None
    return Part(category="口", name=opened.name, path=opened.path, frames=[closed.path, opened.path])


def _simple_layers(character: Character, expr: Expression) -> list[Layer]:
    """シンプル立ち絵は画像 1 枚。口を閉じている（mouth_frame が 0）ときは体の画像、開いているときは口の画像。"""
    mouth = simple_mouth(character, expr)
    if mouth is None:
        return []
    # mouth_frame は frames = [閉じ, 開き] の添字（-1 = 開き）。母音は開きの画像
    path = mouth.path if expr.mouth_vowel is not None else _frame(mouth, expr.mouth_frame)
    return [Layer(mouth, path, 1.0)]


def _with_opacity(image: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return image
    image = image.copy()
    image.putalpha(image.getchannel("A").point(lambda a: round(a * max(opacity, 0.0))))
    return image


def _multiply(backdrop: Image.Image, source: Image.Image) -> Image.Image:
    """W3C Compositing の乗算。下地が透明な所では元の色のまま描く。"""
    src_rgb = source.convert("RGB")
    mixed = ImageChops.multiply(src_rgb, backdrop.convert("RGB"))
    # Cr = (1 - αb)·Cs + αb·(Cs·Cb)
    color = Image.composite(mixed, src_rgb, backdrop.getchannel("A")).convert("RGBA")
    color.putalpha(source.getchannel("A"))
    return color


def _fit(image: Image.Image, size: tuple[int, int], path: Path) -> Image.Image:
    if image.size == size:
        return image
    warnings.warn(f"画像サイズが他のパーツと違います（左上に揃えます）: {path} {image.size}", stacklevel=3)
    canvas = Image.new("RGBA", size)
    canvas.paste(image, (0, 0))
    return canvas


def render_layers(layers: list[Layer]) -> Image.Image:
    if not layers:
        raise ValueError("描くパーツがありません")
    size = load_image(layers[0].image_path).size
    canvas = Image.new("RGBA", size)
    for l in layers:
        image = _with_opacity(_fit(load_image(l.image_path), size, l.image_path), l.opacity)
        if l.part.blend == BLEND_MULTIPLY:
            image = _multiply(canvas, image)
        elif l.part.blend != BLEND_NORMAL:
            warnings.warn(f"合成モード {l.part.blend} は未対応のため通常で描きます: {l.image_path}", stacklevel=2)
        canvas.alpha_composite(image)
    return canvas


def compose(character: Character, expr: Expression | None = None) -> Image.Image:
    expr = expr or Expression()
    if character.is_psd:
        return character.psd.render(psd_state(character, expr))
    return render_layers(build_layers(character, expr))


def psd_state(character: Character, expr: Expression) -> dict[str, bool]:
    """PSD 立ち絵の表示状態: 表情の上書きに、口パク・まばたきの段階（mouth_frame / eye_frame）を当てる。"""
    from .psd import active_animation, apply_layers, choose

    state = apply_layers(character.psd, expr.layers)
    for key, frame, vowel in (("mouth", expr.mouth_frame, expr.mouth_vowel), ("eye", expr.eye_frame, None)):
        anim = active_animation(character.animations.get(key, []), state)
        if anim is None or (frame is None and vowel is None):
            continue
        frames = list(reversed(anim))  # 閉じ → 開き（Part.frames と同じ並び）
        target = frames[-1] if frame is None else frames[max(-len(frames), min(frame, len(frames) - 1))]
        for node in anim:  # `*` でない段のレイヤーは choose で消えないので、選ばない段を消しておく
            if node is not target:
                state[node.path()] = False
        choose(state, target)
    return state


class PsdPart(Part):
    """PSD 立ち絵の口パク・まばたきを、動く立ち絵の連番のパーツと同じ形で表す（frames は段階の数だけ使う）。"""

    @property
    def levels(self) -> list[Path]:
        return self.frames


def psd_part(character: Character, expr: Expression, key: str) -> Part | None:
    from .psd import active_animation, apply_layers

    anim = active_animation(character.animations.get(key, []), apply_layers(character.psd, expr.layers))
    if anim is None:
        return None
    frames = [Path(f"n{n.index}") for n in reversed(anim)]
    return PsdPart(category="口" if key == "mouth" else "目", name=anim[0].label, path=character.path, frames=frames)


def part_thumbnail(path: Path, size: int = 96) -> Image.Image:
    """パーツの画像の見本（パーツを選ぶ一覧に出す。YMM4 と同じく、パーツ単体を写っている範囲で切り抜いた正方形）。
    透明なパーツ（_ など）は透明の正方形。"""
    image = load_image(path)
    box = image.getchannel("A").getbbox()
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if box is None:
        return out
    part = image.crop(box)
    scale = size / max(part.size)  # 小さなパーツ（目・口）も見本いっぱいに拡大する
    part = part.resize((max(1, round(part.width * scale)), max(1, round(part.height * scale))), Image.Resampling.LANCZOS)
    out.paste(part, ((size - part.width) // 2, (size - part.height) // 2))
    return out
