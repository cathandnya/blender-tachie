"""動く立ち絵の素材から、同じキャラの PSD 立ち絵用の口パク・まばたきの設定（YMM4 の <PSD 名>-ymm.json）を作る。

動く立ち絵の連番（例: `口/むふ.1.png`）が、どのパーツの画像と同じ中身かで口パク・まばたきの相手を割り出し
（`むふ.1.png` が `お.png` と同じなら「むふ ↔ お」）、PSD の同じ名前のレイヤーの番号に置き換える。
`普通白目_カメラ目線` のように「_」でつないだ名前は、PSD の `!目` の中で、その名前の部品をすべて含む
選択肢（`*目セット` など）に当てる。

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
from .psd import PsdDocument, PsdNode, read_psd

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


def animations_from_material(character: Character, doc: PsdDocument) -> tuple[dict, list[str]]:
    """(設定ファイルの中身, 警告) を返す。"""
    config: dict = {"Presets": [], "MouthAnimations": [], "EyeAnimations": []}
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
            layers = [f"n{n.index}" for n in nodes]
            if tuple(layers) in seen:
                continue
            seen.add(tuple(layers))
            config[key].append({"Layers": layers})
    return config, warnings


def config_path(psd: str | Path) -> Path:
    psd = Path(psd)
    return psd.with_name(f"{psd.stem}-ymm.json")


def write_config(psd: str | Path, config: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else config_path(psd)
    # 既にあるファイルの口パク・まばたき以外（YMM4 で作った Presets など）は残す
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(existing, dict):
            config = {**existing, "MouthAnimations": config["MouthAnimations"],
                      "EyeAnimations": config["EyeAnimations"]}
    # YMM4 の書き出しと同じく BOM 付き UTF-8、インデント 2
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    return path


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
