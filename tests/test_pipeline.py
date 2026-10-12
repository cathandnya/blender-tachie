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
    ({"a": {"speaker": 1}}, "folder"),
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
    assert (e.eye, e.mouth, e.others) == ("ジト目", "閉じ", ["汗"])
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
