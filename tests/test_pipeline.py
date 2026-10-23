import io
import json
import math
import wave
from pathlib import Path

import pytest
from PIL import Image

from tachie_studio.pipeline import load_characters, parse_script, render_script
from tachie_studio.pipeline.config import (dump_characters, dump_raw_table, dump_table, load_material_table,
                                           merge_tables, parse_characters)
from tachie_studio.pipeline.render import resolve_mouth_mode
from tachie_studio.tachie import load_character


def _png(path: Path, color, box=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (8, 8))
    im.paste(color, box or (0, 0, 8, 8))
    im.save(path)


@pytest.fixture
def char_dir(tmp_path: Path) -> Path:
    d = tmp_path / "chars" / "テスト"
    _png(d / "体" / "服.png", (200, 200, 200, 255))
    for name, c in [("目.png", 10), ("目.0.png", 11), ("目.1.png", 12)]:
        _png(d / "目" / name, (c, 0, 0, 255), (0, 2, 8, 3))
    for name, c in [("口.png", 22), ("口.0.png", 20), ("口.1.png", 21)]:
        _png(d / "口" / name, (0, 0, c, 255), (0, 5, 8, 6))
    return d


class FakeClient:
    """audio_query と、その長さの wav（前後無音・中央に音）を返す。"""

    def __init__(self):
        self.calls = []

    def audio_query(self, text, speaker):
        self.calls.append((text, speaker))
        return {
            "accent_phrases": [{"moras": [
                {"text": "カ", "consonant": "k", "consonant_length": 0.1, "vowel": "a", "vowel_length": 0.3},
                {"text": "ム", "consonant": "m", "consonant_length": 0.1, "vowel": "u", "vowel_length": 0.3},
            ], "accent": 1, "pause_mora": None, "is_interrogative": False}],
            "speedScale": 1.0, "prePhonemeLength": 0.1, "postPhonemeLength": 0.1,
            "pauseLength": None, "pauseLengthScale": 1.0,
        }

    def synthesis(self, query, speaker):
        rate = 24000
        n = round(1.0 / query["speedScale"] * rate)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(
                int(12000 * math.sin(i / 5) if rate * 0.1 < i < n - rate * 0.1 else 0).to_bytes(2, "little", signed=True)
                for i in range(n)))
        return buf.getvalue()


def test_parse_script():
    lines = parse_script("# コメント\nずんだもん: こんにちは\n\n四国めたん：はい: そうよ\n", {"ずんだもん", "四国めたん"})
    assert [(l.index, l.character, l.text) for l in lines] == [(1, "ずんだもん", "こんにちは"), (2, "四国めたん", "はい: そうよ")]
    with pytest.raises(ValueError, match="2 行目"):
        parse_script("a: x\nセリフだけ", None)
    with pytest.raises(ValueError, match="設定にありません"):
        parse_script("だれか: x", {"ずんだもん"})


def test_load_characters(tmp_path: Path, char_dir: Path):
    toml = tmp_path / "chars.toml"
    toml.write_text('''
["テスト"]
folder = "chars/テスト"
speaker = 3
speed = 1.2
mouth = "volume"

["テスト".parts]
"目" = "目"
"他" = "腕"
''', encoding="utf-8")
    cfg = load_characters(toml)["テスト"]
    assert cfg.folder == char_dir.resolve()
    assert cfg.speaker == 3 and cfg.mouth == "volume"
    assert cfg.voice == {"speedScale": 1.2}
    assert cfg.expression.eye == "目" and cfg.expression.others == ["腕"]


@pytest.mark.parametrize("data,match", [
    ({"a": {}}, "speaker"),
    ({"a": {"folder": "x", "speaker": 1, "mouth": "lip"}}, "mouth"),
    ({"a": {"folder": "x", "speaker": 1, "parts": {"耳": "x"}}}, "耳"),
])
def test_config_errors(data, match):
    with pytest.raises(ValueError, match=match):
        parse_characters(data, Path("."))


def test_resolve_mouth_mode(char_dir: Path):
    mouth = load_character(char_dir).part("口", "口")
    assert resolve_mouth_mode("auto", mouth) == "volume"
    assert resolve_mouth_mode("vowel", mouth) == "vowel"
    assert resolve_mouth_mode("auto", None) == "static"


