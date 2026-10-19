from tachie_studio.subtitle import chars_per_line, wrap


def test_wrap_basic():
    assert wrap("あいうえおかきくけこ", 5) == "あいうえお\nかきくけこ"
    assert wrap("abcdefghij", 2.5) == "abcde\nfghij"  # 半角は 0.5
    assert wrap("短い", 10) == "短い"


def test_wrap_hangs_punctuation():
    assert wrap("あいうえお。かきく", 5) == "あいうえお。\nかきく"
    assert wrap("あいうえお、」かき", 5) == "あいうえお、」\nかき"


def test_wrap_moves_opening_bracket():
    assert wrap("あいう「えおかき」", 4) == "あいう\n「えおか\nき」"


def test_wrap_keeps_newlines_and_max_lines():
    assert wrap("あいう\nえお", 10) == "あいう\nえお"
    assert wrap("あいうえおかきくけこさしす", 4, max_lines=2) == "あいうえ\nおかきくけこさしす"


def test_chars_per_line():
    assert chars_per_line(1920, 64) == 1920 * 0.8 / 64


def test_breaks():
    """セリフの改行は \\n の書き方で持ち（Blender の入力欄に改行を入れられない）、字幕・読み上げのときに改行にする。"""
    from tachie_studio.subtitle import breaks, mark_breaks

    assert mark_breaks("一行目\r\n二行目¥n三行目") == "一行目\\n二行目\\n三行目"
    assert breaks("一行目\\n二行目¥n三行目") == "一行目\n二行目\n三行目"
    assert breaks(mark_breaks("a\nb")) == "a\nb"
