"""PSD のレイヤー構造（フォルダの入れ子・表示状態・名前）を読む。Pillow を使わない（Pillow はフォルダを読めず、
レイヤー情報で落ちる PSD もある）。

PSD のレイヤーは下から上の順に並び、フォルダは「フォルダの終わり（lsct = 3）」の記録が下、「フォルダ本体
（lsct = 1 / 2、名前を持つ）」の記録が上に来る。`PsdNode.index` はこの並びでの 0 始まりの番号で、YMM4 の
PSD 立ち絵の設定ファイル（<PSD 名>-ymm.json）が `n<番号>` でレイヤーを指すのと同じ数え方。

名前の先頭の `*` は「同じフォルダの * のうち 1 つだけ表示」、`!` は「常に表示」（PSDToolKit の決まり）。

画像は RGB 8bit のみ。圧縮は無圧縮・RLE・ZIP（予測あり／なし）。合成モードは通常・通過（フォルダ）・乗算で、
それ以外とクリッピングは通常として描く。
"""

from __future__ import annotations

import struct
import warnings
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

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
    channels: list[tuple[int, int, int]] = field(default_factory=list, repr=False)  # (チャンネル ID, 位置, 長さ)

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
    data: bytes = field(default=b"", repr=False)
    depth: int = 8  # 1 チャンネルのビット数
    mode: int = 3  # カラーモード（3 = RGB）
    _images: dict = field(default_factory=dict, repr=False)

    def nodes(self) -> dict[str, PsdNode]:
        """{パス: ノード}（上から）。根はパス ""。"""
        return {"": self.root, **{n.path(): n for n in self.root.walk()}}

    def layer_image(self, node: PsdNode) -> Image.Image | None:
        """レイヤーの画像（RGBA、大きさは rect）。空のレイヤーやフォルダは None。"""
        if node.index in self._images:
            return self._images[node.index]
        if (self.depth, self.mode) != (8, 3):
            raise ValueError(f"RGB 8bit 以外の PSD は描けません（{self.depth}bit、カラーモード {self.mode}）")
        left, top, right, bottom = node.rect
        w, h = right - left, bottom - top
        image = None
        if not node.group and w > 0 and h > 0 and node.channels:
            bands = {cid: _decode_channel(self.data, offset, length, w, h) for cid, offset, length in node.channels
                     if cid in (0, 1, 2, -1)}
            if {0, 1, 2} <= set(bands):
                alpha = bands.get(-1) or Image.new("L", (w, h), 255)
                image = Image.merge("RGBA", (bands[0], bands[1], bands[2], alpha))
        self._images[node.index] = image
        return image

    def visible(self, state: dict[str, bool] | None = None) -> list[PsdNode]:
        """表示するレイヤー（下から）。`state` は {パス: 表示するか} の上書き（無いものは PSD の表示状態）。

        `!` は常に表示、`*` は同じフォルダの `*` のうち表示になっているもの（複数なら上のもの）だけ表示。
        フォルダが表示されていなければ中身も出さない。
        """
        state = state or {}

        def on(node: PsdNode) -> bool:
            if node.required:
                return True
            return state.get(node.path(), not node.hidden)

        out: list[PsdNode] = []

        def visit(node: PsdNode) -> None:
            chosen = next((c for c in node.children if c.radio and on(c)), None)
            for child in reversed(node.children):  # 下から
                shown = child is chosen if child.radio else on(child)
                if not shown:
                    continue
                if child.group:
                    visit(child)
                else:
                    out.append(child)

        visit(self.root)
        return out

    def render(self, state: dict[str, bool] | None = None) -> Image.Image:
        canvas = Image.new("RGBA", (self.width, self.height))
        for node in self.visible(state):
            image = self.layer_image(node)
            if image is None:
                continue
            opacity = node.opacity / 255
            parent = node.parent
            while parent is not None and parent.parent is not None:
                opacity *= parent.opacity / 255
                parent = parent.parent
            if opacity < 1:
                image = image.copy()
                image.putalpha(image.getchannel("A").point(lambda a: round(a * opacity)))
            layer = Image.new("RGBA", canvas.size)
            layer.paste(image, node.rect[:2])
            if node.blend == "mul ":
                from .compose import _multiply
                layer = _multiply(canvas, layer)
            elif node.blend not in ("norm", "pass"):
                warnings.warn(f"合成モード {node.blend!r} は未対応のため通常で描きます: {node.path()}", stacklevel=2)
            canvas.alpha_composite(layer)
        return canvas

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


def _decode_channel(data: bytes, offset: int, length: int, w: int, h: int) -> Image.Image:
    compression = struct.unpack_from(">H", data, offset)[0]
    body = data[offset + 2:offset + length]
    if compression == 0:
        return Image.frombytes("L", (w, h), body[:w * h])
    if compression == 1:  # 行ごとの長さ（2 バイト × 行数）のあとに PackBits
        return Image.frombytes("L", (w, h), body[2 * h:], "packbits", "L")
    if compression in (2, 3):
        raw = bytearray(zlib.decompress(body))
        if compression == 3:  # 行ごとに左隣との差
            for y in range(h):
                row = y * w
                for x in range(1, w):
                    raw[row + x] = (raw[row + x] + raw[row + x - 1]) & 0xFF
        return Image.frombytes("L", (w, h), bytes(raw[:w * h]))
    raise ValueError(f"未対応の圧縮形式です: {compression}")