@pytest.mark.parametrize("mouth", ["volume", "vowel"])
def test_render_script(tmp_path: Path, char_dir: Path, mouth: str):
    configs = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 7, "mouth": mouth, "speed": 2.0}},
                               tmp_path)
    lines = parse_script("テスト: かむ\nテスト: もう一回", set(configs))
    client = FakeClient()
    out = tmp_path / "out"
    manifest = render_script(lines, configs, out, client=client, fps=10, idle_seconds=3)

    assert client.calls == [("かむ", 7), ("もう一回", 7)]
    assert manifest["fps"] == 10 and manifest["size"] == [8, 8]
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest
    line, _, idle = manifest["clips"]
    assert line["kind"] == "line" and line["duration"] == pytest.approx(0.5)  # speed 2.0 で半分
    assert line["frames"] == 5 and (out / line["wav"]).is_file()
    frames = sorted((out / line["frames_dir"]).glob("*.png"))
    assert len(frames) == 5
    # 最初のフレームは無音なので口は閉じ（.0）、途中で開く
    mouth_colors = [Image.open(f).getpixel((0, 5))[:3] for f in frames]
    assert mouth_colors[0] == (0, 0, 20)
    assert any(c != (0, 0, 20) for c in mouth_colors)
    assert idle["kind"] == "idle" and idle["frames"] == 30
    assert {Image.open(f).getpixel((0, 5))[:3] for f in (out / idle["frames_dir"]).glob("*.png")} == {(0, 0, 20)}


def test_render_scale(tmp_path: Path, char_dir: Path):
    configs = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)
    manifest = render_script(parse_script("テスト: あ", set(configs)), configs, tmp_path / "o",
                             client=FakeClient(), fps=10, scale=0.5)
    assert manifest["size"] == [4, 4]


def test_dump_characters_roundtrip(tmp_path: Path, char_dir: Path):
    data = {
        "テスト": {
            "folder": str(char_dir), "speaker": 3, "mouth": "vowel", "speed": 1.25,
            "parts": {"目": "目", "他": ["腕 \"左\"", "記号"]},
            "expressions": {"困り": {"眉": "困り", "他": ["汗"]}, "空": {}},
            "layout": {"position": -0.3, "height": 0.8, "channel": 4},
            "subtitle": {"font": "C:\\Fonts\\a.ttf", "size": 60.0, "color": [1.0, 0.5, 0.0, 1.0]},
        },
        "zunda": {"folder": str(char_dir), "speaker": 1},
    }
    configs = parse_characters(data, tmp_path)
    toml = tmp_path / "out.toml"
    toml.write_text(dump_characters(configs, base=tmp_path), encoding="utf-8")
    assert 'folder = "chars/テスト"' in toml.read_text(encoding="utf-8")
    again = load_characters(toml)
    assert again == configs


def test_parse_script_expression(tmp_path: Path, char_dir: Path):
    configs = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1,
                                         "expressions": {"困り": {"目": "目"}}}}, tmp_path)
    lines = parse_script("テスト(困り): あ\nテスト（困り）：い\nテスト [困り] : う\nテスト: (笑)え", configs)
    assert [(l.expression, l.text) for l in lines] == [("困り", "あ"), ("困り", "い"), ("困り", "う"), (None, "(笑)え")]
    with pytest.raises(ValueError, match="表情 '怒り'"):
        parse_script("テスト(怒り): あ", configs)


def test_expression_for(tmp_path: Path):
    cfg = parse_characters({"a": {"folder": "x", "speaker": 1,
                                  "parts": {"目": "普通", "口": "閉じ", "他": ["右腕", "左腕"]},
                                  "expressions": {"困り": {"目": "ジト目", "他": "汗"}}}}, tmp_path)["a"]
    assert cfg.expression_for(None) == cfg.expression
    e = cfg.expression_for("困り")
    assert (e.eye, e.mouth, e.others) == ("ジト目", "閉じ", ["汗", "左腕"])  # 他はスロットごと
    with pytest.raises(ValueError):
        cfg.expression_for("怒り")
    with pytest.raises(ValueError, match="耳"):
        parse_characters({"a": {"folder": "x", "speaker": 1, "expressions": {"x": {"耳": "y"}}}}, tmp_path)


def test_render_script_with_expression(tmp_path: Path, char_dir: Path):
    _png(char_dir / "目" / "閉じ目.png", (99, 0, 0, 255), (0, 2, 8, 3))
    configs = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1,
                                         "expressions": {"眠い": {"目": "閉じ目"}}}}, tmp_path)
    out = tmp_path / "o"
    manifest = render_script(parse_script("テスト: あ\nテスト(眠い): い", configs), configs, out,
                             client=FakeClient(), fps=10)
    first, second = manifest["clips"]
    assert "expression" not in first["extra"] and second["extra"]["expression"] == "眠い"
    eye = lambda clip: Image.open(next((out / clip["frames_dir"]).glob("*.png"))).getpixel((0, 2))[0]
    assert eye(first) in (10, 11, 12) and eye(second) == 99


def test_merge_tables():
    material = {"speaker": 3, "mouth": "vowel", "parts": {"目": "a", "口": "b"}, "layout": {"position": 0.3},
                "expressions": {"困り": {"眉": "x", "目": "y"}, "笑い": {"口": "z"}}}
    prefs = {"speaker": 8, "parts": {"目": "c"}, "expressions": {"困り": {"眉": "w"}}}
    m = merge_tables(material, prefs)
    assert m["speaker"] == 8 and m["mouth"] == "vowel"
    assert m["parts"] == {"目": "c", "口": "b"} and m["layout"] == {"position": 0.3}
    # 表情プリセットは名前単位で置き換える（混ぜない）
    assert m["expressions"] == {"困り": {"眉": "w"}, "笑い": {"口": "z"}}
    assert material["parts"] == {"目": "a", "口": "b"}  # 元は変えない
    with pytest.raises(ValueError, match="parts"):
        merge_tables(material, {"parts": "目"})


