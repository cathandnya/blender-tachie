"""テンプレート（よく使うストリップをまとめて登録し、再生位置に置く）の形式と、YMM4 のテンプレートの読み込み。

テンプレートは JSON の `{"version", "fps", "width", "height", "items": [...]}`。アイテムはテンプレートの頭からの
フレーム（`frame`・`length`）と、並び順（`layer`、大きいほど手前）を持つ:

- `{"kind": "media", "media": {...}}` — 画像・動画・音声・テキスト・カラー（`ymmp.Media` の値。座標は YMM4 と同じく
  画面中央からのピクセルで下が正、Zoom は %）
- `{"kind": "line", "character", "text", "kana", "accent", "accent_kana", "style", "voice", "audio", "expression",
  "layout", "subtitle_offset", "subtitle_hidden", "face_parts"}` — セリフ（置くときに合成し直す）
- `{"kind": "face", "character", "expression", "parts"}` — 表情ストリップ

YMM4 のテンプレートは `user/setting/<版>/YukkuriMovieMaker.Settings.ItemSettings.json` の `Templates`
（`{Name, Path: [フォルダ…], FPS, Width, Height, Items: [...]}`、Items は .ymmp のアイテムと同じ形）。
表情は YMM4 のパーツ（`face_parts` / `parts`）のまま持ち、アドオンのキャラの表情プリセットにするのは Blender 側。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .ymmp import ITEM_LABELS, MEDIA_KINDS, Media, _media, _note_unsupported, _span, _type, _voice, parts_of

VERSION = 1
# YMM4 の音量（50 が等倍）
VOLUME_BASE = 50.0
_MEDIA_FIELDS = {f.name for f in fields(Media)}


def media_to_dict(m: Media) -> dict:
    return asdict(m)


def media_from_dict(d: dict) -> Media:
    values = {k: v for k, v in d.items() if k in _MEDIA_FIELDS}
    for key in ("font_color", "outline_color", "color"):
        if key in values and isinstance(values[key], list):
            values[key] = tuple(values[key])
    values["anim"] = {k: tuple(v) for k, v in (values.get("anim") or {}).items()}
    return Media(**values)


def dumps(data: dict) -> str:
    return json.dumps({"version": VERSION, **data}, ensure_ascii=False)


def loads(text: str) -> dict:
    try:
        data = json.loads(text) if text else {}
    except ValueError as e:
        raise ValueError(f"テンプレートを読めません: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("テンプレートの形式が違います")
    data.setdefault("items", [])
    return data


def normalize(items: list[dict]) -> list[dict]:
    """いちばん早い開始を 0 にする。"""
    if not items:
        return items
    first = min(_start(i) for i in items)
    for i in items:
        if i["kind"] == "media":
            i["media"]["frame"] -= first
        else:
            i["frame"] -= first
    return items


def _start(item: dict) -> int:
    return int(item["media"]["frame"] if item["kind"] == "media" else item["frame"])


def end_frame(data: dict) -> int:
    ends = [(i["media"]["frame"] + i["media"]["length"]) if i["kind"] == "media" else i["frame"] + i["length"]
            for i in data.get("items", [])]
    return max(ends, default=0)


def from_ymm4(template: dict) -> tuple[dict, bool, Counter]:
    """YMM4 のテンプレート 1 つ → (テンプレート, 立ち絵より奥に置くか, 読まなかったもの)。"""
    fps = float(template.get("FPS") or 60)
    items: list[dict] = []
    skipped: Counter = Counter()
    for item in template.get("Items") or []:
        kind = _type(item)
        if kind in MEDIA_KINDS:
            m = _media(item, MEDIA_KINDS[kind], set())
            if m.hidden:
                continue
            items.append({"kind": "media", "media": media_to_dict(m)})
            label = ITEM_LABELS[kind]
            for e in m.effects:
                skipped[f"{label}のエフェクト {e}"] += 1
            # .ymmp の読み込み（load_project）と同じく、写せないものを報告する
            if item.get("IsLooped"):
                skipped[f"{label}のループ再生"] += 1
            if (item.get("Blend") or "Normal") != "Normal":
                skipped[f"{label}の合成モード {item['Blend']}"] += 1
            if kind == "TextItem" and item.get("Decorations"):
                skipped["テキストの装飾（Decorations）"] += 1
            _note_unsupported(SimpleNamespace(unsupported=skipped), kind, item)
        elif kind == "VoiceItem":
            v = _voice(item)
            if v.hidden or not v.text.strip():
                continue
            items.append({
                "kind": "line", "frame": v.frame, "length": v.length, "layer": v.layer, "character": v.character,
                "text": v.text, "kana": v.kana, "accent": v.accent, "accent_kana": v.kana or None, "style": v.style_id,
                "voice": v.voice, "expression": "", "layout": None,
                # テンプレートのボイスは声の長さ（VoiceLength）が 0 なので、余韻はアイテムの AdditionalTime。
                # どちらも無ければキャラの既定値にする
                "audio": {"volume": v.volume / VOLUME_BASE, "pan": v.pan / 100, "fade_in": v.fade_in,
                          "fade_out": v.fade_out, **_after(item, v, fps)},
                "subtitle_offset": None, "subtitle_hidden": v.subtitle == "Hidden", "face_parts": v.face})
        elif kind == "TachieFaceItem":
            span = _span(item, parts_of(item.get("TachieFaceParameter"), full=False))
            if not span.hidden:
                items.append({"kind": "face", "frame": span.frame, "length": span.length, "layer": span.layer,
                              "character": span.character, "expression": "", "parts": span.parts})
        else:
            skipped[ITEM_LABELS.get(kind, kind)] += 1
    normalize(items)
    visuals = [i["media"] for i in items if i["kind"] == "media"]
    behind = bool(visuals) and len(visuals) == len(items) and all(
        m["kind"] in ("image", "video") for m in visuals) and min(m["layer"] for m in visuals) == 0
    data = {"fps": fps, "width": int(template.get("Width") or 1920), "height": int(template.get("Height") or 1080),
            "items": items}
    return data, behind, skipped


def _after(item: dict, v, fps: float) -> dict:
    """ボイスの余韻（秒）。声の長さが分かればアイテムの長さとの差、無ければ AdditionalTime。"""
    if v.voice_seconds > 0:
        return {"after": max(0.0, v.length / fps - v.voice_seconds)}
    if item.get("AdditionalTime") is not None:
        return {"after": max(0.0, float(item["AdditionalTime"]))}
    return {}


def template_name(template: dict) -> str:
    """YMM4 のテンプレートの名前（フォルダがあれば「フォルダ/名前」）。"""
    path = [str(p) for p in template.get("Path") or [] if p]
    return "/".join(path) or str(template.get("Name") or "テンプレート")


def load_ymm4_templates(path: str | Path) -> list[tuple[str, dict, bool, Counter]]:
    """ItemSettings.json → [(名前, テンプレート, 立ち絵より奥に置くか, 読まなかったもの)]。"""
    path = Path(path)
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{path.name} を読めません: {e}") from None
    templates = data.get("Templates") if isinstance(data, dict) else None
    if not isinstance(templates, list):
        raise ValueError(f"{path.name} に YMM4 のテンプレート（Templates）がありません")
    return [(template_name(t), *from_ymm4(t)) for t in templates if isinstance(t, dict)]
