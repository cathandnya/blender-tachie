"""台本から、セリフごとの wav と口パク入り透明連番画像を書き出す。

    uv run python -m ymm.pipeline examples/script.txt -c examples/characters.toml -o out/demo

出力: lineNNN.wav、lineNNN/000000.png…、（--idle 指定時）idle_*/、manifest.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ..voicevox import DEFAULT_URL, VoicevoxClient
from .config import load_characters
from .render import render_script
from .script import parse_script


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ymm.pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("script", type=Path, help="台本（1 行に「キャラ名: セリフ」）")
    p.add_argument("-c", "--characters", type=Path, required=True, help="キャラクター設定（TOML）")
    p.add_argument("-o", "--output", type=Path, required=True, help="出力フォルダ")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--scale", type=float, default=1.0, help="立ち絵の拡大率（例: 0.5）")
    p.add_argument("--idle", type=float, default=0.0, metavar="SECONDS", help="待機用（まばたきのみ）の連番の長さ")
    p.add_argument("--url", default=DEFAULT_URL, help="VOICEVOX エンジンの URL")
    args = p.parse_args(argv)

    configs = load_characters(args.characters)
    lines = parse_script(args.script.read_text(encoding="utf-8"), set(configs))
    started = time.monotonic()
    manifest = render_script(lines, configs, args.output, client=VoicevoxClient(args.url), fps=args.fps,
                             scale=args.scale, idle_seconds=args.idle, progress=print)
    total = sum(c["duration"] for c in manifest["clips"] if c["kind"] == "line")
    print(f"{len(lines)} セリフ / {total:.1f} 秒 / {time.monotonic() - started:.1f} 秒で書き出し → "
          f"{args.output / 'manifest.json'}")


if __name__ == "__main__":
    main()
