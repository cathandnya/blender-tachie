"""台本から、セリフごとの wav と口パク入り透明連番画像を書き出す。

    uv run python -m tachie_studio.pipeline examples/script.txt -c examples/characters.toml -o out/demo

出力: lineNNN.wav、lineNNN/000000.png…、（--idle 指定時）idle_*/、manifest.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ..srt import Cue, to_srt
from ..voicevox import DEFAULT_URL, VoicevoxClient
from .config import load_characters
from .render import render_script
from .script import load_script


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m tachie_studio.pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("script", type=Path, help="台本（1 行に「キャラ名: セリフ」）")
    p.add_argument("-c", "--characters", type=Path, required=True, help="キャラクター設定（TOML）")
    p.add_argument("-o", "--output", type=Path, required=True, help="出力フォルダ")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--scale", type=float, default=1.0, help="立ち絵の拡大率（例: 0.5）")
    p.add_argument("--idle", type=float, default=0.0, metavar="SECONDS", help="待機用（まばたきのみ）の連番の長さ")
    p.add_argument("--url", default=DEFAULT_URL, help="VOICEVOX エンジンの URL")
    p.add_argument("--srt", action="store_true",
                   help="字幕を subtitles.srt にも書き出す（セリフを順に、--gap 秒あけて並べた時刻）")
    p.add_argument("--gap", type=float, default=0.0, metavar="SECONDS", help="--srt で並べるときのセリフの間隔")
    args = p.parse_args(argv)

    configs = load_characters(args.characters)
    # .csv / .tsv は YMM4 の台本の形。BOM 付き・Shift_JIS でも読める。表情プリセット名も書き出し前に検査する
    lines = load_script(args.script, configs)
    started = time.monotonic()
    manifest = render_script(lines, configs, args.output, client=VoicevoxClient(args.url), fps=args.fps,
                             scale=args.scale, idle_seconds=args.idle, progress=print)
    total = sum(c["duration"] for c in manifest["clips"] if c["kind"] == "line")
    print(f"{len(lines)} セリフ / {total:.1f} 秒 / {time.monotonic() - started:.1f} 秒で書き出し → "
          f"{args.output / 'manifest.json'}")
    if args.srt:
        path = args.output / "subtitles.srt"
        path.write_text(to_srt(manifest_cues(manifest, args.gap)), encoding="utf-8")
        print(f"字幕 → {path}")


def manifest_cues(manifest: dict, gap: float = 0.0) -> list[Cue]:
    """manifest のセリフを順に `gap` 秒あけて並べたときの字幕（余韻も含めたセリフの長さ）。"""
    cues, t = [], 0.0
    for clip in manifest["clips"]:
        if clip["kind"] != "line":
            continue
        length = clip["frames"] / clip["fps"]
        cues.append(Cue(t, t + length, clip["text"], clip["character"]))
        t += length + gap
    return cues


if __name__ == "__main__":
    main()