def test_dump_raw_table_keeps_partial_table():
    import tomllib

    # 手で書いた一部だけの既定値は、項目を足さず（speaker / mouth を書かず）そのまま書き戻す
    table = {"name": "て", "layout": {"flip": True}, "expressions": {"困 り": {"他": ["a", "b"]}},
             "parts": {"髪": "x"}}
    text = dump_raw_table(table, "ずんだもん2")
    assert tomllib.loads(text) == {"ずんだもん2": table}
    assert "speaker" not in text and "mouth" not in text


def test_dump_under_parent_table():
    import tomllib

    # 立ち絵の無いキャラの初期値は [親."キャラ名"] に書く。パーツが空なら parts を書かない
    table = {"speaker": 8, "subtitle": {"size": 40}}
    assert tomllib.loads(dump_raw_table(table, "春日部 つむぎ", parent="立ち絵なし")) == {
        "立ち絵なし": {"春日部 つむぎ": table}}
    cfg = parse_characters({"語り手": {"speaker": 8, "layout": {"lines": "AUDIO"}}}, Path("."))["語り手"]
    data = tomllib.loads(dump_table(cfg, key="語り手", parent="立ち絵なし"))
    assert set(data) == {"立ち絵なし"} and data["立ち絵なし"]["語り手"]["speaker"] == 8
    assert "parts" not in data["立ち絵なし"]["語り手"] and data["立ち絵なし"]["語り手"]["layout"] == {"lines": "AUDIO"}


def test_material_table_fills_missing(tmp_path: Path, char_dir: Path):
    (char_dir / "tachie.toml").write_text(
        'name = "てすと"\nspeaker = 8\nspeed = 1.2\n\n[parts]\n"目" = "目"\n\n[layout]\nposition = -0.2\nchannel = 5\n',
        encoding="utf-8")
    cfg = parse_characters({"テスト": {"folder": str(char_dir), "layout": {"channel": 6}}}, tmp_path)["テスト"]
    assert cfg.speaker == 8 and cfg.voice == {"speedScale": 1.2} and cfg.expression.eye == "目"
    assert cfg.layout == {"position": -0.2, "channel": 6}  # キャラ設定の方が優先
    plain = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path, use_material=False)
    assert plain["テスト"].expression.eye is None


def test_material_table_errors(tmp_path: Path, char_dir: Path):
    assert load_material_table(char_dir) == {}
    (char_dir / "tachie.toml").write_text("speaker = \n", encoding="utf-8")
    with pytest.raises(ValueError, match="tachie.toml"):
        load_material_table(char_dir)


def test_dump_table_roundtrip(tmp_path: Path, char_dir: Path):
    data = {"テスト": {"folder": str(char_dir), "speaker": 3, "pitch": 0.05, "parts": {"目": "目", "他": ["腕"]},
                     "expressions": {"困り": {"眉": "困り"}}, "layout": {"position": -0.3, "flip": True},
                     "subtitle": {"size": 60.0}}}
    cfg = parse_characters(data, tmp_path)["テスト"]
    text = dump_table(cfg, name="てすと")
    assert "folder" not in text and text.startswith('name = "てすと"')
    (char_dir / "tachie.toml").write_text(text, encoding="utf-8")
    assert load_material_table(char_dir)["name"] == "てすと"
    assert parse_characters({"テスト": {"folder": str(char_dir)}}, tmp_path)["テスト"] == cfg
    # アドオンの既定値はキャラ名のテーブルつき
    import tomllib
    keyed = tomllib.loads(dump_table(cfg, key="テスト"))
    assert keyed["テスト"]["layout"]["flip"] is True


def test_empty_parts_override_material(tmp_path: Path, char_dir: Path):
    (char_dir / "tachie.toml").write_text('speaker = 8\n\n[parts]\n"目" = "目"\n"他" = ["腕"]\n', encoding="utf-8")
    # 空にしたキャラを書き出すと、空も明示されて tachie.toml の値で補われない
    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path, use_material=False)["テスト"]
    text = dump_table(cfg)
    assert '"目" = ""' in text and '"他" = []' in text
    import tomllib
    merged = merge_tables(load_material_table(char_dir), tomllib.loads(text))
    again = parse_characters({"テスト": {**merged, "folder": str(char_dir)}}, tmp_path, use_material=False)["テスト"]
    assert again.expression.eye is None and again.expression.others == [] and again.speaker == 1


