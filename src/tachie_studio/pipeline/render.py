"""セリフ 1 つ分の wav と透明連番画像を作る。

動くのは目と口だけなので、「目の画像 × 口の状態」の組み合わせを 1 回ずつ合成して
キャッシュし、連番はそのハードリンク（できなければコピー）で並べる。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from PIL import Image

from ..lipsync import blink_track, frame_count, phonemes_from_query, read_wav, scale_to
from ..lipsync.mouth import CLOSED, volume_track, vowel_track, vowels_to_levels
from ..tachie import Character, Expression, Part, compose, load_character
from ..voicevox import VoicevoxClient
from .config import CharacterConfig
from .script import Line

# 口の状態: ("level", 0〜) か ("vowel", "a"〜"o")
MouthState = tuple[str, int | str]


def _slug(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


class FrameCache:
    """表情 1 つ分の「目 × 口」合成画像のキャッシュ（メモリとディスク）。"""

    def __init__(self, character: Character, expression: Expression, directory: Path, scale: float = 1.0):
        self.character = character
        self.expression = expression
        self.scale = scale
        key = json.dumps([str(character.path), asdict(expression), scale], ensure_ascii=False, sort_keys=True)
        self.directory = directory / _slug(key)
        self._paths: dict[tuple[int, MouthState], Path] = {}

    def get(self, eye_frame: int, mouth: MouthState) -> Path:
        key = (eye_frame, mouth)
        if key in self._paths:
            return self._paths[key]
        kind, value = mouth
        path = self.directory / f"e{eye_frame}_{kind[0]}{value}.png"
        if not path.exists():
            expr = replace(self.expression, eye_frame=eye_frame,
                           mouth_frame=value if kind == "level" else None,
                           mouth_vowel=value if kind == "vowel" else None)
            image = compose(self.character, expr)
            if self.scale != 1.0:
                w, h = image.size
                image = image.resize((max(1, round(w * self.scale)), max(1, round(h * self.scale))),
                                     Image.Resampling.LANCZOS)
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.png")
            image.save(tmp, compress_level=3)
            tmp.replace(path)
        self._paths[key] = path
        return path


@dataclass
class RenderedClip:
    """書き出した 1 本分（セリフ、または待機用）。パスは manifest の置き場所からの相対で持つ。"""

    kind: str  # "line" / "idle"
    character: str
    frames_dir: str
    frames: int
    fps: float
    duration: float
    index: int | None = None
    text: str | None = None
    speaker: int | None = None
    wav: str | None = None
    extra: dict = field(default_factory=dict)


def _mouth_part(character: Character, expr: Expression) -> Part | None:
    return character.part("口", expr.mouth) if expr.mouth else character.default("口")


def _eye_part(character: Character, expr: Expression) -> Part | None:
    return character.part("目", expr.eye) if expr.eye else character.default("目")


def resolve_mouth_mode(mode: str, mouth: Part | None) -> str:
    """"vowel" / "volume" / "static" のどれで口を動かすか。"""
    if mouth is None:
        return "static"
    if mode == "auto":
        return "vowel" if mouth.has_vowels else ("volume" if len(mouth.levels) > 1 else "static")
    if mode == "vowel" and not mouth.has_vowels and len(mouth.levels) <= 1:
        return "static"
    return mode


def _link_frames(paths: list[Path], frames_dir: Path) -> None:
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    for i, src in enumerate(paths):
        dst = frames_dir / f"{i:06d}.png"
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)


def _seed(*parts: object) -> int:
    return int(_slug(json.dumps(parts, ensure_ascii=False, default=str)), 16)


def render_line(client: VoicevoxClient, config: CharacterConfig, character: Character, line: Line,
                out_dir: Path, *, fps: float, cache: FrameCache, stem: str | None = None) -> RenderedClip:
    """`out_dir` に `<stem>.wav` と `<stem>/000000.png…` を書く。stem の既定は lineNNN。"""
    speaker = line.speaker if line.speaker is not None else config.speaker
    query = config.apply_voice(client.audio_query(line.text, speaker))
    query.update(line.voice)
    kana_auto = query.get("kana", "")
    # 自動の読みはユーザー辞書などで変わる。編集したときの読みと違えば抑揚の編集は使わない
    use_accent = line.accent_phrases is not None and (
        line.accent_kana is None or line.accent_kana == (line.kana or kana_auto))
    if use_accent:
        query["accent_phrases"] = line.accent_phrases
        if line.kana:
            query["kana"] = line.kana
    elif line.kana:
        query["accent_phrases"] = client.accent_phrases(line.kana, speaker, is_kana=True)
        query["kana"] = line.kana
    wav_bytes = client.synthesis(query, speaker)
    wav = read_wav(wav_bytes)
    n = frame_count(wav.duration, fps)

    expr = cache.expression
    mouth = _mouth_part(character, expr)
    eye = _eye_part(character, expr)
    mode = resolve_mouth_mode(config.mouth, mouth)

    if mode == "vowel":
        vowels = vowel_track(scale_to(phonemes_from_query(query), wav.duration), fps, n)
        if mouth.has_vowels:
            mouth_states: list[MouthState] = [("level", 0) if v is CLOSED else ("vowel", v) for v in vowels]
        else:
            mouth_states = [("level", lv) for lv in vowels_to_levels(vowels, len(mouth.levels))]
    elif mode == "volume":
        mouth_states = [("level", lv) for lv in volume_track(wav, fps, len(mouth.levels), n)]
    else:
        mouth_states = [("level", len(mouth.frames) - 1 if mouth else 0)] * n

    eyes = blink_track(n, fps, len(eye.frames) if eye else 1, seed=_seed(line.index, line.character, line.text))

    stem = stem or f"line{line.index:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{stem}.wav"
    wav_path.write_bytes(wav_bytes)
    _link_frames([cache.get(e, m) for e, m in zip(eyes, mouth_states)], out_dir / stem)

    extra = {"mouth_mode": mode, "kana": query.get("kana", ""), "kana_auto": kana_auto}
    if use_accent:
        extra["accent"] = True
    if line.expression:
        extra["expression"] = line.expression
    return RenderedClip(kind="line", index=line.index, character=line.character, text=line.text,
                        speaker=speaker, wav=wav_path.name, frames_dir=stem, frames=n, fps=fps,
                        duration=wav.duration, extra=extra)


def idle_frames(cache: FrameCache, frames: int, fps: float, seed: object = None) -> list[Path]:
    """喋っていない区間の各フレームの画像（まばたきのみ、口は閉じ）。キャッシュの画像を指す。"""
    eye = _eye_part(cache.character, cache.expression)
    eyes = blink_track(frames, fps, len(eye.frames) if eye else 1, seed=_seed("idle", seed))
    return [cache.get(e, ("level", 0)) for e in eyes]


def render_idle(config: CharacterConfig, character: Character, out_dir: Path, *, seconds: float, fps: float,
                cache: FrameCache) -> RenderedClip:
    """喋っていない区間用（まばたきのみ、口は閉じ）。"""
    n = frame_count(seconds, fps)
    stem = f"idle_{_slug(config.name)}"
    _link_frames(idle_frames(cache, n, fps, seed=config.name), out_dir / stem)
    return RenderedClip(kind="idle", character=config.name, frames_dir=stem, frames=n, fps=fps, duration=n / fps)


def render_script(lines: list[Line], configs: dict[str, CharacterConfig], out_dir: Path, *,
                  client: VoicevoxClient | None = None, fps: float = 30.0, scale: float = 1.0,
                  idle_seconds: float = 0.0, progress: Callable[[str], None] | None = None) -> dict:
    """台本全体を書き出し、manifest.json を書いてその内容を返す。"""
    client = client or VoicevoxClient()
    out_dir.mkdir(parents=True, exist_ok=True)
    characters: dict[str, Character] = {}
    caches: dict[tuple[str, str | None], FrameCache] = {}

    def setup(name: str, preset: str | None = None) -> tuple[Character, FrameCache]:
        if name not in characters:
            characters[name] = load_character(configs[name].folder)
        if (name, preset) not in caches:
            caches[name, preset] = FrameCache(characters[name], configs[name].expression_for(preset),
                                              out_dir / "cache", scale)
        return characters[name], caches[name, preset]

    clips: list[RenderedClip] = []
    for line in lines:
        if progress:
            progress(f"[{line.index}/{len(lines)}] {line.character}: {line.text}")
        character, cache = setup(line.character, line.expression)
        clips.append(render_line(client, configs[line.character], character, line, out_dir, fps=fps, cache=cache))

    if idle_seconds > 0:
        for name in dict.fromkeys(line.character for line in lines):
            character, cache = setup(name)
            clips.append(render_idle(configs[name], character, out_dir, seconds=idle_seconds, fps=fps, cache=cache))

    first = next(iter(caches.values()), None)
    size = Image.open(next(first.directory.glob("*.png"))).size if first else None
    manifest = {
        "version": 1,
        "fps": fps,
        "size": list(size) if size else None,
        "characters": {name: {"folder": str(configs[name].folder), "speaker": configs[name].speaker}
                       for name in characters},
        "clips": [asdict(c) for c in clips],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest
