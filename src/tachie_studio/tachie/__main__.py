"""動く立ち絵を 1 枚の PNG に合成する。

    uv run python -m tachie_studio.tachie char/ずんだもん --list
    uv run python -m tachie_studio.tachie char/ずんだもん --eye 普通白目_カメラ目線 --mouth むふ \
        --other 右腕_基本 --other 左腕_基本 -o out/zundamon.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .compose import Expression, compose
from .loader import CATEGORIES, load_character


def _print_parts(character) -> None:
    for category in CATEGORIES:
        parts = character.parts.get(category)
        if parts is None:
            continue
        print(f"[{category}]")
        for name in sorted(parts):
            part = parts[name]
            notes = []
            if part.animated:
                notes.append(f"連番{len(part.frames) - 1}枚")
            if part.has_vowels:
                notes.append("母音:" + "".join(sorted(part.vowels)))
            if part.opacity != 1.0:
                notes.append(f"opacity={part.opacity}")
            if part.blend:
                notes.append(f"blend={part.blend}")
            print(f"  {name}" + (f"  ({', '.join(notes)})" if notes else ""))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m tachie_studio.tachie", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="動く立ち絵の素材フォルダ")
    p.add_argument("--list", action="store_true", help="パーツ一覧を表示する")
    p.add_argument("--eyebrow", help="眉")
    p.add_argument("--eye", help="目")
    p.add_argument("--mouth", help="口")
    p.add_argument("--hair", help="髪")
    p.add_argument("--complexion", help="顔色")
    p.add_argument("--body", help="体")
    p.add_argument("--other", action="append", default=[], help="他（手前から最大3つ、複数回指定）")
    p.add_argument("--back", action="append", default=[], help="後（手前から最大3つ、複数回指定）")
    p.add_argument("--eye-frame", type=int, help="目の連番（0 = 閉じ、省略 = 開き）")
    p.add_argument("--mouth-frame", type=int, help="口の連番（0 = 閉じ、省略 = 開き）")
    p.add_argument("--vowel", choices=list("aiueo"), help="あいうえお口パクの母音")
    p.add_argument("-o", "--output", type=Path, help="出力 PNG")
    args = p.parse_args(argv)

    character = load_character(args.folder)
    if args.list:
        _print_parts(character)
        return
    if args.output is None:
        p.error("-o/--output を指定してください（一覧は --list）")

    expr = Expression(
        eyebrow=args.eyebrow, eye=args.eye, mouth=args.mouth, hair=args.hair,
        complexion=args.complexion, body=args.body, others=args.other, backs=args.back,
        eye_frame=args.eye_frame, mouth_frame=args.mouth_frame, mouth_vowel=args.vowel,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    compose(character, expr).save(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
