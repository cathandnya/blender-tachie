import random

import pytest

from tachie_studio.channels import (DEFAULT_BASE, Item, LaneChar, Layout, assign, channel_layout, lane_label,
                                    move_plan)

CAST = [LaneChar("ずんだもん", True, False), LaneChar("四国めたん", True, True), LaneChar("ナレーター", False, False)]


def test_layout_order():
    lay = channel_layout(DEFAULT_BASE, CAST)
    assert [(x.channel, x.label) for x in lay.lanes] == [
        (10, "ずんだもん 音声"), (11, "四国めたん 音声"), (12, "ナレーター 音声"),
        (13, "ずんだもん 立ち絵"), (14, "ずんだもん 表情"), (15, "四国めたん 立ち絵"), (16, "四国めたん 表情"),
        (17, "ずんだもん 字幕"), (18, "四国めたん 字幕"), (19, "ナレーター 字幕"),
        (20, "四国めたん キャラ名"), (21, "見出し")]
    assert lay.top == 21 and lay.channel("image", "ナレーター") is None and lay.channel("headline") == 21
    assert lay.key_of(15) == ("image", "四国めたん") and lay.key_of(9) is None
    assert channel_layout(3, CAST).channel("audio", "ずんだもん") == 3
    assert Layout.from_json(lay.to_json()) == lay and Layout.from_json("壊れた") is None
    assert channel_layout(1, []).lanes[0].label == "見出し"
    with pytest.raises(ValueError, match="チャンネルが足りません"):
        channel_layout(120, CAST)


def test_lane_label():
    assert lane_label("image", "ずんだもん", registered=False) == "ずんだもん（未登録） 立ち絵"
    long = lane_label("subtitle", "あ" * 40)
    assert len(long.encode("utf-8")) <= 63 and long.startswith("あ") and long.endswith(" 字幕")
    lay = channel_layout(10, [LaneChar("あ" * 40, True, True)])
    assert len({x.label for x in lay.lanes}) == len(lay.lanes)  # 長い名前でも役割で見分けられる
    with pytest.raises(ValueError, match="同じ名前"):
        channel_layout(10, [LaneChar("a", True, False), LaneChar("a", False, False)])


def test_assign_lanes_overflow_and_faces():
    lay = channel_layout(10, CAST[:2])  # 10,11 音声 / 12,13 ずんだもん 立ち絵・表情 / 14,15 めたん / 16,17 字幕 / 18 名 / 19 見出し
    items = [Item("a", "audio", "ずんだもん", 0, 100), Item("b", "audio", "ずんだもん", 50, 150),  # 重なる → あふれ
             Item("f1", "face", "ずんだもん", 0, 100, (12, 0)), Item("f2", "face", "ずんだもん", 50, 80, (30, 50)),
             Item("h", "headline", None, 0, 10)]
    got = assign(items, lay, obstacles=[(20, 0, 60)])  # top より上に動かさないものがある
    assert got["a"] == 10 and got["h"] == 19
    assert got["b"] == 21  # 20 は 0〜60 が埋まっている
    assert got["f1"] == 13 and got["f2"] > got["f1"] and got["f2"] > lay.top  # 後から置いた表情ほど上
    with pytest.raises(ValueError, match="チャンネルが足りません"):
        assign([Item(i, "audio", "ずんだもん", 0, 10) for i in range(200)], lay, [])


def _overlap(state):
    seen = {}
    for i, (c, a, b) in state.items():
        for j, (c2, a2, b2) in seen.items():
            if c == c2 and a < b2 and a2 < b:
                return (i, j)
        seen[i] = (c, a, b)
    return None


def _apply(current, steps):
    state = {i: list(v) for i, v in current.items()}
    for i, c in steps:
        state[i][0] = c
        assert _overlap(state) is None, (i, c, state)
    return state


def test_move_plan_swap_uses_parking():
    current = {"x": (5, 0, 100), "y": (6, 0, 100), "z": (7, 0, 50)}
    targets = {"x": 6, "y": 5, "z": 9}
    state = _apply(current, move_plan(current, targets))
    assert {i: v[0] for i, v in state.items()} == targets


def test_move_plan_conflicting_targets():
    with pytest.raises(ValueError, match="重なっています"):
        move_plan({"a": (1, 0, 10), "b": (2, 0, 10)}, {"a": 3, "b": 3})


def test_move_plan_random():
    rnd = random.Random(1)
    for _ in range(50):
        current, targets = {}, {}
        for i in range(8):  # 時間が重ならないよう、各ストリップは自分の区間を持つ
            a = i * 10
            current[i] = (rnd.randint(1, 6), a, a + rnd.choice([5, 25]))
            targets[i] = rnd.randint(1, 6)
        if _overlap({i: (targets[i], *current[i][1:]) for i in current}) or _overlap(current):
            continue
        state = _apply(current, move_plan(current, targets, limit=20))
        assert {i: v[0] for i, v in state.items()} == targets
