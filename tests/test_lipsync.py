import array
import io
import math
import wave

import pytest

from tachie_studio.lipsync import (
    CLOSED,
    Phoneme,
    blink_track,
    phonemes_from_query,
    query_duration,
    read_wav,
    scale_to,
    volume_track,
    vowel_track,
    vowels_to_levels,
)
from tachie_studio.voicevox import VoicevoxClient, VoicevoxError


def _mora(text, consonant, c_len, vowel, v_len):
    return {"text": text, "consonant": consonant, "consonant_length": c_len,
            "vowel": vowel, "vowel_length": v_len, "pitch": 5.0}


def _query(**overrides):
    q = {
        "accent_phrases": [
            {"moras": [_mora("カ", "k", 0.1, "a", 0.2), _mora("ン", None, None, "N", 0.1)],
             "accent": 1, "pause_mora": {"text": "、", "consonant": None, "consonant_length": None,
                                          "vowel": "pau", "vowel_length": 0.3, "pitch": 0.0},
             "is_interrogative": False},
            {"moras": [_mora("マ", "m", 0.1, "a", 0.2), _mora("イ", None, None, "i", 0.2)],
             "accent": 1, "pause_mora": None, "is_interrogative": False},
        ],
        "speedScale": 1.0, "prePhonemeLength": 0.1, "postPhonemeLength": 0.1,
        "pauseLength": None, "pauseLengthScale": 1.0,
    }
    q.update(overrides)
    return q


def test_timing_accumulates():
    ph = phonemes_from_query(_query(), frame_rate=None)
    assert [p.phoneme for p in ph] == ["pau", "k", "a", "N", "pau", "m", "a", "i", "pau"]
    assert ph[0].start == 0 and ph[1].start == pytest.approx(0.1)
    assert [p.is_vowel for p in ph] == [True, False, True, True, True, False, True, True, True]
    assert ph[-1].end == pytest.approx(0.1 + 0.3 + 0.1 + 0.3 + 0.3 + 0.2 + 0.1)


def test_timing_speed_and_pause():
    base = query_duration(_query())
    assert query_duration(_query(speedScale=2.0), frame_rate=None) == pytest.approx(base / 2, abs=0.02)
    ph = phonemes_from_query(_query(pauseLength=0.5, pauseLengthScale=2.0), frame_rate=None)
    assert ph[4].duration == pytest.approx(1.0)


def test_timing_frame_rounding():
    ph = phonemes_from_query(_query(speedScale=1.3))
    for p in ph:
        frames = p.duration * 93.75
        assert frames == pytest.approx(round(frames))


def test_upspeak_adds_vowel():
    q = _query()
    q["accent_phrases"][1]["is_interrogative"] = True
    ph = phonemes_from_query(q, frame_rate=None)
    assert [p.phoneme for p in ph][-3:] == ["i", "i", "pau"]
    assert ph[-2].duration == pytest.approx(0.15)
    assert len(phonemes_from_query(q, upspeak=False)) == len(ph) - 1


def test_scale_to():
    ph = scale_to(phonemes_from_query(_query()), 2.6)
    assert ph[-1].end == pytest.approx(2.6)


def test_vowel_track():
    ph = phonemes_from_query(_query(), frame_rate=None)
    track = vowel_track(ph, fps=20)  # 1 フレーム 0.05 秒
    assert len(track) == math.ceil(1.4 * 20)
    assert track[0] is CLOSED  # 前の無音
    assert track[2:4] == ["a", "a"]  # k は続く a を先取りする
    assert track[5] == "a"
    assert track[10] is CLOSED  # ポーズ
    assert track[16] is CLOSED and track[17] is CLOSED  # m は唇を閉じる
    assert track[18] == "a"
    assert track[20] == "a" and track[24] == "i"
    assert track[-1] is CLOSED


def test_unvoiced_vowel_and_cl():
    ph = [Phoneme("s", 0.0, 0.1, False), Phoneme("U", 0.1, 0.2, True), Phoneme("cl", 0.2, 0.3, True)]
    assert vowel_track(ph, fps=10) == ["u", "u", CLOSED]