def read_psd(path: str | Path) -> PsdDocument:
    data = Path(path).read_bytes()
    if data[:4] != b"8BPS":
        raise ValueError(f"PSD ではありません: {path}")
    version = struct.unpack_from(">H", data, 4)[0]
    if version != 1:
        raise ValueError(f"PSB（大きいドキュメント形式）には対応していません: {path}")
    height, width, depth, mode = struct.unpack_from(">IIHH", data, 14)
    p = 26
    p += 4 + _u32(data, p)  # カラーモードデータ
    p += 4 + _u32(data, p)  # 画像リソース
    layer_mask_len = _u32(data, p)  # レイヤーとマスク情報の長さ
    p += 4
    layer_info_len = _u32(data, p) if layer_mask_len >= 4 else 0
    p += 4
    if layer_info_len == 0:
        return PsdDocument(width, height, PsdNode("", -1, group=True), data, depth, mode)
    count = abs(struct.unpack_from(">h", data, p)[0])
    p += 2

    records = []
    for index in range(count):
        top, left, bottom, right = struct.unpack_from(">iiii", data, p)
        p += 16
        channels = struct.unpack_from(">H", data, p)[0]
        channel_list = [struct.unpack_from(">hI", data, p + 2 + 6 * k) for k in range(channels)]
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
        records.append([index, name, section, bool(flags & 2), blend.decode("latin-1"), opacity, bool(clipping),
                        (left, top, right, bottom), channel_list])

    # 記録のあとに、記録の順・チャンネルの順で画像が並ぶ
    for record in records:
        located = []
        for cid, length in record[8]:
            located.append((cid, p, length))
            p += length
        record[8] = located

    # 上（最後の記録）から読んで木にする
    root = PsdNode("", -1, group=True)
    stack = [root]
    for index, name, section, hidden, blend, opacity, clipping, rect, channels in reversed(records):
        if section == SECTION_END:
            if len(stack) > 1:
                stack.pop()
            continue
        node = PsdNode(name, index, hidden, section in (SECTION_OPEN, SECTION_CLOSED), blend, opacity, clipping, rect,
                       parent=stack[-1], channels=channels)
        stack[-1].children.append(node)
        if node.group:
            stack.append(node)
    return PsdDocument(width, height, root, data, depth, mode)


# ---- 表情（表示するレイヤーの上書き）と口パク・まばたき ----

def choose(state: dict[str, bool], node: PsdNode) -> None:
    """`*` のレイヤーを選ぶ（同じフォルダの他の `*` は消す）。`*` でなければ表示にする。"""
    if node.radio and node.parent is not None:
        for sibling in node.parent.children:
            if sibling.radio:
                state[sibling.path()] = sibling is node
    else:
        state[node.path()] = True


def apply_layers(doc: PsdDocument, layers: dict[str, bool] | None) -> dict[str, bool]:
    """表情の上書き {パス: 表示するか} を表示状態にする。`*` を True にすると同じフォルダの他の `*` は消える。"""
    state: dict[str, bool] = {}
    nodes = doc.nodes()
    for path, value in (layers or {}).items():
        node = nodes.get(path)
        if node is None:
            continue
        if value and node.radio:
            choose(state, node)
        else:
            state[path] = bool(value)
    return state


def is_on(node: PsdNode, state: dict[str, bool]) -> bool:
    """親の表示は見ずに、このノード自身が表示か（`!` は常に、`*` は同じフォルダで選ばれているか）。"""
    if node.required:
        return True
    if node.radio and node.parent is not None:
        chosen = next((c for c in node.parent.children if c.radio and state.get(c.path(), not c.hidden)), None)
        return chosen is node
    return state.get(node.path(), not node.hidden)


def is_shown(node: PsdNode, state: dict[str, bool]) -> bool:
    while node is not None and node.parent is not None:
        if not is_on(node, state):
            return False
        node = node.parent
    return True


def radio_groups(doc: PsdDocument) -> list[PsdNode]:
    """中に `*` の選択肢を持つフォルダ（上から）。いちばん外に `*` があれば根（パスは ""）も入る。"""
    nodes = [doc.root, *doc.root.walk()]
    return [n for n in nodes if n.group and any(c.radio for c in n.children)]


def toggles(doc: PsdDocument) -> list[PsdNode]:
    """表示・非表示を切り替えられるレイヤーとフォルダ（`*` でも `!` でもないもの、上から）。"""
    return [n for n in doc.root.walk() if not n.radio and not n.required]


def animation_config_path(psd: str | Path) -> Path:
    psd = Path(psd)
    return psd.with_name(f"{psd.stem}-ymm.json")


def load_animations(psd: str | Path, doc: PsdDocument) -> dict[str, list[list[PsdNode]]]:
    """PSD の隣の口パク・まばたきの設定（YMM4 の <PSD 名>-ymm.json）。{"mouth": [[開き…閉じ], …], "eye": …}。"""
    import json

    out: dict[str, list[list[PsdNode]]] = {"mouth": [], "eye": []}
    path = animation_config_path(psd)
    if not path.is_file():
        return out
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{path} を読めません: {e}") from None
    by_index = {n.index: n for n in doc.root.walk()}
    for key, name in (("MouthAnimations", "mouth"), ("EyeAnimations", "eye")):
        for anim in config.get(key) or []:
            layers = []
            for ref in anim.get("Layers") or []:
                try:
                    layers.append(by_index[int(str(ref).lstrip("n"))])
                except (ValueError, KeyError):
                    layers = []
                    break
            if len(layers) > 1:
                out[name].append(layers)
    return out


def active_animation(animations: list[list[PsdNode]], state: dict[str, bool]) -> list[PsdNode] | None:
    """今の表示状態で使う口パク（まばたき）。表示されているレイヤーを含むもの。"""
    for anim in animations:
        if any(is_shown(n, state) for n in anim):
            return anim
    return None
