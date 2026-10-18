import glob
import json
import os

import pytest

from tachie_studio.ymmp import (
    color, difference, find_by_tail, front_layer, load_project, other_items_summary, overlay, part_name, parts_of, preset_name,
    seconds,
)

CHAR = "C:\\ymm4\\sozai\\char\\ずんだもん"


def anim(v, kind="なし", more=()):
    return {"Values": [{"Value": v}, *({"Value": x} for x in more)], "Span": 0.0, "AnimationType": kind}


def project_json(old: bool = False) -> dict:
    voice = {
        "$type": "YukkuriMovieMaker.Project.Items.VoiceItem, YukkuriMovieMaker",
        "CharacterName": "ずんだもん", "Serif": "こんにちはなのだ", "Hatsuon": "コンニチワナ'/ノダ'",
        "Pronounce": {"AudioQuery": {"accent_phrases": [{"moras": [], "accent": 1}], "speedScale": 1.25,
                                     "pitchScale": 0.0, "intonationScale": 1.0, "volumeScale": 1.0,
                                     "prePhonemeLength": 0.1, "postPhonemeLength": 0.1, "pauseLengthScale": 1.0}},
        "VoiceLength": "00:00:01.2480000", "Volume": anim(50.0), "Pan": anim(0.0),
        "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                           "StyleID": 3, "Speed": 120, "Pitch": 95.0, "Intonation": 100.0, "PrePhonemeLength": 0.1,
                           "PostPhonemeLength": 0.1},
        "JimakuVisibility": "Hidden",
        "TachieFaceParameter": {"Eye": f"{CHAR}\\目\\普通白目_カメラ目線.png", "Etc2": f"{CHAR}\\他\\右腕_指差し.png"},
        "Frame": 120, "Layer": 3, "Length": 93, "KeyFrames": {"Count": 0},
    }
    return {
        "FilePath": "C:\\x\\test.ymmp",
        **({"Timeline": None} if old else {"SelectedTimelineIndex": 0}),
        "Characters": [{
            "Name": "ずんだもん", "Color": "#FF33A65E", "Voice": {"API": "voicevox", "Arg": ""},
            "Volume": anim(50.0), "Pan": anim(0.0), "AdditionalTime": 0.3,
            "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                               "StyleID": 3, "Speed": 110, "Pitch": 100.0, "Intonation": 80.0,
                               "PrePhonemeLength": 0.1, "PostPhonemeLength": 0.1, "PauseLengthScale": 100.0},
            "Font": "メイリオ", "FontSize": anim(80.0), "Zoom": anim(150.0), "FontColor": "#FFFFFFFF", "Style": "Border",
            "StyleColor": "#FF33A65E", "Y": anim(540.0), "IsJimakuVisible": True,
            "TachieType": "YukkuriMovieMaker.Plugin.Tachie.AnimationTachie.AnimationTachiePlugin, X",
            "TachieCharacterParameter": {"Directory": CHAR},
            "TachieDefaultItemParameter": {"Eyebrow": f"{CHAR}\\眉\\普通眉.png", "Eye": f"{CHAR}\\目\\普通白目_普通目.png",
                                           "Mouth": f"{CHAR}\\口\\むふ.png", "Complexion": None,
                                           "Etc1": f"{CHAR}\\他\\左腕_基本.png", "Etc2": f"{CHAR}\\他\\右腕_基本.png"},
        }, {"Name": "ゆっくり霊夢", "Voice": {"API": "aquestalk", "Arg": ""}, "TachieType": None}],
    } | {"Timeline" if old else "Timelines": (lambda tl: tl if old else [tl])({
        "VideoInfo": {"FPS": 60, "Hz": 48000, "Width": 1920, "Height": 1080}, "Length": 600,
        "LayerSettings": {"Items": [{"Layer": 10, "IsHidden": True}, {"Layer": 9, "IsHidden": False}]},
        "Items": [
            {**voice, "Frame": 300, "Serif": "あとのセリフ"},
            voice,
            {"$type": "YukkuriMovieMaker.Project.Items.TachieItem, YukkuriMovieMaker", "CharacterName": "ずんだもん",
             "TachieItemParameter": {"Eye": f"{CHAR}\\目\\普通白目_目逸らし.png"}, "X": anim(769.0),
             "Y": anim(530.0, "直線移動", (600.0,)), "Zoom": anim(80.0), "Frame": 0, "Layer": 1, "Length": 600,
             "IsInverted": True, "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.InOutJumpEffect, YukkuriMovieMaker",
                               "IsEnabled": True}]},
            {"$type": "YukkuriMovieMaker.Project.Items.TachieFaceItem, YukkuriMovieMaker",
             "CharacterName": "ずんだもん", "TachieFaceParameter": {"Eyebrow": f"{CHAR}\\眉\\困り眉1.png"},
             "Frame": 400, "Layer": 4, "Length": 60},
            {"$type": "YukkuriMovieMaker.Project.Items.ImageItem, YukkuriMovieMaker", "FilePath": "C:\\a.png",
             "Frame": 0, "Layer": 0, "Length": 600, "X": anim(10.0), "Y": anim(-20.0), "Zoom": anim(50.0),
             "Opacity": anim(100.0, "直線移動", (0.0,)), "Rotation": anim(90.0),
             "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.OutlineEffect, YukkuriMovieMaker"}]},
            {"$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker", "FilePath": "C:\\v.mov",
             "Frame": 10, "Layer": 1, "Length": 100, "PlaybackRate": 400.0, "ContentOffset": "00:00:12.5000000",
             "Volume": anim(0.0), "Zoom": anim(100.0, "Cubic_InOut", (150.0,))},
            {"$type": "YukkuriMovieMaker.Project.Items.AudioItem, YukkuriMovieMaker", "FilePath": "C:\\bgm.m4a",
             "Frame": 0, "Layer": 8, "Length": 600, "Volume": anim(5.0, "直線移動", (0.0,)), "FadeOut": 2.0,
             "IsLooped": True},
            {"$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker", "Text": "タイトル\r\n2 行目",
             "Font": "Noto Sans JP", "FontSize": anim(80.0), "FontColor": "#FF1A76FF", "Style": "Border",
             "StyleColor": "#FFFFFFFF", "Bold": True, "BasePoint": "CenterTop", "X": anim(-100.0),
             "Y": anim(-500.0), "Zoom": anim(100.0, "直線移動", (200.0,)), "Frame": 0, "Layer": 9, "Length": 120,
             "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.FillBackgroundEffect, YukkuriMovieMaker",
                               "Opacity": anim(80.0), "Round": anim(6.0), "IsBackgroundOnly": False,
                               "Brush": {"Parameter": {"$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, "
                                                                "YukkuriMovieMaker", "Color": "#FF2196F3"}},
                               "Top": anim(15.0), "Bottom": anim(15.0), "Left": anim(20.0), "Right": anim(20.0),
                               "IsEnabled": True}]},
            {"$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker", "Text": "隠す",
             "Frame": 0, "Layer": 10, "Length": 120},
            {"$type": "YukkuriMovieMaker.Project.Items.TransitionItem, YukkuriMovieMaker", "Frame": 0, "Layer": 2,
             "Length": 30},
        ]})}


