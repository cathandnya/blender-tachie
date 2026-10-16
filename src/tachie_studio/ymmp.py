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
from dataclasses import dataclass, field, replace
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
    line_height: float = 100.0  # 字幕の行間（%、100 が通常）
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
    # 字幕の位置（JimakuVisibility が Custom のときに使う。画面中央からのピクセル、下が正。BasePoint は CenterBottom）
    sub_x: float = 0.0
    sub_y: float = 0.0
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
    inverted: bool = False  # 左右反転（IsInverted）


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
    # テキストの背景（YMM4 の映像エフェクト「背景塗りつぶし」）。色は不透明度を掛けた RGBA、余白と角の丸みはピクセル
    background: tuple | None = None
    background_margin: float = 0.0  # 上下左右の余白の平均（Blender の帯の余白は 1 つだけ）
    background_round: float = 0.0
    line_spacing: float = 1.0  # テキストの行間（YMM4 の LineHeight2 % の 1/100。1 が通常）
    # 画面外から移動して登場・退場（YMM4 の映像エフェクト）。{"direction": Left/Right/Top/Bottom, "in", "out",
    # "seconds", "easing": Expo など, "mode": In/Out/InOut}
    slide: dict | None = None
    # 画像の縁取り（YMM4 の映像エフェクト）。{"thickness": 元の画像のピクセル, "color": 不透明度を掛けた RGBA,
    # "blur", "angular", "only": 縁だけ}
    stroke: dict | None = None
    # モザイク（kind == "mosaic"。YMM4 のエフェクトアイテムの四角い範囲のモザイク）。{"size": ブロックの大きさ,
    # "width", "height": 範囲（ピクセル）, "invert": 範囲の外にかける}。X / Y は範囲の中心
    mosaic: dict | None = None
    # 場面転換（YMM4 のトランジションアイテム）の残像。前の場面の最後のコマを止めて新しい場面の上に置き、動かす。
    # {"type": fade / push, "easing", "mode", "angle": 度, "after": 新しい場面の Media（無ければ None）}
    transition: dict | None = None
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
    c.line_height = value(d.get("LineHeight2"), 100.0)


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
        sub_x=value(item.get("X")), sub_y=value(item.get("Y")), cache=item.get("VoiceCache") or "")


def _effects(item: dict, *keys: str) -> list[str]:
    return [_type(e) for k in keys for e in (item.get(k) or []) if e.get("IsEnabled", True)]


def _span(item: dict, parts: dict) -> Span:
    return Span(character=item.get("CharacterName") or "", frame=int(item.get("Frame", 0)),
                length=int(item.get("Length", 0)), layer=int(item.get("Layer", 0)), parts=parts,
                x=value(item.get("X")), y=value(item.get("Y")), zoom=value(item.get("Zoom"), 100.0),
                effects=_effects(item, "VideoEffects", "TachieFaceEffects"), hidden=bool(item.get("IsHidden")),
                inverted=bool(item.get("IsInverted")))


def _slide(m: Media, item: dict) -> None:
    """映像エフェクト「画面外から移動して登場・退場」を Media.slide にする（写したものはエフェクトの一覧から外す）。"""
    for e in item.get("VideoEffects") or []:
        if _type(e) != "InOutMoveFromOutsideFrameEffect" or not e.get("IsEnabled", True):
            continue
        if not (e.get("IsInEffect") or e.get("IsOutEffect")):
            continue
        m.slide = {"direction": e.get("Value") or "Left", "in": bool(e.get("IsInEffect")),
                   "out": bool(e.get("IsOutEffect")), "seconds": float(e.get("EffectTimeSeconds") or 0.3),
                   "easing": e.get("EasingType") or "Linear", "mode": e.get("EasingMode") or "In"}
        m.effects.remove("InOutMoveFromOutsideFrameEffect")
        return


def _stroke(m: Media, item: dict) -> None:
    """画像の映像エフェクト「縁取り」（単色）を Media.stroke にする（写したものはエフェクトの一覧から外す）。
    縁の位置のずらし・拡大・回転は写さない（使っていればエフェクトの一覧に残して報告する）。"""
    for e in item.get("VideoEffects") or []:
        if _type(e) != "OutlineEffect" or not e.get("IsEnabled", True):
            continue
        brush = (e.get("StrokeBrush") or {}).get("Parameter") or {}
        if "SolidColorBrush" not in str(brush.get("$type", "")):
            continue
        r, g, b, a = color(brush.get("Color"), (1.0, 1.0, 1.0, 1.0))
        thickness = value(e.get("StrokeThickness"), 10.0)
        m.effects.remove("OutlineEffect")
        if thickness <= 0:
            return  # 太さ 0 の縁取りは何も描かない
        m.stroke = {"thickness": thickness,
                    "color": (r, g, b, a * value(e.get("Opacity"), 100.0) / 100), "blur": value(e.get("Blur")),
                    "angular": bool(e.get("IsAngular")), "only": bool(e.get("IsOutlineOnly"))}
        if value(e.get("X")) or value(e.get("Y")) or value(e.get("Zoom"), 100.0) != 100.0 or value(e.get("Rotation")):
            m.effects.append("OutlineEffect の位置・拡大・回転")
        return


