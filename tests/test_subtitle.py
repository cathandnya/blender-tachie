from tachie_studio.subtitle import wrap


def test_wrap_basic():
    assert wrap("あいうえおかきくけこ", 5) == "あいうえお\nかきくけこ"
    assert wrap("abcdefghij", 2.5) == "abcde\nfghij"  # 半角は 0.5
    assert wrap("短い", 10) == "短い"


def test_wrap_keeps_words():
    """英単語は途中で切らずに次の行へ送る。空白で改行したら空白は残さない。"""
    assert wrap("今日はBlenderの話", 5) == "今日は\nBlenderの\n話"
    assert wrap("hello world foo", 3) == "hello\nworld\nfoo"
    assert wrap("Supercalifragilistic", 3) == "Superc\nalifra\ngilist\nic"  # 1 行に入らない単語は切る
    assert wrap("  あいうえお", 5) == "  あいうえ\nお"  # 段落の頭の空白は残す
    assert wrap("Blender.", 3.5) == "Blender."  # 句読点はぶら下げる
    assert wrap("あいう「Blender」", 5) == "あいう\n「Blender」"  # 開き括弧は単語と一緒に送る
    assert wrap("  Supercalifragilistic", 3) == "  Supe\nrcalif\nragili\nstic"  # 空行を作らない


def test_wrap_phrases():
    """文節に分ける関数を渡すと、文節の切れ目で折り返す。1 行に入らない文節は文字の間で切る。"""
    from tachie_studio.subtitle import phrase_splitter

    split = phrase_splitter()
    assert split is not None
    assert split("今日は良い天気です。") == ["今日は", "良い", "天気です。"]
    assert wrap("今日は良い天気です。", 6, phrases=split) == "今日は良い\n天気です。"
    assert wrap("今日は良い天気です。", 6) == "今日は良い天\n気です。"  # 渡さなければ文字の間
    assert wrap("今日はBlenderで動く立ち絵の動画を作ります。", 9, phrases=split) == \
        "今日はBlenderで\n動く立ち絵の動画を\n作ります。"
    assert wrap("あいうえおかきくけこさしすせそ", 5, phrases=lambda t: [t]) == "あいうえお\nかきくけこ\nさしすせそ"


def test_wrap_measured_width():
    """幅を測る関数を渡すと、max_width はその単位（px）になる。"""
    widths = {"W": 30.0, "i": 10.0}
    assert wrap("WWii iWW", 80, width_of=lambda c: widths.get(c, 20.0)) == "WWii\niWW"


def test_wrap_hangs_punctuation():
    assert wrap("あいうえお。かきく", 5) == "あいうえお。\nかきく"
    assert wrap("あいうえお、」かき", 5) == "あいうえお、」\nかき"


def test_wrap_moves_opening_bracket():
    assert wrap("あいう「えおかき」", 4) == "あいう\n「えおか\nき」"


def test_wrap_keeps_newlines_and_max_lines():
    assert wrap("あいう\nえお", 10) == "あいう\nえお"
    assert wrap("あいうえおかきくけこさしす", 4, max_lines=2) == "あいうえ\nおかきくけこさしす"


def test_breaks():
    """セリフの改行は \\n の書き方で持ち（Blender の入力欄に改行を入れられない）、字幕・読み上げのときに改行にする。"""
    from tachie_studio.subtitle import breaks, mark_breaks

    assert mark_breaks("一行目\r\n二行目¥n三行目") == "一行目\\n二行目\\n三行目"
    assert breaks("一行目\\n二行目¥n三行目") == "一行目\n二行目\n三行目"
    assert breaks(mark_breaks("a\nb")) == "a\nb"
