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