def test_render_line_voice_override(tmp_path: Path, char_dir: Path):
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1, "speed": 1.2, "pause": 0.5}},
                           tmp_path)["テスト"]
    assert cfg.voice == {"speedScale": 1.2, "pauseLengthScale": 0.5}
    character = load_character(char_dir)
    client = FakeClient()
    queries = []
    synth = client.synthesis
    client.synthesis = lambda q, s: (queries.append((dict(q), s)), synth(q, s))[1]
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    # キャラの既定値のまま
    clip = render_line(client, cfg, character, Line(1, "テスト", "あ"), tmp_path / "o", fps=10, cache=cache)
    assert queries[-1][1] == 1 and queries[-1][0]["speedScale"] == 1.2 and clip.speaker == 1
    # セリフで上書き
    clip2 = render_line(client, cfg, character, Line(2, "テスト", "い", speaker=7, voice={"speedScale": 2.0}),
                        tmp_path / "o", fps=10, cache=cache)
    assert client.calls[-1] == ("い", 7) and queries[-1][1] == 7 and clip2.speaker == 7
    assert queries[-1][0]["speedScale"] == 2.0 and queries[-1][0]["pauseLengthScale"] == 0.5
    assert clip2.frames < clip.frames
    assert "pause = 0.5" in dump_table(cfg)


def test_split_kana():
    from tachie_studio.pipeline.script import split_kana

    assert split_kana("今日はいい天気{キョ'ウワ/イ'イ/テ'ンキ}") == ("今日はいい天気", "キョ'ウワ/イ'イ/テ'ンキ")
    assert split_kana("天気 ｛テ'ンキ｝ ") == ("天気", "テ'ンキ")
    assert split_kana("{だけ}") == ("{だけ}", None)  # セリフが無い
    assert split_kana("空{ }") == ("空{ }", None)
    assert split_kana("途中{x}です") == ("途中{x}です", None)
    lines = parse_script("a: 天気{テ'ンキ}\na(困り): 雨")
    assert (lines[0].text, lines[0].kana) == ("天気", "テ'ンキ") and lines[1].kana is None


def test_render_line_kana(tmp_path: Path, char_dir: Path):
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    client = FakeClient()
    base_query = client.audio_query
    client.audio_query = lambda t, s: {**base_query(t, s), "kana": "カ'ム"}
    asked = []

    def accent_phrases(text, speaker, is_kana=False):
        asked.append((text, speaker, is_kana))
        return [{"moras": [{"text": "ム", "consonant": "m", "consonant_length": 0.1, "vowel": "u", "vowel_length": 0.2}],
                 "accent": 1, "pause_mora": None, "is_interrogative": False}]

    client.accent_phrases = accent_phrases
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    clip = render_line(client, cfg, character, Line(1, "テスト", "かむ"), tmp_path / "o", fps=10, cache=cache)
    assert not asked and clip.extra["kana"] == clip.extra["kana_auto"] == "カ'ム"
    clip = render_line(client, cfg, character, Line(2, "テスト", "かむ", kana="ム'"), tmp_path / "o", fps=10, cache=cache)
    assert asked == [("ム'", 1, True)] and clip.extra["kana"] == "ム'" and clip.extra["kana_auto"] == "カ'ム"


def test_render_line_edited_accent(tmp_path: Path, char_dir: Path):
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    client = FakeClient()
    client.accent_phrases = lambda *a, **k: (_ for _ in ()).throw(AssertionError("呼ばない"))
    sent = []
    synth = client.synthesis
    client.synthesis = lambda q, s: (sent.append(q), synth(q, s))[1]
    edited = [{"moras": [{"text": "カ", "consonant": "k", "consonant_length": 0.3, "vowel": "a", "vowel_length": 0.5,
                          "pitch": 6.1}], "accent": 1, "pause_mora": None, "is_interrogative": False}]
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    render_line(client, cfg, character, Line(1, "テスト", "かむ", kana="カ'", accent_phrases=edited),
                tmp_path / "o", fps=10, cache=cache)
    assert sent[-1]["accent_phrases"] == edited and sent[-1]["kana"] == "カ'"
    # 編集したときの読みが今の自動の読み（ユーザー辞書などで変わる）と違えば使わない
    client.audio_query = lambda t, s, base=client.audio_query: {**base(t, s), "kana": "カ'ム"}
    clip = render_line(client, cfg, character, Line(2, "テスト", "かむ", accent_phrases=edited, accent_kana="カム'"),
                       tmp_path / "o", fps=10, cache=cache)
    assert sent[-1]["accent_phrases"] != edited and "accent" not in clip.extra
    clip = render_line(client, cfg, character, Line(3, "テスト", "かむ", accent_phrases=edited, accent_kana="カ'ム"),
                       tmp_path / "o", fps=10, cache=cache)
    assert sent[-1]["accent_phrases"] == edited and clip.extra["accent"]


