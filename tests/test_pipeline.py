import io
import json
import math
import wave
from pathlib import Path

import pytest
from PIL import Image

from tachie_studio.pipeline import load_characters, parse_script, render_script
from tachie_studio.pipeline.config import dump_characters, parse_characters
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
