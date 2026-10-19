"""アクセント句の編集（結合・分割・発音）。"""

import pytest

from tachie_studio.voicevox.accent import join_phrases, kana_from_phrases, split_phrase


def mora(text, vowel="a", pitch=5.0):
    return {"text": text, "consonant": None, "consonant_length": None, "vowel": vowel, "vowel_length": 0.1,
            "pitch": pitch}


PAUSE = {"text": "、", "consonant": None, "consonant_length": None, "vowel": "pau", "vowel_length": 0.25, "pitch": 0.0}


def phrases():
    # キョ'オワ/イ'イ/テ'ンキデ_スネ、ソオデ_ス'カ？（エンジンが返した形）
    return [
        {"accent": 1, "pause_mora": None, "is_interrogative": False,
         "moras": [mora("キョ", "o"), mora("オ", "o"), mora("ワ")]},
        {"accent": 1, "pause_mora": None, "is_interrogative": False, "moras": [mora("イ", "i"), mora("イ", "i")]},
        {"accent": 1, "pause_mora": PAUSE, "is_interrogative": False,
         "moras": [mora("テ", "e"), mora("ン", "N"), mora("キ", "i"), mora("デ", "e"), mora("ス", "U", 0.0),
                   mora("ネ", "e")]},
        {"accent": 4, "pause_mora": None, "is_interrogative": True,
         "moras": [mora("ソ", "o"), mora("オ", "o"), mora("デ", "e"), mora("ス", "U", 0.0), mora("カ")]},
    ]


def test_kana_matches_engine():
    assert kana_from_phrases(phrases()) == "キョ'オワ/イ'イ/テ'ンキデ_スネ、ソオデ_ス'カ？"


def test_join():
    joined = join_phrases(phrases(), 2)
    assert len(joined) == 3 and [m["text"] for m in joined[2]["moras"]][-2:] == ["ス", "カ"]
    assert joined[2]["accent"] == 1 and joined[2]["is_interrogative"] and joined[2]["pause_mora"] is None
    assert kana_from_phrases(joined) == "キョ'オワ/イ'イ/テ'ンキデ_スネソオデ_スカ？"
    assert len(phrases()) == 4  # 元の句は変えない
    with pytest.raises(ValueError):
        join_phrases(phrases(), 3)


def test_split():
    split = split_phrase(phrases(), 3, 2)  # ソオ|デ_ス'カ？
    assert [len(p["moras"]) for p in split[3:]] == [2, 3]
    assert (split[3]["accent"], split[4]["accent"]) == (1, 2)  # アクセントは後ろの句の中に移る
    assert not split[3]["is_interrogative"] and split[4]["is_interrogative"]
    assert kana_from_phrases(split) == "キョ'オワ/イ'イ/テ'ンキデ_スネ、ソ'オ/デ_ス'カ？"
    head = split_phrase(phrases(), 2, 4)  # テ'ンキデ|_スネ、
    assert head[2]["accent"] == 1 and head[2]["pause_mora"] is None and head[3]["pause_mora"] == PAUSE
    with pytest.raises(ValueError):
        split_phrase(phrases(), 0, 0)