def test_render_line_after_and_fade(tmp_path: Path, char_dir: Path):
    from tachie_studio.lipsync import read_wav
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    out = tmp_path / "o"
    plain = render_line(FakeClient(), cfg, character, Line(1, "テスト", "あ"), out, fps=10, cache=cache, stem="p")
    longer = render_line(FakeClient(), cfg, character, Line(2, "テスト", "あ", after=0.5, fade_out=0.3), out,
                         fps=10, cache=cache, stem="l")
    assert longer.frames == plain.frames + 5 and longer.extra["after_frames"] == 5
    assert longer.duration == plain.duration  # 音声の長さは変わらない
    assert len(list((out / "l").iterdir())) == longer.frames
    # 余韻の間は口を閉じる（口の .0 = 閉じは青 20）
    last = Image.open(sorted((out / "l").iterdir())[-1]).getpixel((0, 5))
    assert last[2] == 20
    assert read_wav(out / "l.wav").samples[-1] == 0



def test_render_line_without_tachie(tmp_path: Path, char_dir: Path):
    """立ち絵を出さないセリフ（cache=None）は wav と json だけ書き、長さは立ち絵ありと同じ。"""
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    out = tmp_path / "o"
    full = render_line(FakeClient(), cfg, character, Line(1, "テスト", "あ", after=0.3), out, fps=10, cache=cache,
                       stem="f")
    voice = parse_characters({"声": {"speaker": 1}}, tmp_path)["声"]
    bare = render_line(FakeClient(), voice, None, Line(1, "声", "あ", after=0.3), out, fps=10, cache=None, stem="v")
    assert voice.folder is None and bare.frames == full.frames and bare.extra["mouth_mode"] == ""
    assert (out / "v.wav").is_file() and (out / "v.json").is_file() and not (out / "v").exists()


def test_characters_without_folder(tmp_path: Path):
    """folder の無いキャラ（立ち絵なし）は書き出しでも folder を書かず、CLI では使えない。"""
    import tomllib

    from tachie_studio.pipeline.render import render_script
    from tachie_studio.pipeline.script import Line

    configs = parse_characters({"ナレーション": {"speaker": 2, "layout": {"lines": "AUDIO"}}}, tmp_path)
    text = dump_characters(configs, base=tmp_path)
    assert "folder" not in text
    again = parse_characters(tomllib.loads(text), tmp_path)["ナレーション"]
    assert again.folder is None and again.speaker == 2 and again.layout == {"lines": "AUDIO"}
    with pytest.raises(ValueError, match="立ち絵の無いキャラクター"):
        render_script([Line(1, "ナレーション", "あ")], configs, tmp_path / "o", client=FakeClient())


def test_expression_for_slots(tmp_path: Path):
    from tachie_studio.pipeline.config import merge_slots

    assert merge_slots(["A", "B"], ["", "X"]) == ["A", "X"]
    assert merge_slots(["A", "B"], ["X"]) == ["X", "B"]
    assert merge_slots(["A"], ["", "", "Z"]) == ["A", "", "Z"]
    assert merge_slots([], ["", ""]) == []
    cfg = parse_characters({"a": {"folder": "x", "speaker": 1, "parts": {"他": ["右腕", "左腕"], "後": ["影"]},
                                  "expressions": {"指差し": {"他": ["", "指差し"]}, "消す": {"後": ["_"]}}}},
                           tmp_path, use_material=False)["a"]
    assert cfg.expression_for("指差し").others == ["右腕", "指差し"]
    assert cfg.expression_for("消す").backs == ["_"] and cfg.expression_for("消す").others == ["右腕", "左腕"]
    # 空きスロットを含む基本の表情も往復する
    cfg2 = parse_characters({"a": {"folder": "x", "speaker": 1, "parts": {"他": ["", "左腕"]},
                                   "expressions": {"p": {"他": ["", "x"]}}}}, tmp_path, use_material=False)["a"]
    assert cfg2.expression.others == ["", "左腕"]
    again = parse_characters(__import__("tomllib").loads(dump_characters({"a": cfg2})), tmp_path, use_material=False)
    assert again["a"].expression == cfg2.expression and again["a"].expressions == cfg2.expressions


def test_material_presets_fill(tmp_path: Path, char_dir: Path):
    (char_dir / "preset.ini").write_text("[眠い]\n目=目.png\n\n[困り]\n目=目.png\n", encoding="utf-8")
    (char_dir / "tachie.toml").write_text('speaker = 2\n\n[expressions."困り"]\n"口" = "口"\n', encoding="utf-8")
    cfg = parse_characters({"テスト": {"folder": str(char_dir)}}, tmp_path)["テスト"]
    assert cfg.expressions == {"眠い": {"目": "目"}, "困り": {"口": "口"}}  # 同じ名前は tachie.toml


