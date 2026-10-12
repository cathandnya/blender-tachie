import json
import struct
from pathlib import Path

import pytest
from PIL import Image

from tachie_studio.tachie import load_character
from tachie_studio.tachie.psd import read_psd
from tachie_studio.tachie.psd_animation import animations_from_material, config_path, describe, write_config

ROOT = Path(__file__).resolve().parent.parent


def _record(name: str, section: int = 0, hidden: bool = False) -> bytes:
    """画素の無いレイヤー記録（名前は cp932 と luni の両方に入れる）。"""
    raw = name.encode("cp932")
    pascal = bytes([len(raw)]) + raw
    pascal += b"\0" * (-len(pascal) % 4)
    extra = b"\0\0\0\0" + b"\0\0\0\0" + pascal
    luni = struct.pack(">I", len(name)) + name.encode("utf-16-be")
    extra += b"8BIM" + b"luni" + struct.pack(">I", len(luni)) + luni
    if section:
        extra += b"8BIM" + b"lsct" + struct.pack(">I", 4) + struct.pack(">I", section)
    rec = struct.pack(">iiii", 0, 0, 10, 10) + struct.pack(">H", 0)
    rec += b"8BIM" + b"norm" + bytes([255, 0, 2 if hidden else 0, 0])
    return rec + struct.pack(">I", len(extra)) + extra


def make_psd(path: Path, layers: list) -> None:
    """`layers` は上から: 名前（レイヤー）か (名前, [子…]) / (名前, [子…], True)（非表示のフォルダ）。"""
    records: list[bytes] = []

    def add(items):
        # 記録は下から並ぶので、下の要素から
        for item in reversed(items):
            if isinstance(item, tuple):
                records.append(_record("</Layer group>", 3))
                add(item[1])
                records.append(_record(item[0], 1, hidden=len(item) > 2 and item[2]))
            else:
                records.append(_record(item))

    add(layers)
    layer_info = struct.pack(">h", len(records)) + b"".join(records)
    layer_info += b"\0" * (len(layer_info) % 2)
    lm = struct.pack(">I", len(layer_info)) + layer_info + struct.pack(">I", 0)
    header = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, 20, 10, 8, 3)
    body = struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">I", len(lm)) + lm
    path.write_bytes(header + body + struct.pack(">H", 0))


def test_read_psd_tree(tmp_path: Path):
    psd = tmp_path / "t.psd"
    make_psd(psd, ["記号", ("!目", [("*目セット", ["*普通目", "*白目"]), "*UU"]), ("!口", ["*お", "*むふ"]), "体"])
    doc = read_psd(psd)
    assert (doc.width, doc.height) == (10, 20)
    assert [c.name for c in doc.root.children] == ["記号", "!目", "!口", "体"]
    eye = doc.find("目")
    assert eye.group and eye.required and [c.label for c in eye.children] == ["目セット", "UU"]
    assert doc.find("!目/*目セット/*白目").path() == "!目/*目セット/*白目"
    # 番号は下から 0 始まり（フォルダの終わりの記録も数える）
    assert doc.find("体").index == 0 and doc.find("記号").index == len(list(doc.root.walk())) + 3 - 1
    assert doc.find("口/むふ").index < doc.find("口/お").index < doc.find("口").index


def _png(path: Path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), color).save(path)


def test_animations_from_material(tmp_path: Path):
    psd = tmp_path / "キャラ.psd"
    make_psd(psd, [("!目", [("*目セット", ["*普通目", "*普通白目"]), "*UU"]), ("!口", ["*ほあ", "*ほー", "*むふ"])])
    m = tmp_path / "素材"
    _png(m / "体" / "服.png", (1, 1, 1, 255))
    # 目: 数字なし = .0 = 開き、.1 = UU（閉じ）
    for name, c in [("普通白目_普通目.png", 10), ("普通白目_普通目.0.png", 10), ("普通白目_普通目.1.png", 11),
                    ("UU.png", 11)]:
        _png(m / "目" / name, (c, 0, 0, 255))
    # 口: 数字なし = .0 = 閉じ（むふ）、.1 = ほー、.2 = ほあ（最も開く）
    for name, c in [("むふ.png", 20), ("むふ.0.png", 20), ("むふ.1.png", 21), ("むふ.2.png", 22),
                    ("ほー.png", 21), ("ほあ.png", 22)]:
        _png(m / "口" / name, (c, 0, 0, 255))
    doc = read_psd(psd)
    config, warnings = animations_from_material(load_character(m), doc)
    assert not warnings
    assert describe(config, doc) == ["MouthAnimations: !口/*ほあ → !口/*ほー → !口/*むふ",
                                     "EyeAnimations: !目/*目セット → !目/*UU"]
    path = write_config(psd, config)
    assert path == config_path(psd) == tmp_path / "キャラ-ymm.json"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(path.read_text(encoding="utf-8-sig"))["MouthAnimations"][0]["Layers"][-1] == \
        f"n{doc.find('口/むふ').index}"