def write(tmp_path, data) -> str:
    path = tmp_path / "test.ymmp"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
    return str(path)


@pytest.mark.parametrize("old", [False, True])
def test_load_project(tmp_path, old):
    p = load_project(write(tmp_path, project_json(old)))
    assert (p.fps, p.width, p.height, p.length) == (60.0, 1920, 1080, 600)
    c = p.characters["ずんだもん"]
    assert c.folder_name == "ずんだもん" and c.style_id == 3 and c.has_tachie and c.after == 0.3
    assert c.voice["speedScale"] == 1.1 and c.voice["intonationScale"] == 0.8 and c.voice["pitchScale"] == 0.0
    assert c.font == "メイリオ" and c.font_size == 120.0 and c.subtitle_y == 540.0 and c.outline
    assert c.outline_color == pytest.approx((0x33 / 255, 0xA6 / 255, 0x5E / 255, 1.0))
    assert c.parts == {"眉": "普通眉", "目": "普通白目_普通目", "口": "むふ", "髪": "", "顔色": "", "体": "",
                       "他": ["左腕_基本", "右腕_基本"], "後": []}
    assert [v.frame for v in p.voices] == [120, 300]  # 始まり順
    v = p.voices[0]
    assert v.text == "こんにちはなのだ" and v.kana == "コンニチワナ'/ノダ'" and v.style_id == 3
    assert v.voice["speedScale"] == 1.25  # audio_query の値が VoiceParameter（1.2）より優先
    assert v.voice["pitchScale"] == 0.0 and v.accent == [{"moras": [], "accent": 1}]
    assert v.voice_seconds == pytest.approx(1.248) and v.subtitle == "Hidden"
    assert v.face == {"目": "普通白目_カメラ目線", "他": ["", "右腕_指差し"]}
    assert p.tachies[0].parts["目"] == "普通白目_目逸らし" and p.tachies[0].x == 769.0 and p.tachies[0].zoom == 80.0
    assert p.tachies[0].inverted
    assert p.faces[0].parts == {"眉": "困り眉1"} and p.faces[0].frame == 400
    assert other_items_summary(p) == {"トランジション": 1}
    assert [m.kind for m in p.media] == ["image", "audio", "text", "text", "video"]
    image, audio, text, hidden, video = p.media
    assert (image.file, image.x, image.y, image.zoom, image.rotation) == ("C:\\a.png", 10.0, -20.0, 50.0, 90.0)
    assert image.anim == {"opacity": (100.0, 0.0, "直線移動")} and image.effects == ["OutlineEffect"]
    assert (video.rate, video.offset, video.volume, video.layer) == (400.0, 12.5, 0.0, 1)
    assert video.anim["zoom"] == (100.0, 150.0, "Cubic_InOut")
    assert audio.anim["volume"] == (5.0, 0.0, "直線移動") and audio.fade_out == 2.0
    assert text.text == "タイトル\n2 行目" and text.font_size == 80.0 and text.bold and text.outline
    assert text.base_point == "CenterTop" and text.font_color == pytest.approx((0x1A / 255, 0x76 / 255, 1.0, 1.0))
    # 背景塗りつぶし → 背景（不透明度を掛けた色、余白は上下左右の平均）。写したのでエフェクトの一覧から外す
    assert text.background == pytest.approx((0x21 / 255, 0x96 / 255, 0xF3 / 255, 0.8))
    assert text.background_margin == 17.5 and text.background_round == 6.0 and text.effects == []
    assert not text.hidden and hidden.hidden  # 非表示のレイヤー
    assert front_layer(p) == 1  # 立ち絵アイテムのレイヤー
    assert p.unsupported["画像のエフェクト OutlineEffect"] == 1
    assert p.unsupported["VideoItem の Zoom のアニメーション"] == 1  # 直線移動以外
    assert "ImageItem の Opacity のアニメーション" not in p.unsupported  # 始め → 終わりの直線は写せる
    assert p.unsupported["TextItem の Zoom のアニメーション"] == 1  # テキストの拡大はキーフレームを打たない
    assert p.unsupported["音声のループ再生"] == 1
    assert p.unsupported["立ち絵のエフェクト InOutJumpEffect"] == 1
    assert p.unsupported["TachieItem の Y のアニメーション"] == 1
    assert p.unsupported["VOICEVOX 以外の声（ゆっくり霊夢: aquestalk）"] == 1


