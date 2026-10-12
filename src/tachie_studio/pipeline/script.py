"""台本テキスト。

1 行に「キャラ名: セリフ」（全角の「：」も可）。表情プリセットは「キャラ名(表情): セリフ」
（（）や [] も可）。空行と # で始まる行は無視する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINE_RE = re.compile(r"^(?P<name>[^:：(（\[]+?)\s*(?:[(（\[](?P<expr>[^)）\]]*)[)）\]])?\s*[:：]\s*(?P<text>.+)$")


@dataclass(frozen=True)
class Line:
    index: int  # 1 から
    character: str
    text: str
    expression: str | None = None  # 表情プリセット名
    speaker: int | None = None  # スタイル ID の上書き（None ならキャラクターの話者）
    voice: dict[str, float] = field(default_factory=dict, hash=False)  # audio_query の値の上書き


def parse_script(text: str, characters=None) -> list[Line]:
    """`characters` にキャラ名の集合か {名前: CharacterConfig} を渡すと、名前（と表情）を検査する。"""
    lines: list[Line] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = _LINE_RE.match(raw)
        if m is None:
            raise ValueError(f"{lineno} 行目: 「キャラ名: セリフ」の形ではありません: {raw}")
        name, body = m["name"].strip(), m["text"].strip()
        expr = (m["expr"] or "").strip() or None
        if characters is not None and name not in characters:
            raise ValueError(f"{lineno} 行目: キャラクター {name!r} が設定にありません")
        if expr and isinstance(characters, dict) and expr not in characters[name].expressions:
            raise ValueError(f"{lineno} 行目: {name} に表情 {expr!r} がありません")
        lines.append(Line(len(lines) + 1, name, body, expr))
    return lines
