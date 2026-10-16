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
