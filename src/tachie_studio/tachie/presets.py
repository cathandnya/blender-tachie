"""素材フォルダの表情プリセット（preset.ini）を読む。YMM4 が書き出す形式。

    [泣く]
    眉=上がり眉.png
    目=_.png
    他1=左腕_手を挙げる.png
    他3=記号_涙.png

セクションがプリセット名で、変えるパーツだけを書く。他・後はスロット番号つき（他1〜他3、後1〜後3）で、
書いていないスロットは基本のまま。
"""

from __future__ import annotations

import configparser
import re
import unicodedata
from pathlib import Path
from typing import Any

from .compose import SLOT_COUNT
from .loader import CATEGORIES, load_character

PRESET_FILE = "preset.ini"
SLOT_CATEGORIES = ("他", "後")
_KEY_RE = re.compile(rf"^(?P<category>\D+?)(?P<slot>[1-{SLOT_COUNT}]?)$")  # 他1〜他3、後1〜後3


def _read(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"{path} の文字コードが分かりません")


def load_presets(folder: str | Path, path: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """({プリセット名: {フォルダ名: パーツ名、他・後はスロット順のリスト}}, 警告) を返す。

    `path` を省くと `folder/preset.ini`。無ければ空。素材に無いパーツと不明なキーは警告にして飛ばす。
    """
    folder = Path(folder)
    path = Path(path) if path is not None else folder / PRESET_FILE
    if not path.is_file():
        return {}, []
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",), comment_prefixes=(";",))
    parser.optionxform = str  # キーをそのまま（既定は小文字にする）
    try:
        parser.read_string(_read(path))
    except configparser.Error as e:
        raise ValueError(f"{path} を読めません: {e}") from None
    character = load_character(folder)
    presets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for section in parser.sections():
        name = unicodedata.normalize("NFC", section)
        parts: dict[str, Any] = {}
        for key, value in parser.items(section):
            m = _KEY_RE.match(unicodedata.normalize("NFC", key.strip()))
            category, slot = (m["category"], m["slot"]) if m else (key, "")
            if category not in CATEGORIES or bool(slot) != (category in SLOT_CATEGORIES):
                warnings.append(f"[{name}] {key} は不明なキーです")
                continue
            part = unicodedata.normalize("NFC", value.strip())
            if part.lower().endswith(".png"):
                part = part[:-4]
            if not part:
                continue
            if part not in character.parts.get(category, {}):
                warnings.append(f"[{name}] {category}/{part} が素材にありません")
                continue
            if slot:
                slots = parts.setdefault(category, [])
                slots += [""] * (int(slot) - len(slots))
                slots[int(slot) - 1] = part
            else:
                parts[category] = part
        presets[name] = parts
    return presets, warnings
