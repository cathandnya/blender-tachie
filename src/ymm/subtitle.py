"""字幕の改行。Blender のテキストストリップは日本語（空白の無い文）の自動改行が弱いので、こちらで改行を入れる。"""

from __future__ import annotations

import unicodedata

# 行頭に来てはいけない文字（句読点・閉じ括弧・小書き文字など）
_NO_LINE_START = set("、。，．,.!?！？」』）)]】〉》ー…‥ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々〜～")
# 行末に来てはいけない文字（開き括弧）
_NO_LINE_END = set("「『（([【〈《")


def char_width(ch: str) -> float:
    """全角 = 1、半角 = 0.5。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 0.5


def wrap(text: str, max_width: float, max_lines: int | None = None) -> str:
    """全角 `max_width` 文字ごとに改行する。

    禁則: 句読点・閉じ括弧などは行末にぶら下げ、開き括弧が行末に来るときは次の行へ送る。
    元の改行はそのまま残す。`max_lines` を超える分は最後の行に詰める。
    """
    if max_width <= 0:
        return text
    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line, width = "", 0.0
        for ch in paragraph:
            w = char_width(ch)
            if line and width + w > max_width and ch not in _NO_LINE_START:
                carry = line[-1] if line[-1] in _NO_LINE_END and len(line) > 1 else ""
                out.append(line[:-1] if carry else line)
                line, width = carry, sum(map(char_width, carry))
            line += ch
            width += w
        out.append(line)
    if max_lines is not None and len(out) > max_lines > 0:
        out = out[:max_lines - 1] + ["".join(out[max_lines - 1:])]
    return "\n".join(out)


def chars_per_line(frame_width: int, font_size: float, margin: float = 0.1) -> float:
    """画面幅とフォントサイズから、1 行に入る全角文字数の目安。"""
    return frame_width * (1 - 2 * margin) / font_size
