"""セリフから wav と、フレームごとの口・目の状態（JSON）を出す。

    uv run python -m ymm.lipsync "こんにちは、ずんだもんなのだ。" --speaker 3 -o out/line
    → out/line.wav と out/line.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..voicevox import DEFAULT_URL, VoicevoxClient
from .blink import blink_track
from .mouth import volume_track, vowel_track
from .timing import frame_count, phonemes_from_query, scale_to
from .wav import read_wav


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ymm.lipsync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="?", help="セリフ")
    p.add_argument("--speaker", type=int, default=3, help="VOICEVOX のスタイル ID（既定 3 = ずんだもん ノーマル）")
    p.add_argument("--speakers", action="store_true", help="スタイル ID の一覧を表示する")
    p.add_argument("--speed", type=float, help="話速 (speedScale)")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--levels", type=int, default=3, help="音量口パクの段階数（口パーツの連番の枚数）")
    p.add_argument("--eye-images", type=int, default=3, help="目パーツの画像の枚数（.0〜.N と数字なし）")
    p.add_argument("--seed", type=int, help="まばたきの乱数シード")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("-o", "--output", type=Path, help="出力先（拡張子なし）")
    args = p.parse_args(argv)

    client = VoicevoxClient(args.url)
    if args.speakers:
        for style_id, name in client.styles():
            print(f"{style_id:4d}  {name}")
        return
    if not args.text or args.output is None:
        p.error("セリフと -o/--output を指定してください（一覧は --speakers）")

    query = client.audio_query(args.text, args.speaker)
    if args.speed is not None:
        query["speedScale"] = args.speed
    wav_bytes = client.synthesis(query, args.speaker)
    wav = read_wav(wav_bytes)

    phonemes = scale_to(phonemes_from_query(query), wav.duration)
    n_frames = frame_count(wav.duration, args.fps)
    result = {
        "text": args.text,
        "speaker": args.speaker,
        "fps": args.fps,
        "duration": wav.duration,
        "frames": n_frames,
        "phonemes": [[ph.phoneme, round(ph.start, 4), round(ph.end, 4)] for ph in phonemes],
        "vowel": vowel_track(phonemes, args.fps, n_frames),
        "volume": volume_track(wav, args.fps, args.levels, n_frames),
        "blink": blink_track(n_frames, args.fps, args.eye_images, seed=args.seed),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".wav").write_bytes(wav_bytes)
    args.output.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(args.output.with_suffix(".wav"))
    print(args.output.with_suffix(".json"))


if __name__ == "__main__":
    main()