def _mosaic_item(item: dict, hidden_layers: set[int]) -> Media | None:
    """エフェクトアイテムの、四角い範囲の四角いモザイク → Media(kind="mosaic")。それ以外のエフェクトアイテムは None。"""
    effects = [e for e in item.get("VideoEffects") or [] if e.get("IsEnabled", True)]
    mosaic = next((e for e in effects if _type(e) == "MosaicEffect" and e.get("MosaicType") == "Rectangle"), None)
    shape = item.get("ShapeParameter") or {}
    if mosaic is None or "QuadrilateralShape" not in str(item.get("ShapeType2", "")) \
            or "RectangleShapeParameter" not in str(shape.get("$type", "")):
        return None
    if shape.get("SizeMode") == "SizeAspect":
        # AviUtl の図形と同じ: アスペクト比（-100〜100）が正なら幅を、負なら高さを縮める
        size, aspect = value(shape.get("Size"), 100.0), value(shape.get("AspectRate")) / 100
        width, height = size * (1 - max(0.0, aspect)), size * (1 + min(0.0, aspect))
    else:
        width, height = value(shape.get("Width"), 100.0), value(shape.get("Height"), 100.0)
    m = Media(kind="mosaic", frame=int(item.get("Frame", 0)), length=int(item.get("Length", 0)),
              layer=int(item.get("Layer", 0)), x=value(item.get("X")), y=value(item.get("Y")),
              zoom=value(item.get("Zoom"), 100.0), rotation=value(item.get("Rotation")),
              opacity=value(item.get("Opacity"), 100.0),
              fade_in=float(item.get("FadeIn") or 0.0), fade_out=float(item.get("FadeOut") or 0.0),
              hidden=bool(item.get("IsHidden")) or int(item.get("Layer", 0)) in hidden_layers,
              mosaic={"size": value((mosaic.get("MosaicParameter") or {}).get("Size"), 10.0), "width": width,
                      "height": height, "invert": bool(item.get("InvertMask"))})
    m.effects = [_type(e) for e in effects if e is not mosaic]
    if value(shape.get("Round")) or value(item.get("Blur")):
        m.effects.append("範囲の角の丸み・ぼかし")
    return m


# YMM4 のトランジション → Media.transition の type
TRANSITION_TYPES = {"FadeTransitionPlugin": "fade", "PushTransitionPlugin": "push"}


def _transitions(project: Project, hidden_layers: set[int]) -> None:
    """トランジションアイテムを、前の場面（区間の始まりで終わる下のレイヤーの画像・動画）の残像の Media にする。
    YMM4 のトランジションは新しい場面の始まりから置かれ、前の場面の最後のコマから新しい場面へ切り替える。"""
    rest = []
    for item in project.others:
        kind = TRANSITION_TYPES.get(_type({"$type": item.get("TransitionType", "")}))
        if (_type(item) != "TransitionItem" or kind is None or item.get("IsHidden")
                or int(item.get("Layer", 0)) in hidden_layers):
            rest.append(item)
            continue
        start, length, layer = int(item.get("Frame", 0)), int(item.get("Length", 0)), int(item.get("Layer", 0))
        lower = [m for m in project.media if m.kind in ("image", "video") and not m.hidden and m.layer < layer
                 and m.transition is None]  # 他の場面転換の残像は場面にしない
        before = max((m for m in lower if abs(m.frame + m.length - start) <= 1), key=lambda m: m.layer, default=None)
        after = max((m for m in lower if abs(m.frame - start) <= 1), key=lambda m: m.layer, default=None)
        if before is None:
            project.unsupported["前の場面が画像・動画でないトランジション"] += 1
            continue
        p = item.get("TransitionParameter") or {}
        end = {attr: v[1] for attr, v in before.anim.items()}  # 動いていた画像は終わりの位置で止める
        # 残像は前と新しい場面のすぐ上（トランジションのレイヤーが立ち絵より上でも、キャラを隠さない）
        hold_layer = max(before.layer, after.layer if after is not None else before.layer) + 0.5
        hold = replace(before, frame=start, length=length, layer=hold_layer, anim={}, fade_in=0.0, fade_out=0.0,
                       effects=[], slide=None, volume=0.0, x=end.get("x", before.x), y=end.get("y", before.y),
                       zoom=end.get("zoom", before.zoom), rotation=end.get("rotation", before.rotation),
                       opacity=end.get("opacity", before.opacity),
                       transition={"type": kind, "easing": p.get("EasingType") or "Linear",
                                   "mode": p.get("EasingMode") or "InOut", "angle": value(p.get("Angle")),
                                   "after": after})
        if before.kind == "video":  # 最後のコマで止める（再生速度 0）
            hold.offset = before.offset + max(0, before.length - 1) / project.fps * before.rate / 100
            hold.rate = 0.0
        project.media.append(hold)
        for e in _effects(item, "VideoEffects", "BeforeVideoEffects", "AfterVideoEffects"):
            project.unsupported[f"トランジションのエフェクト {e}"] += 1
    project.others = rest


