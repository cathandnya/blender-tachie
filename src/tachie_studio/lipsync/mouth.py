"""フレームごとの口の状態。

- あいうえお式: `vowel_track` が "a"〜"o" か CLOSED（閉じ）を返す。
- 音量式: `volume_track` が口の開き具合 0（閉じ）〜 levels-1（最大）を返す。
"""

from __future__ import annotations

import math

from .timing import Phoneme, frame_count
from .wav import Wav, frame_rms

CLOSED = None

# 子音のうち唇を閉じるもの。これらの子音の間は口を閉じる
BILABIALS = {"m", "my", "b", "by", "p", "py"}


def _phoneme_mouth(p: Phoneme, next_vowel: str | None) -> str | None:
    if p.is_vowel:
        v = p.phoneme.lower()  # 無声母音 A I U E O も同じ口の形
        return v if v in "aiueo" and len(v) == 1 else CLOSED  # N, cl, pau は閉じ
    if p.phoneme in BILABIALS:
        return CLOSED
    return next_vowel  # 他の子音は続く母音の形を先取りする


def vowel_track(phonemes: list[Phoneme], fps: float, n_frames: int | None = None) -> list[str | None]:
    """各フレームの中央時刻での口の形。"""
    if n_frames is None:
        n_frames = frame_count(phonemes[-1].end if phonemes else 0.0, fps)

    shapes: list[str | None] = []
    for i, p in enumerate(phonemes):
        nxt = phonemes[i + 1] if i + 1 < len(phonemes) else None
        next_vowel = _phoneme_mouth(nxt, None) if nxt is not None and nxt.is_vowel else None
        shapes.append(_phoneme_mouth(p, next_vowel))

    out: list[str | None] = []
    j = 0
    for f in range(n_frames):
        t = (f + 0.5) / fps
        while j < len(phonemes) - 1 and phonemes[j].end <= t:
            j += 1
        out.append(shapes[j] if phonemes and phonemes[j].start <= t < phonemes[j].end else CLOSED)
    return out


# 母音式の素材が無いとき、母音を音量式の開き具合に置き換える割合（1.0 = 最大）
_VOWEL_OPENNESS = {"a": 1.0, "o": 0.8, "e": 0.7, "i": 0.4, "u": 0.4}


def vowels_to_levels(track: list[str | None], levels: int) -> list[int]:
    """あいうえお式の列を音量式の開き具合に変換する（母音画像の無い素材用）。"""
    if levels <= 1:
        return [0] * len(track)
    return [0 if v is CLOSED else max(1, round(_VOWEL_OPENNESS[v] * (levels - 1))) for v in track]


def volume_track(wav: Wav, fps: float, levels: int, n_frames: int | None = None,
                 silence: float = 0.02, peak_percentile: float = 0.9) -> list[int]:
    """音量から各フレームの開き具合 0〜levels-1 を決める（`levels_from_rms`）。"""
    return levels_from_rms(frame_rms(wav, fps, n_frames), levels, silence, peak_percentile)


def levels_from_rms(rms: list[float], levels: int, silence: float = 0.02, peak_percentile: float = 0.9) -> list[int]:
    """フレームごとの RMS から開き具合 0〜levels-1 を決める。

    RMS を発話部分の `peak_percentile` 分位点で正規化し、`silence` 未満を閉じ、
    残りを 1〜levels-1 に等分する。
    """
    if levels <= 1:
        return [0] * len(rms)
    voiced = sorted(r for r in rms if r >= silence)
    if not voiced:
        return [0] * len(rms)
    peak = voiced[min(len(voiced) - 1, int(len(voiced) * peak_percentile))]
    out = []
    for r in rms:
        if r < silence:
            out.append(0)
        else:
            x = min(r / peak, 1.0)
            out.append(max(1, min(levels - 1, math.ceil(x * (levels - 1)))))
    return out
