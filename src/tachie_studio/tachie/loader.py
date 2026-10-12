"""YMM4 の「動く立ち絵」「シンプル立ち絵」素材フォルダの読み込み。

動く立ち絵: https://www.manjubox.net/ymm4/faq/立ち絵機能/動く立ち絵素材の作り方
シンプル立ち絵: https://www.manjubox.net/ymm4/faq/立ち絵機能/シンプル立ち絵素材の作り方
  フォルダに画像を入れるだけ（名前は任意）。表情は画像 1 枚で、YMM4 では「口を閉じた画像」と
  「セリフ中の画像（口を開いた画像）」を選ぶ。ここでは体 = 口を閉じた画像、口 = 口を開いた画像として持つ。
"""

from __future__ import annotations

import filecmp
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = ("眉", "目", "口", "髪", "顔色", "体", "他", "後")
IMAGE_SUFFIXES = (".png", ".gif", ".webp")
VOWELS = ("a", "i", "u", "e", "o")

_FRAME_RE = re.compile(r"^(?P<name>.+)\.(?P<tag>\d+|[aiueo])$")


@dataclass
class Part:
    """一覧に出る 1 つのパーツ。

    `frames` はまばたき・音量口パクの連番で、閉じ (.0) → 中間 (.1, .2, …) → 開き (数字なし)
    の順に並ぶ。連番の無いパーツでは `[path]` だけになる。
    """

    category: str
    name: str
    path: Path
    frames: list[Path] = field(default_factory=list)
    vowels: dict[str, Path] = field(default_factory=dict)
    opacity: float = 1.0
    blend: int = 0

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1

    @property
    def has_vowels(self) -> bool:
        return bool(self.vowels)

    @property
    def levels(self) -> list[Path]:
        """音量口パクの段階（閉じ → 開き）。`frames` の先頭部分。

        数字なしの画像が `.0` と同じ内容の素材（数字なし = 閉じのサムネイル、開きは最後の番号）
        では、数字なしを除く。
        """
        if len(self.frames) > 2 and filecmp.cmp(self.frames[0], self.path, shallow=False):
            return self.frames[:-1]
        return self.frames


@dataclass
class Character:
    path: Path
    parts: dict[str, dict[str, Part]]
    kind: str = "moving"  # "moving"（動く立ち絵）/ "simple"（シンプル立ち絵）

    @property
    def simple(self) -> bool:
        return self.kind == "simple"

    @property
    def name(self) -> str:
        return self.path.name

    def part(self, category: str, name: str) -> Part:
        try:
            return self.parts[category][_nfc(name)]
        except KeyError:
            raise KeyError(f"{category}/{name} がありません") from None

    def default(self, category: str) -> Part | None:
        """既定のパーツ。連番（まばたき・口パク）のあるものを優先し、「非表示」や「_」のような
        空のパーツを避け、その中で名前順の最初。"""
        parts = self.parts.get(category)
        if not parts:
            return None
        if self.simple:
            # 体（口を閉じた画像）は「デフォルト」、口（セリフ中）は「口開」を含む名前を優先する。
            # 口は、名前で決まらなければ体の既定と違う画像を選ぶ（同じだと口パクしない）
            if category == "体":
                return min(parts.values(), key=lambda p: ("デフォルト" not in p.name, p.name))
            body = self.default("体")
            return min(parts.values(),
                       key=lambda p: ("口開" not in p.name, body is not None and p.name == body.name, p.name))
        return min(parts.values(), key=lambda p: (not (p.animated or p.has_vowels), _is_placeholder(p.name), p.name))


def _is_placeholder(name: str) -> bool:
    return "非表示" in name or not name.strip("_ ")


def _nfc(name: str) -> str:
    # macOS のファイル名は NFD のことがあるので、比較はすべて NFC で行う
    return unicodedata.normalize("NFC", name)


def read_ini(path: Path) -> dict[str, str]:
    """`key=value` 形式の ini を読む。セクション行とコメントは無視する。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def _load_category(folder: Path, category: str) -> dict[str, Part]:
    bases: dict[str, Path] = {}
    numbered: dict[str, dict[int, Path]] = {}
    vowels: dict[str, dict[str, Path]] = {}

    for file in folder.iterdir():
        if file.suffix.lower() not in IMAGE_SUFFIXES or file.name.startswith("."):
            continue
        stem = _nfc(file.stem)
        m = _FRAME_RE.match(stem)
        if m is None:
            # 拡張子以外に「.」を含むファイルは YMM4 の一覧に出ないので同じ扱いにする
            if "." not in stem:
                bases[stem] = file
        elif m["tag"].isdigit():
            numbered.setdefault(m["name"], {})[int(m["tag"])] = file
        else:
            vowels.setdefault(m["name"], {})[m["tag"]] = file

    parts: dict[str, Part] = {}
    for name, path in bases.items():
        nums = numbered.get(name, {})
        part = Part(
            category=category,
            name=name,
            path=path,
            frames=[nums[i] for i in sorted(nums)] + [path],
            vowels=vowels.get(name, {}),
        )
        ini = folder / f"{name}.ini"
        if ini.is_file():
            values = read_ini(ini)
            if "opacity" in values:
                part.opacity = float(values["opacity"])
            if "blend" in values:
                part.blend = int(values["blend"])
        parts[name] = part
    return parts


def load_character(path: str | Path) -> Character:
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"素材フォルダがありません: {path}")
    parts = {
        category: _load_category(path / category, category)
        for category in CATEGORIES
        if (path / category).is_dir()
    }
    if parts:
        return Character(path=path, parts=parts)
    images = sorted(f for f in path.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES and not f.name.startswith("."))
    if not images:
        raise ValueError(f"立ち絵の素材フォルダではありません（眉・目・口などのフォルダも画像も無い）: {path}")
    # シンプル立ち絵: 同じ画像の一覧を体（口を閉じた画像）と口（セリフ中の画像）の両方から選べるようにする
    simple = {category: {_nfc(f.stem): Part(category=category, name=_nfc(f.stem), path=f, frames=[f]) for f in images}
              for category in ("体", "口")}
    return Character(path=path, parts=simple, kind="simple")
