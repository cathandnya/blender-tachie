"""wav の読み込みと、フレームごとの音量（RMS）。numpy を使わない。"""

from __future__ import annotations

import array
import io
import math
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from .timing import frame_count


@dataclass
class Wav:
    samples: array.array  # モノラル、-1.0〜1.0
    rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.rate


def read_wav(source: str | Path | bytes) -> Wav:
    """16bit PCM の wav を読む。ステレオは平均してモノラルにする。"""
    f = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")
    with f, wave.open(f, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"16bit PCM 以外の wav には対応していません（{w.getsampwidth() * 8}bit）")
        channels, rate = w.getnchannels(), w.getframerate()
        raw = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        raw.byteswap()
    if channels > 1:
        mono = array.array("f", (sum(raw[i:i + channels]) / channels / 32768.0
                                 for i in range(0, len(raw), channels)))
    else:
        mono = array.array("f", (s / 32768.0 for s in raw))
    return Wav(mono, rate)


def frame_rms(wav: Wav, fps: float, n_frames: int | None = None) -> list[float]:
    """動画の 1 フレームごとの RMS。"""
    if n_frames is None:
        n_frames = frame_count(wav.duration, fps)
    out = []
    for i in range(n_frames):
        a = int(i / fps * wav.rate)
        b = min(int((i + 1) / fps * wav.rate), len(wav.samples))
        chunk = wav.samples[a:b]
        out.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)) if chunk else 0.0)
    return out