def test_animations_official_order(tmp_path: Path):
    """公式の決まり（.0 = 閉じ → 数字なし = 開き）の素材でも、開き → 閉じの順になる。"""
    psd = tmp_path / "キャラ.psd"
    make_psd(psd, [("!目", ["*普通目", "*閉じ目"]), ("!口", ["*あ", "*ん"])])
    m = tmp_path / "素材"
    for name, c in [("普通目.png", 10), ("普通目.0.png", 11), ("閉じ目.png", 11)]:
        _png(m / "目" / name, (c, 0, 0, 255))
    for name, c in [("あ.png", 20), ("あ.0.png", 21), ("ん.png", 21)]:
        _png(m / "口" / name, (c, 0, 0, 255))
    doc = read_psd(psd)
    config, warnings = animations_from_material(load_character(m), doc)
    assert not warnings
    assert describe(config, doc) == ["MouthAnimations: !口/*あ → !口/*ん", "EyeAnimations: !目/*普通目 → !目/*閉じ目"]


def test_write_config_keeps_presets(tmp_path: Path):
    psd = tmp_path / "キャラ.psd"
    config_path(psd).write_text(json.dumps({"Presets": [{"Name": "笑顔"}], "MouthAnimations": [{"Layers": ["n1"]}]}),
                                encoding="utf-8-sig")
    write_config(psd, {"Presets": [], "MouthAnimations": [{"Layers": ["n3", "n2"]}], "EyeAnimations": []})
    written = json.loads(config_path(psd).read_text(encoding="utf-8-sig"))
    assert written == {"Presets": [{"Name": "笑顔"}], "MouthAnimations": [{"Layers": ["n3", "n2"]}],
                       "EyeAnimations": []}


def test_read_psd_without_layers(tmp_path: Path):
    psd = tmp_path / "flat.psd"
    header = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, 20, 10, 8, 3)
    # レイヤーとマスク情報の長さ 0 の直後は画像データ（圧縮方式 1 = RLE）
    psd.write_bytes(header + struct.pack(">III", 0, 0, 0) + struct.pack(">H", 1) + b"\x00\x10" * 16)
    doc = read_psd(psd)
    assert (doc.width, doc.height) == (10, 20) and doc.root.children == []


def test_not_psd(tmp_path: Path):
    (tmp_path / "x.psd").write_bytes(b"nope")
    with pytest.raises(ValueError):
        read_psd(tmp_path / "x.psd")


CHAR = ROOT / "char"
EXPECTED = {
    "ずんだもん": (["!口/*ほあ → !口/*ほー → !口/*むふ", "!口/*お → !口/*むー", "!口/*お → !口/*んー"],
                 ["!目/*目セット → !目/*UU", "!目/*上向き → !目/*UU"]),
    "四国めたん": (["!口/*わあー → !口/*ほほえみ"], ["!目/*目セット → !目/*目閉じ", "!目/*見上げ → !目/*目閉じ"]),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_psd(name: str):
    psds = sorted((CHAR / name).glob("*.psd")) if (CHAR / name).is_dir() else []
    if not psds:
        pytest.skip("char/ に PSD がない")
    doc = read_psd(psds[0])
    config, warnings = animations_from_material(load_character(CHAR / name), doc)
    lines = describe(config, doc)
    mouths, eyes = EXPECTED[name]
    for m in mouths:
        assert f"MouthAnimations: {m}" in lines
    for e in eyes:
        assert f"EyeAnimations: {e}" in lines
    assert not warnings, warnings


def _pixel_record(name, section=0, hidden=False, rect=(0, 0, 0, 0), channels=()):
    raw = name.encode("cp932")
    pascal = bytes([len(raw)]) + raw
    pascal += b"\0" * (-len(pascal) % 4)
    extra = b"\0\0\0\0" + b"\0\0\0\0" + pascal
    luni = struct.pack(">I", len(name)) + name.encode("utf-16-be")
    extra += b"8BIM" + b"luni" + struct.pack(">I", len(luni)) + luni
    if section:
        extra += b"8BIM" + b"lsct" + struct.pack(">I", 4) + struct.pack(">I", section)
    left, top, right, bottom = rect
    rec = struct.pack(">iiii", top, left, bottom, right) + struct.pack(">H", len(channels))
    for cid, data in channels:
        rec += struct.pack(">hI", cid, len(data))
    rec += b"8BIM" + b"norm" + bytes([255, 0, 2 if hidden else 0, 0])
    return rec + struct.pack(">I", len(extra)) + extra


def make_pixel_psd(path: Path, layers: list, size=(4, 4)) -> None:
    """`layers` は上から: ("名前", (r, g, b), hidden, x) のレイヤー（x 列目だけ塗る）か ("名前", [子…], hidden) のフォルダ。"""
    w, h = size
    records, datas = [], []

    def add(items):
        for name, body, hidden, *col in reversed(items):
            if isinstance(body, list):
                records.append(_pixel_record("</Layer group>", 3))
                datas.append(b"")
                add(body)
                records.append(_pixel_record(name, 1, hidden))
                datas.append(b"")
            else:
                x = col[0] if col else 0
                chans = [(cid, b"\0\0" + bytes([v]) * h) for cid, v in zip((0, 1, 2, -1), (*body, 255))]
                records.append(_pixel_record(name, 0, hidden, (x, 0, x + 1, h), chans))
                datas.append(b"".join(d for _, d in chans))

    add(layers)
    layer_info = struct.pack(">h", len(records)) + b"".join(records) + b"".join(datas)
    layer_info += b"\0" * (len(layer_info) % 2)
    lm = struct.pack(">I", len(layer_info)) + layer_info + struct.pack(">I", 0)
    header = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3)
    body = struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">I", len(lm)) + lm
    path.write_bytes(header + body + struct.pack(">H", 0))


