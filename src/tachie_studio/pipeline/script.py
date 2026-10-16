"""台本テキスト。

1 行に「キャラ名: セリフ」（全角の「：」も可）。表情プリセットは「キャラ名(表情): セリフ」
（（）や [] も可）。空行と # で始まる行は無視する。
セリフの最後に `{キョ'ウワ/イ'イ/テ'ンキ}` と書くと、その発音（AquesTalk 風記法）で読む。

YMM4 の台本の形も読める（キャラ名の欄では表情・発音の書き方もそのまま使える）:
- テキスト: `キャラ名「セリフ」`。セリフの中の括弧は `\\「` `\\」`、「」の中で改行してよい
- CSV / TSV（`load_script` が拡張子で選ぶ）: `キャラ名,セリフ`。カンマ・改行を含むセリフは `"` で囲む
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

_LINE_RE = re.compile(r"^(?P<name>[^:：(（\[]+?)\s*(?:[(（\[](?P<expr>[^)）\]]*)[)）\]])?\s*[:：]\s*(?P<text>.+)$")
_NAME_RE = re.compile(r"^(?P<name>[^(（\[]+?)\s*(?:[(（\[](?P<expr>[^)）\]]*)[)）\]])?$")
# YMM4 のテキスト台本: キャラ名「セリフ」（キャラ名に : や括弧の「」は入らない）
_BRACKET_RE = re.compile(r"^(?P<head>[^:：「」]+?)\s*「(?P<rest>.*)$", re.S)
FORMATS = {".csv": "csv", ".tsv": "tsv"}  # 他の拡張子はテキスト


def script_format(name: str | Path) -> str:
    """ファイル名（テキストの名前）の拡張子から台本の形（"text" / "csv" / "tsv"）。"""
    return FORMATS.get(Path(name).suffix.lower(), "text")


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
    # 合成済みの音声（WAV）。あれば VOICEVOX で合成せずにこれを使う（YMM4 が .ymmp に保存した音声など）
    wav: bytes | None = field(default=None, hash=False, compare=False, repr=False)


_KANA_RE = re.compile(r"^(?P<text>.*?)\s*[{｛](?P<kana>[^{}｛｝]*)[}｝]\s*$")


def split_kana(text: str) -> tuple[str, str | None]:
    """「セリフ{発音}」をセリフと発音に分ける。発音が無ければ (text, None)。"""
    m = _KANA_RE.match(text)
    if not m or not m["kana"].strip() or not m["text"].strip():
        return text, None
    return m["text"], m["kana"].strip()


def _close_bracket(text: str) -> int:
    """エスケープされていない最初の「」」の位置（無ければ -1）。"""
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "」":
            return i
        i += 1
    return -1


def _text_entries(text: str):
    """テキスト台本 → (行番号, キャラ名の欄, セリフ)。"""
    rows = text.splitlines()
    i = 0
    while i < len(rows):
        lineno, raw = i + 1, rows[i].strip()
        i += 1
        if not raw or raw.startswith("#"):
            continue
        m = _BRACKET_RE.match(raw)
        if m is not None:
            rest = m["rest"]
            while (end := _close_bracket(rest)) < 0 and i < len(rows):  # 「」の中の改行
                rest += "\n" + rows[i]
                i += 1
            if end < 0:
                raise ValueError(f"{lineno} 行目: 「」が閉じていません: {raw}")
            if rest[end + 1:].strip():
                raise ValueError(f"{lineno} 行目: 」の後ろに文字があります（セリフの中の括弧は \\「 \\」）: {raw}")
            body = re.sub(r"\\([「」\\])", r"\1", rest[:end])
            yield lineno, m["head"], body
            continue
        m = _LINE_RE.match(raw)
        if m is None:
            raise ValueError(f"{lineno} 行目: 「キャラ名: セリフ」か「キャラ名「セリフ」」の形ではありません: {raw}")
        yield lineno, raw[:m.start("text")].rstrip().rstrip(":："), m["text"]


def _table_entries(text: str, delimiter: str):
    """CSV / TSV 台本 → (行番号, キャラ名の欄, セリフ)。3 列目より後ろは使わない。"""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    while True:
        lineno = reader.line_num + 1  # 改行を含むセリフでも始まりの行
        try:
            row = next(reader, None)
        except csv.Error as e:
            raise ValueError(f"{lineno} 行目: 読めません: {e}") from None
        if row is None:
            return
        if not any(c.strip() for c in row) or row[0].lstrip().startswith("#"):
            continue
        if len(row) < 2 or not row[1].strip():
            sep = "," if delimiter == "," else "<タブ>"
            raise ValueError(f"{lineno} 行目: 「キャラ名{sep}セリフ」の形ではありません: {sep.join(row)}")
        yield lineno, row[0], row[1]


def read_script(path: str | Path) -> str:
    """台本ファイルを読む。UTF-8（BOM 付きも）で読めなければ Shift_JIS（Excel の CSV）。"""
    data = Path(path).read_bytes()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # Excel の「Unicode テキスト」
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp932")
        except UnicodeDecodeError:
            raise ValueError(f"{Path(path).name}: 文字コードが UTF-8 でも Shift_JIS でもありません") from None


def load_script(path: str | Path, characters=None) -> list[Line]:
    """台本ファイルを拡張子に合った形（.csv / .tsv / それ以外はテキスト）で読む。"""
    return parse_script(read_script(path), characters, fmt=script_format(path))


def parse_script(text: str, characters=None, fmt: str = "text") -> list[Line]:
    """`characters` にキャラ名の集合か {名前: CharacterConfig} を渡すと、名前（と表情）を検査する。
    `fmt` は "text"（「キャラ名: セリフ」か YMM4 の「キャラ名「セリフ」」）/ "csv" / "tsv"。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # CRLF の CSV の「"」の中の改行
    if fmt == "text":
        entries = _text_entries(text)
    elif fmt in ("csv", "tsv"):
        entries = _table_entries(text, "," if fmt == "csv" else "\t")
    else:
        raise ValueError(f"台本の形 {fmt!r} は読めません")
    lines: list[Line] = []
    for lineno, head, body in entries:
        m = _NAME_RE.match(head.strip())
        if m is None:
            raise ValueError(f"{lineno} 行目: キャラ名が読めません: {head}")
        name, body = m["name"].strip(), body.strip()
        expr = (m["expr"] or "").strip() or None
        if characters is not None and name not in characters:
            raise ValueError(f"{lineno} 行目: キャラクター {name!r} が設定にありません")
        if expr and isinstance(characters, dict) and expr not in characters[name].expressions:
            raise ValueError(f"{lineno} 行目: {name} に表情 {expr!r} がありません")
        body, kana = split_kana(body)
        if not body:
            raise ValueError(f"{lineno} 行目: セリフがありません")
        lines.append(Line(len(lines) + 1, name, body, expr, kana=kana))
    return lines