# ボイスごとの字幕（Custom）で、位置のほかに比べる設定（違えば写せなかった設定に出す）
CUSTOM_SUBTITLE_KEYS = ("FontSize", "Font", "FontColor", "Style", "StyleColor", "BasePoint", "Zoom", "Rotation",
                        "LineHeight2")


def _setting(v):
    """比べるための設定の値（アニメーションできる値は最初の値）。"""
    return value(v) if isinstance(v, dict) and "Values" in v else v


def _fill_background(m: Media, item: dict) -> None:
    """テキストの映像エフェクト「背景塗りつぶし」（単色）を背景の色・余白にする（写したものはエフェクトの一覧から外す）。"""
    for e in item.get("VideoEffects") or []:
        if _type(e) != "FillBackgroundEffect" or not e.get("IsEnabled", True) or e.get("IsBackgroundOnly"):
            continue
        brush = (e.get("Brush") or {}).get("Parameter") or {}
        if "SolidColorBrush" not in str(brush.get("$type", "")):
            continue  # グラデーションなどは写せない
        r, g, b, a = color(brush.get("Color"), (0.0, 0.0, 0.0, 1.0))
        m.background = (r, g, b, a * value(e.get("Opacity"), 100.0) / 100)
        m.background_margin = sum(value(e.get(k)) for k in ("Top", "Bottom", "Left", "Right")) / 4
        m.background_round = value(e.get("Round"))
        m.effects.remove("FillBackgroundEffect")
        if any(animated(e.get(k)) for k in ("Opacity", "Round", "Top", "Bottom", "Left", "Right")):
            m.effects.append("FillBackgroundEffect のアニメーション")  # 最初の値だけ写す
        return


def _media(item: dict, kind: str, hidden_layers: set[int]) -> Media:
    m = Media(kind=kind, frame=int(item.get("Frame", 0)), length=int(item.get("Length", 0)),
              layer=int(item.get("Layer", 0)), file=item.get("FilePath") or "",
              x=value(item.get("X")), y=value(item.get("Y")), zoom=value(item.get("Zoom"), 100.0),
              rotation=value(item.get("Rotation")), opacity=value(item.get("Opacity"), 100.0),
              volume=value(item.get("Volume"), 50.0), rate=value(item.get("PlaybackRate"), 100.0) or 100.0,
              offset=seconds(item.get("ContentOffset")), fade_in=float(item.get("FadeIn") or 0.0),
              fade_out=float(item.get("FadeOut") or 0.0), effects=_effects(item, "VideoEffects", "AudioEffects"),
              hidden=bool(item.get("IsHidden")) or int(item.get("Layer", 0)) in hidden_layers)
    _slide(m, item)
    if kind == "image":
        _stroke(m, item)
    if kind == "text":
        m.text = (item.get("Text") or "").replace("\r\n", "\n").replace("\r", "\n")
        m.font = item.get("Font") or ""
        m.font_size = value(item.get("FontSize"), 34.0)
        m.font_color = color(item.get("FontColor"))
        m.outline = item.get("Style") == "Border"
        m.outline_color = color(item.get("StyleColor"), (0.0, 0.0, 0.0, 1.0))
        m.bold = bool(item.get("Bold"))
        m.base_point = item.get("BasePoint") or "LeftTop"
        m.line_spacing = value(item.get("LineHeight2"), 100.0) / 100
        _fill_background(m, item)
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
    raw_characters = {}  # 名前 → .ymmp のキャラの設定（ボイスごとの字幕と比べる）
    for d in data.get("Characters") or []:
        c = _character(d)
        project.characters[c.name] = c
        raw_characters.setdefault(c.name, d)
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
            if v.subtitle == "Custom":  # 位置は写す。文字の設定がキャラと違えば報告する
                c = raw_characters.get(v.character, {})
                if any(_setting(item.get(k)) != _setting(c.get(k)) for k in CUSTOM_SUBTITLE_KEYS):
                    project.unsupported["字幕をボイスごとに変えた文字の設定（位置だけ写す）"] += 1
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
        elif kind == "EffectItem" and (m := _mosaic_item(item, hidden_layers)) is not None:
            project.media.append(m)
            for e in m.effects:
                project.unsupported[f"エフェクトアイテムのエフェクト {e}"] += 1
        else:
            project.others.append(item)
            continue
        _note_unsupported(project, kind, item)
    _transitions(project, hidden_layers)
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
