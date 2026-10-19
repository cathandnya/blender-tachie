import json
from pathlib import Path

import pytest

from tachie_studio.templates import (
    dumps, end_frame, from_ymm4, load_ymm4_templates, loads, media_from_dict, media_to_dict, normalize, template_name,
)
from tachie_studio.ymmp import Media

ITEM = "YukkuriMovieMaker.Project.Items."


def anim(v, kind="なし", more=()):
    return {"Values": [{"Value": v}, *({"Value": x} for x in more)], "AnimationType": kind}


def ymm4_settings() -> dict:
    voice = {"$type": ITEM + "VoiceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん", "Serif": "こんにちはなのだ",
             "Hatsuon": "コンニチワ", "VoiceLength": "00:00:01", "Volume": anim(25.0), "Pan": anim(0.0),
             "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                                "StyleID": 3, "Speed": 110},
             "TachieFaceParameter": {"Eye": "C:\\char\\ずんだもん\\目\\なごみ目.png"},
             "JimakuVisibility": "Hidden", "Frame": 1200, "Layer": 6, "Length": 90}
    return {"DefaultItems": [], "Templates": [
        {"Name": "こんにちは", "Path": ["挨拶", "こんにちは"], "FPS": 60, "Items": [
            voice,
            {"$type": ITEM + "TachieFaceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん",
             "TachieFaceParameter": {"Eyebrow": "C:\\char\\ずんだもん\\眉\\怒り眉.png"},
             "Frame": 1230, "Layer": 7, "Length": 30},
            {"$type": ITEM + "TransitionItem, YukkuriMovieMaker", "Frame": 1200, "Layer": 2, "Length": 30}]},
        {"Name": "背景", "Path": ["背景"], "FPS": 30, "Width": 1280, "Height": 720, "Items": [
            {"$type": ITEM + "ImageItem, YukkuriMovieMaker", "FilePath": "C:\\bg.png", "Zoom": anim(50.0),
             "Frame": 500, "Layer": 0, "Length": 300}]},
        {"Name": "立ち絵", "Path": [], "Items": [
            {"$type": ITEM + "TachieItem, YukkuriMovieMaker", "CharacterName": "ずんだもん", "Frame": 0,
             "Layer": 1, "Length": 60, "X": anim(769.0), "Y": anim(540.0), "Zoom": anim(80.0), "IsInverted": True,
             "Opacity": anim(50.0), "Rotation": anim(10.0), "FadeIn": 0.2, "FadeOut": 0.0, "Blend": "Add",
             "IsAlwaysOnTop": True,
             "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.InOutJumpEffect, YukkuriMovieMaker",
                               "IsInEffect": True, "IsOutEffect": False, "IsOutReverse": True, "EffectTimeSeconds": 0.5,
                               "JumpHeight": 10.0, "Stretch": 10.0, "Period": 0.5, "X": 0.0, "Y": 900.0,
                               "IsEnabled": True}],
             "TachieItemParameter": {"Eye": "C:\\char\\ずんだもん\\目\\なごみ目.png", "IsHiddenWhenNoSpeech": True}},
            {"$type": ITEM + "TachieItem, YukkuriMovieMaker", "CharacterName": "めたん", "Frame": 0,
             "Layer": 2, "Length": 60, "IsHidden": True}]},
    ]}


