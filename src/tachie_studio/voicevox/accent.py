"""アクセント句（audio_query の accent_phrases）の編集: 句の結合・分割と、句から発音（AquesTalk 風記法）を作る。

句の区切りやアクセントの位置を変えたあとの音高・長さは、エンジンの /mora_pitch・/mora_data で計算し直す（呼ぶのは使う側）。
"""

from __future__ import annotations

import copy
from typing import Any

UNVOICED = set("AIUEO")  # 無声化した母音（エンジンは大文字で返す）


def kana_from_phrases(phrases: list[dict[str, Any]]) -> str:
    """accent_phrases → 発音（例: キョ'オワ/イ'イ/テ'ンキデ_スネ、ソオデ_ス'カ？）。エンジンの書き方と同じ:
    無声化したモーラの前に _、アクセントのモーラの後ろに '、句の区切りは /（間があれば 、）、疑問文は句の終わりに ？。"""
    out = []
    for i, ap in enumerate(phrases):
        text = ""
        for k, m in enumerate(ap["moras"], start=1):
            text += ("_" if m.get("vowel") in UNVOICED else "") + m["text"]
            if k == ap.get("accent"):
                text += "'"
        if ap.get("is_interrogative"):
            text += "？"
        out.append(text)
        if i < len(phrases) - 1:
            out.append("、" if ap.get("pause_mora") else "/")
    return "".join(out)


def join_phrases(phrases: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """`index` 番目の句と次の句を 1 つにする（アクセントは前の句の位置、間と疑問文は後ろの句のもの）。"""
    if not 0 <= index < len(phrases) - 1:
        raise ValueError("結合する句がありません")
    out = copy.deepcopy(phrases)
    a, b = out[index], out.pop(index + 1)
    a["moras"] = a["moras"] + b["moras"]
    a["pause_mora"] = b.get("pause_mora")
    a["is_interrogative"] = b.get("is_interrogative", False)
    return out


def split_phrase(phrases: list[dict[str, Any]], index: int, mora: int) -> list[dict[str, Any]]:
    """`index` 番目の句を `mora` 番目（0 始まり）のモーラの手前で 2 つに分ける（アクセントは前の句の中ならそのまま、
    後ろの句の中ならそちらに移し、もう片方は 1 モーラ目。間と疑問文は後ろの句に）。"""
    out = copy.deepcopy(phrases)
    ap = out[index]
    if not 0 < mora < len(ap["moras"]):
        raise ValueError("句の最初や最後では分けられません")
    accent = ap.get("accent", 1)
    head = {**ap, "moras": ap["moras"][:mora], "accent": accent if accent <= mora else 1,
            "pause_mora": None, "is_interrogative": False}
    tail = {**ap, "moras": ap["moras"][mora:], "accent": accent - mora if accent > mora else 1}
    out[index:index + 1] = [head, tail]
    return out


def split_kana_lines(kana: str, lines: int = 3) -> list[str]:
    """発音を句の区切りで `lines` 行に分ける（区切りの / と 、 は前の行の終わりに残す。つなげると元の発音）。
    長さがだいたいそろうように、行の長さが全体の 1/lines を超えたら次の行へ。"""
    phrases, current = [], ""
    for ch in kana:
        current += ch
        if ch in "/、":
            phrases.append(current)
            current = ""
    if current:
        phrases.append(current)
    out = [""] * lines
    target, row = len(kana) / lines, 0
    for phrase in phrases:
        if out[row] and row < lines - 1 and len(out[row]) + len(phrase) / 2 > target:
            row += 1
        out[row] += phrase
    return out
