from pathlib import Path

import pytest
from PIL import Image

from tachie_studio.tachie import Expression, build_layers, compose, load_character
from tachie_studio.tachie.compose import load_image

ROOT = Path(__file__).resolve().parent.parent
SIZE = (8, 8)


def _png(path: Path, color: tuple[int, int, int, int], box: tuple[int, int, int, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", SIZE)
    im.paste(color, box or (0, 0, *SIZE))
    im.save(path)


@pytest.fixture(autouse=True)
def _clear_cache():
    load_image.cache_clear()
    yield
    load_image.cache_clear()


@pytest.fixture
def char_dir(tmp_path: Path) -> Path:
    d = tmp_path / "テスト"
    _png(d / "体" / "服.png", (200, 200, 200, 255))
    _png(d / "顔色" / "なし.png", (0, 0, 0, 0))
    _png(d / "髪" / "前髪.png", (0, 255, 0, 255), (0, 0, 8, 2))
    _png(d / "眉" / "普通.png", (0, 0, 255, 255), (0, 0, 8, 3))
    # まばたき: .0 閉じ → .1 中間 → 数字なし 開き
    _png(d / "目" / "開き.png", (10, 0, 0, 255), (0, 4, 8, 5))
    _png(d / "目" / "開き.0.png", (11, 0, 0, 255), (0, 4, 8, 5))
    _png(d / "目" / "開き.1.png", (12, 0, 0, 255), (0, 4, 8, 5))
    _png(d / "目" / "静止.png", (20, 0, 0, 255), (0, 4, 8, 5))
    # 一覧に出ない名前
    _png(d / "目" / "パーツ.の名前.png", (30, 0, 0, 255))
    # あいうえお口パク
    _png(d / "口" / "あ.png", (0, 0, 10, 255), (0, 6, 8, 7))
    _png(d / "口" / "あ.0.png", (0, 0, 11, 255), (0, 6, 8, 7))
    for i, v in enumerate("aiueo"):
        _png(d / "口" / f"あ.{v}.png", (0, 0, 20 + i, 255), (0, 6, 8, 7))
    _png(d / "他" / "腕.png", (255, 0, 0, 255), (0, 7, 8, 8))
    _png(d / "他" / "記号.png", (254, 0, 0, 255), (0, 7, 8, 8))
    _png(d / "後" / "影.png", (50, 50, 50, 255))
    return d


def test_load_classifies_files(char_dir: Path):
    c = load_character(char_dir)
    assert set(c.parts["目"]) == {"開き", "静止"}
    eye = c.part("目", "開き")
    assert [p.name for p in eye.frames] == ["開き.0.png", "開き.1.png", "開き.png"]
    assert eye.animated and not c.part("目", "静止").animated
    mouth = c.part("口", "あ")
    assert sorted(mouth.vowels) == list("aeiou")
    assert [p.name for p in mouth.frames] == ["あ.0.png", "あ.png"]


def test_default_prefers_animated(char_dir: Path):
    c = load_character(char_dir)
    assert c.default("目").name == "開き"
    assert c.default("体").name == "服"


def test_default_skips_placeholders(tmp_path: Path):
    d = tmp_path / "c"
    for name in ["(非表示).png", "_.png", "制服.png"]:
        _png(d / "体" / name, (1, 1, 1, 255))
    assert load_character(d).default("体").name == "制服"


def test_nfd_names_match(char_dir: Path):
    import unicodedata

    nfd = unicodedata.normalize("NFD", "ジト目")
    _png(char_dir / "目" / f"{nfd}.png", (40, 0, 0, 255))
    c = load_character(char_dir)
    assert c.part("目", "ジト目").name == "ジト目"


def test_ini(char_dir: Path):
    (char_dir / "髪" / "前髪.ini").write_text("opacity=0.5\n", encoding="cp932")
    (char_dir / "顔色" / "なし.ini").write_text("blend=3\n")
    c = load_character(char_dir)
    assert c.part("髪", "前髪").opacity == 0.5
    assert c.part("顔色", "なし").blend == 3


def test_layer_order(char_dir: Path):
    c = load_character(char_dir)
    layers = build_layers(c, Expression(others=["腕", "記号"], backs=["影"]))
    order = [(l.part.category, l.part.name) for l in layers]
    assert order == [
        ("後", "影"),
        ("体", "服"),
        ("顔色", "なし"),
        ("髪", "前髪"),
        ("口", "あ"),
        ("目", "開き"),
        ("眉", "普通"),
        ("髪", "前髪"),
        ("他", "記号"),
        ("他", "腕"),
    ]


def test_non_normal_blend_drawn_in_front(char_dir: Path):
    (char_dir / "顔色" / "なし.ini").write_text("blend=3\n")
    c = load_character(char_dir)
    layers = build_layers(c, Expression(others=["腕"]))
    assert layers[-1].part.category == "顔色"


def test_compose_pixels(char_dir: Path):
    c = load_character(char_dir)
    im = compose(c, Expression(others=["腕"]))
    assert im.size == SIZE
    assert im.getpixel((0, 0)) == (0, 255, 0, 255)  # 前髪が眉より手前
    assert im.getpixel((0, 2)) == (0, 0, 255, 255)  # 眉
    assert im.getpixel((0, 4)) == (10, 0, 0, 255)  # 目（開き）
    assert im.getpixel((0, 6)) == (0, 0, 10, 255)  # 口（開き）
    assert im.getpixel((0, 7)) == (255, 0, 0, 255)  # 他
    assert im.getpixel((0, 5)) == (200, 200, 200, 255)  # 体


def test_frames_and_vowels(char_dir: Path):
    c = load_character(char_dir)
    im = compose(c, Expression(eye_frame=0, mouth_frame=0))
    assert im.getpixel((0, 4))[:3] == (11, 0, 0)
    assert im.getpixel((0, 6))[:3] == (0, 0, 11)
    im = compose(c, Expression(eye_frame=1, mouth_vowel="u"))
    assert im.getpixel((0, 4))[:3] == (12, 0, 0)
    assert im.getpixel((0, 6))[:3] == (0, 0, 22)
    # 範囲外は端に丸める
    im = compose(c, Expression(eye_frame=99))
    assert im.getpixel((0, 4))[:3] == (10, 0, 0)


def test_hair_opacity_shows_eyebrow(char_dir: Path):
    (char_dir / "髪" / "前髪.ini").write_text("opacity=0.5\n")
    c = load_character(char_dir)
    r, g, b, a = compose(c).getpixel((0, 0))
    # 奥: 前髪(不透明) → 眉 → 手前: 前髪(50%) で緑と青が混ざる
    assert a == 255
    assert 120 <= g <= 135 and 120 <= b <= 135


def test_multiply(char_dir: Path):
    _png(char_dir / "顔色" / "影.png", (128, 128, 128, 255), (0, 5, 8, 6))
    (char_dir / "顔色" / "影.ini").write_text("blend=3\n")
    c = load_character(char_dir)
    im = compose(c, Expression(complexion="影"))
    r, g, b, a = im.getpixel((0, 5))
    assert a == 255 and 99 <= r <= 101  # 200 * 128 / 255


def test_multiply_over_transparent_keeps_source(tmp_path: Path):
    d = tmp_path / "c"
    _png(d / "体" / "服.png", (200, 200, 200, 255), (0, 0, 4, 8))
    _png(d / "顔色" / "影.png", (128, 64, 32, 255))
    (d / "顔色" / "影.ini").write_text("blend=3\n")
    im = compose(load_character(d))
    assert im.getpixel((6, 0)) == (128, 64, 32, 255)


def test_too_many_slots(char_dir: Path):
    c = load_character(char_dir)
    with pytest.raises(ValueError):
        build_layers(c, Expression(others=["腕", "記号", "腕", "記号"]))


def test_not_a_character(tmp_path: Path):
    with pytest.raises(ValueError):
        load_character(tmp_path)


CHAR = ROOT / "char"


@pytest.mark.skipif(not CHAR.is_dir(), reason="char/ に素材がない")
@pytest.mark.parametrize("name", sorted(p.name for p in CHAR.iterdir() if p.is_dir()) if CHAR.is_dir() else [])
def test_real_material(name: str):
    c = load_character(CHAR / name)
    assert {"目", "口", "体"} <= set(c.parts)
    im = compose(c)
    assert im.mode == "RGBA" and im.getbbox() is not None


def test_levels_drop_base_identical_to_closed(tmp_path: Path):
    d = tmp_path / "c"
    _png(d / "体" / "服.png", (1, 1, 1, 255))
    # 数字なし = .0（閉じ）と同じ内容で、最後の番号が開きの素材
    for name, color in [("むふ.png", 10), ("むふ.0.png", 10), ("むふ.1.png", 11), ("むふ.2.png", 12)]:
        _png(d / "口" / name, (color, 0, 0, 255))
    # 公式どおり（数字なし = 開き）の素材
    for name, color in [("あ.png", 22), ("あ.0.png", 20), ("あ.1.png", 21)]:
        _png(d / "口" / name, (color, 0, 0, 255))
    c = load_character(d)
    assert [p.name for p in c.part("口", "むふ").levels] == ["むふ.0.png", "むふ.1.png", "むふ.2.png"]
    assert [p.name for p in c.part("口", "あ").levels] == ["あ.0.png", "あ.1.png", "あ.png"]


def test_empty_slot_skipped(char_dir: Path):
    c = load_character(char_dir)
    layers = build_layers(c, Expression(others=["", "腕"]))
    assert [l.part.name for l in layers if l.part.category == "他"] == ["腕"]


def test_load_presets(char_dir: Path):
    from tachie_studio.tachie import load_presets

    assert load_presets(char_dir) == ({}, [])
    text = ("[指差し]\r\n他2=記号.png\r\n目=静止.png\r\n\r\n[><]\r\n目=開き.png\r\n後1=影.png\r\n"
            "\r\n[無い]\r\n眉=困り.png\r\n耳=x.png\r\n他=腕.png\r\n他4=腕.png\r\n")
    (char_dir / "preset.ini").write_bytes("﻿".encode() + text.encode("utf-8"))
    presets, warnings = load_presets(char_dir)
    assert presets == {"指差し": {"他": ["", "記号"], "目": "静止"}, "><": {"目": "開き", "後": ["影"]}, "無い": {}}
    assert len(warnings) == 4 and any("眉/困り" in w for w in warnings) and any("耳" in w for w in warnings)
    assert any("他4" in w for w in warnings)  # スロットは 3 つまで
    # cp932 と、別の場所のファイル
    other = char_dir.parent / "p.ini"
    other.write_bytes("[静止]\n目=静止.png\n".encode("cp932"))
    assert load_presets(char_dir, other) == ({"静止": {"目": "静止"}}, [])
    other.write_text("目=静止.png\n", encoding="utf-8")  # セクションが無い
    with pytest.raises(ValueError):
        load_presets(char_dir, other)


@pytest.mark.skipif(not CHAR.is_dir(), reason="char/ に素材がない")
@pytest.mark.parametrize("name", sorted(p.name for p in CHAR.iterdir() if (p / "preset.ini").is_file())
                         if CHAR.is_dir() else [])
def test_real_presets(name: str):
    from tachie_studio.pipeline.config import parse_characters
    from tachie_studio.tachie import load_presets

    presets, warnings = load_presets(CHAR / name)
    assert presets and not warnings, warnings
    # キャラ設定に補われ、どのプリセットでも合成できる
    cfg = parse_characters({name: {"folder": str(CHAR / name), "speaker": 3}}, ROOT)[name]
    assert set(presets) <= set(cfg.expressions)
    c = load_character(CHAR / name)
    for preset in presets:
        assert compose(c, cfg.expression_for(preset)).getbbox() is not None


def test_simple_material(tmp_path: Path):
    d = tmp_path / "シンプル"
    _png(d / "00 デフォルト.png", (10, 0, 0, 255))
    _png(d / "01 口開き.png", (20, 0, 0, 255))
    _png(d / "02 Xd.png", (30, 0, 0, 255))
    c = load_character(d)
    assert c.simple and set(c.parts) == {"体", "口"} and len(c.parts["体"]) == 3
    assert c.default("体").name == "00 デフォルト" and c.default("口").name == "01 口開き"
    # 口を閉じている（mouth_frame 0）ときは体の画像、開いているときは口の画像
    assert compose(c, Expression(mouth_frame=0)).getpixel((0, 0))[0] == 10
    assert compose(c, Expression(mouth_frame=1)).getpixel((0, 0))[0] == 20
    assert compose(c, Expression(body="02 Xd", mouth="02 Xd", mouth_frame=1)).getpixel((0, 0))[0] == 30
    assert compose(c, Expression(body="02 Xd", mouth_frame=0)).getpixel((0, 0))[0] == 30


def test_simple_material_frames_and_default(tmp_path: Path):
    d = tmp_path / "シンプル2"
    _png(d / "閉じ.png", (10, 0, 0, 255))
    _png(d / "開き.png", (20, 0, 0, 255))
    c = load_character(d)
    # 「デフォルト」「口開」が名前に無くても、口は体と違う画像になる
    assert c.default("体").name != c.default("口").name
    closed, opened = c.default("体").name, c.default("口").name
    px = {"閉じ": 10, "開き": 20}
    # mouth_frame は frames の添字なので -1 は開き、母音も開き
    assert compose(c, Expression(mouth_frame=-1)).getpixel((0, 0))[0] == px[opened]
    assert compose(c, Expression(mouth_frame=0)).getpixel((0, 0))[0] == px[closed]
    assert compose(c, Expression(mouth_vowel="a")).getpixel((0, 0))[0] == px[opened]
