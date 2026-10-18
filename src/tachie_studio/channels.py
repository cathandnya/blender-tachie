"""シーケンサーのチャンネルの割り振り（Blender 非依存）。

キャラ × 役割（音声・立ち絵・表情・字幕・キャラ名）と見出しに 1 本ずつ「レーン」を割り当てる。並びは役割ごとで、
どのキャラの字幕も全員の立ち絵より手前に描かれる（番号が大きいチャンネルほど手前）::

    base …  <キャラ> 音声        全キャラ（一覧の順）
            <キャラ> 立ち絵      立ち絵のあるキャラ（下のキャラほど奥）
            <キャラ> 表情        そのキャラの立ち絵のすぐ上（ミュートしたストリップなので見た目に影響しない）
            <キャラ> 字幕        全キャラ
            <キャラ> キャラ名    キャラ名を出すキャラだけ
    top     見出し

`base` より下は動画・背景・効果音・BGM の場所で、アドオンは管理しない。レーンが埋まっているときは
`top` より上の空き（あふれ）に置く。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

ROLE_LABELS = {"audio": "音声", "image": "立ち絵", "face": "表情", "subtitle": "字幕", "name": "キャラ名",
               "headline": "見出し"}
DEFAULT_BASE = 10
MAX_CHANNEL = 128
NAME_BYTES = 63  # Blender のチャンネル名は 64 バイト（終端を含む）


@dataclass(frozen=True)
class LaneChar:
    name: str
    image: bool  # 立ち絵がある（立ち絵・表情のレーンを持つ）
    show_name: bool  # キャラ名を出す（キャラ名のレーンを持つ）
    registered: bool = True  # キャラクター一覧にいる（消した・名前を変えたキャラの残ったストリップは False）


@dataclass(frozen=True)
class Lane:
    role: str
    char: str | None
    channel: int
    label: str


@dataclass(frozen=True)
class Layout:
    base: int
    lanes: tuple[Lane, ...]

    @property
    def top(self) -> int:
        return max((lane.channel for lane in self.lanes), default=self.base - 1)

    def channel(self, role: str, char: str | None = None) -> int | None:
        return next((lane.channel for lane in self.lanes if lane.role == role and lane.char == char), None)

    def key_of(self, channel: int) -> tuple[str, str | None] | None:
        return next(((lane.role, lane.char) for lane in self.lanes if lane.channel == channel), None)

    def to_json(self) -> str:
        return json.dumps({"base": self.base, "lanes": [[x.role, x.char, x.channel, x.label] for x in self.lanes]},
                          ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Layout | None:
        try:
            data = json.loads(text)
            lanes = tuple(Lane(r, c, int(n), label) for r, c, n, label in data["lanes"])
            if any(lane.role not in ROLE_LABELS for lane in lanes):
                return None
            return cls(int(data["base"]), lanes)
        except (ValueError, KeyError, TypeError):
            return None


def lane_label(role: str, char: str | None = None, registered: bool = True) -> str:
    """チャンネル名（「ずんだもん 立ち絵」「見出し」。一覧にいないキャラは「名前（未登録） 立ち絵」）。"""
    if char is None:
        return ROLE_LABELS[role]
    suffix = f"{'' if registered else '（未登録）'} {ROLE_LABELS[role]}"
    while char and len((char + suffix).encode("utf-8")) > NAME_BYTES:  # 名前を文字の途中で切らずに縮め、役割は残す
        char = char[:-1]
    return char + suffix


def channel_layout(base: int, chars: list[LaneChar]) -> Layout:
    if len({c.name for c in chars}) != len(chars):
        raise ValueError("同じ名前のキャラクターが複数あります")
    lanes: list[tuple[str, LaneChar | None]] = [("audio", c) for c in chars]
    for c in chars:
        if c.image:
            lanes += [("image", c), ("face", c)]
    lanes += [("subtitle", c) for c in chars]
    lanes += [("name", c) for c in chars if c.show_name]
    lanes.append(("headline", None))
    if base + len(lanes) - 1 > MAX_CHANNEL:
        raise ValueError(f"チャンネルが足りません（{base} 番から {len(lanes)} 本）")
    return Layout(base, tuple(Lane(role, c.name if c else None, base + i,
                                   lane_label(role, c.name if c else None, c.registered if c else True))
                              for i, (role, c) in enumerate(lanes)))


@dataclass(frozen=True)
class Item:
    """レーンに置くストリップ。`priority` の小さい順に置く（表情は優先が低い＝先に置くものほど下になる）。"""
    id: object
    role: str
    char: str | None
    start: int
    end: int
    priority: tuple = ()


def _free(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return all(end <= a or b <= start for a, b in spans)


def assign(items: list[Item], layout: Layout, obstacles: list[tuple[int, int, int]],
           limit: int = MAX_CHANNEL) -> dict:
    """各ストリップのチャンネル {id: channel}。レーンが時間で空いていればレーン、埋まっていれば top より上の空き。
    表情は、同じキャラの重なる表情より上に置く（後から置いた表情ほど上＝優先される、を保つ）。
    `obstacles` は動かさないストリップ (channel, start, end)。"""
    used: dict[int, list[tuple[int, int]]] = {}
    for channel, a, b in obstacles:
        used.setdefault(channel, []).append((a, b))
    faces: dict[str | None, list[tuple[int, int, int]]] = {}  # キャラ → 置いた表情 (channel, start, end)
    out = {}
    for item in sorted(items, key=lambda i: (i.priority, i.start)):
        lane = layout.channel(item.role, item.char)
        floor = 0
        if item.role == "face":
            floor = max((c + 1 for c, a, b in faces.get(item.char, []) if a < item.end and item.start < b), default=0)
        candidates = ([lane] if lane is not None and lane >= floor else []) + list(
            range(max(layout.top + 1, floor), limit + 1))
        channel = next((c for c in candidates if _free(used.get(c, []), item.start, item.end)), None)
        if channel is None:
            raise ValueError(f"チャンネルが足りません（{item.start}〜{item.end} フレームの{ROLE_LABELS.get(item.role, item.role)}）")
        used.setdefault(channel, []).append((item.start, item.end))
        if item.role == "face":
            faces.setdefault(item.char, []).append((channel, item.start, item.end))
        out[item.id] = channel
    return out


def move_plan(current: dict, targets: dict, fixed: list[tuple[int, int, int]] = (),
              limit: int = MAX_CHANNEL) -> list[tuple[object, int]]:
    """ストリップを重ねずに動かす手順 [(id, channel)]。`current` は {id: (channel, start, end)}。
    行き先が埋まっていて進めなくなったら、待っているものを 1 つ空いている高いチャンネルへ一時退避する
    （Blender は重なる位置へ動かすと勝手に別のチャンネルへ逃がすため）。"""
    pos = {i: list(v) for i, v in current.items()}
    steps: list[tuple[object, int]] = []

    def free(channel: int, start: int, end: int, me) -> bool:
        if any(c == channel and not (end <= a or b <= start) for c, a, b in fixed):
            return False
        return all(i == me or c != channel or end <= a or b <= start for i, (c, a, b) in pos.items())

    by_channel: dict[int, list[tuple[int, int]]] = {}  # 行き先どうしが重なるなら、どう動かしても終わらない
    for i, (c, a, b) in pos.items():
        by_channel.setdefault(targets.get(i, c), []).append((a, b))
    for c, spans in by_channel.items():
        spans.sort()
        if any(spans[k + 1][0] < spans[k][1] for k in range(len(spans) - 1)):
            raise ValueError(f"チャンネル {c} の行き先で、ストリップが時間で重なっています")
    pending = [i for i in targets if pos[i][0] != targets[i]]
    for _ in range(len(pending) * 4 + 4):
        if not pending:
            return steps
        progress = False
        for i in list(pending):
            _, a, b = pos[i]
            if free(targets[i], a, b, i):
                pos[i][0] = targets[i]
                steps.append((i, targets[i]))
                pending.remove(i)
                progress = True
        if progress:
            continue
        # 行き先をふさいでいるものを退避する（行き先にならない、空いている高いチャンネルへ）
        goals = set(targets.values())
        i = pending[0]
        _, a, b = pos[i]
        blocker = next((j for j, (c, ja, jb) in pos.items() if j != i and c == targets[i] and ja < b and a < jb), None)
        if blocker is None or blocker not in targets:  # 動かさないものと重なる行き先（assign の obstacles の漏れ）
            raise ValueError(f"チャンネル {targets[i]} の {a}〜{b} フレームに動かせないストリップがあります")
        _, ba, bb = pos[blocker]
        park = next((c for c in range(limit, 0, -1) if c not in goals and free(c, ba, bb, blocker)), None)
        if park is None:
            raise ValueError("ストリップを動かす場所がありません")
        pos[blocker][0] = park
        steps.append((blocker, park))
        if blocker not in pending:
            pending.append(blocker)
    raise ValueError("ストリップの並べ替えが終わりません")
