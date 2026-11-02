"""字幕の改行。Blender のテキストストリップは日本語（空白の無い文）の自動改行が弱いので、こちらで改行を入れる。"""

from __future__ import annotations

import unicodedata
from typing import Callable

# 行頭に来てはいけない文字（句読点・閉じ括弧・小書き文字など）
_NO_LINE_START = set("、。，．,.!?！？」』）)]】〉》ー…‥ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々〜～")
# 行末に来てはいけない文字（開き括弧）
_NO_LINE_END = set("「『（([【〈《")


BREAK = "\\n"  # セリフの改行の書き方（Blender の入力欄には改行を入れられないので、文字で書く）


def mark_breaks(text: str) -> str:
    """改行と ¥n（Mac の JIS キーボードの \\）を \\n の書き方にそろえる（入力欄・ストリップに持つテキストの形）。"""
    return text.replace("\r\n", "\n").replace("\n", BREAK).replace("¥n", BREAK)


def breaks(text: str) -> str:
    """\\n の書き方（と ¥n）を改行にする（字幕・字幕ファイル・読み上げに渡すとき）。"""
    return mark_breaks(text).replace(BREAK, "\n")


def char_width(ch: str) -> float:
    """全角 = 1、半角 = 0.5。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 0.5


def _in_word(ch: str) -> bool:
    """英単語（半角の英数字の並び）の文字。単語の途中では改行しない。"""
    return ch.isascii() and (ch.isalnum() or ch in "'_")


def wrap(text: str, max_width: float, max_lines: int | None = None,
         width_of: Callable[[str], float] = char_width) -> str:
    """1 行の幅が `max_width` を超えないように改行する。幅は `width_of`（1 文字の幅。既定は全角 = 1、半角 = 0.5 の
    文字数。フォントで測った px を渡せば `max_width` も px）。

    禁則: 句読点・閉じ括弧などは行末にぶら下げ、開き括弧が行末に来るときは次の行へ送る。英単語は途中で切らずに
    単語ごと次の行へ送る（1 行に入らない長さの単語だけは途中で切る）。空白で改行するときは空白を行末・行頭に残さない。
    元の改行はそのまま残す。`max_lines` を超える分は最後の行に詰める。
    """
    if max_width <= 0:
        return text
    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line, width = "", 0.0
        word = None  # line の中で今の英単語が始まる位置（単語の途中でなければ None）
        wrapped = False  # 折り返した直後（行頭の空白を捨てる。元の段落の頭の空白は残す）
        for ch in paragraph:
            w = width_of(ch)
            if line and width + w > max_width and ch not in _NO_LINE_START:
                if ch == " ":  # 空白で改行するなら空白は捨てる
                    out.append(line.rstrip(" "))
                    line, width, word, wrapped = "", 0.0, None, True
                    continue
                # 単語ごと次の行へ。直前の開き括弧も一緒に送る（行頭から始まる長い単語は途中で切る）
                start = word - 1 if word and line[word - 1] in _NO_LINE_END else word
                if _in_word(ch) and start is not None and line[:start].strip(" "):
                    carry = line[start:]
                    out.append(line[:start].rstrip(" "))
                    word -= start
                else:
                    carry = line[-1] if line[-1] in _NO_LINE_END and len(line) > 1 else ""
                    out.append(line[:-1] if carry else line.rstrip(" "))
                    word = None
                line, width = carry, sum(map(width_of, carry))
                wrapped = True
            if wrapped and not line and ch == " ":
                continue
            wrapped = False
            if _in_word(ch):
                if word is None:
                    word = len(line)
            else:
                word = None
            line += ch
            width += w
        out.append(line)
    if max_lines is not None and len(out) > max_lines > 0:
        out = out[:max_lines - 1] + ["".join(out[max_lines - 1:])]
    return "\n".join(out)


WRAP_MARGIN = 0.1  # 折り返し幅を決めていないときの、画面の左右の余白（幅に対する割合）
