"""BGM のダッキング: セリフの音声が鳴っている間だけ BGM の音量を下げる。

BGM は印（`tachie_bgm`）を付けた音声ストリップで、元の音量は `tachie_bgm_volume` に持つ。`volume` には
キーフレームを打つので、BGM の音量はアドオンが管理する（パネルの「音量」で変える）。`refresh_ducking` は
BGM の volume の F カーブを全部消してから作り直すので、何度呼んでもよい。
"""

from __future__ import annotations

from .motion import _PATH_RE, _channelbag

# これより短いセリフの切れ目は、BGM を戻さずに下げたままにする（秒、フェードの分は別）
MERGE_SECONDS = 0.5


def bgm_strips(sed) -> list:
    return [s for s in sed.strips if s.type == "SOUND" and s.get("tachie_bgm")] if sed else []


def _muted(sed, strip) -> bool:
    return strip.mute or sed.channels[strip.channel].mute


def voice_spans(sed, merge: int) -> list[tuple[int, int]]:
    """セリフの音声が鳴っている区間 [(開始, 終わり)]。`merge` フレーム以下の切れ目はつなぐ。"""
    spans: list[list[int]] = []
    sounds = [s for s in sed.strips if s.type == "SOUND" and s.get("tachie_id") and not _muted(sed, s)]
    for s in sorted(sounds, key=lambda s: s.left_handle):
        if spans and s.left_handle - spans[-1][1] <= merge:
            spans[-1][1] = max(spans[-1][1], s.right_handle)
        else:
            spans.append([s.left_handle, s.right_handle])
    return [(a, b) for a, b in spans]


def clear_ducking(scene, names: set[str]) -> None:
    """BGM `names` と、もう無いストリップの volume のキーフレームを消す。"""
    bag = _channelbag(scene)
    if bag is None:
        return
    existing = {s.name for s in scene.sequence_editor.strips_all} if scene.sequence_editor else set()
    for fcurve in list(bag.fcurves):
        m = _PATH_RE.match(fcurve.data_path)
        if m is None or m["attr"] != "volume":
            continue
        name = m["name"].replace('\\"', '"').replace("\\\\", "\\")
        if name in names or name not in existing:
            bag.fcurves.remove(fcurve)


def _key(strip, frame: int, value: float) -> None:
    strip.volume = value
    strip.keyframe_insert("volume", frame=frame)


def refresh_ducking(scene) -> int:
    """BGM の音量のキーフレームを作り直す。下げた区間の数を返す。"""
    sed = scene.sequence_editor
    if sed is None:
        return 0
    settings = scene.tachie
    bgms = bgm_strips(sed)
    clear_ducking(scene, {s.name for s in bgms})
    fps = scene.render.fps / scene.render.fps_base
    fade = max(1, round(settings.duck_fade * fps))
    spans = voice_spans(sed, round(MERGE_SECONDS * fps) + 2 * fade)
    count = 0
    for bgm in bgms:
        base = bgm.get("tachie_bgm_volume", 1.0)
        low = base * settings.duck_volume
        bgm.volume = base
        if low == base:
            continue
        for a, b in spans:
            if b + fade <= bgm.left_handle or a - fade >= bgm.right_handle:
                continue  # この BGM が鳴っていない
            _key(bgm, a - fade, base)
            _key(bgm, a, low)
            _key(bgm, b, low)
            _key(bgm, b + fade, base)
            count += 1
    return count


def ducking_signature(scene) -> str:
    """ダッキングに関わる値（BGM とセリフの音声の位置・ミュート、BGM の音量、下げ方の設定）。BGM が無ければ ""。"""
    sed = scene.sequence_editor
    bgms = bgm_strips(sed)
    if not bgms:
        return ""
    settings = scene.tachie
    items = [("bgm", s.name, s.left_handle, s.right_handle, round(s.get("tachie_bgm_volume", 1.0), 4))
             for s in bgms]
    items += [("line", s.left_handle, s.right_handle, _muted(sed, s))
              for s in sed.strips if s.type == "SOUND" and s.get("tachie_id")]
    return repr((round(settings.duck_volume, 4), round(settings.duck_fade, 3),
                 scene.render.fps, scene.render.fps_base, sorted(items, key=repr)))


def set_bgm(strip, on: bool) -> None:
    """音声ストリップを BGM にする（今の音量を元の音量にする）・BGM から外す（元の音量に戻す）。"""
    if on:
        if not strip.get("tachie_bgm"):
            strip["tachie_bgm_volume"] = strip.volume
            strip.id_properties_ui("tachie_bgm_volume").update(min=0.0, max=100.0, soft_max=2.0,
                                                               description="BGM の音量（セリフの無いところ）")
        strip["tachie_bgm"] = True
        return
    volume = strip.get("tachie_bgm_volume", strip.volume)
    for key in ("tachie_bgm", "tachie_bgm_volume"):
        if key in strip:
            del strip[key]
    scene = strip.id_data
    clear_ducking(scene, {strip.name})
    strip.volume = volume