def test_not_a_project(tmp_path):
    path = tmp_path / "x.ymmp"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(path)


def test_helpers():
    assert seconds("00:00:05.8833333") == pytest.approx(5.8833333)
    assert seconds("1.00:00:01") == 86401 and seconds(None) == 0.0
    assert color("#80FF0000") == pytest.approx((1.0, 0.0, 0.0, 128 / 255))
    assert part_name("C:\\a\\目\\ジト目.png") == "ジト目" and part_name(None) == ""
    assert parts_of({"Eye": "C:\\目\\x.png", "Back3": "C:\\後\\y.png"}, full=False) == {"目": "x", "後": ["", "", "y"]}


def test_overlay_and_difference():
    base = {"眉": "普通", "目": "普通", "他": ["左腕", "右腕"], "後": []}
    full = overlay(base, {"目": "ジト目", "他": ["", "右腕_指差し"]})
    assert full == {"眉": "普通", "目": "ジト目", "他": ["左腕", "右腕_指差し"], "後": []}
    assert difference(base, full) == {"目": "ジト目", "他": ["", "右腕_指差し"]}
    assert difference(base, base) == {}


def test_preset_name():
    assert preset_name({"目": "ジト目", "他": ["", "右腕"]}, set()) == "ジト目・右腕"
    assert preset_name({"目": "ジト目"}, {"ジト目"}) == "ジト目 2"