@pytest.fixture
def psd_char(tmp_path: Path) -> Path:
    psd = tmp_path / "テスト.psd"
    make_pixel_psd(psd, [
        ("!口", [("*開", (200, 0, 0), False, 0), ("*閉", (100, 0, 0), True, 0), ("*笑", (150, 0, 0), True, 0)], False),
        ("!目", [("*目", (0, 200, 0), False, 1), ("*閉じ目", (0, 100, 0), True, 1)], False),
        ("汗", (0, 0, 99), True, 2),
        ("体", (1, 1, 1), False, 3),
    ])
    doc = read_psd(psd)
    # 口パク: 開 → 閉、まばたき: 目 → 閉じ目
    (tmp_path / "テスト-ymm.json").write_text(json.dumps({"Presets": [], "MouthAnimations": [
        {"Layers": [f"n{doc.find('口/開').index}", f"n{doc.find('口/閉').index}"]}],
        "EyeAnimations": [{"Layers": [f"n{doc.find('目/目').index}", f"n{doc.find('目/閉じ目').index}"]}]}),
        encoding="utf-8-sig")
    return psd


def test_psd_render_and_layers(psd_char: Path):
    from tachie_studio.tachie.psd import apply_layers, is_shown, radio_groups, toggles

    doc = read_psd(psd_char)
    im = doc.render()
    assert [im.getpixel((x, 0)) for x in range(4)] == [(200, 0, 0, 255), (0, 200, 0, 255), (0, 0, 0, 0),
                                                       (1, 1, 1, 255)]  # 口=開、目、汗は非表示、体
    state = apply_layers(doc, {"!口/*笑": True, "汗": True})
    assert is_shown(doc.find("口/笑"), state) and not is_shown(doc.find("口/開"), state)
    im = doc.render(state)
    assert im.getpixel((0, 0))[0] == 150 and im.getpixel((2, 0)) == (0, 0, 99, 255)
    # * を全部消すと何も出ない、! は消せない
    assert doc.render({"!口/*開": False}).getpixel((0, 0))[3] == 0
    assert doc.render({"!目": False}).getpixel((1, 0))[1] == 200
    assert [g.label for g in radio_groups(doc)] == ["口", "目"]
    assert [t.label for t in toggles(doc)] == ["汗", "体"]


def test_psd_character_lipsync(psd_char: Path, tmp_path: Path):
    from tachie_studio.tachie import Expression, compose
    from tachie_studio.pipeline.render import _eye_part, _mouth_part, resolve_mouth_mode

    c = load_character(psd_char)
    assert c.is_psd and c.name == "テスト" and c.height == 4
    assert len(c.animations["mouth"]) == 1 and len(c.animations["eye"]) == 1
    mouth = _mouth_part(c, Expression())
    assert len(mouth.levels) == 2 and resolve_mouth_mode("auto", mouth) == "volume"
    assert len(_eye_part(c, Expression()).frames) == 2
    assert compose(c, Expression(mouth_frame=0)).getpixel((0, 0))[0] == 100  # 閉
    assert compose(c, Expression(mouth_frame=1)).getpixel((0, 0))[0] == 200  # 開
    assert compose(c, Expression(mouth_frame=-1)).getpixel((0, 0))[0] == 200
    assert compose(c, Expression(eye_frame=0)).getpixel((1, 0))[1] == 100  # 閉じ目
    assert compose(c, Expression()).getpixel((1, 0))[1] == 200
    # 口パクの無い口（笑）では口は動かない
    assert _mouth_part(c, Expression(layers={"!口/*笑": True})) is None


