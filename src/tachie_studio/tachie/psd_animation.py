"""動く立ち絵の素材から、同じキャラの PSD 立ち絵用の口パク・まばたきの設定（YMM4 の <PSD 名>-ymm.json）を作る。

動く立ち絵の連番（例: `口/むふ.1.png`）が、どのパーツの画像と同じ中身かで口パク・まばたきの相手を割り出し
（`むふ.1.png` が `お.png` と同じなら「むふ ↔ お」）、PSD の同じ名前のレイヤーの番号に置き換える。
`普通白目_カメラ目線` のように「_」でつないだ名前は、PSD の `!目` の中で、その名前の部品をすべて含む
選択肢（`*目セット` など）の中の、いちばん深い部品のレイヤー（`*目セット/!黒目/*カメラ目線`）に当てる。
YMM4 はフォルダを指した組を使わない（PSD 立ち絵素材マネージャーに出てこず、まばたきしない。YMM4 Lite 4.56 で確認）。
各組には YMM4 と同じく `LayerPaths`（レイヤーのパス）も書く。

YMM4 の設定ファイルでは 1 つのアニメーションのレイヤーを「開いた状態 → 閉じた状態」の順に並べる。
連番は公式の決まりでは .0 = 閉じ → … → 数字なし = 開き。手元の素材は数字なしが .0 と同じで、口は .1, .2 … と
開いていき（数字なし = 閉じ）、目は .1, .2 … と閉じていく（数字なし = 開き）。.0 が数字なしと同じかで見分ける。

    uv run python -m tachie_studio.tachie.psd_animation char/ずんだもん char/ずんだもん/ずんだもん立ち絵素材2.3.psd
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .loader import Character, load_character
from .psd import PsdDocument, PsdNode, animation_entry, animation_config_path, read_psd, write_animation_config

# (素材のフォルダ, 設定ファイルのキー, 数字なしの画像が開いた状態か)
ANIMATIONS = (("口", "MouthAnimations", False), ("目", "EyeAnimations", True))


def _digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _option(group: PsdNode, name: str) -> PsdNode | None:
    """PSD のフォルダ `group` の中で、動く立ち絵のパーツ名 `name` に当たる選択肢。"""
    for child in group.children:
        if child.label == name:
            return child
    pieces = name.split("_")
    if len(pieces) > 1:
        for child in group.children:
            labels = {child.label} | {d.label for d in child.walk()}
            if all(p in labels for p in pieces):
                return child
    return None


def _depth(node: PsdNode) -> int:
    depth = 0
    while node.parent is not None:
        node, depth = node.parent, depth + 1
    return depth


def _leaf(node: PsdNode, name: str) -> PsdNode:
    """選択肢がフォルダなら、パーツ名の部品に当たる中のレイヤーのうちいちばん深いもの（同じ深さなら後の部品）。
    部品に当たるものが無ければ、PSD で表示されているいちばん深いレイヤー。"""
    if not node.group:
        return node
    pieces = name.split("_")
    leaves = [d for d in node.walk() if not d.group]
    matched = [d for d in leaves if d.label in pieces]
    if matched:
        return max(matched, key=lambda d: (_depth(d), pieces.index(d.label)))
    shown = [d for d in leaves if not d.hidden or d.required]
    return max(shown or leaves or [node], key=_depth)


def animations_from_material(character: Character, doc: PsdDocument) -> tuple[dict, list[str]]:
    """(設定ファイルの中身, 警告) を返す。"""
    config: dict = {"MouthAnimations": [], "EyeAnimations": []}
    warnings: list[str] = []
    for category, key, base_open in ANIMATIONS:
        group = next((c for c in doc.root.children if c.label == category), None)
        parts = character.parts.get(category, {})
        if group is None or not parts:
            warnings.append(f"PSD か素材に「{category}」がありません")
            continue
        by_digest = {}
        for part in sorted(parts.values(), key=lambda p: p.name):
            by_digest.setdefault(_digest(part.path), part.name)
        seen = set()
        for part in sorted(parts.values(), key=lambda p: p.name):
            if not part.animated:
                continue
            own = _digest(part.path)
            names = []
            for frame in part.frames:
                digest = _digest(frame)
                # 同じ画像のパーツが他にあっても、自分の画像は自分の名前にする
                name = part.name if digest == own else by_digest.get(digest)
                if name is None:
                    warnings.append(f"{category}/{frame.name} と同じ画像のパーツがありません")
                    continue
                if name not in names:
                    names.append(name)
            if len(names) < 2:
                continue
            # names は連番の順。.0 が数字なしと同じ目だけは開き → 閉じ、他（公式の決まり、手元の口）は閉じ → 開き
            sequence = names if base_open and names[0] == part.name else names[::-1]
            nodes = [_option(group, n) for n in sequence]
            if any(n is None for n in nodes):
                missing = [s for s, n in zip(sequence, nodes) if n is None]
                warnings.append(f"PSD の「{category}」に {'、'.join(missing)} がありません（{part.name} を飛ばします）")
                continue
            nodes = [_leaf(n, s) for n, s in zip(nodes, sequence)]
            entry = animation_entry(nodes)
            if tuple(entry["Layers"]) in seen or len(set(entry["Layers"])) < 2:
                continue
            seen.add(tuple(entry["Layers"]))
            config[key].append(entry)
    return config, warnings


def config_path(psd: str | Path) -> Path:
    return animation_config_path(psd)


def write_config(psd: str | Path, config: dict, path: str | Path | None = None) -> Path:
    """口パク・まばたきだけを書き換える（既にあるファイルの Presets など、YMM4 で作ったものは残す）。"""
    return write_animation_config(path or config_path(psd), {"MouthAnimations": config["MouthAnimations"],
                                                             "EyeAnimations": config["EyeAnimations"]})


def describe(config: dict, doc: PsdDocument) -> list[str]:
    nodes = {n.index: n for n in doc.root.walk()}
    lines = []
    for key in ("MouthAnimations", "EyeAnimations"):
        for anim in config[key]:
            names = [nodes[int(l[1:])].path() if int(l[1:]) in nodes else l for l in anim["Layers"]]
            lines.append(f"{key}: {' → '.join(names)}")
    return lines


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="動く立ち絵の素材から PSD 立ち絵用の口パク・まばたきの設定を作る")
    p.add_argument("material", type=Path, help="動く立ち絵の素材フォルダ")
    p.add_argument("psd", type=Path, help="同じキャラの PSD 立ち絵")
    p.add_argument("-o", "--output", type=Path, help="書き出し先（既定は PSD の隣の <PSD 名>-ymm.json）")
    p.add_argument("--dry-run", action="store_true", help="書き出さずに中身だけ表示する")
    args = p.parse_args(argv)
    doc = read_psd(args.psd)
    config, warnings = animations_from_material(load_character(args.material), doc)
    for line in describe(config, doc):
        print(line)
    for w in warnings:
        print("警告:", w)
    if not args.dry_run:
        print("書き出し:", write_config(args.psd, config, args.output))


if __name__ == "__main__":
    main()