# 手元の YMM4 のプロジェクトのフォルダ（YMMP_DIR、下の階層も）を全部読めること
REAL = sorted(glob.glob(os.path.join(os.environ["YMMP_DIR"], "**", "*.ymmp"), recursive=True)) if os.environ.get("YMMP_DIR") else []


@pytest.mark.skipif(not REAL, reason="YMMP_DIR が無い")
def test_real_projects_load():
    for path in REAL:
        p = load_project(path)
        assert all(v.character for v in p.voices), path


def test_find_by_tail(tmp_path):
    proj = tmp_path / "YouTube" / "proj"
    proj.mkdir(parents=True)
    char = tmp_path / "YouTube" / "sozai" / "char" / "ずんだもん"
    char.mkdir(parents=True)
    (tmp_path / "YouTube" / "sozai" / "bgm.mp3").write_bytes(b"")
    (tmp_path / "bgm.mp3").write_bytes(b"")
    assert find_by_tail(CHAR, proj, directory=True) == char
    assert find_by_tail("C:\\ymm4\\sozai\\bgm.mp3", proj) == tmp_path / "YouTube" / "sozai" / "bgm.mp3"
    # ファイルは名前だけの一致を上の階層で拾わない
    assert find_by_tail("D:\\other\\bgm.mp3", proj) is None
    assert find_by_tail("\\\\server\\share\\sozai\\bgm.mp3", proj) == tmp_path / "YouTube" / "sozai" / "bgm.mp3"
    assert find_by_tail("", proj) is None


def test_voice_wav(tmp_path):
    """YMM4 の VoiceCache（Brotli で圧縮した WAV、終わりの印なし）を WAV に戻す。"""
    import base64
    import io
    import wave

    import brotli

    from tachie_studio.ymmp import Voice, voice_wav

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x01" * 24000)  # 1 秒
    wav = buf.getvalue()
    c = brotli.Compressor()
    unfinished = c.process(wav) + c.flush()  # YMM4 と同じく finish しない
    with pytest.raises(brotli.error):
        brotli.decompress(unfinished)
    cache = base64.b64encode(unfinished).decode()
    assert voice_wav(Voice("a", "x", 0, 1, 0, voice_seconds=1.0, cache=cache)) == wav
    assert voice_wav(Voice("a", "x", 0, 1, 0, voice_seconds=1.0,
                           cache=base64.b64encode(brotli.compress(wav)).decode())) == wav
    assert voice_wav(Voice("a", "x", 0, 1, 0, voice_seconds=2.0, cache=cache)) is None  # 長さが違う
    assert voice_wav(Voice("a", "x", 0, 1, 0, cache="")) is None
    assert voice_wav(Voice("a", "x", 0, 1, 0, cache="!!壊れた!!")) is None
    assert voice_wav(Voice("a", "x", 0, 1, 0, cache=base64.b64encode(brotli.compress(b"not a wav" * 10)).decode())) is None


