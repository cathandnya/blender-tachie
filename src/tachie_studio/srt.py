"""字幕ファイル（SRT / WebVTT）を作る。YouTube などの字幕に使う。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    start: float  # 秒
    end: float
    text: str
    speaker: str = ""


def _time(seconds: float, sep: str) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _body(cue: Cue, with_speaker: bool) -> str:
    text = "\n".join(line.strip() for line in cue.text.strip().splitlines() if line.strip())
    return f"{cue.speaker}: {text}" if with_speaker and cue.speaker else text


def _sorted(cues: list[Cue]) -> list[Cue]:
    return sorted((c for c in cues if c.end > c.start and c.text.strip()), key=lambda c: (c.start, c.end))


def to_srt(cues: list[Cue], with_speaker: bool = False) -> str:
    blocks = [f"{i}\n{_time(c.start, ',')} --> {_time(c.end, ',')}\n{_body(c, with_speaker)}\n"
              for i, c in enumerate(_sorted(cues), 1)]
    return "\n".join(blocks)


def _vtt_escape(text: str) -> str:
    # WebVTT の本文では & と < はタグ・文字参照になり、「-->」は書けない
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_vtt(cues: list[Cue], with_speaker: bool = False) -> str:
    blocks = [f"{_time(c.start, '.')} --> {_time(c.end, '.')}\n{_vtt_escape(_body(c, with_speaker))}\n"
              for c in _sorted(cues)]
    return "WEBVTT\n\n" + "\n".join(blocks)
