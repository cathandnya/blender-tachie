"""台本テキスト。

1 行に「キャラ名: セリフ」（全角の「：」も可）。表情プリセットは「キャラ名(表情): セリフ」
（（）や [] も可）。空行と # で始まる行は無視する。
セリフの最後に `{キョ'ウワ/イ'イ/テ'ンキ}` と書くと、その発音（AquesTalk 風記法）で読む。
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
    kana: str | None = None  # 発音（AquesTalk 風記法）。None なら VOICEVOX が自動で付けた読み
    accent_phrases: list | None = field(default=None, hash=False)  # 抑揚を編集した accent_phrases（kana より優先）
    # 抑揚を編集したときの発音。指定すると、今の発音（kana か自動の読み）と違えば accent_phrases を使わない
    accent_kana: str | None = None
    # 以下 None ならキャラクター設定の [audio] の値
    fade_in: float | None = None  # 音声の先頭のフェード（秒）
    fade_out: float | None = None  # 音声の末尾のフェード（秒）
    after: float | None = None  # セリフ後の余韻（秒）。音声の後ろに口を閉じた立ち絵（まばたきのみ）を足す


_KANA_RE = re.compile(r"^(?P<text>.*?)\s*[{｛](?P<kana>[^{}｛｝]*)[}｝]\s*$")


def split_kana(text: str) -> tuple[str, str | None]:
    """「セリフ{発音}」をセリフと発音に分ける。発音が無ければ (text, None)。"""
    m = _KANA_RE.match(text)
    if not m or not m["kana"].strip() or not m["text"].strip():
        return text, None
    return m["text"], m["kana"].strip()


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
        body, kana = split_kana(body)
        lines.append(Line(len(lines) + 1, name, body, expr, kana=kana))
    return lines
