from tachie_studio.fonts import default_font, weight_rank


def test_weight_rank():
    names = ["NotoSansCJK-Black.ttc", "NotoSansCJK-Bold.ttc", "NotoSansCJK-DemiLight.ttc", "NotoSansCJK-Light.ttc",
             "NotoSansCJK-Medium.ttc", "NotoSansCJK-Regular.ttc", "NotoSansCJK-Thin.ttc", "NotoSansJP-ExtraBold.otf"]
    assert sorted(names, key=lambda n: (weight_rank(n), n))[:3] == [
        "NotoSansCJK-Bold.ttc", "NotoSansCJK-Medium.ttc", "NotoSansCJK-Regular.ttc"]
    assert weight_rank("meiryo.ttc") < weight_rank("NotoSansCJK-Black.ttc")
    assert weight_rank("NotoSansCJK-DemiLight.ttc") < weight_rank("NotoSansCJK-Black.ttc")


def test_default_font_prefers_bold(tmp_path):
    for name in ("NotoSansCJK-Black.ttc", "NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"):
        (tmp_path / "noto" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "noto" / name).touch()
    assert default_font(((f"{tmp_path}/**/NotoSansCJK*.tt[cf]",),)).endswith("NotoSansCJK-Bold.ttc")
    (tmp_path / "noto" / "NotoSansCJK-Bold.ttc").unlink()
    (tmp_path / "noto" / "NotoSansCJK-Regular.ttc").unlink()
    assert default_font(((f"{tmp_path}/**/NotoSansCJK*.tt[cf]",),)).endswith("NotoSansCJK-Black.ttc")
    assert default_font(((f"{tmp_path}/**/none*.ttc",),)) == ""


def test_default_font_compares_noto_families(tmp_path):
    # Noto Sans CJK に Black しか無ければ、Noto Sans JP の Regular を選ぶ
    (tmp_path / "NotoSansCJK-Black.ttc").touch()
    (tmp_path / "NotoSansJP-Regular.otf").touch()
    group = (f"{tmp_path}/**/NotoSansCJK*.tt[cf]", f"{tmp_path}/**/NotoSansJP*.[ot]tf")
    assert default_font((group,)).endswith("NotoSansJP-Regular.otf")


def test_glyph_width():
    from tachie_studio.fonts import glyph_width

    path = default_font()
    if not path:
        import pytest

        pytest.skip("日本語フォントが無い")
    width_of = glyph_width(path, 64)
    assert width_of is not None
    assert 50 < width_of("あ") <= 70  # 全角はほぼ文字の大きさ
    assert width_of("i") < width_of("W")  # 英字は字ごとに幅が違う
    assert glyph_width("/nonexistent.ttf", 64) is None