def test_effects_and_line_height(tmp_path):
    """画面外からの登場・画像の縁取り・モザイク（エフェクトアイテム）・行間を読む。写したものはエフェクトの一覧から外す。"""
    data = project_json(False)
    data["Characters"][0]["LineHeight2"] = anim(70.0)
    brush = lambda c: {"Parameter": {"$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, Y", "Color": c}}
    items = data["Timelines"][0]["Items"]
    items += [
        {"$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker", "Text": "見出し", "LineHeight2": anim(77.5),
         "Frame": 0, "Layer": 11, "Length": 120,
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.InOutMoveFromOutsideFrameEffect, Y", "Value": "Left",
                           "IsInEffect": True, "IsOutEffect": False, "EffectTimeSeconds": 0.3, "EasingType": "Expo",
                           "EasingMode": "Out", "IsEnabled": True}]},
        {"$type": "YukkuriMovieMaker.Project.Items.ImageItem, YukkuriMovieMaker", "FilePath": "C:\\x\\outlined.png",
         "Frame": 0, "Layer": 12, "Length": 120,
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.OutlineEffect, Y", "StrokeThickness": anim(20.0),
                           "Blur": anim(0.0), "X": anim(0.0), "Y": anim(0.0), "Opacity": anim(50.0), "Zoom": anim(100.0),
                           "Rotation": anim(0.0), "StrokeBrush": brush("#FFFFFFFF"), "IsOutlineOnly": False,
                           "IsAngular": False, "IsEnabled": True}]},
        {"$type": "YukkuriMovieMaker.Project.Items.EffectItem, YukkuriMovieMaker", "Frame": 10, "Layer": 13, "Length": 50,
         "ShapeType2": "YukkuriMovieMaker.Shape.QuadrilateralShapePlugin, Y",
         "ShapeParameter": {"$type": "YukkuriMovieMaker.Project.Items.RectangleShapeParameter, Y", "Round": anim(0.0),
                            "SizeMode": "SizeAspect", "Size": anim(100.0), "AspectRate": anim(75.0)},
         "Blur": anim(0.0), "InvertMask": False, "X": anim(171.0), "Y": anim(-74.0), "Opacity": anim(100.0),
         "Rotation": anim(0.0),
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.MosaicEffect, Y", "MosaicType": "Rectangle",
                           "MosaicParameter": {"Size": anim(40.0)}, "IsEnabled": True}]},
        {"$type": "YukkuriMovieMaker.Project.Items.EffectItem, YukkuriMovieMaker", "Frame": 0, "Layer": 14, "Length": 50,
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.BlurEffect, Y", "IsEnabled": True}]},
    ]
    p = load_project(write(tmp_path, data))
    assert p.characters["ずんだもん"].line_height == 70.0
    text = next(m for m in p.media if m.kind == "text" and m.text == "見出し")
    assert text.line_spacing == 0.775 and text.effects == []
    assert text.slide == {"direction": "Left", "in": True, "out": False, "seconds": 0.3, "easing": "Expo", "mode": "Out"}
    image = next(m for m in p.media if m.kind == "image" and m.file.endswith("outlined.png"))
    assert image.stroke == {"thickness": 20.0, "color": (1.0, 1.0, 1.0, 0.5), "blur": 0.0, "angular": False,
                            "only": False} and image.effects == []
    mosaic = next(m for m in p.media if m.kind == "mosaic")
    assert (mosaic.x, mosaic.y, mosaic.frame, mosaic.length) == (171.0, -74.0, 10, 50)
    assert mosaic.mosaic == {"size": 40.0, "width": 25.0, "height": 100.0, "invert": False}  # 正のアスペクト比は幅を縮める
    assert [i["Layer"] for i in p.others if "EffectItem" in i["$type"]] == [14]  # モザイクでないエフェクトアイテムは読まない


def test_outline_image(tmp_path):
    from PIL import Image

    from tachie_studio.media_effects import outline_image

    src = tmp_path / "a.png"
    Image.new("RGBA", (10, 6), (255, 0, 0, 255)).save(src)
    out = outline_image(src, tmp_path / "fx", 3, (1.0, 1.0, 1.0, 1.0))
    im = Image.open(out)
    assert im.size == (10 + 2 * 4, 6 + 2 * 4)  # 縁の太さ + 1 ずつ大きくなる
    row = [im.getpixel((x, 7)) for x in range(im.width)]
    assert row[0][3] == 0 and row[1] == (255, 255, 255, 255) and row[4] == (255, 0, 0, 255)
    assert outline_image(src, tmp_path / "fx", 3, (1.0, 1.0, 1.0, 1.0)) == out  # 同じ中身なら作り直さない


