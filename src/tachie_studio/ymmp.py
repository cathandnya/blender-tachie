"""YMM4 のプロジェクト（.ymmp）を読む。

UTF-8（BOM 付き）の JSON。トップの `Characters` にキャラクター、`Timelines`（古い版は `Timeline`）の `Items` に
アイテムが並ぶ。アイテムの種類は `$type`（.NET の型名）で、時刻はフレーム（`Frame` 開始、`Length` 長さ、
`VideoInfo.FPS`）、`Layer` はトラック（大きいほど手前）。数値の多くはアニメーションできる形
`{"Values": [{"Value": x}, …], "AnimationType": "なし"}` で、ここでは最初の値を使う。

立ち絵のパーツは素材の画像の絶対パス（Windows）で持つので、ファイル名（拡張子なし）をパーツ名にする。
`Pronounce.AudioQuery` は VOICEVOX の audio_query そのもので、accent_phrases を抑揚の編集として使えば
同じ声に合成し直せる（`VoiceCache` の音声は使わない）。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import ntpath
import re
import unicodedata
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# FaceParameter / ItemParameter のキー → 素材のフォルダ名（他・後はスロット 1〜3）
PART_KEYS = {"Eyebrow": "眉", "Eye": "目", "Mouth": "口", "Hair": "髪", "Complexion": "顔色", "Body": "体"}
SLOT_KEYS = {"Etc": "他", "Back": "後"}
SLOT_COUNT = 3


def _type(item: dict) -> str:
    """`$type` の短い名前（"YukkuriMovieMaker.Project.Items.VoiceItem, …" → "VoiceItem"）。"""
    return str(item.get("$type", "")).split(",")[0].split(".")[-1]


def value(v: Any, default: float = 0.0) -> float:
    """アニメーションできる数値の最初の値（ただの数値ならそのまま）。"""
    if isinstance(v, dict):
        values = v.get("Values") or []
        return float(values[0].get("Value", default)) if values else default
    return float(v) if isinstance(v, (int, float)) else default


def animated(v: Any) -> bool:
    return isinstance(v, dict) and v.get("AnimationType", "なし") != "なし" and len(v.get("Values") or []) > 1


def seconds(text: str | None) -> float:
    """"hh:mm:ss.fffffff" → 秒。"""
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)", text or "")
    if not m:
        return 0.0
    days, h, mi, s = m.groups()
    return int(days or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(s)


def color(text: str | None, default=(1.0, 1.0, 1.0, 1.0)) -> tuple[float, float, float, float]:
    """"#AARRGGBB" → (r, g, b, a)。"""
    m = re.fullmatch(r"#([0-9A-Fa-f]{8})", text or "")
    if not m:
        return default
    a, r, g, b = (int(m[1][i:i + 2], 16) / 255 for i in range(0, 8, 2))
    return (r, g, b, a)


def part_name(path: str | None) -> str:
    """素材の画像のパス → パーツ名（ファイル名、拡張子なし、NFC）。"""
    if not path:
        return ""
    name = ntpath.basename(path)
    if name.lower().endswith(".png"):
        name = name[:-4]
    return unicodedata.normalize("NFC", name)


def parts_of(param: dict | None, full: bool) -> dict[str, Any]:
    """FaceParameter / ItemParameter → {フォルダ名: パーツ名、他・後はスロット順のリスト（"" は空き）}。

    `full`（ItemParameter = 基本の表情）なら空のパーツも "" で入れる。表情（FaceParameter）は変えるパーツだけ。
    """
    param = param or {}
    parts: dict[str, Any] = {}
    for key, category in PART_KEYS.items():
        name = part_name(param.get(key))
        if name or full:
            parts[category] = name
    for key, category in SLOT_KEYS.items():
        slots = [part_name(param.get(f"{key}{i}")) for i in range(1, SLOT_COUNT + 1)]
        while slots and not slots[-1]:
            slots.pop()
        if slots or full:
            parts[category] = slots
    return parts


def overlay(base: dict[str, Any], face: dict[str, Any]) -> dict[str, Any]:
    """基本の表情に表情（変えるパーツだけ）を重ねる。他・後はスロットごと。"""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in base.items()}
    for category, v in face.items():
        if isinstance(v, list):
            slots = list(out.get(category) or [])
            for i, name in enumerate(v):
                if name:
                    slots += [""] * (i + 1 - len(slots))
                    slots[i] = name
            out[category] = slots
        elif v:
            out[category] = v
    return out


def difference(base: dict[str, Any], parts: dict[str, Any]) -> dict[str, Any]:
    """基本の表情から変わったパーツだけ（表情プリセットの形。他・後は変わらないスロットを "" にする）。"""
    out: dict[str, Any] = {}
    for category, v in parts.items():
        b = base.get(category)
        if isinstance(v, list):
            b = list(b or [])
            slots = [name if name != (b[i] if i < len(b) else "") else "" for i, name in enumerate(v)]
            while slots and not slots[-1]:
                slots.pop()
            if slots:
                out[category] = slots
        elif v != (b or ""):
            out[category] = v
    return out


def find_by_tail(original: str, start: Path, levels: int = 3, directory: bool = False) -> Path | None:
    """YMM4 の元のパス（Windows）の後ろの部分を、`start` とその上 `levels` 階層のフォルダの下で探す。
    `C:\\ymm4\\sozai\\char\\ずんだもん` を、Windows と同じ並びで持ってきた `~/YouTube/sozai/char/ずんだもん` に見つける。
    近いフォルダ・長く一致するものが優先。ファイルは名前だけの一致は見ない（よくある名前を上の階層で拾わないため。
    .ymmp のフォルダの同じ名前は `MediaFinder` が別に探す）。"""
    if not original:
        return None
    parts = [p for p in ntpath.normpath(original).split("\\") if p]
    if ntpath.splitdrive(original)[0]:  # ドライブ・\\サーバ\共有 は除く
        parts = parts[1:] if ":" in parts[0] else parts[2:]
    if not parts:
        return None
    base = Path(start)
    for _ in range(levels + 1):
        for k in range(len(parts), 0 if directory else 1, -1):
            candidate = base.joinpath(*parts[-k:])
            if candidate.is_dir() if directory else candidate.is_file():
                return candidate
        if base.parent == base:
            break
        base = base.parent
    return None


@dataclass
class Character:
    name: str
    folder: str = ""  # 素材フォルダ（YMM4 のパス、Windows）
    style_id: int | None = None  # VOICEVOX のスタイル（VOICEVOX 以外の声は None）
    voice_api: str = ""
    voice: dict[str, float] = field(default_factory=dict)  # audio_query のキー → 値
    volume: float = 50.0  # YMM4 の音量
    pan: float = 0.0
    after: float = 0.0  # AdditionalTime（セリフ後の余韻、秒）
    fade_in: float = 0.0
    fade_out: float = 0.0
    color: tuple = (1.0, 1.0, 1.0, 1.0)
    font: str = ""
    font_size: float = 0.0
    font_color: tuple = (1.0, 1.0, 1.0, 1.0)
    outline: bool = False
    outline_color: tuple = (0.0, 0.0, 0.0, 1.0)
    subtitle_y: float = 0.0  # ピクセル、画面中央から下向き
    subtitle_visible: bool = True
    has_tachie: bool = False
    tachie_kind: str = ""
    parts: dict[str, Any] = field(default_factory=dict)  # 基本の表情（TachieDefaultItemParameter）

    @property
    def folder_name(self) -> str:
        return unicodedata.normalize("NFC", ntpath.basename(self.folder.rstrip("\\/"))) if self.folder else ""


@dataclass
class Voice:
    """ボイスアイテム（セリフ）。"""

    character: str
    text: str
    frame: int
    length: int
    layer: int
    kana: str = ""
    accent: list | None = None  # AudioQuery の accent_phrases
    style_id: int | None = None
    voice: dict[str, float] = field(default_factory=dict)
    voice_seconds: float = 0.0
    volume: float = 50.0
    pan: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    face: dict[str, Any] = field(default_factory=dict)  # セリフ中の表情（変えるパーツだけ）
    subtitle: str = "UseCharacterSetting"  # JimakuVisibility
    hidden: bool = False
    cache: str = field(default="", repr=False)  # VoiceCache（YMM4 が合成した音声。Brotli で圧縮した WAV の base64）


# VoiceCache の WAV の長さが VoiceLength とこれ以上違えば、別の音声とみなして使わない（秒）
CACHE_TOLERANCE = 0.05


def voice_wav(voice: Voice) -> bytes | None:
    """YMM4 が .ymmp に保存した音声（VoiceCache）を WAV にする。無い・読めない・長さが合わなければ None
    （VOICEVOX で合成し直す）。Brotli の圧縮で、YMM4 は終わりの印を書かないので、流し込みで展開する。"""
    if not voice.cache:
        return None
    try:
        import brotli
    except ImportError:  # Brotli の wheel が無い OS（Windows arm64）
        return None
    try:
        dec = brotli.Decompressor()
        chunks = [dec.process(base64.b64decode(voice.cache))]
        while chunk := dec.process(b""):  # 1 回の呼び出しで出すのは 32KB ほどまでなので、出し切るまで
            chunks.append(chunk)
        wav = b"".join(chunks)
    except (brotli.error, binascii.Error, ValueError):
        return None
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return None
    try:  # 長さはヘッダーから（全サンプルを読むと、読み込みの準備で画面が固まる）
        with wave.open(io.BytesIO(wav), "rb") as w:
            if w.getsampwidth() != 2:  # 16bit PCM 以外は口パクの計算で読めない
                return None
            duration = w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):  # 壊れた WAV は使わない
        return None
    if voice.voice_seconds and abs(duration - voice.voice_seconds) > CACHE_TOLERANCE:
        return None
    return wav


@dataclass
class Span:
    """立ち絵アイテム（parts = その区間の基本の表情）と表情アイテム（parts = 変えるパーツだけ）。"""

    character: str
    frame: int
    length: int
    layer: int
    parts: dict[str, Any] = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0
    zoom: float = 100.0
    effects: list[str] = field(default_factory=list)
    hidden: bool = False


# 画像・動画・音声・テキストのアイテム → Media.kind
MEDIA_KINDS = {"ImageItem": "image", "VideoItem": "video", "AudioItem": "audio", "TextItem": "text"}
# アニメーション（始め → 終わり）を写す値。Media の属性名
MEDIA_ANIMATED = {"X": "x", "Y": "y", "Zoom": "zoom", "Opacity": "opacity", "Rotation": "rotation", "Volume": "volume"}
LINEAR = "直線移動"


@dataclass
class Media:
    """画像・動画・音声・テキストのアイテム。X / Y は画面中央からのピクセル（下が正）で、画像・動画は中心、
    テキストは BasePoint の点。Zoom は %（100 で素材の 1 ピクセルが出力の 1 ピクセル）、Rotation は度（時計回り）。"""

    kind: str  # image / video / audio / text（テンプレートでは color も）
    frame: int
    length: int
    layer: int
    file: str = ""  # YMM4 のパス（Windows）
    x: float = 0.0
    y: float = 0.0
    zoom: float = 100.0
    rotation: float = 0.0
    opacity: float = 100.0  # %
    volume: float = 50.0  # YMM4 の音量（50 が等倍）
    rate: float = 100.0  # 再生速度（%）
    offset: float = 0.0  # 素材の頭からの再生開始（秒）
    fade_in: float = 0.0
    fade_out: float = 0.0
    text: str = ""
    font: str = ""
    font_size: float = 34.0
    font_color: tuple = (1.0, 1.0, 1.0, 1.0)
    outline: bool = False
    outline_color: tuple = (0.0, 0.0, 0.0, 1.0)
    bold: bool = False
    base_point: str = "LeftTop"
    color: tuple = (0.0, 0.0, 0.0, 1.0)  # カラー（kind == "color"、アドオンのテンプレートだけ）
    anim: dict[str, tuple[float, float, str]] = field(default_factory=dict)  # 属性 → (始め, 終わり, 種類)
    effects: list[str] = field(default_factory=list)
    hidden: bool = False


@dataclass
class Project:
    path: Path
    fps: float = 60.0
    width: int = 1920
    height: int = 1080
    length: int = 0
    characters: dict[str, Character] = field(default_factory=dict)
    voices: list[Voice] = field(default_factory=list)
    tachies: list[Span] = field(default_factory=list)
    faces: list[Span] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)  # 画像・動画・音声・テキスト
    others: list[dict] = field(default_factory=list)  # トランジション・エフェクト・図形など（そのままの dict）
    unsupported: Counter = field(default_factory=Counter)  # 読み込まないもの → 数


VOICE_PARAMS = (  # VOICEVOXVoiceParameter → audio_query（% は 1/100、Pitch は 100 が 0）
    ("Speed", "speedScale", lambda v: v / 100),
    ("Pitch", "pitchScale", lambda v: (v - 100) / 100),
    ("Intonation", "intonationScale", lambda v: v / 100),
    ("PrePhonemeLength", "prePhonemeLength", lambda v: v),
    ("PostPhonemeLength", "postPhonemeLength", lambda v: v),
    ("PauseLengthScale", "pauseLengthScale", lambda v: v / 100),
)
QUERY_KEYS = ("speedScale", "pitchScale", "intonationScale", "volumeScale", "prePhonemeLength", "postPhonemeLength",
              "pauseLengthScale")


def voice_params(param: dict | None) -> tuple[int | None, dict[str, float]]:
    param = param or {}
    if "VOICEVOX" not in str(param.get("$type", "")):
        return None, {}
    voice = {key: round(conv(float(param[k])), 6) for k, key, conv in VOICE_PARAMS if param.get(k) is not None}
    style = param.get("StyleID")
    return (int(style) if style is not None else None), voice


def _subtitle_style(c: Character, d: dict) -> None:
    c.font = d.get("Font") or ""
    c.font_size = value(d.get("FontSize"), 0.0) * value(d.get("Zoom"), 100.0) / 100  # Zoom は字幕の拡大率
    c.font_color = color(d.get("FontColor"))
    c.outline = d.get("Style") == "Border"
    c.outline_color = color(d.get("StyleColor"), (0.0, 0.0, 0.0, 1.0))
    c.subtitle_y = value(d.get("Y"))


def _character(d: dict) -> Character:
    voice = d.get("Voice") or {}
    style, params = voice_params(d.get("VoiceParameter"))
    c = Character(name=d.get("Name") or "", voice_api=voice.get("API") or "", style_id=style, voice=params,
                  volume=value(d.get("Volume"), 50.0), pan=value(d.get("Pan")),
                  after=float(d.get("AdditionalTime") or 0.0), fade_in=float(d.get("VoiceFadeIn") or 0.0),
                  fade_out=float(d.get("VoiceFadeOut") or 0.0), color=color(d.get("Color")),
                  subtitle_visible=d.get("IsJimakuVisible", True))
    _subtitle_style(c, d)
    kind = str(d.get("TachieType") or "")
    c.tachie_kind = kind.split(",")[0].split(".")[-1]
    param = d.get("TachieCharacterParameter") or {}
    if "AnimationTachie" in kind:
        c.has_tachie = True
        c.folder = param.get("Directory") or ""
        c.parts = parts_of(d.get("TachieDefaultItemParameter"), full=True)
    return c


def _query(item: dict) -> dict | None:
    query = (item.get("Pronounce") or {}).get("AudioQuery")
    return query if isinstance(query, dict) and query.get("accent_phrases") else None


def _voice(item: dict) -> Voice:
    style, params = voice_params(item.get("VoiceParameter"))
    query = _query(item)
    if query:  # 合成に使った値（VoiceParameter から計算したものより確か）
        params.update({k: float(query[k]) for k in QUERY_KEYS if query.get(k) is not None})
    return Voice(
        character=item.get("CharacterName") or "", text=item.get("Serif") or "",
        frame=int(item.get("Frame", 0)), length=int(item.get("Length", 0)), layer=int(item.get("Layer", 0)),
        kana=item.get("Hatsuon") or "", accent=query["accent_phrases"] if query else None, style_id=style,
        voice=params, voice_seconds=seconds(item.get("VoiceLength")), volume=value(item.get("Volume"), 50.0),
        pan=value(item.get("Pan")), fade_in=float(item.get("VoiceFadeIn") or 0.0),
        fade_out=float(item.get("VoiceFadeOut") or 0.0), face=parts_of(item.get("TachieFaceParameter"), full=False),
        subtitle=item.get("JimakuVisibility") or "UseCharacterSetting", hidden=bool(item.get("IsHidden")),
        cache=item.get("VoiceCache") or "")


def _effects(item: dict, *keys: str) -> list[str]:
    return [_type(e) for k in keys for e in (item.get(k) or []) if e.get("IsEnabled", True)]


def _span(item: dict, parts: dict) -> Span:
    return Span(character=item.get("CharacterName") or "", frame=int(item.get("Frame", 0)),
                length=int(item.get("Length", 0)), layer=int(item.get("Layer", 0)), parts=parts,
                x=value(item.get("X")), y=value(item.get("Y")), zoom=value(item.get("Zoom"), 100.0),
                effects=_effects(item, "VideoEffects", "TachieFaceEffects"), hidden=bool(item.get("IsHidden")))


def _media(item: dict, kind: str, hidden_layers: set[int]) -> Media:
    m = Media(kind=kind, frame=int(item.get("Frame", 0)), length=int(item.get("Length", 0)),
              layer=int(item.get("Layer", 0)), file=item.get("FilePath") or "",
              x=value(item.get("X")), y=value(item.get("Y")), zoom=value(item.get("Zoom"), 100.0),
              rotation=value(item.get("Rotation")), opacity=value(item.get("Opacity"), 100.0),
              volume=value(item.get("Volume"), 50.0), rate=value(item.get("PlaybackRate"), 100.0) or 100.0,
              offset=seconds(item.get("ContentOffset")), fade_in=float(item.get("FadeIn") or 0.0),
              fade_out=float(item.get("FadeOut") or 0.0), effects=_effects(item, "VideoEffects", "AudioEffects"),
              hidden=bool(item.get("IsHidden")) or int(item.get("Layer", 0)) in hidden_layers)
    if kind == "text":
        m.text = (item.get("Text") or "").replace("\r\n", "\n").replace("\r", "\n")
        m.font = item.get("Font") or ""
        m.font_size = value(item.get("FontSize"), 34.0)
        m.font_color = color(item.get("FontColor"))
        m.outline = item.get("Style") == "Border"
        m.outline_color = color(item.get("StyleColor"), (0.0, 0.0, 0.0, 1.0))
        m.bold = bool(item.get("Bold"))
        m.base_point = item.get("BasePoint") or "LeftTop"
    for key, attr in MEDIA_ANIMATED.items():
        v = item.get(key)
        if animated(v):
            values = v["Values"]
            m.anim[attr] = (float(values[0].get("Value", 0.0)), float(values[-1].get("Value", 0.0)),
                            v.get("AnimationType", ""))
    return m


def front_layer(project: Project) -> int:
    """立ち絵アイテム（無ければボイス）の一番下のレイヤー。これより下のアイテムは立ち絵より奥に描く。"""
    layers = [s.layer for s in project.tachies if not s.hidden] or [v.layer for v in project.voices]
    return min(layers) if layers else 0


# 始め → 終わりの直線のアニメーションを写せる値（テキストの Zoom・Rotation はキーフレームを打たない）
MEDIA_KEYED = {"ImageItem": ("X", "Y", "Zoom", "Opacity", "Rotation"), "VideoItem": tuple(MEDIA_ANIMATED),
               "AudioItem": ("Volume",), "TextItem": ("X", "Y", "Opacity")}


def _note_unsupported(project: Project, kind: str, item: dict) -> None:
    for name, v in item.items():
        if animated(v):
            if name in MEDIA_KEYED.get(kind, ()) and len(v["Values"]) == 2 and (
                    v.get("AnimationType") == LINEAR):
                continue  # 始め → 終わりの直線はキーフレームで写す
            project.unsupported[f"{kind} の {name} のアニメーション"] += 1
    if (item.get("KeyFrames") or {}).get("Count"):
        project.unsupported[f"{kind} の中間点（キーフレーム）"] += 1


def load_project(path: str | Path) -> Project:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{path.name} を読めません: {e}") from None
    if not isinstance(data, dict) or not ("Timelines" in data or "Timeline" in data):
        raise ValueError(f"{path.name} は YMM4 のプロジェクトではありません")
    timelines = data.get("Timelines") or [data.get("Timeline") or {}]
    index = int(data.get("SelectedTimelineIndex") or 0)
    timeline = timelines[index] if 0 <= index < len(timelines) else timelines[0]
    info = timeline.get("VideoInfo") or {}
    project = Project(path=path, fps=float(info.get("FPS") or 60), width=int(info.get("Width") or 1920),
                      height=int(info.get("Height") or 1080), length=int(timeline.get("Length") or 0))
    if len(timelines) > 1:
        project.unsupported[f"選んでいるタイムライン以外（{len(timelines) - 1} 個）"] += 1
    for d in data.get("Characters") or []:
        c = _character(d)
        project.characters[c.name] = c
        if c.voice_api and c.voice_api != "voicevox":
            project.unsupported[f"VOICEVOX 以外の声（{c.name}: {c.voice_api}）"] += 1
        if d.get("TachieType") and not c.has_tachie:
            project.unsupported[f"動く立ち絵以外の立ち絵（{c.name}: {c.tachie_kind}）"] += 1
    hidden_layers = {int(d.get("Layer", -1)) for d in (timeline.get("LayerSettings") or {}).get("Items") or []
                     if d.get("IsHidden")}
    for item in timeline.get("Items") or []:
        kind = _type(item)
        if kind in MEDIA_KINDS:
            m = _media(item, MEDIA_KINDS[kind], hidden_layers)
            project.media.append(m)
            label = ITEM_LABELS[kind]
            for e in m.effects:
                project.unsupported[f"{label}のエフェクト {e}"] += 1
            if item.get("IsLooped"):
                project.unsupported[f"{label}のループ再生"] += 1
            if (item.get("Blend") or "Normal") != "Normal":
                project.unsupported[f"{label}の合成モード {item['Blend']}"] += 1
            if kind == "TextItem" and item.get("Decorations"):
                project.unsupported["テキストの装飾（Decorations）"] += 1
        elif kind == "VoiceItem":
            v = _voice(item)
            project.voices.append(v)
            if v.subtitle == "Custom":
                project.unsupported["字幕をボイスごとに設定したもの（キャラの設定で出す）"] += 1
            if item.get("Decorations"):
                project.unsupported["字幕の装飾（Decorations）"] += 1
            for e in _effects(item, "TachieFaceEffects"):
                project.unsupported[f"表情のエフェクト {e}"] += 1
        elif kind == "TachieItem":
            span = _span(item, parts_of(item.get("TachieItemParameter"), full=True))
            project.tachies.append(span)
            for e in span.effects:
                project.unsupported[f"立ち絵のエフェクト {e}"] += 1
        elif kind == "TachieFaceItem":
            span = _span(item, parts_of(item.get("TachieFaceParameter"), full=False))
            project.faces.append(span)
            for e in span.effects:
                project.unsupported[f"表情のエフェクト {e}"] += 1
        else:
            project.others.append(item)
            continue
        _note_unsupported(project, kind, item)
    project.voices.sort(key=lambda v: (v.frame, v.layer))
    project.tachies.sort(key=lambda s: (s.frame, s.layer))
    project.faces.sort(key=lambda s: (s.frame, s.layer))
    project.media.sort(key=lambda m: (m.frame, m.layer))
    return project


ITEM_LABELS = {"TachieItem": "立ち絵アイテム", "VideoItem": "動画", "ImageItem": "画像", "AudioItem": "音声", "TextItem": "テキスト",
               "TransitionItem": "トランジション", "EffectItem": "エフェクト", "ShapeItem": "図形"}


def other_items_summary(project: Project) -> Counter:
    """キャラ・セリフ以外のアイテムの数（種類の表示名 → 数）。"""
    return Counter(ITEM_LABELS.get(_type(i), _type(i)) for i in project.others)


def preset_name(diff: dict[str, Any], taken: set[str]) -> str:
    """表情の差分から表情プリセットの名前を作る（変えたパーツ名を「・」でつなぐ）。"""
    names = [n for v in diff.values() for n in (v if isinstance(v, list) else [v]) if n]
    base = "・".join(names)[:40] or "基本"
    name, i = base, 2
    while name in taken:
        name, i = f"{base} {i}", i + 1
    return name
