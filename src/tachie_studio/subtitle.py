"""字幕の改行。Blender のテキストストリップは日本語（空白の無い文）の自動改行が弱いので、こちらで改行を入れる。"""

from __future__ import annotations

import functools
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


def _can_start(p: str, i: int) -> bool:
    """`p[i]` から行を始めてよいか（禁則と、英単語の途中でないこと）。"""
    return not (p[i] in _NO_LINE_START or p[i - 1] in _NO_LINE_END or (_in_word(p[i - 1]) and _in_word(p[i])))


def _wrap_paragraph(p: str, max_width: float, width_of: Callable[[str], float],
                    phrases: Callable[[str], list[str]] | None) -> list[str]:
    if sum(map(width_of, p)) <= max_width:  # 1 行に入る（文節に分けずに済ませる）
        return [p]
    n = len(p)
    ok = [False] + [_can_start(p, i) for i in range(1, n)]
    if phrases is None:
        preferred = ok
    else:  # 文節の切れ目（と空白のあと）を優先する
        preferred = [False] * n
        at = 0
        for phrase in phrases(p):
            at += len(phrase)
            if at < n:
                preferred[at] = ok[at]
        for i in range(1, n):
            preferred[i] = preferred[i] or (ok[i] and p[i - 1] == " ")
    lines: list[str] = []
    start, width, i = 0, 0.0, 0
    while i < n:
        w = width_of(p[i])
        if i > start and width + w > max_width and p[i] not in _NO_LINE_START:  # 句読点などはぶら下げる
            # 前に戻って、優先する切れ目 → 切ってよい所 → 今の文字の前、の順に探す（前の行が空白だけになる所は除く）
            def last(marks: list[bool]) -> int | None:
                return next((j for j in range(i, start, -1) if marks[j] and p[start:j].strip(" ")), None)

            cut = last(preferred) or last(ok) or i
            lines.append(p[start:cut].rstrip(" "))
            start = cut
            while start < n and p[start] == " ":  # 折り返した行の頭の空白は捨てる
                start += 1
            i = max(i, start)
            width = sum(map(width_of, p[start:i]))
            continue
        width += w
        i += 1
    lines.append(p[start:])
    return lines


def wrap(text: str, max_width: float, max_lines: int | None = None,
         width_of: Callable[[str], float] = char_width, phrases: Callable[[str], list[str]] | None = None) -> str:
    """1 行の幅が `max_width` を超えないように改行する。幅は `width_of`（1 文字の幅。既定は全角 = 1、半角 = 0.5 の
    文字数。フォントで測った px を渡せば `max_width` も px）。

    `phrases`（文を文節に分ける関数、`phrase_splitter`）を渡すと、文節の切れ目で折り返す（1 行に入らない文節は
    文字の間で切る）。禁則: 句読点・閉じ括弧などは行末にぶら下げ、開き括弧が行末に来るときは次の行へ送る。
    英単語は途中で切らない（1 行に入らない長さの単語だけは途中で切る）。空白で改行するときは空白を行末・行頭に
    残さない。元の改行はそのまま残す。`max_lines` を超える分は最後の行に詰める。
    """
    if max_width <= 0:
        return text
    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        out += _wrap_paragraph(paragraph, max_width, width_of, phrases)
    if max_lines is not None and len(out) > max_lines > 0:
        out = out[:max_lines - 1] + ["".join(out[max_lines - 1:])]
    return "\n".join(out)


@functools.cache
def phrase_splitter() -> Callable[[str], list[str]] | None:
    """日本語の文を文節に分ける関数（BudouX）。BudouX が入っていなければ None（文字の間で折り返す）。
    結果は覚える（入っていないときも、字幕を折り返すたびに import を試し直さない）。"""
    try:
        import budoux
    except ImportError:
        return None
    return functools.lru_cache(maxsize=1024)(budoux.load_default_japanese_parser().parse)  # 同じセリフを分け直さない


WRAP_MARGIN = 0.1  # 折り返し幅を決めていないときの、画面の左右の余白（幅に対する割合）