def test_vowels_to_levels():
    assert vowels_to_levels([CLOSED, "a", "i", "o"], 3) == [0, 2, 1, 2]
    assert vowels_to_levels(["a"], 1) == [0]


def _wav_bytes(amplitudes, rate=1000, per=100):
    samples = array.array("h")
    for a in amplitudes:
        samples.extend(int(a * 32767 * math.sin(i)) for i in range(per))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def test_volume_track():
    wav = read_wav(_wav_bytes([0, 0.05, 0.3, 0.6, 0.6, 0]))
    assert wav.duration == pytest.approx(0.6)
    track = volume_track(wav, fps=10, levels=3)
    assert track[0] == 0 and track[-1] == 0
    assert track[1] == 1 and track[3] == 2
    assert all(0 <= v <= 2 for v in track)


def test_blink_track():
    track = blink_track(300, 30, 3, seed=1)
    assert len(track) == 300
    assert track.count(2) < 300 and set(track) <= {0, 1, 2}
    assert track == blink_track(300, 30, 3, seed=1)
    # .0 → .1 の順に 1 枚ずつ（30fps で 1 フレーム）
    i = track.index(0)
    assert track[i:i + 3] == [0, 1, 2]
    assert blink_track(10, 30, 1) == [0] * 10


def test_blink_hold_scales_with_fps():
    track = blink_track(600, 60, 2, seed=3)
    i = track.index(0)
    assert track[i:i + 3] == [0, 0, 1]


def test_client_connection_error():
    with pytest.raises(VoicevoxError):
        VoicevoxClient("http://127.0.0.1:9", timeout=1).version()


def _engine():
    client = VoicevoxClient(timeout=10)
    try:
        client.version()
    except VoicevoxError:
        pytest.skip("VOICEVOX エンジンが起動していない")
    return client


@pytest.mark.parametrize("text,speed", [("こんにちは、ずんだもんなのだ。", 1.0), ("ついてこれるかな？", 1.5)])
def test_engine_duration_matches_wav(text, speed):
    client = _engine()
    q = client.audio_query(text, 3)
    q["speedScale"] = speed
    wav = read_wav(client.synthesis(q, 3))
    assert query_duration(q) == pytest.approx(wav.duration, abs=0.011)


def test_engine_kana():
    client = _engine()
    auto = client.audio_query("今日はいい天気", 3)["kana"]
    phrases = client.accent_phrases(auto, 3, is_kana=True)
    assert "".join(m["text"] for p in phrases for m in p["moras"]) == "キョオワイイテンキ"  # エンジンの表記（長音はオ）
    changed = client.accent_phrases("キョ'ウワ/イ'イ/テンキ'", 3, is_kana=True)
    assert [p["accent"] for p in changed] == [1, 1, 3]
    with pytest.raises(VoicevoxError, match="判別できない読み仮名"):
        client.accent_phrases("abc", 3, is_kana=True)


def test_engine_user_dict():
    client = _engine()
    surface = "立絵試験語"  # 試験用。必ず消す
    uuid = client.add_user_dict_word(surface, "タチエシケンゴ", 1)
    try:
        assert client.user_dict()[uuid]["pronunciation"] == "タチエシケンゴ"
        assert client.audio_query(surface, 3)["kana"].translate({ord(c): None for c in "'/_"}) == "タチエシケンゴ"  # _ は無声化
    finally:
        client.delete_user_dict_word(uuid)
    assert uuid not in client.user_dict()


def test_apply_fade():
    import io
    import wave

    from tachie_studio.lipsync.wav import apply_fade

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(100)
        w.writeframes(b"".join((10000).to_bytes(2, "little", signed=True) for _ in range(100)))
    raw = buf.getvalue()
    assert apply_fade(raw) is raw
    out = read_wav(apply_fade(raw, fade_in=0.1, fade_out=0.2))
    s = [round(x * 32768) for x in out.samples]
    assert s[0] == 0 and s[5] == 5000 and s[10] == 10000 and s[50] == 10000
    assert s[-1] == 0 and s[90] == 4500 and s[79] == 10000  # 末尾 20 サンプル: (99 - 90) / 20
    assert len(s) == 100