def test_line_motion_relayout(tmp_path: Path, char_dir: Path):
    from tachie_studio.pipeline.render import FrameCache, LineMotion, motion_frames, render_line, segment_frames
    from tachie_studio.pipeline.script import Line
    from tachie_studio.tachie import Expression

    _png(char_dir / "口" / "む.png", (0, 0, 90, 255), (0, 5, 8, 6))  # 連番の無い口（動かない）
    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1, "audio": {"after": 0.3}}},
                           tmp_path)["テスト"]
    character = load_character(char_dir)
    base = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    other = FrameCache(character, Expression(mouth="む"), tmp_path / "cache", 1.0)
    out = tmp_path / "o"
    clip = render_line(FakeClient(), cfg, character, Line(1, "テスト", "かむ"), out, fps=10, cache=base, stem="x")
    motion = LineMotion.load(out / "x.json")
    assert motion.frames == clip.frames == motion.voice_frames + 3
    assert len(motion.vowels) == len(motion.rms) == motion.voice_frames
    # 保存した動きから並べ直すと、書き出した連番と同じ画像になる
    written = [p.stat().st_ino for p in sorted((out / "x").iterdir())]
    assert [p.stat().st_ino for p in motion_frames(motion, base)] == written
    # 区間だけ別の表情に
    frames = segment_frames(motion, [(0, motion.frames, base), (4, 7, other)])
    assert len(frames) == motion.frames
    assert [f.parent for f in frames] == [base.directory] * 4 + [other.directory] * 3 + \
        [base.directory] * (motion.frames - 7)
    assert all(f.name.endswith("_l0.png") for f in frames[4:7])  # 連番の無い口は開き = 0 番


def test_levels_from_rms_matches_volume_track():
    from tachie_studio.lipsync import read_wav
    from tachie_studio.lipsync.mouth import levels_from_rms, volume_track
    from tachie_studio.lipsync.wav import frame_rms

    wav = read_wav(FakeClient().synthesis({"speedScale": 1.0}, 1))
    assert levels_from_rms(frame_rms(wav, 10), 4) == volume_track(wav, 10, 4)


def test_simple_material_lipsync(tmp_path: Path):
    from tachie_studio.pipeline.render import FrameCache, render_line, resolve_mouth_mode, _mouth_part
    from tachie_studio.pipeline.script import Line

    d = tmp_path / "シンプル"
    _png(d / "デフォルト.png", (10, 0, 0, 255))
    _png(d / "口開き.png", (20, 0, 0, 255))
    cfg = parse_characters({"s": {"folder": str(d), "speaker": 1}}, tmp_path)["s"]
    character = load_character(d)
    assert resolve_mouth_mode("auto", _mouth_part(character, cfg.expression)) == "volume"
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    out = tmp_path / "o"
    clip = render_line(FakeClient(), cfg, character, Line(1, "s", "あ"), out, fps=10, cache=cache, stem="x")
    reds = [Image.open(p).getpixel((0, 0))[0] for p in sorted((out / "x").iterdir())]
    assert len(reds) == clip.frames and reds[0] == 10 and reds[-1] == 10 and 20 in reds  # 無音は閉じ、声で開く


def test_engine_per_character(tmp_path: Path, char_dir: Path, monkeypatch):
    import tomllib

    from tachie_studio.pipeline import render as render_mod

    data = {"a": {"folder": str(char_dir), "speaker": 1, "engine": "http://127.0.0.1:10101"},
            "b": {"folder": str(char_dir), "speaker": 2}}
    configs = parse_characters(data, tmp_path)
    assert configs["a"].engine == "http://127.0.0.1:10101" and configs["b"].engine == ""
    again = parse_characters(tomllib.loads(dump_characters(configs)), tmp_path)
    assert again == configs
    # 書き出しではキャラごとのエンジンを使う（既定は渡したクライアント）
    made = {}

    class Engine(FakeClient):
        def __init__(self, url, timeout=60.0):
            super().__init__()
            made[url] = self

    monkeypatch.setattr(render_mod, "VoicevoxClient", Engine)
    default = FakeClient()
    render_script(parse_script("a: あ\nb: い\na: う"), configs, tmp_path / "o", client=default, fps=10)
    assert [t for t, _ in made["http://127.0.0.1:10101"].calls] == ["あ", "う"]
    assert [t for t, _ in default.calls] == ["い"]


def test_engine_without_mora_lengths_uses_volume(tmp_path: Path, char_dir: Path):
    """AivisSpeech のようにモーラの長さが常に 0 のエンジンでは、音量式で口パクする。"""
    from tachie_studio.pipeline.render import FrameCache, LineMotion, render_line
    from tachie_studio.pipeline.script import Line

    client = FakeClient()
    base = client.audio_query

    def dummy_query(text, speaker):
        q = base(text, speaker)
        for ap in q["accent_phrases"]:
            for m in ap["moras"]:
                m["consonant_length"] = m["vowel_length"] = 0.0
        return q

    client.audio_query = dummy_query
    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": -12345, "mouth": "vowel"}},
                           tmp_path)["テスト"]
    character = load_character(char_dir)
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    clip = render_line(client, cfg, character, Line(1, "テスト", "あ"), tmp_path / "o", fps=10, cache=cache, stem="x")
    motion = LineMotion.load(tmp_path / "o" / "x.json")
    assert motion.mouth == "volume" and clip.extra["mouth_mode"] == "volume" and clip.speaker == -12345
    assert len({Image.open(p).getpixel((0, 5)) for p in (tmp_path / "o" / "x").iterdir()}) > 1  # 口が動く