def test_psd_layers_in_config(psd_char: Path, tmp_path: Path):
    import tomllib

    from tachie_studio.pipeline.config import dump_characters, parse_characters

    cfg = parse_characters({"p": {"folder": str(psd_char), "speaker": 1, "parts": {"レイヤー": {"汗": True}},
                                  "expressions": {"笑う": {"レイヤー": {"!口/*笑": True}}}}}, tmp_path)["p"]
    assert cfg.expression.layers == {"汗": True}
    assert cfg.expression_for("笑う").layers == {"汗": True, "!口/*笑": True}
    again = parse_characters(tomllib.loads(dump_characters({"p": cfg})), tmp_path)["p"]
    assert again.expression == cfg.expression and again.expressions == cfg.expressions


def test_psd_render_line(psd_char: Path, tmp_path: Path):
    from test_pipeline import FakeClient

    from tachie_studio.pipeline.config import parse_characters
    from tachie_studio.pipeline.render import FrameCache, render_line
    from tachie_studio.pipeline.script import Line

    cfg = parse_characters({"p": {"folder": str(psd_char), "speaker": 1}}, tmp_path)["p"]
    c = load_character(psd_char)
    cache = FrameCache(c, cfg.expression, tmp_path / "cache", 1.0)
    clip = render_line(FakeClient(), cfg, c, Line(1, "p", "あ"), tmp_path / "o", fps=10, cache=cache, stem="x")
    reds = [Image.open(p).getpixel((0, 0))[0] for p in sorted((tmp_path / "o" / "x").iterdir())]
    assert len(reds) == clip.frames and reds[0] == 100 and reds[-1] == 100 and 200 in reds  # 無音は閉、声で開


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_psd_lipsync(name: str):
    psds = sorted((CHAR / name).glob("*.psd")) if (CHAR / name).is_dir() else []
    if not psds or not (psds[0].with_name(psds[0].stem + "-ymm.json")).is_file():
        pytest.skip("char/ に PSD と -ymm.json がない")
    from tachie_studio.tachie import Expression, compose

    c = load_character(psds[0])
    assert c.animations["mouth"] and c.animations["eye"]
    closed = compose(c, Expression(mouth_frame=0, eye_frame=0))
    opened = compose(c, Expression(mouth_frame=-1))
    assert closed.size == (c.psd.width, c.psd.height) and closed.tobytes() != opened.tobytes()


def test_root_radio(tmp_path: Path):
    from tachie_studio.tachie.psd import apply_layers, is_shown, radio_groups

    psd = tmp_path / "r.psd"
    make_pixel_psd(psd, [("*服1", [("右腕", (10, 0, 0), False, 0)], False),
                         ("*服2", [("右腕", (20, 0, 0), False, 0)], True)])
    doc = read_psd(psd)
    groups = radio_groups(doc)
    assert groups[0] is doc.root and doc.nodes()[""] is doc.root
    assert doc.render().getpixel((0, 0))[0] == 10
    state = apply_layers(doc, {"*服2": True})
    assert doc.render(state).getpixel((0, 0))[0] == 20 and not is_shown(doc.find("服1/右腕"), state)


def test_psd_lipsync_plain_layers(tmp_path: Path):
    """`*` でないレイヤーの口パクでは、選ばない段のレイヤーを消す。"""
    from tachie_studio.tachie import Expression, compose

    psd = tmp_path / "p.psd"
    make_pixel_psd(psd, [("口", [("開", (200, 0, 0), False, 0), ("閉", (100, 0, 0), True, 0)], False)])
    doc = read_psd(psd)
    (tmp_path / "p-ymm.json").write_text(json.dumps({"MouthAnimations": [
        {"Layers": [f"n{doc.find('口/開').index}", f"n{doc.find('口/閉').index}"]}]}), encoding="utf-8-sig")
    c = load_character(psd)
    assert compose(c, Expression(mouth_frame=0)).getpixel((0, 0))[0] == 100  # 開は消える
    assert compose(c, Expression(mouth_frame=1)).getpixel((0, 0))[0] == 200


def test_psd_unsupported_depth(tmp_path: Path):
    psd = tmp_path / "d.psd"
    make_pixel_psd(psd, [("体", (1, 1, 1), False)])
    data = bytearray(psd.read_bytes())
    data[22:24] = struct.pack(">H", 16)
    psd.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="8bit"):
        load_character(psd)
