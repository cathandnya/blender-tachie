"""VOICEVOX の audio_query から音素の時刻を積む。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

# VOICEVOX エンジンは音素の長さを 24000Hz / 256 = 93.75fps のフレーム数に丸めて合成する
ENGINE_FRAME_RATE = 93.75
# 疑問文の語尾上げで、合成時に足される母音の長さ
UPSPEAK_LENGTH = 0.15


@dataclass(frozen=True)
class Phoneme:
    """1 つの音素。`phoneme` は VOICEVOX の表記（a i u e o / 無声の A I U E O / N / cl / pau、子音は k s など）。"""

    phoneme: str
    start: float
    end: float
    is_vowel: bool  # モーラの母音部分（N, cl, pau も含む）なら True、子音なら False

    @property
    def duration(self) -> float:
        return self.end - self.start


def phonemes_from_query(query: dict[str, Any], *, upspeak: bool = True,
                        frame_rate: float | None = ENGINE_FRAME_RATE) -> list[Phoneme]:
    """audio_query の内容から、音声先頭を 0 秒とした音素列を返す。

    前後の無音（prePhonemeLength / postPhonemeLength）は "pau" として含める。
    ポーズは pauseLength（指定時はその長さに置き換え）と pauseLengthScale を反映し、
    全体を speedScale で割り、音素ごとに `frame_rate` のフレームに丸める（VOICEVOX エンジンと同じ）。
    `upspeak` は /synthesis の enable_interrogative_upspeak（既定 True）に合わせる。
    """
    speed = float(query.get("speedScale") or 1.0)
    pause_length = query.get("pauseLength")
    pause_scale = float(query.get("pauseLengthScale") or 1.0)

    segments: list[tuple[str, float, bool]] = [("pau", float(query.get("prePhonemeLength") or 0.0), True)]
    for ap in query["accent_phrases"]:
        for mora in ap["moras"]:
            if mora.get("consonant"):
                segments.append((mora["consonant"], float(mora.get("consonant_length") or 0.0), False))
            segments.append((mora["vowel"], float(mora.get("vowel_length") or 0.0), True))
        if upspeak and ap.get("is_interrogative") and ap["moras"]:
            segments.append((ap["moras"][-1]["vowel"], UPSPEAK_LENGTH, True))
        pause = ap.get("pause_mora")
        if pause:
            length = float(pause_length) if pause_length is not None else float(pause.get("vowel_length") or 0.0)
            segments.append(("pau", length * pause_scale, True))
    segments.append(("pau", float(query.get("postPhonemeLength") or 0.0), True))

    phonemes = []
    t = 0.0
    for phoneme, length, is_vowel in segments:
        length /= speed
        if frame_rate:
            length = round(length * frame_rate) / frame_rate
        phonemes.append(Phoneme(phoneme, t, t + length, is_vowel))
        t += length
    return phonemes


def query_duration(query: dict[str, Any], **kwargs: Any) -> float:
    return phonemes_from_query(query, **kwargs)[-1].end


def frame_count(duration: float, fps: float) -> int:
    """`duration` 秒を覆う動画フレーム数（浮動小数の誤差で 1 フレーム増えないようにする）。"""
    return math.ceil(duration * fps - 1e-6)


def scale_to(phonemes: list[Phoneme], duration: float) -> list[Phoneme]:
    """実際の wav の長さに合わせて時刻を伸縮する（長文でのずれ防止）。"""
    total = phonemes[-1].end if phonemes else 0.0
    if total <= 0:
        return list(phonemes)
    k = duration / total
    return [replace(p, start=p.start * k, end=p.end * k) for p in phonemes]