def test_parse_script_ymm4_text():
    text = ("# YMM4 の台本\n"
            "ずんだもん「こんにちは、ずんだもんなのだ。」\n"
            "四国めたん(困り)「時刻は 12:00 よ。\\「括弧\\」も書けるわ」\n"
            "ずんだもん「1 行目\n"
            "2 行目なのだ」\n"
            "四国めたん: 今までの形も使えるわ\n")
    lines = parse_script(text, {"ずんだもん", "四国めたん"})
    assert [(x.character, x.expression, x.text) for x in lines] == [
        ("ずんだもん", None, "こんにちは、ずんだもんなのだ。"),
        ("四国めたん", "困り", "時刻は 12:00 よ。「括弧」も書けるわ"),
        ("ずんだもん", None, "1 行目\n2 行目なのだ"),
        ("四国めたん", None, "今までの形も使えるわ"),
    ]
    with pytest.raises(ValueError, match="1 行目: 「」が閉じていません"):
        parse_script("ずんだもん「閉じない\nまま")
    with pytest.raises(ValueError, match="後ろに文字"):
        parse_script("ずんだもん「途中」で閉じる」")


def test_parse_script_csv_tsv(tmp_path: Path):
    from tachie_studio.pipeline import load_script

    csv_text = ('ずんだもん,こんにちは\n'
                '# コメント\n'
                '\n'
                '四国めたん(困り),"カンマ, と ""引用符"" と\n改行"\n'
                'ずんだもん,天気{テ\'ンキ},使わない列\n')
    lines = parse_script(csv_text, {"ずんだもん", "四国めたん"}, fmt="csv")
    assert [(x.character, x.expression, x.text, x.kana) for x in lines] == [
        ("ずんだもん", None, "こんにちは", None),
        ("四国めたん", "困り", 'カンマ, と "引用符" と\n改行', None),
        ("ずんだもん", None, "天気", "テ'ンキ"),
    ]
    # 拡張子で形を選び、Shift_JIS（Excel の CSV）も読める
    (tmp_path / "a.csv").write_bytes("ずんだもん,ずんだ餅\n".encode("cp932"))
    assert [x.text for x in load_script(tmp_path / "a.csv")] == ["ずんだ餅"]
    (tmp_path / "a.tsv").write_text("﻿ずんだもん\tタブ, 区切り\n", encoding="utf-8")
    assert [x.text for x in load_script(tmp_path / "a.tsv")] == ["タブ, 区切り"]
    (tmp_path / "a.txt").write_text("ずんだもん「テキスト」\n", encoding="utf-8")
    assert [x.text for x in load_script(tmp_path / "a.txt")] == ["テキスト"]
    with pytest.raises(ValueError, match="2 行目"):
        parse_script("ずんだもん,あ\nセリフだけ\n", fmt="csv")
    with pytest.raises(ValueError, match="キャラクター 'だれか'"):
        parse_script("だれか,あ\n", {"ずんだもん"}, fmt="csv")
    with pytest.raises(ValueError, match="3 行目: キャラクター 'だれか'"):
        parse_script('ずんだもん,"1\n2"\nだれか,あ\n', {"ずんだもん"}, fmt="csv")


def test_parse_script_crlf_empty_and_escapes(tmp_path: Path):
    from tachie_studio.pipeline import load_script

    # CRLF の CSV の「"」の中の改行は \n に、\「 \」 \\ 以外のバックスラッシュは残す
    assert [x.text for x in parse_script('ずんだもん,"1\r\n2"\r\n', fmt="csv")] == ["1\n2"]
    assert [x.text for x in parse_script("ずんだもん「C:\\tmp \\「あ\\」」")] == ["C:\\tmp 「あ」"]
    with pytest.raises(ValueError, match="1 行目: セリフがありません"):
        parse_script("ずんだもん「 」")
    (tmp_path / "u.tsv").write_text("ずんだもん\tユニコード\n", encoding="utf-16")
    assert [x.text for x in load_script(tmp_path / "u.tsv")] == ["ユニコード"]


def test_render_line_uses_given_wav(tmp_path: Path, char_dir: Path):
    """合成済みの音声（YMM4 が保存した音声）があれば合成しない。長さはその音声から。"""
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    client = FakeClient()
    client.synthesis = lambda q, s: pytest.fail("合成した")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x10" * 36000)  # 1.5 秒
    cache = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    clip = render_line(client, cfg, character, Line(1, "テスト", "あ", wav=buf.getvalue()), tmp_path / "o",
                       fps=10, cache=cache)
    assert clip.frames == 15 and abs(clip.duration - 1.5) < 1e-6
    assert (tmp_path / "o" / clip.wav).read_bytes()[:4] == b"RIFF"