def test_from_ymm4(tmp_path: Path):
    path = tmp_path / "YukkuriMovieMaker.Settings.ItemSettings.json"
    path.write_text(json.dumps(ymm4_settings(), ensure_ascii=False), encoding="utf-8-sig")
    (hello, h, h_behind, h_skipped), (bg, b, b_behind, _), (tachie, t, _, t_skipped) = load_ymm4_templates(path)
    assert (hello, bg, tachie) == ("挨拶/こんにちは", "背景", "立ち絵")
    line, face = h["items"]
    assert line["kind"] == "line" and line["frame"] == 0 and face["frame"] == 30  # 頭を 0 にそろえる
    assert line["character"] == "ずんだもん" and line["text"] == "こんにちはなのだ" and line["kana"] == "コンニチワ"
    assert line["style"] == 3 and line["voice"]["speedScale"] == 1.1 and line["audio"]["volume"] == 0.5
    assert line["audio"]["after"] == pytest.approx(0.5) and line["subtitle_hidden"]
    assert h["items"] and "after" not in from_ymm4({"Items": [{**ymm4_settings()["Templates"][0]["Items"][0],
                                                               "VoiceLength": "00:00:00"}]})[0]["items"][0]["audio"]
    # 実物のテンプレートは VoiceLength が 0 で、余韻はアイテムの AdditionalTime
    no_length = {**ymm4_settings()["Templates"][0]["Items"][0], "VoiceLength": "00:00:00", "AdditionalTime": 0.3}
    assert from_ymm4({"Items": [no_length]})[0]["items"][0]["audio"]["after"] == pytest.approx(0.3)
    assert line["face_parts"] == {"目": "なごみ目"} and face["parts"] == {"眉": "怒り眉"}
    assert h_skipped == {"トランジション": 1} and not h_behind
    # 画像だけでいちばん下のレイヤーのテンプレートは立ち絵より奥。fps・解像度はテンプレートのもの
    assert b_behind and (b["fps"], b["width"], b["height"]) == (30.0, 1280, 720)
    image = media_from_dict(b["items"][0]["media"])
    assert image.kind == "image" and image.frame == 0 and image.length == 300 and image.zoom == 50.0
    # 立ち絵アイテムは立ち絵（位置・大きさは YMM4 の値のまま持ち、置くときにキャラとの差にする）。非表示は読まない
    (still,) = t["items"]
    assert still["kind"] == "still" and still["character"] == "ずんだもん" and (still["frame"], still["length"]) == (0, 60)
    assert (still["x"], still["y"], still["zoom"]) == (769.0, 540.0, 80.0) and still["parts"]["目"] == "なごみ目"
    # 左右反転は立ち絵ごと、跳ねながら登場退場は画面に対する割合（上が正）で持つ
    assert still["flip"] is True and still["jump"]["in"] and not still["jump"]["out"] and still["jump"]["out_reverse"]
    assert still["jump"]["y"] == pytest.approx(-900 / 1080) and still["jump"]["x"] == 0.0
    assert (still["jump"]["seconds"], still["jump"]["period"]) == (0.5, 0.5)
    assert still["jump"]["height"] == pytest.approx(10 / 1080)  # 高さは px → 画面の高さに対する割合
    assert still["settings"] == {"opacity": 0.5, "rotation": 10.0, "fade_in": 0.2, "fade_out": 0.0, "speech_only": True}
    assert t_skipped == {"立ち絵アイテムの合成モード Add": 1, "立ち絵アイテムの手前に表示": 1}  # 写せない設定は報告する
    # .ymmp の読み込みと同じく、写せない合成モード・アニメーションは報告する
    image = {**ymm4_settings()["Templates"][1]["Items"][0], "Blend": "Add", "X": anim(0.0, "反復移動", (100.0,))}
    assert from_ymm4({"Items": [image]})[2] == {"画像の合成モード Add": 1, "ImageItem の X のアニメーション": 1}


def test_round_trip_and_helpers():
    m = Media(kind="text", frame=5, length=10, layer=3, text="見出し", font_color=(1.0, 0.0, 0.0, 1.0),
              background=(0.0, 0.5, 1.0, 0.8), background_margin=12.0, background_round=6.0,
              anim={"x": (0.0, 100.0, "直線移動")})
    data = {"fps": 30.0, "width": 1920, "height": 1080,
            "items": normalize([{"kind": "media", "media": media_to_dict(m)},
                                {"kind": "face", "frame": 8, "length": 20, "layer": 9, "character": "a",
                                 "expression": "笑顔", "parts": None}])}
    again = loads(dumps(data))
    assert again["version"] == 1 and end_frame(again) == 23
    back = media_from_dict(again["items"][0]["media"])
    assert back == Media(kind="text", frame=0, length=10, layer=3, text="見出し", font_color=(1.0, 0.0, 0.0, 1.0),
                         background=(0.0, 0.5, 1.0, 0.8), background_margin=12.0, background_round=6.0,
                         anim={"x": (0.0, 100.0, "直線移動")})
    assert again["items"][1]["frame"] == 3
    assert template_name({"Name": "x", "Path": []}) == "x"
    with pytest.raises(ValueError):
        loads("not json")


def test_not_item_settings(tmp_path: Path):
    path = tmp_path / "x.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Templates"):
        load_ymm4_templates(path)
