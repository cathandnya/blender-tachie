"""字幕・見出しの既定のフォント（OS ごとに入っている日本語フォント）。"""

from __future__ import annotations

import functools
import glob
import os
import unicodedata
from typing import Callable

from .subtitle import char_width

# 先の組から探す。同じ組の中はウェイトの順（Linux の Noto Sans CJK と Noto Sans JP は 1 つの組で比べる）
DEFAULT_PATTERNS = (("/System/Library/Fonts/*W6.ttc",), ("/System/Library/Fonts/*W3.ttc",),
                    ("C:/Windows/Fonts/meiryo*.ttc",), ("C:/Windows/Fonts/YuGoth*.ttc",),
                    ("/usr/share/fonts/**/NotoSansCJK*.tt[cf]", "/usr/share/fonts/**/NotoSansJP*.[ot]tf"))
FONT_KEYS = ("ヒラギノ角ゴシック", "meiryo", "yugoth", "notosanscjk", "notosansjp")

# ファイル名のウェイトの優先順（Noto Sans CJK は Black から Thin まで並ぶので、名前順の最初だと極太になる）。
# DemiLight・SemiLight はウェイト名の無いものと同じ。ExtraBold が Bold に、DemiLight が Light に当たらないように、
# DemiLight → 極端なウェイト → 普通のウェイトの順に見る
WEIGHTS = ("bold", "medium", "regular")
MILD_WEIGHTS = ("demilight", "semilight")
EXTREME_WEIGHTS = ("black", "heavy", "thin", "light", "extrabold", "ultra")


def weight_rank(path: str) -> int:
    """小さいほど先に選ぶ。Bold → Medium → Regular → ウェイト名の無いもの・DemiLight → 極端なウェイト。"""
    name = os.path.basename(path).lower()
    if any(w in name for w in MILD_WEIGHTS):
        return len(WEIGHTS)
    if any(w in name for w in EXTREME_WEIGHTS):
        return len(WEIGHTS) + 1
    return next((i for i, w in enumerate(WEIGHTS) if w in name), len(WEIGHTS))


def default_font(patterns: tuple[tuple[str, ...], ...] = DEFAULT_PATTERNS) -> str:
    for group in patterns:
        paths = {p for pattern in group for p in glob.glob(pattern, recursive=True)}
        for path in sorted(paths, key=lambda p: (weight_rank(p), p)):
            name = unicodedata.normalize("NFC", path)
            if any(key in name.lower() for key in FONT_KEYS):
                return path
    return ""


MISSING_CHAR = "\U000F0000"  # どのフォントにも字形の無い文字（私用面）。.notdef の字形と比べる


@functools.lru_cache(maxsize=32)
def glyph_width(path: str, size: float) -> Callable[[str], float] | None:
    """フォント `path` の `size` px での 1 文字の送り幅（px）を返す関数（字幕の折り返し）。読めなければ None。
    字詰め（カーニング）は数えない。フォントに無い文字（英字のフォントの日本語など。Blender は別のフォントで描く）は
    全角 = `size`、半角 = その半分とみなす（Pillow は字形の無い文字を .notdef の幅で返すため）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(path, size)

        def shape(ch: str) -> bytes:
            image = Image.new("L", (int(size * 2) + 1, int(size * 2) + 1))
            ImageDraw.Draw(image).text((0, 0), ch, font=font, fill=255)
            return image.tobytes()

        notdef = shape(MISSING_CHAR)
    except (OSError, ValueError, ImportError):
        return None
    widths: dict[str, float] = {}

    def width_of(ch: str) -> float:
        if ch not in widths:
            if not ch.isspace() and shape(ch) == notdef:
                widths[ch] = char_width(ch) * size
            else:
                widths[ch] = font.getlength(ch)
        return widths[ch]

    return width_of