def test_still_frames(tmp_path: Path, char_dir: Path):
    """立ち絵は重なるセリフの間だけ口パクし、表情は 立ち絵 < セリフ < 表情ストリップ。"""
    from tachie_studio.pipeline.render import (FrameCache, LineMotion, blink_segment_frames, mouth_states,
                                               render_line, still_frames)
    from tachie_studio.pipeline.script import Line
    from tachie_studio.tachie import Expression

    _png(char_dir / "口" / "む.png", (0, 0, 90, 255), (0, 5, 8, 6))
    cfg = parse_characters({"テスト": {"folder": str(char_dir), "speaker": 1}}, tmp_path)["テスト"]
    character = load_character(char_dir)
    base = FrameCache(character, cfg.expression, tmp_path / "cache", 1.0)
    talk = FrameCache(character, cfg.expression, tmp_path / "cache2", 1.0)  # 別の表情のつもり（置き場所で区別）
    face = FrameCache(character, Expression(mouth="む"), tmp_path / "cache3", 1.0)
    out = tmp_path / "o"
    render_line(FakeClient(), cfg, character, Line(1, "テスト", "かむ"), out, fps=10, cache=None, stem="x")
    motion = LineMotion.load(out / "x.json")
    n = motion.frames + 20
    # セリフが無ければ blink_segment_frames と同じ
    assert still_frames(base, [], [], n, 10, seed="s") == blink_segment_frames([(0, n, base)], n, 10, "s")
    frames = still_frames(base, [], [(5, motion, talk)], n, 10, seed="s")
    plain = blink_segment_frames([(0, n, base)], n, 10, "s")
    inside = range(5, 5 + motion.frames)
    assert all(frames[j] == plain[j] for j in range(n) if j not in inside)  # 外は閉じた口・同じまばたき
    assert all(frames[j].parent == talk.directory for j in inside)  # セリフの間はセリフの表情
    states = mouth_states(motion, character, talk.expression)[1]
    assert [frames[j].name.split("_", 1)[1] for j in inside] == \
        [f"{k[0]}{v}.png" for k, v in states]
    assert len({frames[j].name.split("_", 1)[1] for j in inside}) > 1  # 口が動く
    # 表情ストリップはセリフより優先
    frames = still_frames(base, [(6, 8, face)], [(5, motion, talk)], n, 10, seed="s")
    assert [frames[j].parent for j in (5, 6, 7, 8)] == [talk.directory, face.directory, face.directory,
                                                         talk.directory]
    # 重なるセリフは後から始まるものが口を持つ（始まりの順に関係なく）
    frames = still_frames(base, [], [(8, motion, base), (5, motion, talk)], n, 10, seed="s")
    assert frames[7].parent == talk.directory and frames[8].parent == base.directory
    first = mouth_states(motion, character, base.expression)[1][0]
    assert frames[8].name.split("_", 1)[1] == f"{first[0][0]}{first[1]}.png"
    # 立ち絵の外にはみ出したセリフは立ち絵の区間で切る
    frames = still_frames(base, [], [(-2, motion, talk)], 4, 10, seed="s")
    states = mouth_states(motion, character, talk.expression)[1]
    assert [f.name.split("_", 1)[1] for f in frames] == [f"{k[0]}{v}.png" for k, v in states[2:6]]


def test_overlay_parts():
    """表情に、変えるパーツだけを重ねる（立ち絵ごとのパーツ）。他・後はスロットごと。"""
    from tachie_studio.pipeline.config import overlay_parts
    from tachie_studio.tachie import Expression

    base = Expression(eye="普通", mouth="むふ", others=["左腕", "右腕"])
    out = overlay_parts(base, {"目": "ジト目", "他": ["", "右腕_指差し"]})
    assert out.eye == "ジト目" and out.mouth == "むふ" and out.others == ["左腕", "右腕_指差し"]
    assert overlay_parts(base, {}) == base


def test_merge_parts_and_layers():
    """パーツの表を重ねる: スロットごと、空は元のまま。PSD のレイヤーは重ねたものを後ろへ（3 段でも上の段の選択が勝つ）。"""
    from tachie_studio.pipeline.config import LAYERS_KEY, merge_layers, merge_parts

    assert merge_parts({"目": "a", "他": ["x", "y"]}, {"目": "b", "眉": "", "他": ["", "z"]}) == {"目": "b", "他": ["x", "z"]}
    assert merge_parts({}, {"他": ["", ""]}) == {}
    assert list(merge_layers({"X/*a": True, "Y": True}, {"X/*b": True})) == ["X/*a", "Y", "X/*b"]
    stacked = merge_layers(merge_layers({"X/*a": True}, {"X/*b": True}), {"X/*a": True})
    assert list(stacked)[-1] == "X/*a"  # 3 段目で選び直した a が最後（選ばれる）
    assert merge_parts({LAYERS_KEY: {"X/*a": True}}, {LAYERS_KEY: {"X/*b": True}})[LAYERS_KEY] == {"X/*a": True, "X/*b": True}
