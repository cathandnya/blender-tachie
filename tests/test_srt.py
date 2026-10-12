from tachie_studio.srt import Cue, to_srt, to_vtt


def test_srt_and_vtt():
    cues = [Cue(61.5, 63.25, "二つ目\n改行あり", "めたん"), Cue(0, 1.0, "こんにちは", "ずんだもん"),
            Cue(5, 5, "長さ 0 は出さない"), Cue(6, 7, "  ")]
    assert to_srt(cues) == ("1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n"
                            "2\n00:01:01,500 --> 00:01:03,250\n二つ目\n改行あり\n")
    assert "ずんだもん: こんにちは" in to_srt(cues, with_speaker=True)
    vtt = to_vtt(cues)
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nこんにちは\n") and "01:03.250" in vtt
    assert to_srt([Cue(3600 * 2 + 0.0004, 3600 * 2 + 1, "x")]).startswith("1\n02:00:00,000 --> 02:00:01,000")


def test_manifest_cues():
    from tachie_studio.pipeline.__main__ import manifest_cues

    manifest = {"clips": [
        {"kind": "line", "frames": 30, "fps": 30.0, "text": "あ", "character": "a"},
        {"kind": "idle", "frames": 90, "fps": 30.0, "text": None, "character": "a"},
        {"kind": "line", "frames": 45, "fps": 30.0, "text": "い", "character": "b"},
    ]}
    cues = manifest_cues(manifest, gap=0.5)
    assert [(c.start, c.end, c.text) for c in cues] == [(0.0, 1.0, "あ"), (1.5, 3.0, "い")]


def test_vtt_escapes_markup():
    vtt = to_vtt([Cue(0, 1, "A&B <3 -->")])
    assert "A&amp;B &lt;3 --&gt;" in vtt and "-->" not in vtt.split("\n", 3)[3]
