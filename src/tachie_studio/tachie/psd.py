"""PSD のレイヤー構造（フォルダの入れ子・表示状態・名前）を読む。Pillow を使わない（Pillow はフォルダを読めず、
レイヤー情報で落ちる PSD もある）。

PSD のレイヤーは下から上の順に並び、フォルダは「フォルダの終わり（lsct = 3）」の記録が下、「フォルダ本体
（lsct = 1 / 2、名前を持つ）」の記録が上に来る。`PsdNode.index` はこの並びでの 0 始まりの番号で、YMM4 の
PSD 立ち絵の設定ファイル（<PSD 名>-ymm.json）が `n<番号>` でレイヤーを指すのと同じ数え方。

名前の先頭の `*` は「同じフォルダの * のうち 1 つだけ表示」、`!` は「常に表示」（PSDToolKit の決まり）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

SECTION_OPEN, SECTION_CLOSED, SECTION_END = 1, 2, 3


@dataclass
class PsdNode:
    name: str
    index: int  # 下から数えた 0 始まりのレイヤー番号
    hidden: bool = False
    group: bool = False
    blend: str = "norm"
    opacity: int = 255
    clipping: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    children: list["PsdNode"] = field(default_factory=list)  # 上から下の順
    parent: "PsdNode | None" = field(default=None, repr=False)

    @property
    def label(self) -> str:
        """`*` `!` を除いた名前。"""
        return self.name.lstrip("*!")

    @property
    def radio(self) -> bool:
        return self.name.startswith("*")

    @property
    def required(self) -> bool:
        return self.name.startswith("!")

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def path(self) -> str:
        names = []
        node = self
        while node is not None and node.parent is not None:
            names.append(node.name)
            node = node.parent
        return "/".join(reversed(names))


@dataclass
class PsdDocument:
    width: int
    height: int
    root: PsdNode  # 名前の無い根。children がいちばん外側のレイヤー（上から）

    def find(self, path: str) -> PsdNode | None:
        """`!目/*目セット` のような名前の並び（`*` `!` は省いてもよい）でノードを探す。"""
        node = self.root
        for part in path.split("/"):
            node = next((c for c in node.children if c.name == part or c.label == part.lstrip("*!")), None)
            if node is None:
                return None
        return node


def _u32(d: bytes, p: int) -> int:
    return struct.unpack_from(">I", d, p)[0]


def read_psd(path: str | Path) -> PsdDocument:
    data = Path(path).read_bytes()
    if data[:4] != b"8BPS":
        raise ValueError(f"PSD ではありません: {path}")
    version = struct.unpack_from(">H", data, 4)[0]
    if version != 1:
        raise ValueError(f"PSB（大きいドキュメント形式）には対応していません: {path}")
    height, width = struct.unpack_from(">II", data, 14)
    p = 26
    p += 4 + _u32(data, p)  # カラーモードデータ
    p += 4 + _u32(data, p)  # 画像リソース
    layer_mask_len = _u32(data, p)  # レイヤーとマスク情報の長さ
    p += 4
    layer_info_len = _u32(data, p) if layer_mask_len >= 4 else 0
    p += 4
    if layer_info_len == 0:
        return PsdDocument(width, height, PsdNode("", -1, group=True))
    count = abs(struct.unpack_from(">h", data, p)[0])
    p += 2

    records = []
    for index in range(count):
        top, left, bottom, right = struct.unpack_from(">iiii", data, p)
        p += 16
        channels = struct.unpack_from(">H", data, p)[0]
        p += 2 + 6 * channels
        _, blend, opacity, clipping, flags, _ = struct.unpack_from(">4s4sBBBB", data, p)
        p += 12
        extra_len = _u32(data, p)
        p += 4
        e, end = p, p + extra_len
        p = end
        e += 4 + _u32(data, e)  # レイヤーマスク
        e += 4 + _u32(data, e)  # ブレンド範囲
        name_len = data[e]
        name = data[e + 1:e + 1 + name_len].decode("cp932", errors="replace")
        e += (1 + name_len + 3) // 4 * 4
        section = 0
        while e + 12 <= end:
            key = data[e + 4:e + 8]
            length = _u32(data, e + 8)
            block = data[e + 12:e + 12 + length]
            e += 12 + length
            if key == b"luni" and len(block) >= 4:
                chars = _u32(block, 0)
                name = block[4:4 + 2 * chars].decode("utf-16-be", errors="replace")
            elif key in (b"lsct", b"lsdk") and len(block) >= 4:
                section = _u32(block, 0)
        records.append((index, name, section, bool(flags & 2), blend.decode("latin-1"), opacity, bool(clipping),
                        (left, top, right, bottom)))

    # 上（最後の記録）から読んで木にする
    root = PsdNode("", -1, group=True)
    stack = [root]
    for index, name, section, hidden, blend, opacity, clipping, rect in reversed(records):
        if section == SECTION_END:
            if len(stack) > 1:
                stack.pop()
            continue
        node = PsdNode(name, index, hidden, section in (SECTION_OPEN, SECTION_CLOSED), blend, opacity, clipping, rect,
                       parent=stack[-1])
        stack[-1].children.append(node)
        if node.group:
            stack.append(node)
    return PsdDocument(width, height, root)
