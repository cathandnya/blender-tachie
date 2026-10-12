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
    return render_layers(build_layers(character, expr or Expression()))
