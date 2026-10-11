"""キャラクター設定（TOML）。

    ["ずんだもん"]
    folder = "../char/ずんだもん"   # 設定ファイルからの相対パスでもよい
    speaker = 3                     # VOICEVOX のスタイル ID
    mouth = "auto"                  # auto / vowel（あいうえお）/ volume（音量）
    speed = 1.1                     # 省略可: speed pitch intonation volume pre_phoneme post_phoneme

    ["ずんだもん".parts]               # 既定の表情。キーは素材のフォルダ名
    "目" = "普通白目_カメラ目線"
    "口" = "むふ"
    "他" = ["右腕_基本", "左腕_基本"]

    ["ずんだもん".expressions."困り"]  # 表情プリセット（省略可）。基本の表情から変えるパーツだけ書く
    "眉" = "困り眉1"                 # 台本では「ずんだもん(困り): セリフ」
    "目" = "ジト目"

    ["ずんだもん".layout]              # 省略可（Blender アドオンで使う）
    position = 0.3                  # 画面中央 0、右端 0.5
    flip = false                    # 左右反転
    height = 0.9                    # 画面の高さに対する立ち絵の高さ
    channel = 3                     # 立ち絵のチャンネル

    ["ずんだもん".subtitle]            # 省略可
    font = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"
    size = 64
    color = [1.0, 1.0, 1.0, 1.0]
    outline_color = [0.0, 0.4, 0.0, 1.0]
    outline_width = 0.15
    y = 0.12
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..tachie import Expression

MOUTH_MODES = ("auto", "vowel", "volume")

# 設定のキー → audio_query のキー
VOICE_KEYS = {
    "speed": "speedScale",
    "pitch": "pitchScale",
    "intonation": "intonationScale",
    "volume": "volumeScale",
    "pre_phoneme": "prePhonemeLength",
    "post_phoneme": "postPhonemeLength",
}

# 素材のフォルダ名 → Expression のフィールド
PART_KEYS = {
    "眉": "eyebrow",
    "目": "eye",
    "口": "mouth",
    "髪": "hair",
    "顔色": "complexion",
    "体": "body",
    "他": "others",
    "後": "backs",
}


@dataclass
class CharacterConfig:
    name: str
    folder: Path
    speaker: int
    expression: Expression = field(default_factory=Expression)
    mouth: str = "auto"
    voice: dict[str, float] = field(default_factory=dict)  # audio_query のキーで持つ
    expressions: dict[str, dict[str, Any]] = field(default_factory=dict)  # プリセット名 → 上書きするパーツ
    layout: dict[str, Any] = field(default_factory=dict)  # position / flip / height / channel
    subtitle: dict[str, Any] = field(default_factory=dict)  # font / size / color / outline_color / outline_width / y

    def apply_voice(self, query: dict[str, Any]) -> dict[str, Any]:
        query.update(self.voice)
        return query

    def expression_for(self, preset: str | None) -> Expression:
        """表情プリセットを基本の表情に重ねたもの。None や "" は基本の表情。他・後は丸ごと置き換える。"""
        if not preset:
            return self.expression
        if preset not in self.expressions:
            raise ValueError(f"{self.name} に表情 {preset!r} がありません")
        return replace(self.expression, **_expression(self.expressions[preset], f"{self.name}.expressions.{preset}",
                                                     as_kwargs=True))


def _expression(parts: dict[str, Any], name: str, as_kwargs: bool = False) -> Expression | dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for key, value in parts.items():
        if key not in PART_KEYS:
            raise ValueError(f"[{name}.parts] の {key!r} は不明です（{'・'.join(PART_KEYS)} のどれか）")
        attr = PART_KEYS[key]
        if attr in ("others", "backs"):
            kwargs[attr] = [value] if isinstance(value, str) else list(value)
        else:
            kwargs[attr] = value
    return kwargs if as_kwargs else Expression(**kwargs)


def parse_characters(data: dict[str, Any], base: Path) -> dict[str, CharacterConfig]:
    characters = {}
    for name, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"{name!r} はテーブル（[{name}]）で書いてください")
        try:
            folder, speaker = table["folder"], int(table["speaker"])
        except KeyError as e:
            raise ValueError(f"[{name}] に {e.args[0]} がありません") from None
        mouth = table.get("mouth", "auto")
        if mouth not in MOUTH_MODES:
            raise ValueError(f"[{name}] mouth は {' / '.join(MOUTH_MODES)} のどれかです: {mouth!r}")
        expressions = {str(k): dict(v) for k, v in table.get("expressions", {}).items()}
        for preset, parts in expressions.items():
            _expression(parts, f"{name}.expressions.{preset}")  # キーの検査
        characters[name] = CharacterConfig(
            name=name,
            folder=(base / folder).resolve(),
            speaker=speaker,
            expression=_expression(table.get("parts", {}), name),
            mouth=mouth,
            voice={VOICE_KEYS[k]: float(v) for k, v in table.items() if k in VOICE_KEYS},
            expressions=expressions,
            layout=dict(table.get("layout", {})),
            subtitle=dict(table.get("subtitle", {})),
        )
    return characters


def load_characters(path: str | Path) -> dict[str, CharacterConfig]:
    path = Path(path)
    with path.open("rb") as f:
        return parse_characters(tomllib.load(f), path.parent)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(round(value, 6) if isinstance(value, float) else value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"TOML に書けない値です: {value!r}")


def _toml_key(key: str) -> str:
    return key if key.isascii() and key.replace("_", "").isalnum() else _toml_value(key)


def dump_characters(configs: dict[str, CharacterConfig], base: Path | None = None) -> str:
    """`load_characters` で読める TOML を返す。`base` を渡すと folder をそこからの相対パスで書く。"""
    voice_names = {v: k for k, v in VOICE_KEYS.items()}
    out: list[str] = []
    for name, c in configs.items():
        folder = Path(c.folder)
        if base is not None:
            try:
                folder = Path(os.path.relpath(folder, base))
            except ValueError:  # Windows でドライブが違う
                pass
        key = _toml_key(name)
        out.append(f"[{key}]")
        out.append(f"folder = {_toml_value(folder.as_posix())}")
        out.append(f"speaker = {c.speaker}")
        out.append(f"mouth = {_toml_value(c.mouth)}")
        for qkey, value in c.voice.items():
            out.append(f"{voice_names[qkey]} = {_toml_value(value)}")
        parts = {k: getattr(c.expression, attr) for k, attr in PART_KEYS.items()}
        parts = {k: v for k, v in parts.items() if v}
        sections = [("parts", parts)]
        sections += [(f"expressions.{_toml_key(p)}", v) for p, v in c.expressions.items()]
        sections += [("layout", c.layout), ("subtitle", c.subtitle)]
        for section, values in sections:
            if values or section.startswith("expressions."):
                out.append("")
                out.append(f"[{key}.{section}]")
                out.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in values.items())
        out.append("")
    return "\n".join(out)