def test_transitions(tmp_path):
    """トランジションアイテム → 前の場面（区間の始まりで終わる画像・動画）の最後のコマの残像。"""
    data = project_json(False)
    items = data["Timelines"][0]["Items"]
    items[:] = [i for i in items if "TransitionItem" not in i["$type"]]
    image = lambda f, frame, length, layer=0: {"$type": "YukkuriMovieMaker.Project.Items.ImageItem, Y",
                                               "FilePath": f, "Frame": frame, "Length": length, "Layer": layer,
                                               "X": anim(10.0, "直線移動", (50.0,)), "Zoom": anim(40.0)}
    video = {"$type": "YukkuriMovieMaker.Project.Items.VideoItem, Y", "FilePath": "C:\\x\\v.mp4", "Frame": 300,
             "Length": 120, "Layer": 20, "PlaybackRate": 200.0, "ContentOffset": "00:00:01"}
    tr = lambda frame, kind, layer: {"$type": "YukkuriMovieMaker.Project.Items.TransitionItem, Y", "Frame": frame,
                                     "Length": 48, "Layer": layer,
                                     "TransitionType": f"YukkuriMovieMaker.Transition.{kind}, YukkuriMovieMaker",
                                     "TransitionParameter": {"Angle": anim(0.0), "EasingType": "Expo",
                                                             "EasingMode": "InOut"}}
    items += [image("C:\\x\\before.png", 100, 200, 20), image("C:\\x\\after.png", 300, 100, 20), video,
              {**image("C:\\x\\next.png", 420, 60, 20)},
              tr(300, "PushTransitionPlugin", 21), tr(420, "FadeTransitionPlugin", 21),
              tr(0, "FadeTransitionPlugin", 22), tr(50, "UnknownTransitionPlugin", 21)]
    p = load_project(write(tmp_path, data))
    holds = sorted((m for m in p.media if m.transition), key=lambda m: m.frame)
    assert len(holds) == 2
    push, fade = holds
    assert push.file.endswith("before.png") and push.frame == 300 and push.length == 48 and push.layer == 20.5
    assert push.x == 50.0 and push.anim == {}  # 動いていた画像は終わりの位置で止める
    assert push.transition["type"] == "push" and push.transition["after"].file.endswith("after.png")
    # 動画は最後のコマで止める（1 秒 + 119 フレーム × 2 倍速）
    assert fade.kind == "video" and fade.rate == 0.0 and fade.volume == 0.0
    assert abs(fade.offset - (1.0 + 119 / 60 * 2.0)) < 1e-9 and fade.transition["after"].file.endswith("next.png")
    assert p.unsupported["前の場面が画像・動画でないトランジション"] == 1
    assert sum(1 for i in p.others if "TransitionItem" in i["$type"]) == 1  # 写せない種類は読み込まないアイテムに残す


def test_custom_subtitle(tmp_path):
    """字幕をボイスごとに設定したセリフ（Custom）は位置を読む。文字の設定がキャラと違うときだけ報告する。"""
    data = project_json(False)
    items = data["Timelines"][0]["Items"]
    voice = next(i for i in items if "VoiceItem" in i["$type"])
    items += [{**voice, "Frame": 400, "JimakuVisibility": "Custom", "X": anim(-20.5), "Y": anim(0.0),
               "FontSize": anim(80.0), "Zoom": anim(150.0), "Font": "メイリオ", "FontColor": "#FFFFFFFF",
               "Style": "Border", "StyleColor": "#FF33A65E"}]
    p = load_project(write(tmp_path, data))
    v = next(v for v in p.voices if v.frame == 400)
    assert v.subtitle == "Custom" and (v.sub_x, v.sub_y) == (-20.5, 0.0)
    assert "字幕をボイスごとに変えた文字の設定（位置だけ写す）" not in p.unsupported
    items[-1]["FontSize"] = anim(40.0)
    p = load_project(write(tmp_path, data))
    assert p.unsupported["字幕をボイスごとに変えた文字の設定（位置だけ写す）"] == 1


def test_jump_keys():
    """跳ねながら登場: 効果時間でずれが 0 になり、周期ごとに跳ね、着地でつぶれて間隔のあと次のジャンプ。"""
    from tachie_studio.ymmp import jump_keys

    jump = {"seconds": 1.0, "height": 0.1, "stretch": 10.0, "period": 0.5, "distortion": 5.0, "interval": 0.2,
            "x": 0.0, "y": -0.5}
    keys = jump_keys(jump, 10)
    frames = [k[0] for k in keys]
    assert frames == sorted(frames) and keys[0] == (0, 1.0, 0.0, 1.0)
    # 1 回目: 0〜5 フレームで跳ね（真ん中が最高・いちばん伸びる）、6 フレームでつぶれ、7 で戻る。2 回目は 7〜12
    by = {f: (rest, h, sy) for f, rest, h, sy in keys}
    assert by[2][1] == max(h for _, _, h, _ in keys) or by[3][1] == max(h for _, _, h, _ in keys)
    assert max(sy for *_, sy in keys) == pytest.approx(1.1, abs=0.01)
    assert by[6] == (pytest.approx(0.4), 0.0, pytest.approx(0.95)) and by[7][1:] == (0.0, 1.0)
    assert by[12][1] == 0.0 and by[10][0] == 0.0  # 効果時間（10 フレーム）でずれは 0
    assert frames[-1] == 14  # 2 回目の着地まで（12 + 間隔 2）
