"""Blender 内で動かす結合テスト。scripts/smoke.sh から呼ぶ。

キャラ設定を TOML から読み込み、セリフを置き、編集して再生成（後ろがずれること）、表情を変えて全再生成、
TOML への書き出しを確かめ、途中のフレームを PNG に書き出す。
引数: -- <キャラ設定 TOML> <出力フォルダ>
"""

import json
import os
import sys
from pathlib import Path

import addon_utils
import bpy

characters_toml, out = map(Path, sys.argv[sys.argv.index("--") + 1:])
out.mkdir(parents=True, exist_ok=True)

addon_utils.enable("bl_ext.user_default.blender_tachie", default_set=True)
prefs = bpy.context.preferences.addons["bl_ext.user_default.blender_tachie"].preferences
prefs.output_dir = str(out / "tachie")
prefs.voicevox_url = os.environ.get("VOICEVOX_URL", prefs.voicevox_url)

scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1920, 1080, 50
scene.render.fps = 30
sed = scene.sequence_editor_create()
sed.use_prefetch = True  # アドオンの操作の間だけ止めて、終わったら戻すこと（最後に確かめる）
bg = sed.strips.new_effect(name="背景", type="COLOR", channel=1, frame_start=1, length=900)
bg.color = (0.55, 0.7, 0.85)
s = scene.tachie
from bl_ext.user_default.blender_tachie import channels as chmod  # noqa: E402


def lane(role, char=None):
    """そのキャラ × 役割のレーン（チャンネル）。"""
    return chmod.lane(scene, role, char)


def run(op, **kwargs):
    result = op(**kwargs)
    assert result == {"FINISHED"}, (op, result)


def subtitle_texts():
    """見えるセリフの字幕のテキスト（キャラ名・ミュートしたものは除く。改行の \\n は外す）。"""
    return [x.get("tachie_placed_text", x.get("tachie_text", x.text)).replace("\\n", "")
            for x in sed.strips if x.type == "TEXT" and x.get("tachie_id") and x.get("tachie_role") != "name"
            and not x.mute and not sed.channels[x.channel].mute]


def lines():
    """{tachie_id: (left, right, [ストリップ])}（始まり順）"""
    out = {}
    for st in sed.strips:
        cid = st.get("tachie_id")
        if cid:
            l, r, sts = out.get(cid, (10**9, -1, []))
            out[cid] = (min(l, st.left_handle), max(r, st.right_handle), sts + [st])
    return dict(sorted(out.items(), key=lambda kv: kv[1][0]))


def still(name):
    """キャラ `name` の立ち絵（1 本だけ置いている前提）。"""
    found = [st for st in sed.strips if st.get("tachie_still") == name]
    assert len(found) == 1, (name, [st.name for st in found])
    return found[0]


def activate(strip):
    """ストリップを選んでアクティブにする（選ばれていないアクティブなストリップは、パネルもオペレーターも相手にしない）。"""
    strip.select = True
    sed.active_strip = strip


def check_stills():
    """立ち絵は今の長さの連番で、重なるセリフ・表情ストリップに合わせて作り直されている。"""
    for st in sed.strips:
        if st.get("tachie_still"):
            ch = next(c for c in s.characters if c.name == st["tachie_still"])
            assert len(st.elements) == ops.held_elements(st.right_handle - st.left_handle), st.name
            assert st["tachie_faced"] == ops.still_key(bpy.context, sed, ch, st), st.name


def frame_inode(strip, frame):
    """立ち絵の `frame`（シーンのフレーム）の画像の inode。"""
    i = min(frame - int(strip.content_start), len(strip.elements) - 1)  # 最後のコマは前のコマを止めた絵
    return os.stat(Path(bpy.path.abspath(strip.directory)) / strip.elements[i].filename).st_ino


def cache_file(strip, frame):
    """立ち絵の `frame` の画像の、合成キャッシュの中の元（表情のフォルダ, e<目>_<口>.png）。"""
    ino = frame_inode(strip, frame)
    for p in ops.shared_cache_dir().glob("*/*/*.png"):  # 共有キャッシュ/<素材>/<表情>/e<目>_<口>.png
        if p.stat().st_ino == ino:
            return p.parent.name, p.name
    raise AssertionError(f"{strip.name} の {frame} フレームの画像がキャッシュにありません")


def mouth(strip, frame):
    """立ち絵の `frame` の口（l0 = 閉じ、l<n> = 音量の段階、v<母音> = あいうえお）。"""
    return cache_file(strip, frame)[1].split("_", 1)[1].removesuffix(".png")


def expression_dir(strip, frame):
    return cache_file(strip, frame)[0]


def cache_dir(ch, preset):
    return ops.character_cache(bpy.context, ch, ops.preset_parts(ch, preset)).directory.name


def dump(title):
    print(f"--- {title}")
    for cid, (l, r, sts) in lines().items():
        print(f"  {l:4d}-{r:4d} {sts[0]['tachie_character']}: {sts[0]['tachie_text']}  "
              f"[{', '.join(f'{x.type}@{x.channel}' for x in sorted(sts, key=lambda x: x.channel))}]")


# キャラ設定の読み込み
run(bpy.ops.tachie.import_characters, filepath=str(characters_toml))
assert [c.name for c in s.characters] == ["ずんだもん", "四国めたん"], [c.name for c in s.characters]
zunda, metan = s.characters
assert zunda.part_other1 == "左腕_基本" and metan.part_body == "白ロリ服"
assert metan.flip_x and abs(metan.position_x + 0.28) < 1e-6
# チャンネルはキャラ × 役割のレーン（10 番から、役割ごとに並べる）。チャンネルに名前が付く
assert [(x.channel, x.label) for x in chmod.scene_layout(scene).lanes] == [
    (10, "ずんだもん 音声"), (11, "四国めたん 音声"), (12, "ずんだもん 立ち絵"), (13, "ずんだもん 表情"),
    (14, "四国めたん 立ち絵"), (15, "四国めたん 表情"), (16, "ずんだもん 字幕"), (17, "四国めたん 字幕"), (18, "見出し")]
assert (zunda.channel, metan.channel, s.headline_channel) == (12, 14, 18)
assert sed.channels[14].name == "四国めたん 立ち絵" and sed.channels[18].name == "見出し"
assert bg.channel == 1  # レーンより下（背景など）は動かさない
metan.flip_x = False
zunda.position_x = 0.28

# 立ち絵は別に置く（セリフは音声と字幕だけ）。立ち絵は重なるそのキャラのセリフの間だけ口パクする
from bl_ext.user_default.blender_tachie import operators as ops  # noqa: E402

scene.frame_current = 1
s.still_seconds = 30.0
for index in (0, 1):
    s.active_character, s.expression = index, ""
    scene.frame_current = 1
    run(bpy.ops.tachie.add_still)
assert still("ずんだもん").channel == zunda.channel and still("四国めたん").channel == metan.channel
assert len(still("ずんだもん").elements) == 899  # 最後の 1 コマは止めた絵（お尻を伸ばせるように）

scene.frame_current = 1
for index, text in [(0, "こんにちは、ずんだもんなのだ。"), (1, "四国めたんよ。"), (0, "今日は Blender で動画を作るのだ。")]:
    s.active_character, s.text = index, text
    run(bpy.ops.tachie.add_line)
dump("追加")
check_stills()
before = list(lines().values())
assert len(before) == 3 and all(len(v[2]) == 2 for v in before)  # 音声と字幕
assert not any(x.type == "IMAGE" for _, _, sts in before for x in sts), "セリフに立ち絵がある"
assert before[0][1] == before[1][0] and before[1][1] == before[2][0], "隙間なく並んでいない"
# ずんだもんの立ち絵はずんだもんのセリフの間だけ口が動き、めたんのセリフの間は閉じている
(zl, zr, _), (ml, mr, _) = before[0], before[1]
assert len({mouth(still("ずんだもん"), f) for f in range(zl, zr)}) > 1
assert {mouth(still("ずんだもん"), f) for f in range(ml, mr)} == {"l0"}
assert len({mouth(still("四国めたん"), f) for f in range(ml, mr)}) > 1
assert {mouth(still("四国めたん"), f) for f in range(zl, zr)} == {"l0"}
assert mouth(still("ずんだもん"), 900) == "l0"  # セリフの後ろも閉じた口

# 2 本目を長くして再生成 → 3 本目が後ろにずれる（立ち絵は動かず、口パクの区間が付いてくる）
second_id = list(lines())[1]
target = next(x for x in lines()[second_id][2] if x.type == "TEXT")
target["tachie_text"] = "四国めたんよ。「動く立ち絵」の素材をそのまま読み込めるのね。"
activate(target)
run(bpy.ops.tachie.regenerate)
dump("2 本目を長くして再生成")
check_stills()
after = list(lines().values())
delta = after[1][1] - before[1][1]
assert delta > 0 and after[2][0] == before[2][0] + delta, (delta, after[2][0], before[2][0])
assert after[1][0] == before[1][0]
assert still("ずんだもん").left_handle == 1 and len(still("ずんだもん").elements) == 899
# 選択を外したアクティブなストリップ（Blender は覚えている）には、再生成などが効かない
for x in sed.strips_all:
    x.select = False
assert sed.active_strip is not None and not bpy.ops.tachie.regenerate.poll()
sed.active_strip = still("ずんだもん")
assert not bpy.ops.tachie.remake_still.poll() and not bpy.ops.tachie.flip_still.poll()

# 表情を変えて全再生成（位置と長さは変わらない。立ち絵も作り直す）
zunda.part_eye, zunda.part_eyebrow = "ジト目", "困り眉1"
spans = [(l, r) for l, r, _ in lines().values()]
run(bpy.ops.tachie.regenerate_all, character_only=True)
dump("ずんだもんの表情を変えて再生成")
check_stills()
assert [(l, r) for l, r, _ in lines().values()] == spans
assert expression_dir(still("ずんだもん"), 1) == ops.character_cache(bpy.context, zunda, "").directory.name
# パーツを選び直すと、自動の反映で立ち絵を作り直す（画面ではハンドラがやる）
signature, old_dir = ops.faces_signature(scene), cache_dir(zunda, "")
zunda.part_eyebrow = "怒り眉"
assert ops.faces_signature(scene) != signature
ops._apply_faces_later()
check_stills()
assert expression_dir(still("ずんだもん"), 1) == cache_dir(zunda, "") != old_dir
assert len({mouth(still("ずんだもん"), f) for f in range(*list(lines().values())[0][:2])}) > 1  # 口パクはそのまま
zunda.part_eyebrow = "困り眉1"
ops._apply_faces_later()
assert expression_dir(still("ずんだもん"), 1) == old_dir

# セリフごとの声: 置いたときのキャラの既定値が音声ストリップにコピーされ、あとで既定値を変えても変わらない
zunda.speed_scale = 1.1
first_id = next(iter(lines()))
sound = lambda cid: next(x for x in ops.line_strips(sed, cid) if x.type == "SOUND")
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.0) < 1e-6
l0, r0, _ = lines()[first_id]
second_start = list(lines().values())[1][0]
# 1 本だけ速くして再生成 → 短くなり、後ろが詰まる
sound(first_id)["tachie_voice_speed_scale"] = 1.5
activate(sound(first_id))
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
l1, r1, _ = lines()[first_id]
assert l1 == l0 and r1 < r0, (l0, r0, l1, r1)
assert list(lines().values())[1][0] == second_start - (r0 - r1)
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.5) < 1e-6
check_stills()
# スタイルを変える
ops._style_items = [("1", "ずんだもん（あまあま）", "", 1)]
run(bpy.ops.tachie.pick_speaker, style="1", target="LINE")
first_id = sed.active_strip["tachie_id"]
assert all(x["tachie_speaker"] == 1 for x in ops.line_strips(sed, first_id))
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.5) < 1e-6
# キャラクターを変える → そのキャラのチャンネル・話者・声になり、そのキャラの立ち絵が口パクする
run(bpy.ops.tachie.pick_line_character, character="四国めたん")
first_id = sed.active_strip["tachie_id"]
fl, fr, _ = lines()[first_id]
assert sound(first_id).channel == lane("audio", "四国めたん") and sound(first_id)["tachie_character"] == "四国めたん"
assert sound(first_id)["tachie_speaker"] == metan.speaker
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.0) < 1e-6
check_stills()
assert len({mouth(still("四国めたん"), f) for f in range(fl, fr)}) > 1
assert {mouth(still("ずんだもん"), f) for f in range(fl, fr)} == {"l0"}
run(bpy.ops.tachie.pick_line_character, character="ずんだもん")
assert abs(sound(sed.active_strip["tachie_id"])["tachie_voice_speed_scale"] - 1.1) < 1e-6
# キャラクターの声に戻す
first_id = sed.active_strip["tachie_id"]
sound(first_id)["tachie_voice_pitch_scale"] = 0.1
activate(sound(first_id))
run(bpy.ops.tachie.reset_line_voice)
assert abs(sound(sed.active_strip["tachie_id"])["tachie_voice_pitch_scale"]) < 1e-6
# 発音: 自動の読みが入り、編集すると再生成でも保たれ、テキストを変えると自動に戻る
first_id = sed.active_strip["tachie_id"]
snd = sound(first_id)
auto = snd["tachie_kana"]
assert auto and auto == snd["tachie_kana_auto"], auto
print("発音:", auto)
head, sep, rest = auto.partition("、")
head = head.replace("'", "")
edited = head[0] + "'" + head[1:] + sep + rest  # 最初の句のアクセントを 1 モーラ目に動かす
assert edited != auto
snd["tachie_kana"] = edited
activate(snd)
run(bpy.ops.tachie.preview_line)
run(bpy.ops.tachie.regenerate)
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == edited and snd["tachie_kana_auto"] == auto
run(bpy.ops.tachie.reset_kana)
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == auto
# 発音を編集のダイアログ（execute だけ）: その発音で作り直す。空にすると自動の読み
run(bpy.ops.tachie.edit_kana, text=edited)
assert sound(sed.active_strip["tachie_id"])["tachie_kana"] == edited
run(bpy.ops.tachie.edit_kana, text="")
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == auto
snd["tachie_kana"] = "ア'"
snd["tachie_text"] = "こんにちは、ずんだもんだよ。"
activate(snd)
run(bpy.ops.tachie.regenerate)  # テキストを変えたので編集した発音は使わない
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == snd["tachie_kana_auto"] != "ア'"
# 読めない発音はエラーで、置いてあるセリフはそのまま
snd["tachie_kana"] = "abc"
activate(snd)
try:
    bpy.ops.tachie.regenerate()
    raise AssertionError("読めない発音で再生成できた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
assert sound(sed.active_strip["tachie_id"])["tachie_kana"] == "abc"
run(bpy.ops.tachie.reset_kana)
# テキストの最後の {発音} は再生成でも発音として使い、字幕には残さない
snd = sound(sed.active_strip["tachie_id"])
inline = snd["tachie_kana_auto"]
sed.active_strip["tachie_text"] = snd["tachie_text"] + "{" + inline + "}"
run(bpy.ops.tachie.preview_line)
run(bpy.ops.tachie.regenerate)
assert sed.active_strip["tachie_text"] == "こんにちは、ずんだもんだよ。", sed.active_strip["tachie_text"]
assert sound(sed.active_strip["tachie_id"])["tachie_kana"] == inline
# ユーザー辞書（試験用の単語を登録して消す）
wm = bpy.context.window_manager
wm.tachie_dict.surface, wm.tachie_dict.pronunciation, wm.tachie_dict.accent_type = "立絵試験語", "たちえしけんご", 1
run(bpy.ops.tachie.dict_add)
word = next(w for w in ops._dict_words if w[1] == "立絵試験語")
assert word[2] == "タチエシケンゴ" and word[3] == 1
run(bpy.ops.tachie.dict_remove, uuid=word[0])
assert not any(w[1] == "立絵試験語" for w in ops._dict_words)
# 抑揚の編集（ダイアログは画面が要るので、中身を入れて execute だけ呼ぶ）
first_id = sed.active_strip["tachie_id"]
activate(sound(first_id))
l0, r0, _ = lines()[first_id]
edit = wm.tachie_accent
strips_, text_, ch_, speaker_, kana_ = ops._line_context(bpy.context)
ops.load_accent_edit(edit, ops._fresh_accent(bpy.context, text_, speaker_, kana_))
edit.clip_id, edit.text, edit.kana, edit.speaker = first_id, text_, kana_ or "", speaker_
phrase = edit.phrases[0]
before = [m.pitch for m in phrase.moras]
phrase.shift = 0.3  # 句全体を上げる
assert all(abs(m.pitch - (b + 0.3 if m.voiced else b)) < 1e-4 for m, b in zip(phrase.moras, before))
for m in phrase.moras:
    m.vowel_length *= 2  # 最初の句をゆっくり
run(bpy.ops.tachie.preview_accent)
run(bpy.ops.tachie.edit_accent)
first_id = sed.active_strip["tachie_id"]
l1, r1, _ = lines()[first_id]
assert l1 == l0 and r1 > r0, (l0, r0, l1, r1)
saved = __import__("json").loads(sound(first_id)["tachie_accent"])
assert abs(saved[0]["moras"][0]["pitch"] - phrase.moras[0].pitch) < 1e-4
# 再生成しても抑揚の編集は残る（長さも同じ）
activate(sound(first_id))
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
assert sound(first_id).get("tachie_accent") and lines()[first_id][1] == r1
run(bpy.ops.tachie.preview_line)
# 抑揚の編集を消すと元の長さに戻る
run(bpy.ops.tachie.clear_accent)
first_id = sed.active_strip["tachie_id"]
assert not sound(first_id).get("tachie_accent") and lines()[first_id][1] == r0
# 確定せずに閉じた抑揚の編集は、同じセリフをもう一度開くと続きから（セリフが変わっていれば捨てる）
activate(sound(first_id))
strips_, text_, ch_, speaker_, kana_ = ops._line_context(bpy.context)
fresh_ = ops._fresh_accent(bpy.context, text_, speaker_, kana_)
ops.start_accent_edit(edit, first_id, text_, kana_ or "", speaker_, ch_.name, fresh_)
assert not edit.resumed
edit.phrases[0].moras[0].vowel_length += 0.2
ops.start_accent_edit(edit, first_id, text_, kana_ or "", speaker_, ch_.name, fresh_)
assert edit.resumed and abs(edit.phrases[0].moras[0].vowel_length - fresh_[0]["moras"][0]["vowel_length"] - 0.2) < 1e-4
ops.start_accent_edit(edit, first_id, text_ + "よ", kana_ or "", speaker_, ch_.name, fresh_)
assert not edit.resumed
# 抑揚の編集の棒・アクセントの位置・句の結合と分割（YMM4 の「抑揚を編集」に近い形）
from bl_ext.user_default.blender_tachie.tachie_studio.voicevox.accent import kana_from_phrases  # noqa: E402

activate(sound(first_id))
strips_, text_, ch_, speaker_, kana_ = ops._line_context(bpy.context)
ops.load_accent_edit(edit, ops._fresh_accent(bpy.context, text_, speaker_, kana_))
edit.clip_id, edit.text, edit.kana, edit.speaker = first_id, text_, kana_ or "", speaker_
edit.character, edit.changed = ch_.name, False
m0 = edit.phrases[0].moras[0]
m0.pitch_level = "11"  # いちばん上の段 = 範囲の上端
assert abs(m0.pitch - edit.pitch_hi) < 1e-3 and m0.pitch_level == "11"
n_phrases, len0, len1 = len(edit.phrases), len(edit.phrases[0].moras), len(edit.phrases[1].moras)
assert n_phrases >= 2, n_phrases
run(bpy.ops.tachie.accent_join, phrase=0)
assert len(edit.phrases) == n_phrases - 1 and edit.changed and len(edit.phrases[0].moras) == len0 + len1
run(bpy.ops.tachie.accent_split, phrase=0, mora=len0)
assert len(edit.phrases) == n_phrases and len(edit.phrases[0].moras) == len0
target = edit.phrases[-1]
before_pitch = [round(m.pitch, 3) for m in target.moras]
new_accent = 1 if target.accent != 1 else len(target.moras)
target.moras[new_accent - 1].is_accent = True  # アクセントのボタンを押す → 音高をエンジンで計算し直す
assert target.accent == new_accent and target.moras[new_accent - 1].is_accent
assert [round(m.pitch, 3) for m in target.moras] != before_pitch, before_pitch
run(bpy.ops.tachie.edit_accent)
snd = sound(sed.active_strip["tachie_id"])
saved = __import__("json").loads(snd["tachie_accent"])
assert snd["tachie_kana"] == kana_from_phrases(saved) and saved[-1]["accent"] == target.accent  # 発音も句から作り直す
run(bpy.ops.tachie.reset_kana)
first_id = sed.active_strip["tachie_id"]
assert not sound(first_id).get("tachie_accent")
# 発音を変えると抑揚の編集は捨てる
ops.load_accent_edit(edit, ops._fresh_accent(bpy.context, text_, speaker_, kana_))
edit.clip_id = first_id
edit.phrases[0].moras[0].vowel_length += 0.2
run(bpy.ops.tachie.edit_accent)
first_id = sed.active_strip["tachie_id"]
assert sound(first_id).get("tachie_accent")
snd = sound(first_id)
snd["tachie_kana"] = edited
activate(snd)
run(bpy.ops.tachie.regenerate)
assert not sound(sed.active_strip["tachie_id"]).get("tachie_accent")
run(bpy.ops.tachie.reset_kana)
# 自動の読みが編集したときから変わったら（ユーザー辞書の登録など）抑揚の編集は捨てる
first_id = sed.active_strip["tachie_id"]
ops.load_accent_edit(edit, ops._fresh_accent(bpy.context, text_, speaker_, None))
edit.clip_id, edit.kana = first_id, ""
edit.phrases[0].moras[0].vowel_length += 0.2
run(bpy.ops.tachie.edit_accent)
snd = sound(sed.active_strip["tachie_id"])
assert snd.get("tachie_accent")
key = __import__("json").loads(snd["tachie_accent_key"])
snd["tachie_kana"] = snd["tachie_kana_auto"] = "ア'"  # 編集したときの自動の読みが今と違ったことにする
snd["tachie_accent_key"] = __import__("json").dumps([key[0], "ア'", key[2]], ensure_ascii=False)
activate(snd)
run(bpy.ops.tachie.regenerate)
assert not sound(sed.active_strip["tachie_id"]).get("tachie_accent")
check_stills()
# 音量・パン（すぐ反映）とフェード・余韻（再生成で反映）
first_id = sed.active_strip["tachie_id"]
snd = sound(first_id)
assert snd.volume == 1.0 and snd["tachie_after"] == 0.0
voice_end = snd.right_handle
subtitle_end = ops.line_subtitle(ops.line_strips(sed, first_id)).right_handle
snd.volume, snd.pan = 0.5, -0.5
snd["tachie_after"], snd["tachie_fade_out"] = 0.5, 0.2
activate(snd)
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
snd = sound(first_id)
assert (snd.volume, snd.pan) == (0.5, -0.5) and snd.right_handle == voice_end
subtitle = ops.line_subtitle(ops.line_strips(sed, first_id))
assert subtitle.right_handle == subtitle_end + 15, (subtitle.right_handle, subtitle_end)  # 0.5 秒 = 15 フレーム
assert ops.line_end(ops.line_strips(sed, first_id)) == subtitle.right_handle
# 余韻の間は立ち絵の口を閉じる
assert {mouth(still("ずんだもん"), f) for f in range(voice_end, subtitle.right_handle)} == {"l0"}
from bl_ext.user_default.blender_tachie.tachie_studio.lipsync import read_wav  # noqa: E402

assert read_wav(bpy.path.abspath(snd.sound.filepath)).samples[-1] == 0
check_stills()
zunda.audio_after = 0.3

# 表情ストリップ: 重なる範囲だけ立ち絵の表情が変わり、音声はそのまま
zunda_id = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
wav_before = sound(zunda_id).sound.filepath
l, r = lines()[zunda_id][0], ops.line_end(ops.line_strips(sed, zunda_id))
mid = (l + r) // 2
s.active_character, s.expression = 0, "ドヤ"
scene.frame_current = mid
run(bpy.ops.tachie.add_face)
face = sed.active_strip
assert face["tachie_face"] == "ずんだもん" and face.mute and (face.left_handle, face.right_handle) == (mid, r)
assert face.channel == lane("face", "ずんだもん") and sed.channels[face.channel].name == "ずんだもん 表情"
doya, base = cache_dir(zunda, "ドヤ"), cache_dir(zunda, "")
zs = still("ずんだもん")
assert expression_dir(zs, mid) == doya and expression_dir(zs, r - 1) == doya
assert expression_dir(zs, l) == base and expression_dir(zs, mid - 1) == base and expression_dir(zs, r) == base
assert len({mouth(zs, f) for f in range(mid, r)}) > 1  # 表情ストリップの間も口パクする
assert sound(zunda_id).sound.filepath == wav_before
# 動かして反映（画面ではハンドラが自動でやる）
face.content_start = l
run(bpy.ops.tachie.apply_faces)
zs = still("ずんだもん")
assert expression_dir(zs, l) == doya and expression_dir(zs, r - 1) != doya
# チャンネルが上の表情ストリップが優先
s.expression = "困り"
scene.frame_current = l
run(bpy.ops.tachie.add_face)
face2 = sed.active_strip
face2.duration = 3
run(bpy.ops.tachie.apply_faces)
assert face2.channel > face.channel  # 重なる表情は上（あふれ）に置き、後から置いたほうが優先
komari = cache_dir(zunda, "困り")
zs = still("ずんだもん")
assert expression_dir(zs, l) == komari and expression_dir(zs, l + 3) == doya
# 表情の見本（顔のまわりを切り抜いた合成画像。表情ごとに違う画像）
zunda_ch = s.characters["ずんだもん"]
thumbs = [ops._thumb_path(*ops.expression_thumb(zunda_ch, ops.preset_parts(zunda_ch, name)))
          for name in ("", "困り", "ドヤ")]
assert len({t.read_bytes() for t in thumbs}) == 3, thumbs
ops.expression_icon(zunda_ch, ops.preset_parts(zunda_ch, "困り"))  # 画面が無いとアイコンは 0 だが、落ちないこと
s.active_character = list(s.characters).index(zunda_ch)
run(bpy.ops.tachie.expression_select, index=1)  # 表情プリセットの一覧のボタン
assert zunda_ch.active_expression == 1
# 表情ストリップの表情を変える
activate(face2)
ops._expression_items = [("ドヤ", "ドヤ", "")]
run(bpy.ops.tachie.pick_expression, target="FACE", expression="ドヤ")
assert face2.name.startswith("表情 ずんだもん: ドヤ") and expression_dir(still("ずんだもん"), l) == doya
# 再生成しても表情ストリップに従う
activate(sound(zunda_id))
run(bpy.ops.tachie.regenerate)
zunda_id = sed.active_strip["tachie_id"]
assert expression_dir(still("ずんだもん"), l) == doya
# 喋っていない間（めたんのセリフ）も表情ストリップの表情になる
metan_id = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "四国めたん")
ml, mr, _ = lines()[metan_id]
s.expression = "困り"
scene.frame_current = ml + 2
run(bpy.ops.tachie.add_face)
face3 = sed.active_strip
face3.duration = 5
run(bpy.ops.tachie.apply_faces)
zs = still("ずんだもん")
assert [expression_dir(zs, f) == komari for f in range(ml + 1, ml + 8)] == [False] + [True] * 5 + [False]
assert {mouth(zs, f) for f in range(ml + 2, ml + 7)} == {"l0"}
check_stills()
# 表情ストリップの表情はパーツで持つ（表示用のプリセットの名前を書き換えても見た目は変わらない）
face3["tachie_expression"] = "無い表情"
run(bpy.ops.tachie.apply_faces)
assert expression_dir(still("ずんだもん"), ml + 2) == komari
check_stills()
# 表情ストリップを消すと元に戻る
for f in [face, face2, face3]:
    sed.strips.remove(f)
run(bpy.ops.tachie.apply_faces)
assert expression_dir(still("ずんだもん"), l) == base and expression_dir(still("ずんだもん"), mid) == base
# セリフの表情は、そのセリフの間だけ立ち絵の表情になる（音声は合成し直さない）
activate(sound(zunda_id))
wav_before = sound(zunda_id).sound.filepath
ops._expression_items = [("困り", "困り", "")]
zs_before = still("ずんだもん")
zs_ptr, zs_channel = zs_before.as_pointer(), zs_before.channel
run(bpy.ops.tachie.pick_expression, target="LINE", expression="困り")
assert sed.active_strip["tachie_id"] == zunda_id and sound(zunda_id).sound.filepath == wav_before
zs = still("ずんだもん")
# 位置・長さが同じ立ち絵は消さずにコマのファイル名だけ書き換える（合成キャッシュの画像を直接指す）
assert zs.as_pointer() == zs_ptr and zs.channel == zs_channel
assert any("/" in e.filename for e in zs.elements) and "faces" not in zs.directory
assert not ops._needs_apply(scene)  # 操作の中で反映済み（自動の反映でもう一度描き直さない）
assert expression_dir(zs, l) == komari and expression_dir(zs, r - 1) == komari
assert expression_dir(zs, l - 1) == base and expression_dir(zs, r) == base
assert len({mouth(zs, f) for f in range(l, r)}) > 1
# 表情ストリップはセリフの表情より優先
s.expression = "ドヤ"
scene.frame_current = l
run(bpy.ops.tachie.add_face)
face = sed.active_strip
assert (face.left_handle, face.right_handle) == (l, r)  # 再生位置のセリフの終わりまで
assert expression_dir(still("ずんだもん"), l) == doya
sed.strips.remove(face)
run(bpy.ops.tachie.apply_faces)
# セリフの表情で表情ストリップを置く（見た目は同じ。セリフの表情はそのまま）
activate(sound(zunda_id))
run(bpy.ops.tachie.line_faces)
moved = [f for f in sed.strips if f.get("tachie_face") == "ずんだもん"]
assert len(moved) == 1 and (moved[0].left_handle, moved[0].right_handle) == (l, r)
assert moved[0]["tachie_expression"] == "困り" and moved[0].mute
assert sed.active_strip == moved[0] and [x for x in sed.strips if x.select] == [moved[0]]  # できたものだけを選ぶ
assert all(x["tachie_expression"] == "困り" for x in ops.line_strips(sed, zunda_id))
assert expression_dir(still("ずんだもん"), l) == komari and expression_dir(still("ずんだもん"), r) == base
assert not ops._needs_apply(scene)
activate(sound(zunda_id))
assert bpy.ops.tachie.line_faces() == {"CANCELLED"}  # 表情ストリップが重なるので二重に置かない
sed.strips.remove(moved[0])
ops._expression_items = [(ops.NONE, "（立ち絵のまま）", "")]
activate(sound(zunda_id))
run(bpy.ops.tachie.pick_expression, target="LINE", expression=ops.NONE)
run(bpy.ops.tachie.apply_faces)
assert expression_dir(still("ずんだもん"), l) == base
# 立ち絵の表情を変えると、セリフの表情が "" のセリフの間もその表情
activate(still("ずんだもん"))
ops._expression_items = [("困り", "困り", "")]
run(bpy.ops.tachie.pick_expression, target="STILL", expression="困り")
zs = still("ずんだもん")
assert zs["tachie_expression"] == "困り" and expression_dir(zs, l) == komari and expression_dir(zs, 1) == komari
assert len({mouth(zs, f) for f in range(l, r)}) > 1
activate(zs)
ops._expression_items = [(ops.NONE, "（基本）", "")]
run(bpy.ops.tachie.pick_expression, target="STILL", expression=ops.NONE)
s.expression = ""
# 立ち絵ごとのパーツ（その立ち絵だけ。表情プリセットの上に重ねる）。セリフの表情が立ち絵のままの間もその見た目
activate(still("ずんだもん"))
ops._part_items = [("怒り眉", "怒り眉", "")]
run(bpy.ops.tachie.pick_part, prop="part_eyebrow", item="STILL", part="怒り眉")
zs = still("ずんだもん")
custom = ops.character_cache(bpy.context, zunda, {"眉": "怒り眉"}).directory.name
assert ops.item_parts(zs) == {"眉": "怒り眉"} and expression_dir(zs, 1) == custom != base
assert expression_dir(zs, l) == custom and len({mouth(zs, f) for f in range(l, r)}) > 1
check_stills()
activate(zs)
ops._part_items = [("右腕_指差し", "右腕_指差し", "")]
run(bpy.ops.tachie.pick_part, prop="part_other2", item="STILL", part="右腕_指差し")  # 他はスロットごと
assert ops.item_parts(still("ずんだもん")) == {"眉": "怒り眉", "他": ["", "右腕_指差し"]}
activate(still("ずんだもん"))
run(bpy.ops.tachie.clear_item_parts)
assert not ops.item_parts(still("ずんだもん")) and expression_dir(still("ずんだもん"), 1) == base
# 表情は選んだときにパーツを写す（後でプリセットを編集しても置いた立ち絵は変わらない。欄には「名前*」と出る）
activate(still("ずんだもん"))
ops._expression_items = [("困り", "困り", "")]
run(bpy.ops.tachie.pick_expression, target="STILL", expression="困り")
assert ops.item_parts(still("ずんだもん")) == ops.preset_parts(zunda, "困り")
assert expression_dir(still("ずんだもん"), 1) == komari
komari_preset = ops.find_expression(zunda, "困り")
old_brow, komari_preset.part_eyebrow = komari_preset.part_eyebrow, "怒り眉"
ops._apply_faces_later()
assert expression_dir(still("ずんだもん"), 1) == komari
assert ops.expression_label(zunda, still("ずんだもん"), "（基本）") == "困り*"
komari_preset.part_eyebrow = old_brow
assert ops.expression_label(zunda, still("ずんだもん"), "（基本）") == "困り"
activate(still("ずんだもん"))
ops._expression_items = [(ops.NONE, "（基本）", "")]
run(bpy.ops.tachie.pick_expression, target="STILL", expression=ops.NONE)
# セリフでもパーツを 1 つずつ変えられる。セリフのパーツは指定したものだけ立ち絵の上に重ねる（立ち絵で変えた眉は残る）
activate(still("ずんだもん"))
ops._part_items = [("怒り眉", "怒り眉", "")]
run(bpy.ops.tachie.pick_part, prop="part_eyebrow", item="STILL", part="怒り眉")
activate(sound(zunda_id))
ops._part_items = [("なごみ目", "なごみ目", "")]
run(bpy.ops.tachie.pick_part, prop="part_eye", item="LINE", part="なごみ目")
assert all(ops.item_parts(x) == {"目": "なごみ目"} for x in ops.line_strips(sed, zunda_id))
zs = still("ずんだもん")
both = ops.character_cache(bpy.context, zunda, {"眉": "怒り眉"}, {"目": "なごみ目"}).directory.name
assert expression_dir(zs, l) == both and expression_dir(zs, r) == custom  # セリフの外は立ち絵のまま
assert len({mouth(zs, f) for f in range(l, r)}) > 1
check_stills()
# セリフの「基本の表情」は立ち絵で変えたパーツも打ち消し、「立ち絵のまま」は何も重ねない
activate(sound(zunda_id))
ops._expression_items = [(ops.BASE, "基本の表情", "")]
run(bpy.ops.tachie.pick_expression, target="LINE", expression=ops.BASE)
assert ops.item_parts(sound(zunda_id)) == ops.base_parts(zunda)
reset = ops.character_cache(bpy.context, zunda, {"眉": "怒り眉"}, ops.base_parts(zunda))
assert expression_dir(still("ずんだもん"), l) == reset.directory.name and reset.expression.eyebrow != "怒り眉"
activate(sound(zunda_id))
ops._expression_items = [(ops.NONE, "（立ち絵のまま）", "")]
run(bpy.ops.tachie.pick_expression, target="LINE", expression=ops.NONE)
assert ops.item_parts(sound(zunda_id)) == {} and expression_dir(still("ずんだもん"), l) == custom
# 前の版のアイテム（表情プリセットの名前だけ。立ち絵は + 立ち絵ごとのパーツ）は、開いたときにパーツを書く
for x in ops.line_strips(sed, zunda_id):
    del x["tachie_look"]
    x["tachie_expression"] = "困り"
zs = still("ずんだもん")
del zs["tachie_look"]
zs["tachie_expression"], zs["tachie_parts"] = "ドヤ", json.dumps({"眉": "怒り眉"}, ensure_ascii=False)
assert ops.migrate_parts(scene) == len(ops.line_strips(sed, zunda_id)) + 1
assert ops.item_parts(sound(zunda_id)) == ops.preset_parts(zunda, "困り")
assert ops.item_parts(still("ずんだもん")) == {**ops.preset_parts(zunda, "ドヤ"), "眉": "怒り眉"}
assert "tachie_parts" not in still("ずんだもん")
ops.set_line_expression(bpy.context, zunda_id, "")
activate(still("ずんだもん"))
ops._expression_items = [(ops.NONE, "（基本）", "")]
run(bpy.ops.tachie.pick_expression, target="STILL", expression=ops.NONE)
assert expression_dir(still("ずんだもん"), 1) == base and expression_dir(still("ずんだもん"), l) == base
check_stills()
# コマの画像（合成キャッシュ）が無ければ作り直す（tachie/cache を消した・別の Mac に持っていったとき）
import shutil  # noqa: E402

zs = still("ずんだもん")
gone = {(Path(bpy.path.abspath(zs.directory)) / e.filename).parent for e in zs.elements}
for folder in gone:
    shutil.rmtree(folder)
assert ops.missing_images(zs)
run(bpy.ops.tachie.apply_faces)
assert not ops.missing_images(still("ずんだもん")) and all(f.is_dir() for f in gone)
check_stills()
# 前の版のプロジェクトごとのキャッシュ（//tachie/cache）を指す立ち絵は、共有キャッシュへ移す
zs = still("ずんだもん")
old_cache = Path(prefs.output_dir) / "cache"
src = Path(bpy.path.abspath(zs.directory))
names = {e.filename for e in zs.elements}
for name in names:
    (old_cache / name).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / name, old_cache / name)
zs.directory = str(old_cache) + os.sep
assert not ops.missing_images(zs)
run(bpy.ops.tachie.apply_faces)
assert ops._under(Path(bpy.path.abspath(still("ずんだもん").directory)), ops.shared_cache_dir()) is not None
shutil.rmtree(old_cache)
check_stills()

# まとめて変える（ダイアログは画面が要るので execute だけ）
def select_only(strips_):
    for x in sed.strips_all:
        x.select = False
    for x in strips_:
        x.select = True


ids_ = list(lines())
zunda_ids = [cid for cid in ids_ if lines()[cid][2][0]["tachie_character"] == "ずんだもん"][:2]
assert len(zunda_ids) == 2
scene.frame_current = lines()[zunda_ids[0]][0]
s.active_character, s.expression = 0, ""
run(bpy.ops.tachie.add_face)
bface = sed.active_strip
select_only([sound(zunda_ids[0]), ops.line_subtitle(ops.line_strips(sed, zunda_ids[1])), bface])
assert ops.selected_targets(sed)[0] == zunda_ids and ops.selected_targets(sed)[2] == [bface]
run(bpy.ops.tachie.batch_edit, use_expression=True, expression="ドヤ")
new_ids = [cid for cid in lines() if lines()[cid][2][0]["tachie_character"] == "ずんだもん"][:2]
assert new_ids == zunda_ids, "表情だけで作り直された"
assert all(lines()[cid][2][0]["tachie_expression"] == "ドヤ" for cid in new_ids)
assert bface["tachie_expression"] == "ドヤ"
# 音量だけ: 作り直さない
select_only([sound(cid) for cid in new_ids])
run(bpy.ops.tachie.batch_edit, use_audio_volume=True, audio_volume=0.4)
assert [cid for cid in lines() if cid in new_ids] == new_ids, "音量だけで作り直された"
assert all(abs(sound(cid).volume - 0.4) < 1e-6 for cid in new_ids)
# 読み上げ速度: 短くなり、テキストと音量は保たれる
before = {cid: (lines()[cid][1] - lines()[cid][0], lines()[cid][2][0]["tachie_text"]) for cid in new_ids}
run(bpy.ops.tachie.batch_edit, use_speed_scale=True, speed_scale=1.6)
after_ids = [cid for cid in lines() if lines()[cid][2][0]["tachie_character"] == "ずんだもん"][:2]
for old_id, cid in zip(new_ids, after_ids):
    assert lines()[cid][1] - lines()[cid][0] < before[old_id][0]
    assert lines()[cid][2][0]["tachie_text"] == before[old_id][1]
    assert abs(sound(cid)["tachie_voice_speed_scale"] - 1.6) < 1e-6 and abs(sound(cid).volume - 0.4) < 1e-6
check_stills()
# 何も変えなければ何もしない
assert bpy.ops.tachie.batch_edit() == {"CANCELLED"}
# 別キャラが混ざると、両方が持っている表情だけ選べる
metan_ids = [cid for cid in lines() if lines()[cid][2][0]["tachie_character"] == "四国めたん"][:1]
select_only([sound(after_ids[0]), sound(metan_ids[0])])
names = [i[0] for i in ops._batch_expressions(type("P", (), {"use_character": False, "character": ""})(), bpy.context)]
common = {e.name for e in zunda.expressions} & {e.name for e in metan.expressions}
assert names[0] == ops.NONE and set(names[1:]) == common and "ドヤ" not in names, names
# 候補が変わっても同じ表情は同じ番号（動的な Enum は番号で値を持つ）
mixed_items = ops._batch_expressions(type("P", (), {"use_character": False, "character": ""})(), bpy.context)
zunda_items = ops._batch_expressions(type("P", (), {"use_character": True, "character": "ずんだもん"})(), bpy.context)
zunda_numbers = {i[0]: i[3] for i in zunda_items}
assert all(zunda_numbers[i[0]] == i[3] for i in mixed_items) and len(set(zunda_numbers.values())) == len(zunda_items)
sed.strips.remove(bface)
for cid in after_ids:
    ops.set_line_expression(bpy.context, cid, "")
run(bpy.ops.tachie.apply_faces)
check_stills()

# 立ち絵の動き: 喋っている間に跳ねる・揺れる、登場（フェード）・退場（スライド）、立ち絵ごとの大きさ
from bl_ext.user_default.blender_tachie import motion as motion_mod  # noqa: E402


def keyed(strip, attr):
    bag = motion_mod._channelbag(scene)
    path = f'sequence_editor.strips_all["{strip.name}"].{attr}'
    fc = next((f for f in bag.fcurves if f.data_path == path), None) if bag else None
    return [tuple(k.co) for k in fc.keyframe_points] if fc else []


zunda.talk_bounce, zunda.talk_sway = 0.02, 3.0  # 変えるとすぐ作り直す
zid = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
zl, zr = lines()[zid][0], ops.line_end(ops.line_strips(sed, zid))
zimg = still("ずんだもん")
ys = keyed(zimg, "transform.offset_y")
assert len(ys) > 4 and max(v for _, v in ys) > 0, ys
assert any(zl <= f < zr for f, v in ys if v > 0), (zl, zr, ys)  # ずんだもんのセリフの間
metan_spans = [(l_, ops.line_end(sts)) for _, (l_, _, sts) in lines().items() if sts[0]["tachie_character"] == "四国めたん"]
assert not any(a_ <= f < b_ and v > 0 for f, v in ys for a_, b_ in metan_spans)  # めたんのセリフの間は跳ねない
assert keyed(zimg, "transform.rotation")
peak = next(f for f, v in ys if v > 0)
scene.frame_set(int(peak))
assert zimg.transform.offset_y > 0
zunda.enter_effect, zunda.exit_effect = "FADE", "SLIDE"
assert [v for _, v in keyed(zimg, "blend_alpha")][:2] == [0.0, 1.0]
assert len(keyed(zimg, "transform.offset_x")) == 2
# セリフを動かすと、動きも口パクもついてくる（画面ではハンドラが自動でやる）
homes = {x.name: x.channel for x in ops.line_strips(sed, zid)}
for i, x in enumerate(ops.line_strips(sed, zid)):
    x.channel = 70 + i  # 空いているチャンネルへ逃がしてから動かす
for x in ops.line_strips(sed, zid):
    x.content_start += 400
ops.refresh_faces(bpy.context)
zimg = still("ずんだもん")
assert any(zl + 400 <= f < zr + 400 for f, v in keyed(zimg, "transform.offset_y") if v > 0)
assert len({mouth(zimg, f) for f in range(zl + 400, zr + 400)}) > 1
assert {mouth(zimg, f) for f in range(zl, zr)} == {"l0"}
for x in ops.line_strips(sed, zid):
    x.content_start -= 400
for x in ops.line_strips(sed, zid):
    x.channel = homes[x.name]
assert all(x.channel == homes[x.name] for x in ops.line_strips(sed, zid))
ops.refresh_faces(bpy.context)
check_stills()
# 立ち絵を手で別のチャンネルに動かしても、動きは付く
moved, home = still("ずんだもん"), still("ずんだもん").channel
moved.channel = 60
assert moved.channel == 60
ops.refresh_motion_now(scene)
assert keyed(moved, "transform.offset_y"), "動かした立ち絵に動きが付かない"
moved.channel = home
assert moved.channel == home
# 立ち絵を縮めて伸ばすと作り直す（今の長さの連番になる）
moved.duration = 599
run(bpy.ops.tachie.apply_faces)
assert len(still("ずんだもん").elements) == 598
still("ずんだもん").duration = 900
run(bpy.ops.tachie.apply_faces)
assert len(still("ずんだもん").elements) == 899  # 最後の 1 コマは止めた絵（お尻を伸ばせるように）
check_stills()
# 立ち絵ごとの大きさ（足元が中心）。セリフを再生成して立ち絵を作り直しても残る
zimg = still("ずんだもん")
zimg["tachie_scale"] = 1.5
ops.refresh_motion_now(scene)
assert abs(zimg.transform.scale_x - 1.5) < 1e-6 and tuple(zimg.transform.origin) == (0.5, 0.0)
activate(sound(zid))
run(bpy.ops.tachie.regenerate)
zid = sed.active_strip["tachie_id"]
assert abs(still("ずんだもん")["tachie_scale"] - 1.5) < 1e-6 and keyed(still("ずんだもん"), "transform.offset_y")
# 動きを切るとキーフレームは消える
zunda.talk_bounce = zunda.talk_sway = 0.0
zunda.enter_effect = zunda.exit_effect = "NONE"
still("ずんだもん")["tachie_scale"] = 1.0
ops.refresh_motion_now(scene)
assert not keyed(still("ずんだもん"), "transform.offset_y") and not keyed(still("ずんだもん"), "blend_alpha")
assert abs(still("ずんだもん").transform.scale_x - 1.0) < 1e-6

# 字幕の帯・キャラ名・フェードイン、見出しテロップ
metan.subtitle_box, metan.box_roundness = True, 0.5
msubs = [x for x in sed.strips if x.type == "TEXT" and x.get("tachie_character") == "四国めたん"]
assert msubs and all(x.use_box and abs(x.box_roundness - 0.5) < 1e-6 for x in msubs)
metan.show_name, metan.subtitle_fade = True, 0.2
mid = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "四国めたん")
activate(sound(mid))
run(bpy.ops.tachie.regenerate)
mid = sed.active_strip["tachie_id"]
mstrips = ops.line_strips(sed, mid)
name_strip = next(x for x in mstrips if x.get("tachie_role") == "name")
msub = next(x for x in mstrips if x.type == "TEXT" and x.get("tachie_role") != "name")
assert name_strip.text == "四国めたん" and name_strip.channel == lane("name", "四国めたん")
# キャラ名を出すとキャラ名のレーンが増え、見出しは上へずれる
assert sed.channels[lane("name", "四国めたん")].name == "四国めたん キャラ名" and lane("headline") == 19
assert msub.channel == lane("subtitle", "四国めたん") < name_strip.channel
assert name_strip.location[1] > msub.location[1] and name_strip.left_handle == msub.left_handle
assert [v for _, v in keyed(msub, "blend_alpha")] == [0.0, 1.0] and keyed(name_strip, "blend_alpha")
s.headline_text, s.headline_seconds = "第 1 章 Blender で動画", 2.0
scene.frame_current = 1
run(bpy.ops.tachie.add_headline)
head = sed.active_strip
assert head["tachie_headline"] and head.text == "第 1 章 Blender で動画" and head.use_box
head.text = "第 1 章 Blender で動画を作る"  # ストリップで直した文字はスタイルを変えても残す
s.headline_size = 120
assert head.font_size == 120 and head.text == "第 1 章 Blender で動画を作る"  # スタイルはすぐ反映
metan.show_name, metan.subtitle_fade, metan.subtitle_box = False, 0.0, False
activate(sound(mid))
run(bpy.ops.tachie.regenerate)
assert not any(x.get("tachie_role") == "name" for x in sed.strips)
# フェードしない字幕に手で付けた不透明度は、動きを作り直しても消さない
zsub = next(x for x in sed.strips if x.type == "TEXT" and x.get("tachie_character") == "ずんだもん")
zsub.blend_alpha = 0.5
ops.refresh_motion_now(scene)
assert abs(zsub.blend_alpha - 0.5) < 1e-6 and not any(x.get("tachie_fade_keyed") for x in sed.strips)
zsub.blend_alpha = 1.0

# BGM のダッキング: セリフの音声が鳴っている間だけ BGM の音量を下げる
import wave  # noqa: E402

bgm_path = out / "bgm.wav"
with wave.open(str(bgm_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\0\0" * 8000 * 120)
bgm = sed.strips.new_sound(name="BGM", filepath=str(bgm_path), channel=30, frame_start=1)
for x in sed.strips_all:
    x.select = False
bgm.select, bgm.volume = True, 0.8
run(bpy.ops.tachie.set_bgm)
assert bgm["tachie_bgm"] and abs(bgm["tachie_bgm_volume"] - 0.8) < 1e-6
from bl_ext.user_default.blender_tachie import ducking  # noqa: E402

voice = ducking.voice_spans(sed, 0)
keys = keyed(bgm, "volume")
low = 0.8 * s.duck_volume
assert voice and keys
assert any(f == voice[0][0] and abs(v - low) < 1e-4 for f, v in keys)  # 喋り始めで下がっている
assert all(abs(v - 0.8) < 1e-4 or abs(v - low) < 1e-4 for _, v in keys)
s.duck_volume = 0.5  # 設定を変えるとすぐ作り直す
assert all(abs(v - 0.8) < 1e-4 or abs(v - 0.4) < 1e-4 for _, v in keyed(bgm, "volume"))
bgm["tachie_bgm_volume"] = 0.6
ducking.refresh_ducking(scene)
assert abs(max(v for _, v in keyed(bgm, "volume")) - 0.6) < 1e-4
run(bpy.ops.tachie.unset_bgm, strip=bgm.name)
assert not keyed(bgm, "volume") and abs(bgm.volume - 0.6) < 1e-6 and "tachie_bgm" not in bgm
sed.strips.remove(bgm)
s.duck_volume = 0.3

# 効果音: 登録して、再生位置と、選んだセリフの頭に置く
se_path = out / "se.wav"
with wave.open(str(se_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\0\0" * 4000)
effects = bpy.context.preferences.addons["bl_ext.user_default.blender_tachie"].preferences.sound_effects
bad_se = out / "bad_se.mp3"
bad_se.write_text("not audio")
count = len(effects)
try:
    bpy.ops.tachie.register_sound_effect(filepath=str(bad_se))
except RuntimeError:
    pass  # 音声として読めないファイルは登録しない（ERROR の報告はバックグラウンドでは例外になる）
assert len(effects) == count
run(bpy.ops.tachie.register_sound_effect, filepath=str(se_path))
assert effects[-1].name == "se"
effects[-1].volume = 0.7
se_index = len(effects) - 1
s.se_at_lines = False
scene.frame_current = 5
run(bpy.ops.tachie.add_sound_effect, index=se_index)
se_strip = sed.active_strip
assert se_strip["tachie_se"] == "se" and se_strip.left_handle == 5 and abs(se_strip.volume - 0.7) < 1e-6
assert se_strip.channel == 2  # 効果音はレーンより下の空き（1 番は背景）
s.se_at_lines = True
heads = sorted(v[0] for v in lines().values())[:2]
for x in sed.strips_all:
    x.select = False
for cid, (left, _, sts) in lines().items():
    if left in heads:
        sts[0].select = True
run(bpy.ops.tachie.add_sound_effect, index=se_index)
placed = sorted(x.left_handle for x in sed.strips if x.get("tachie_se") and x.select)
assert placed == heads, (placed, heads)
assert all(x.channel < s.channel_base for x in sed.strips if x.get("tachie_se"))
for x in [x for x in sed.strips if x.get("tachie_se")]:
    sed.strips.remove(x)
run(bpy.ops.tachie.remove_sound_effect, index=se_index)
assert len(effects) == se_index
s.se_at_lines = False

# 同時に喋るセリフ: 別のキャラなら重ねて置ける（音声・字幕・キャラ名は上の空いたチャンネル）。同じキャラは重ねられない
frame_end_before = scene.frame_end
t0 = max(r for _, r, _ in lines().values()) + 30
metan.show_name = True
scene.frame_current = t0
s.active_character, s.expression, s.text = 0, "", "同時に喋るのだ。"
run(bpy.ops.tachie.add_line)
scene.frame_current = t0 + 5
s.active_character, s.text = 1, "同時に喋りますわね、ずんだもんさん。"
run(bpy.ops.tachie.add_line)
both = {sts[0]["tachie_text"]: sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"].startswith("同時に")}
zsts, msts = both["同時に喋るのだ。"], both["同時に喋りますわね、ずんだもんさん。"]
role = lambda sts, kind: next(x for x in sts if (x.type, x.get("tachie_role")) == kind)
SOUND, SUB, NAME = ("SOUND", None), ("TEXT", None), ("TEXT", "name")
# キャラごとのレーンなので、同時に喋ってもそれぞれのレーンのまま（あふれない）
assert role(zsts, SOUND).channel == lane("audio", "ずんだもん") and role(zsts, SUB).channel == lane("subtitle", "ずんだもん")
msound, msub, mname = role(msts, SOUND), role(msts, SUB), role(msts, NAME)
assert (msound.channel, msub.channel, mname.channel) == (
    lane("audio", "四国めたん"), lane("subtitle", "四国めたん"), lane("name", "四国めたん"))
assert msound.left_handle == t0 + 5 and msound.left_handle < role(zsts, SOUND).right_handle
scene.frame_current = t0 + 10  # 同じキャラでも重ねて置ける（レーンが埋まっていれば上の空き）
s.active_character, s.text = 0, "かぶせるのだ。"
run(bpy.ops.tachie.add_line)
over = next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"] == "かぶせるのだ。")
assert role(over, SOUND).channel > chmod.scene_layout(scene).top and role(over, SOUND).left_handle == t0 + 10
for x in over:
    sed.strips.remove(x)
# 縮めて再生成: 同時に喋るめたんのセリフは動かさず、その後ろのめたんのセリフはそこに重ならないところまで詰める
zl, zr, _ = next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋るのだ。")
ml, mr, _ = next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋りますわね、ずんだもんさん。")
assert zl < ml < zr < mr, (zl, zr, ml, mr)
scene.frame_current = mr
s.active_character, s.text = 1, "同時に続けますわ。"
run(bpy.ops.tachie.add_line)
s.ripple = True
activate(role(zsts, SOUND))
sed.active_strip["tachie_text"] = "同時に。"
run(bpy.ops.tachie.regenerate)
(cl, cr, csts), = [v for v in lines().values() if v[2][0]["tachie_text"] == "同時に続けますわ。"]
assert cl >= mr and role(csts, SOUND).channel == lane("audio", "四国めたん"), (cl, mr)
assert next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋りますわね、ずんだもんさん。")[:2] == (ml, mr)
# 字幕をセリフごとにずらす（キャラ名も一緒に動く）。再生成・スタイル変更でも残る
name_y = mname.location[1]
msub["tachie_sub_x"], msub["tachie_sub_y"] = -0.1, 0.3
ops.refresh_motion_now(scene)
assert abs(msub.location[0] - 0.4) < 1e-6 and abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6
assert abs(mname.location[1] - min(0.98, name_y + 0.3)) < 1e-6 and abs(mname.location[0] - 0.4) < 1e-6
activate(msound)
run(bpy.ops.tachie.regenerate)
msts = ops.line_strips(sed, sed.active_strip["tachie_id"])
msub = role(msts, SUB)
assert abs(msub["tachie_sub_y"] - 0.3) < 1e-6 and abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6
assert role(msts, SOUND).channel == lane("audio", "四国めたん") and msub.channel == lane("subtitle", "四国めたん")
metan.font_size += 2
assert abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6 and abs(role(msts, NAME).location[0] - 0.4) < 1e-6
metan.font_size -= 2
# プレビューで動かした字幕: 置いた位置（tachie_placed_loc）から動いたものを、ずらす量に書き戻す（キャラ名もついてくる）
from bl_ext.user_default.blender_tachie.properties import name_height  # noqa: E402

mname = role(msts, NAME)
assert not motion_mod.moved_texts(scene)
msub.location = (0.45, metan.subtitle_y + 0.1)
assert motion_mod.moved_texts(scene) == [msub]
ops._apply_faces_later()  # 自動の反映（ドラッグが終わったあと）
assert abs(msub["tachie_sub_x"] + 0.05) < 1e-5 and abs(msub["tachie_sub_y"] - 0.1) < 1e-5
assert abs(mname.location[0] - 0.45) < 1e-5 and abs(mname["tachie_sub_y"] - 0.1) < 1e-5
assert not motion_mod.moved_texts(scene)
# キャラ名を動かしたら、キャラ名の基準の位置からの差を字幕のずらす量にする
base = name_height(metan, mname.get("tachie_placed_text", mname["tachie_text"]))
mname.location = (0.5, base + 0.05)
ops._apply_faces_later()
assert abs(msub["tachie_sub_x"]) < 1e-5 and abs(msub["tachie_sub_y"] - 0.05) < 1e-5
assert abs(msub.location[1] - (metan.subtitle_y + 0.05)) < 1e-5 and abs(msub.location[0] - 0.5) < 1e-5
# 解像度を変えてキャラ名の基準の位置がずれただけ（置き直していない）のものは、動かしたとみなさない
res = scene.render.resolution_y
scene.render.resolution_y = res // 2
assert not motion_mod.moved_texts(scene) and motion_mod.sync_text_offsets(scene) == 0
scene.render.resolution_y = res
# undo で「動かした位置 + 前のずらす量」の段に戻っても、置いた位置と比べるので動かした方を使う
placed = list(msub["tachie_placed_loc"])
msub["tachie_sub_y"] = 0.2
msub.location = (0.5, metan.subtitle_y + 0.05)
assert motion_mod.sync_text_offsets(scene) == 0  # 置いた位置のまま（ずらす量とずれていても動かしていない）
msub.location = (0.5, metan.subtitle_y + 0.12)
msub["tachie_placed_loc"] = placed
assert motion_mod.sync_text_offsets(scene) == 1 and abs(msub["tachie_sub_y"] - 0.12) < 1e-5
ops.refresh_motion_now(scene)
# セリフごとの字幕の文字の大きさ・折り返し幅（px。0 はキャラの設定）。キャラ名の高さも行数に合わせる
assert msub.text.count("\n") == 0 and abs(msub.font_size - metan.font_size) < 1e-6
name_y = mname.location[1]
msub["tachie_sub_width"] = 300.0
ops._apply_faces_later()
assert msub.text.count("\n") >= 2 and mname["tachie_sub_width"] == 300.0 and mname.location[1] > name_y
msub["tachie_sub_size"] = 30.0
ops._apply_faces_later()
assert abs(msub.font_size - 30.0) < 1e-6 and mname["tachie_sub_size"] == 30.0
activate(role(msts, SOUND))
run(bpy.ops.tachie.regenerate)  # 再生成でも残る
msts = ops.line_strips(sed, sed.active_strip["tachie_id"])
msub, mname = role(msts, SUB), role(msts, NAME)
assert ops.text_style(msub) == (30.0, 300.0) and abs(msub.font_size - 30.0) < 1e-6
msub["tachie_sub_size"], msub["tachie_sub_width"] = 0.0, 0.0
ops._apply_faces_later()
assert msub.text.count("\n") == 0 and abs(msub.font_size - metan.font_size) < 1e-6
# キャラの折り返し幅（0 は画面の幅の 80%）
metan.subtitle_width = 300.0
assert msub.text.count("\n") >= 2
metan.subtitle_width = 0.0
assert msub.text.count("\n") == 0
# キャラの字幕の位置は YMM4 と同じく画面の中央からの px（X は右が正、Y は字幕の下端で下が正）。キャラ名も一緒に動く
w_px, h_px = scene.render.resolution_x, scene.render.resolution_y
y_before, name_x = metan.subtitle_y, mname.location[0]
assert abs(metan.subtitle_pos_y - (0.5 - y_before) * h_px) < 1e-3
metan.subtitle_pos_x, metan.subtitle_pos_y = 192.0, h_px / 2
dx_sub = msub["tachie_sub_x"]
assert abs(metan.subtitle_x - 192.0 / w_px) < 1e-6 and abs(metan.subtitle_y) < 1e-6
assert abs(msub.location[0] - (0.5 + 192.0 / w_px + dx_sub)) < 1e-5 and abs(mname.location[0] - (name_x + 192.0 / w_px)) < 1e-5
assert abs(msub.location[1] - msub["tachie_sub_y"]) < 1e-5  # 下端が画面の下端
metan.subtitle_x, metan.subtitle_y = 0.0, y_before
# セリフごとの字幕の位置も px（画面の中央からの、そのセリフの字幕の X / Y）。中身はキャラの位置からのずれ（割合）
activate(msub)
s.line_sub_pos_x, s.line_sub_pos_y = -96.0, h_px / 2 - 100.0
assert abs(msub["tachie_sub_x"] - (-96.0 / w_px - metan.subtitle_x)) < 1e-6
assert abs(msub["tachie_sub_y"] - (100.0 / h_px - metan.subtitle_y)) < 1e-6
assert mname["tachie_sub_x"] == msub["tachie_sub_x"] and abs(msub.location[0] - (0.5 - 96.0 / w_px)) < 1e-5
assert abs(msub.location[1] - 100.0 / h_px) < 1e-5 and abs(s.line_sub_pos_x + 96.0) < 1e-3
msub["tachie_sub_x"] = msub["tachie_sub_y"] = 0.0
ops.refresh_motion_now(scene)
cue_texts = subtitle_texts()
assert any("同時に。" in t for t in cue_texts) and any("同時に喋りますわね" in t for t in cue_texts)
assert any(a <= t0 and b >= role(msts, SOUND).right_handle for a, b in ducking.voice_spans(sed, 0))
for x in [x for x in sed.strips if x.get("tachie_text", "").startswith("同時に")]:
    sed.strips.remove(x)
metan.show_name = False
ops.refresh_faces(bpy.context)
scene.frame_end = frame_end_before

# セリフの形: 音声と字幕・音声だけ
import tomllib  # noqa: E402

from bl_ext.user_default.blender_tachie.tachie_studio.pipeline.config import (  # noqa: E402
    dump_characters, parse_characters)

frame_end_before = scene.frame_end
t1 = max(r for _, r, _ in lines().values()) + 30
by_text = lambda text: next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"] == text)
span = lambda sts: (min(x.left_handle for x in sts), max(x.right_handle for x in sts))
cue_has = lambda text: any(text in t for t in subtitle_texts())
assert zunda.line_layout == "SUBTITLE"
scene.frame_current = t1
s.active_character, s.expression, s.text = 0, "", "形を変えるのだ。"
run(bpy.ops.tachie.add_line)
fsts = by_text("形を変えるのだ。")
fl, fr = span(fsts)
assert sorted(x.type for x in fsts) == ["SOUND", "TEXT"] and all(x["tachie_layout"] == "SUBTITLE" for x in fsts)
assert cue_has("形を変えるのだ")
activate(role(fsts, SOUND))
run(bpy.ops.tachie.pick_line_layout, line_layout="AUDIO")
fsts = by_text("形を変えるのだ。")
assert [x.type for x in fsts] == ["SOUND"] and not cue_has("形を変えるのだ") and span(fsts)[0] == fl
assert ops.line_end(fsts) == fr  # 音声だけでも余韻を含む長さ（再生成で後ろがずれない）
activate(fsts[0])
run(bpy.ops.tachie.pick_line_layout, line_layout="SUBTITLE")
fsts = by_text("形を変えるのだ。")
assert sorted(x.type for x in fsts) == ["SOUND", "TEXT"] and span(fsts) == (fl, fr)
# 前の版の TOML の FULL（立ち絵あり）は音声と字幕として読む
cfg = ops.config_of(zunda)
cfg.layout["lines"] = "FULL"
ops.apply_config(zunda, cfg)
assert zunda.line_layout == "SUBTITLE"
# 立ち絵の無いキャラクター: 声と字幕だけ。同じキャラでも重ねて置ける。TOML に folder を書かない
run(bpy.ops.tachie.character_add_voice, name="ナレーション")
nar = s.characters[-1]
assert nar.name == "ナレーション" and nar.folder == "" and nar.line_layout == "SUBTITLE"
assert nar.channel not in {c.channel for c in s.characters if c != nar}
s.active_character = len(s.characters) - 1
for i, text in enumerate(["ナレーションです。", "重ねて話します。"]):
    scene.frame_current = t1 + 5 * i
    s.text = text
    run(bpy.ops.tachie.add_line)
nsts = by_text("ナレーションです。")
assert sorted(x.type for x in nsts) == ["SOUND", "TEXT"] and span(nsts)[0] == t1
activate(role(nsts, SOUND))
run(bpy.ops.tachie.regenerate)
nsts = by_text("ナレーションです。")
assert sorted(x.type for x in nsts) == ["SOUND", "TEXT"] and span(nsts)[0] == t1
again = parse_characters(tomllib.loads(dump_characters({nar.name: ops.config_of(nar)})), out)[nar.name]
assert again.folder is None and "lines" not in again.layout  # 既定値（音声と字幕）は書かない
for x in sed.strips_all:
    x.select = False
for x in nsts:
    x.select = True
run(bpy.ops.tachie.batch_edit, use_line_layout=True, line_layout="AUDIO")
assert [x.type for x in by_text("ナレーションです。")] == ["SOUND"]
for x in [x for x in sed.strips if x.get("tachie_character") == nar.name
          or x.get("tachie_text", "").startswith("形を変える")]:
    sed.strips.remove(x)
s.characters.remove(len(s.characters) - 1)
s.active_character = 0
ops.refresh_faces(bpy.context)
# 入力欄には改行を入れられないので、\n（¥n）を改行にする（字幕は改行し、読み上げは続けて読む）
scene.frame_current = max(r for _, r, _ in lines().values()) + 30
s.active_character, s.expression, s.text = 0, "", "改行の\\nテスト¥nなのだ。"
run(bpy.ops.tachie.add_line)
nl = next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"].startswith("改行の"))
assert nl[0]["tachie_text"] == "改行の\\nテスト\\nなのだ。", nl[0]["tachie_text"]  # パネルで見える書き方のまま持つ
assert not s.text  # 追加したら入力欄を空にする
assert ops.line_subtitle(nl).text.split("\n")[:3] == ["改行の", "テスト", "なのだ。"], ops.line_subtitle(nl).text
for x in nl:
    sed.strips.remove(x)
# 追加のダイアログ（execute だけ。text を渡す）と、セリフを編集のダイアログ（そのテキストで作り直す）
run(bpy.ops.tachie.add_line, text="ダイアログから追加なのだ")
nl = next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"] == "ダイアログから追加なのだ")
activate(ops.line_subtitle(nl))
run(bpy.ops.tachie.edit_line_text, text="編集した\\nセリフなのだ", subtitle_only=False)
nl = next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"].startswith("編集した"))
assert ops.line_subtitle(nl).text.split("\n")[:2] == ["編集した", "セリフなのだ"], ops.line_subtitle(nl).text
# 字幕だけ変える: 音声は作り直さない（同じセリフの id・同じ音声）で、字幕のテキストだけ変わる
cid, wav = nl[0]["tachie_id"], ops.line_sound(nl).sound.filepath
activate(ops.line_subtitle(nl))
run(bpy.ops.tachie.edit_line_text, text="字幕だけ\\n変えたのだ", subtitle_only=True)
nl = ops.line_strips(sed, cid)
assert nl and ops.line_sound(nl).sound.filepath == wav
# 今の音声の読みは編集した発音として持つ（再生成で新しいテキストの自動の読みにならない）
assert ops.line_kana(nl, nl[0]["tachie_text"]) == ops.line_sound(nl)["tachie_kana"], dict(ops.line_sound(nl))
assert all(x["tachie_text"] == "字幕だけ\\n変えたのだ" for x in nl)
assert ops.line_subtitle(nl).text.split("\n")[:2] == ["字幕だけ", "変えたのだ"], ops.line_subtitle(nl).text
for x in nl:
    sed.strips.remove(x)
scene.frame_end = frame_end_before

# YMM4 のプロジェクトの読み込み: 今あるキャラ（ずんだもん）はそのまま、無いキャラは素材フォルダから追加する
import json  # noqa: E402

YMM_CHAR = "C:\\ymm4\\sozai\\char\\"
frame_end_before = scene.frame_end
ymm_base = (scene.frame_end + 60 - scene.frame_start) * 2  # YMM4 は 60fps、このシーンは 30fps


def ymm_voice(name, text, frame, face=None):
    return {"$type": "YukkuriMovieMaker.Project.Items.VoiceItem, YukkuriMovieMaker", "CharacterName": name,
            "Serif": text, "Hatsuon": "", "VoiceLength": "00:00:01", "Volume": 50.0, "Pan": 0.0,
            "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                               "StyleID": 3 if name == "ずんだもん" else 2, "Speed": 100, "Pitch": 100.0,
                               "Intonation": 100.0, "PrePhonemeLength": 0.1, "PostPhonemeLength": 0.1},
            "JimakuVisibility": "UseCharacterSetting", "TachieFaceParameter": face or {},
            "Frame": ymm_base + frame, "Layer": 3, "Length": 90}


metan_dir = YMM_CHAR + "四国めたん"
ymmp = out / "test.ymmp"
# 画像・動画・音声・テキスト（素材は .ymmp と同じフォルダに置き、YMM4 のパスのファイル名で探させる）
import shutil  # noqa: E402
import subprocess  # noqa: E402

bg_img = bpy.data.images.new("ymm_bg", 64, 36)
bg_img.pixels = [0.2, 0.6, 0.3, 1.0] * (64 * 36)
bg_img.filepath_raw, bg_img.file_format = str(out / "背景.png"), "PNG"
bg_img.save()
has_ffmpeg = shutil.which("ffmpeg") is not None
if has_ffmpeg:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x180:rate=30",
                    "-pix_fmt", "yuv420p", str(out / "clip.mp4")], check=True)
YMM_ITEM = "YukkuriMovieMaker.Project.Items."
ymm_media = [
    {"$type": YMM_ITEM + "ImageItem, YukkuriMovieMaker", "FilePath": "C:\\Users\\x\\背景.png", "X": 100.0,
     "Y": -50.0, "Zoom": 50.0, "Opacity": {"Values": [{"Value": 100.0}, {"Value": 0.0}], "AnimationType": "直線移動"},
     "Frame": ymm_base, "Layer": 0, "Length": 100},
    {"$type": YMM_ITEM + "ImageItem, YukkuriMovieMaker", "FilePath": "C:\\missing.png", "Frame": ymm_base,
     "Layer": 2, "Length": 60},
    {"$type": YMM_ITEM + "AudioItem, YukkuriMovieMaker", "FilePath": "D:\\bgm\\bgm.wav", "Frame": ymm_base,
     "Layer": 8, "Length": 300, "ContentOffset": "00:00:01",
     "Volume": {"Values": [{"Value": 5.0}, {"Value": 0.0}], "AnimationType": "直線移動"}},
    {"$type": YMM_ITEM + "TextItem, YukkuriMovieMaker", "Text": "タイトル\r\n2 行目", "FontSize": 80.0,
     "FontColor": "#FFFF0000", "Style": "Border", "StyleColor": "#FFFFFFFF", "Bold": True, "BasePoint": "CenterTop",
     "X": -100.0, "Y": -500.0, "Frame": ymm_base, "Layer": 9, "Length": 120},
]
if has_ffmpeg:  # 30fps の素材を 2 倍速、0.5 秒目から（スピードコントロールで合わせる）
    ymm_media.append({"$type": YMM_ITEM + "VideoItem, YukkuriMovieMaker", "FilePath": "C:\\clip.mp4",
                      "PlaybackRate": 200.0, "ContentOffset": "00:00:00.5000000", "Volume": 0.0,
                      "Frame": ymm_base + 200, "Layer": 0, "Length": 120})
base_before = s.channel_base
first_sound = next(x for x in sed.strips if x.type == "SOUND" and x.get("tachie_id"))
first_sound_channel = first_sound.channel
ymmp.write_text(json.dumps({
    "FilePath": "C:\\x\\test.ymmp", "SelectedTimelineIndex": 0,
    "Characters": [{
        "Name": "めたん（YMM4）", "Voice": {"API": "voicevox"}, "Volume": 50.0, "AdditionalTime": 0.2,
        "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                           "StyleID": 2, "Speed": 110, "Pitch": 100.0, "Intonation": 100.0,
                           "PrePhonemeLength": 0.1, "PostPhonemeLength": 0.1},
        "Font": "存在しないフォント", "FontSize": 70.0, "FontColor": "#FFFFFFFF", "Style": "Border",
        "StyleColor": "#FFDF4C94", "Y": 540.0, "IsJimakuVisible": True,
        "TachieType": "YukkuriMovieMaker.Plugin.Tachie.AnimationTachie.AnimationTachiePlugin, X",
        "TachieCharacterParameter": {"Directory": metan_dir},
        "TachieDefaultItemParameter": {"Eye": metan_dir + "\\目\\普通白目_普通目.png",
                                       "Mouth": metan_dir + "\\口\\ほほえみ.png"}},
        {"Name": "ナレーター（YMM4）", "Voice": {"API": "voicevox"}, "Volume": 50.0, "AdditionalTime": 0.1,
         "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                            "StyleID": 2}, "FontSize": 50.0, "Y": 400.0, "IsJimakuVisible": True,
         "TachieType": None}],
    "Timelines": [{"VideoInfo": {"FPS": 60, "Width": 1920, "Height": 1080}, "Length": ymm_base + 700, "Items": [
        ymm_voice("ずんだもん", "読み込んだセリフなのだ", 0, {"Eye": YMM_CHAR + "ずんだもん\\目\\なごみ目.png"}),
        ymm_voice("めたん（YMM4）", "読み込んだセリフですわ", 300),
        ymm_voice("めたん（YMM4）", "読み込んだかぶせるセリフ", 20),  # ずんだもんと同時に喋る
        ymm_voice("ナレーター（YMM4）", "読み込んだナレーション", 500),  # 立ち絵の無いキャラ
        {"$type": "YukkuriMovieMaker.Project.Items.TachieItem, YukkuriMovieMaker", "CharacterName": "めたん（YMM4）",
         "TachieItemParameter": {"Eye": metan_dir + "\\目\\普通白目_普通目.png", "IsHiddenWhenNoSpeech": True,
                                 "Mouth": metan_dir + "\\口\\ほほえみ.png"}, "X": -770.0, "Y": 600.0, "Blend": "Add",
         "Zoom": 80.0, "Frame": ymm_base, "Layer": 1, "Length": 700},
        # 立ち絵アイテムごとの左右反転・位置・跳ねながら登場退場（ずんだもんのキャラの設定は反転しない）
        {"$type": "YukkuriMovieMaker.Project.Items.TachieItem, YukkuriMovieMaker", "CharacterName": "ずんだもん",
         "X": 300.0, "Y": 300.0, "Zoom": 100.0, "IsInverted": True, "Frame": ymm_base + 560, "Layer": 1, "Length": 120,
         "Opacity": {"Values": [{"Value": 50.0}]}, "Rotation": {"Values": [{"Value": 10.0}]}, "FadeIn": 0.2,
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.InOutJumpEffect, YukkuriMovieMaker",
                           "IsInEffect": True, "IsOutEffect": True, "IsOutReverse": True, "EffectTimeSeconds": 0.5,
                           "JumpHeight": 10.0, "Stretch": 10.0, "Period": 0.25, "X": 0.0, "Y": 900.0,
                           "IsEnabled": True}]},
        {"$type": "YukkuriMovieMaker.Project.Items.TachieFaceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん",
         "TachieFaceParameter": {"Eyebrow": YMM_CHAR + "ずんだもん\\眉\\怒り眉.png"}, "Frame": ymm_base + 600,
         "Layer": 4, "Length": 60},
        *ymm_media,
    ]}]}, ensure_ascii=False), encoding="utf-8-sig")
s.ymmp_material_root = str(Path(zunda.folder).parent if Path(zunda.folder).is_absolute()
                           else Path(bpy.path.abspath(zunda.folder)).parent)
zunda_presets = {e.name for e in zunda.expressions}
n_chars = len(s.characters)
# 立ち絵の無いキャラはアドオン設定の初期値（キャラ名）で始め、YMM4 の設定で上書きする
ops.character_defaults_path().write_text(
    '["立ち絵なし"."ナレーター（YMM4）"]\nspeaker = 1\n\n["立ち絵なし"."ナレーター（YMM4）".layout]\nlines = "AUDIO"\n\n'
    '["立ち絵なし"."ナレーター（YMM4）".subtitle]\nsize = 30\nbox = true\n', encoding="utf-8")
run(bpy.ops.tachie.import_ymmp, filepath=str(ymmp), match_video=False)
ops.character_defaults_path().unlink()
report = bpy.data.texts["YMM4 の読み込み結果"].as_string()
print(report)
assert "セリフ 4 / 4 個" in report and "missing.png" in report and "存在しないフォント" in report, report
assert "画像 1" in report and "音声 1" in report and "テキスト 1" in report, report
# 立ち絵より奥（レイヤー 0）は、レーンより下（1〜9 番）の空きに置く（空いているのでレーンはずらさない）。
# 読み込んだキャラのレーンが増え、置いてあったセリフもそのキャラのレーンのまま
assert s.channel_base == base_before and first_sound.channel == lane("audio", "ずんだもん")
assert all(x.channel == lane(*chmod.lane_key(x)) or x.channel > chmod.scene_layout(scene).top
           for x in sed.strips if chmod.lane_key(x)), ("レーンに置かれていないストリップがある", [
    (x.name, x.channel, chmod.lane_key(x), x.left_handle, x.right_handle) for x in sed.strips
    if chmod.lane_key(x) and x.channel != lane(*chmod.lane_key(x))], chmod.scene_layout(scene).to_json())
assert sed.channels[lane("audio", "めたん（YMM4）")].name == "めたん（YMM4） 音声"
ymm = {x.get("tachie_ymmp"): x for x in sed.strips if x.get("tachie_ymmp") and x.type != "MOVIE"}
bg = ymm["image"]
assert bg.channel < s.channel_base and bg.left_handle == scene.frame_start + ymm_base // 2
assert bg.right_handle == bg.left_handle + 50 and abs(bg.transform.scale_x - 0.5) < 1e-6
assert (bg.transform.offset_x, bg.transform.offset_y) == (100.0, 50.0)
assert [v for _, v in keyed(bg, "blend_alpha")] == [1.0, 0.0]
bgm_strip = ymm["audio"]
assert [round(v, 4) for _, v in keyed(bgm_strip, "volume")] == [0.1, 0.0] and bgm_strip.right_handle - bgm_strip.left_handle == 150
title = ymm["text"]
assert title.text == "タイトル\n2 行目" and title.use_bold and title.anchor_x == "CENTER" and title.anchor_y == "TOP"
assert abs(title.location[0] - (0.5 - 100 / 1920)) < 1e-6 and abs(title.location[1] - (0.5 + 500 / 1080)) < 1e-6
assert title.channel > s.headline_channel == chmod.scene_layout(scene).top
if has_ffmpeg:
    speed = ymm["video"]
    assert speed.type == "SPEED" and speed.input_1.type == "MOVIE" and speed.channel == speed.input_1.channel + 1
    assert [round(v, 3) for _, v in keyed(speed, "speed_frame_number")] == [15.0, 135.0]
ymetan = s.characters[n_chars]
assert ymetan.name == "めたん（YMM4）" and ymetan.speaker == 2 and abs(ymetan.speed_scale - 1.1) < 1e-6
assert abs(ymetan.audio_after - 0.2) < 1e-6 and ymetan.font_size == 70 and ymetan.subtitle_y == 0.0
assert ymetan.position_x < 0 and ymetan.position_y < 0 and ymetan.part_eye == "普通白目_普通目"
ylines = {sts[0]["tachie_text"]: (l, sts) for _, (l, _, sts) in lines().items()
          if sts[0]["tachie_text"].startswith("読み込んだ")}
zl, zsts = ylines["読み込んだセリフなのだ"]
ml, _ = ylines["読み込んだセリフですわ"]
assert zl == scene.frame_start + ymm_base // 2 and ml == zl + 150  # 60fps → 30fps
# ボイスの表情は表情プリセットを作らずにセリフのパーツにする（重なる立ち絵の上に重ねる）
assert ops.item_parts(ops.line_sound(zsts)).get("目") == "なごみ目" and zsts[0]["tachie_expression"] == ""
assert {e.name for e in zunda.expressions} == zunda_presets
# 立ち絵アイテムはその区間の立ち絵になり、重なるそのキャラのセリフの間だけ口パクする
ystill, = [x for x in sed.strips if x.get("tachie_still") == "めたん（YMM4）"]
assert (ystill.left_handle, ystill.right_handle) == (zl, zl + 350) and ystill.channel == ymetan.channel
assert len({mouth(ystill, f) for f in range(ml, ml + 30)}) > 1 and mouth(ystill, ml - 5) == "l0"
# 喋る時のみ表示: 重なるセリフの間だけ見える（境目で切り替える）。写せない合成モードは報告する
assert ystill["tachie_speech_only"] and "立ち絵アイテムの合成モード Add" in report
scene.frame_set(ml + 10)
assert abs(ystill.blend_alpha - 1.0) < 1e-6
scene.frame_set(ml - 5)
assert ystill.blend_alpha == 0.0
zstill, = [x for x in sed.strips if x.get("tachie_still") == "ずんだもん" and x.right_handle > zl]
assert (zstill.left_handle, zstill.right_handle) == (zl + 280, zl + 340) and not zunda.flip_x
assert zstill["tachie_flip"] and zstill.use_flip_x  # 反転は立ち絵ごと（キャラの設定は変えない）
# 不透明度・回転角（時計回りが正）・フェードイン
import math  # noqa: E402

assert abs(zstill["tachie_opacity"] - 0.5) < 1e-6 and abs(zstill.transform.rotation + math.radians(10)) < 1e-6
assert [round(v, 4) for _, v in keyed(zstill, "blend_alpha")][:2] == [0.0, 0.5]
assert abs(zstill["tachie_pos_x"] - (300 / 1920 - zunda.position_x)) < 1e-6
assert abs(zstill["tachie_pos_y"] - (-300 / 1080 - zunda.position_y)) < 1e-6
jumps = keyed(zstill, "transform.offset_y")
base_y = (zunda.position_y + zstill["tachie_pos_y"]) * scene.render.resolution_y
assert jumps and jumps[0][0] == zstill.left_handle and abs(jumps[0][1] - (base_y - 900)) < 1e-3  # 下から登場
assert keyed(zstill, "transform.scale_y") and max(v for _, v in keyed(zstill, "transform.scale_y")) > zstill["tachie_scale"]
assert keyed(zstill, "transform.offset_y")[-1][0] == zstill.right_handle - 1  # 退場も跳ねる
# 立ち絵ごとの反転はキャラの反転を切り替えても変わらず、ボタンでキャラの設定に戻せる
zunda.flip_x = True
assert zstill.use_flip_x
activate(zstill)
run(bpy.ops.tachie.flip_still)
assert not zstill.use_flip_x and "tachie_flip" in zstill and not zstill["tachie_flip"]
run(bpy.ops.tachie.flip_still)
assert zstill.use_flip_x and "tachie_flip" not in zstill  # キャラの設定と同じ向きに戻すと、キャラの設定に従う
zunda.flip_x = False
assert not zstill.use_flip_x
yfaces = [x for x in sed.strips if x.get("tachie_face") == "ずんだもん" and x.left_handle == zl + 300]
assert yfaces and ops.item_parts(yfaces[0]).get("眉") == "怒り眉"
ynar = s.characters[n_chars + 1]  # 立ち絵の無いキャラは素材を探さずに追加し、声と字幕だけで置く
assert ynar.name == "ナレーター（YMM4）" and ynar.folder == "" and ynar.line_layout == "SUBTITLE"
assert ynar.speaker == 2 and ynar.font_size == 50 and ynar.subtitle_box  # YMM4 に無い設定は初期値のまま
_, nsts = ylines["読み込んだナレーション"]
assert sorted(x.type for x in nsts) == ["SOUND", "TEXT"] and nsts[0]["tachie_layout"] == "SUBTITLE"
# 片付け（あとのテストに影響しないように）
for x in [x for x in sed.strips if (x.get("tachie_text", "").startswith("読み込んだ") or x in yfaces or x == zstill
                                    or x.get("tachie_character") in (ymetan.name, ynar.name))]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.get("tachie_ymmp") and x.type == "SPEED"]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.get("tachie_ymmp")]:
    sed.strips.remove(x)
s.characters.remove(n_chars + 1)
s.characters.remove(n_chars)
lay_problems = chmod.ensure_layout(scene)
assert not lay_problems and (zunda.channel, metan.channel) == (12, 14), (lay_problems, zunda.channel, metan.channel)  # レーンは元に戻る
ops.refresh_faces(bpy.context)
scene.frame_end = frame_end_before

# テンプレート: 選んだストリップ（セリフ・表情・画像・テキスト）を登録して、別の位置に置く。YMM4 のテンプレートも読み込む
templates = bpy.context.preferences.addons["bl_ext.user_default.blender_tachie"].preferences.templates
frame_end_before = scene.frame_end
t_base = max(r for _, r, _ in lines().values()) + 30
zline = next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
src_sound = next(x for x in zline if x.type == "SOUND")
tpl_img = sed.strips.new_image(name="テンプレ画像", filepath=str(out / "背景.png"), channel=40, frame_start=t_base + 5,
                               fit_method="ORIGINAL")
tpl_img.right_handle = t_base + 65
tpl_img.transform.offset_x, tpl_img.transform.scale_x, tpl_img.transform.scale_y = 120.0, 2.0, 2.0
tpl_txt = sed.strips.new_effect(name="テンプレ文字", type="TEXT", channel=41, frame_start=t_base, length=40)
tpl_txt.text, tpl_txt.location, tpl_txt.anchor_x = "テンプレの見出し", (0.3, 0.8), "LEFT"
for x in sed.strips_all:
    x.select = False
src_sound.select = tpl_img.select = tpl_txt.select = True
run(bpy.ops.tachie.register_template, name="テスト")
tpl = next(t for t in templates if t.name == "テスト")
from bl_ext.user_default.blender_tachie.tachie_studio.templates import loads  # noqa: E402

kinds = sorted(i["kind"] if i["kind"] != "media" else i["media"]["kind"] for i in loads(tpl.data)["items"])
assert kinds == ["image", "line", "text"], kinds
for x in (tpl_img, tpl_txt):
    sed.strips.remove(x)
t_at = max(r for _, r, _ in lines().values()) + 40
scene.frame_current = t_at
run(bpy.ops.tachie.insert_template, index=list(templates).index(tpl))
placed_line = [sts for _, (l, _, sts) in lines().items() if l >= t_at]
assert len(placed_line) == 1 and placed_line[0][0]["tachie_text"] == src_sound["tachie_text"]
assert placed_line[0][0]["tachie_character"] == "ずんだもん"
p_img = next(x for x in sed.strips if x.get("tachie_ymmp") == "image" and x.left_handle >= t_at)
p_txt = next(x for x in sed.strips if x.get("tachie_ymmp") == "text" and x.left_handle >= t_at)
src_left = min(x.left_handle for x in zline)
offset = min(t_base, src_left)  # テンプレートの頭（いちばん早い開始）
assert p_img.left_handle == t_at + (t_base + 5 - offset) and p_img.right_handle - p_img.left_handle == 60
assert abs(p_img.transform.offset_x - 120.0) < 1e-3 and abs(p_img.transform.scale_x - 2.0) < 1e-6
assert p_txt.text == "テンプレの見出し" and abs(p_txt.location[0] - 0.3) < 1e-6 and p_txt.anchor_x == "LEFT"
# 立ち絵より奥（背景）にすると、レーンより下に置く
base_before = s.channel_base
tpl.behind = True
t_at2 = max(x.right_handle for x in sed.strips) + 20
scene.frame_current = t_at2
run(bpy.ops.tachie.insert_template, index=list(templates).index(tpl))
p_img2 = next(x for x in sed.strips if x.get("tachie_ymmp") == "image" and x.left_handle >= t_at2)
assert p_img2.channel < s.channel_base == base_before  # 下が空いているのでレーンはずらさない
# いちばん下のチャンネルを変えると、レーンと置いてあるストリップがまとめてずれる（下の背景などは動かない）
zimg_ch = still("ずんだもん").channel
s.channel_base = 12
back_color = sed.strips["背景"]
assert still("ずんだもん").channel == zimg_ch + 2 and back_color.channel == 1 and p_img2.channel < 10, (
    still("ずんだもん").channel, zimg_ch, back_color.channel, p_img2.channel)
assert sed.channels[zunda.channel].name == "ずんだもん 立ち絵" and sed.channels[10].name == "Channel 10"
s.channel_base = 10
assert still("ずんだもん").channel == zimg_ch
# YMM4 のテンプレート（ItemSettings.json）を読み込む: セリフ・画像・立ち絵アイテムは置ける、トランジションは読まない
YMM_ITEM2 = "YukkuriMovieMaker.Project.Items."
item_settings = out / "YukkuriMovieMaker.Settings.ItemSettings.json"
item_settings.write_text(json.dumps({"Templates": [
    {"Name": "あいさつ", "Path": ["あいさつ"], "FPS": 60, "Items": [
        {"$type": YMM_ITEM2 + "VoiceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん", "Serif": "テンプレなのだ",
         "Hatsuon": "", "VoiceLength": "00:00:01", "Volume": 50.0, "Pan": 0.0,
         "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                            "StyleID": 3},
         "TachieFaceParameter": {"Eye": "C:\\char\\ずんだもん\\目\\なごみ目.png"},
         "AdditionalTime": 3.0, "Frame": 100, "Layer": 6, "Length": 90},
        # 長い余韻（3 秒）のままだと次の同じキャラのセリフに重なるので、縮めて置く
        {"$type": YMM_ITEM2 + "VoiceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん", "Serif": "続きなのだ",
         "Hatsuon": "", "VoiceLength": "00:00:00", "Volume": 50.0, "Pan": 0.0,
         "VoiceParameter": {"$type": "YukkuriMovieMaker.Voice.VOICEVOXVoiceParameter, YukkuriMovieMaker",
                            "StyleID": 3}, "Frame": 220, "Layer": 6, "Length": 60},
        {"$type": YMM_ITEM2 + "TransitionItem, YukkuriMovieMaker", "Frame": 100, "Layer": 2, "Length": 30}]},
    {"Name": "立ち絵だけ", "Path": [], "Items": [
        {"$type": YMM_ITEM2 + "TachieItem, YukkuriMovieMaker", "CharacterName": "ずんだもん", "Frame": 0, "Layer": 1,
         "Length": 60, "IsInverted": True,
         "VideoEffects": [{"$type": "YukkuriMovieMaker.Project.Effects.InOutJumpEffect, YukkuriMovieMaker",
                           "IsInEffect": True, "EffectTimeSeconds": 0.3, "JumpHeight": 10.0, "Y": 500.0}]}]},
]}, ensure_ascii=False), encoding="utf-8-sig")
n_templates = len(templates)
run(bpy.ops.tachie.import_ymm4_templates, filepath=str(item_settings))
tpl_report = bpy.data.texts["YMM4 のテンプレートの読み込み結果"].as_string()
assert "2 個のうち 2 個" in tpl_report and "トランジション 1" in tpl_report and "立ち絵 1" in tpl_report, tpl_report
assert len(templates) == n_templates + 2
zunda_presets_t = {e.name for e in zunda.expressions}
t_at3 = max(x.right_handle for x in sed.strips) + 20
scene.frame_current = t_at3
run(bpy.ops.tachie.insert_template, index=list(templates).index(next(t for t in templates if t.name == "あいさつ")))
greet = [sts for _, (l, _, sts) in lines().items() if l >= t_at3]
assert [g[0]["tachie_text"] for g in greet] == ["テンプレなのだ", "続きなのだ"]
assert max(x.right_handle for x in greet[0]) <= min(x.left_handle for x in greet[1])
assert ops.item_parts(ops.line_sound(greet[0])).get("目") == "なごみ目"
assert {e.name for e in zunda.expressions} == zunda_presets_t  # 表情プリセットは作らない
# 立ち絵アイテムのテンプレートは立ち絵を置く（60fps の 60 フレーム → 30 フレーム）。重なる立ち絵があれば置かない
t_at4 = max(x.right_handle for x in sed.strips) + 20
scene.frame_current = t_at4
run(bpy.ops.tachie.insert_template, index=list(templates).index(next(t for t in templates if t.name == "立ち絵だけ")))
tstill, = [x for x in sed.strips if x.get("tachie_still") == "ずんだもん" and x.left_handle >= t_at4]
assert (tstill.left_handle, tstill.right_handle, tstill.channel) == (t_at4, t_at4 + 30, zunda.channel)
assert tstill.use_flip_x and tstill["tachie_flip"] and keyed(tstill, "transform.offset_y")[0][0] == t_at4
scene.frame_current = t_at4 + 10
assert bpy.ops.tachie.insert_template(  # 何も置けなければ CANCELLED
    index=list(templates).index(next(t for t in templates if t.name == "立ち絵だけ"))) == {"CANCELLED"}
assert len([x for x in sed.strips if x.get("tachie_still") == "ずんだもん" and x.left_handle >= t_at4]) == 1
# テンプレートの画像・音声が元の場所（Windows のパス）に無ければ、アドオン設定の「素材を探す場所」から探す
# （Windows と同じ並びの後ろの部分。`動画\sound\...` を `<探す場所>/sound/...` に見つける）
se_root = out / "media_root"
(se_root / "sound" / "SE").mkdir(parents=True, exist_ok=True)
(se_root / "sound" / "SE" / "ping.wav").write_bytes((out / "bgm.wav").read_bytes())
se_tpl = templates.add()
se_tpl.name = "探す場所テスト"
se_tpl.data = json.dumps({"version": 1, "fps": 30.0, "items": [{"kind": "media", "media": {
    "kind": "audio", "frame": 0, "length": 30, "layer": 3,
    "file": "C:\\Users\\x\\Documents\\動画\\sound\\SE\\ping.wav"}}]}, ensure_ascii=False)
scene.frame_current = max(x.right_handle for x in sed.strips) + 20
assert bpy.ops.tachie.insert_template(index=list(templates).index(se_tpl)) == {"CANCELLED"}  # 見つからない
prefs.media_root = str(se_root)
run(bpy.ops.tachie.insert_template, index=list(templates).index(se_tpl))
se_strip, = [x for x in sed.strips if x.type == "SOUND" and x.get("tachie_ymmp") and x.left_handle == scene.frame_current]
assert Path(bpy.path.abspath(se_strip.sound.filepath)).name == "ping.wav"
sed.strips.remove(se_strip)
templates.remove(list(templates).index(se_tpl))
prefs.media_root = ""
# 場面転換: 前の場面（再生位置で終わる画像・動画）のお尻を伸ばし、前の場面自身を動かす（再生したまま。blender-tools と同じ）
from bl_ext.user_default.blender_tachie import ymmp_media  # noqa: E402

keys_of = lambda strip, attr: [tuple(k.co) for k in ymmp_media._keyframes(scene, strip, attr)]
pngs = []
for i, color in enumerate(((1.0, 0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0))):
    img = bpy.data.images.new(f"場面{i}", 64, 36)
    img.pixels[:] = color * (64 * 36)
    img.filepath_raw, img.file_format = str(out / f"scene{i}.png"), "PNG"
    img.save()
    bpy.data.images.remove(img)
    pngs.append(out / f"scene{i}.png")
tr_at = max(x.right_handle for x in sed.strips) + 20
scene_a = sed.strips.new_image("場面A", str(pngs[0]), 2, tr_at - 30, fit_method="FIT")
scene_a.right_handle = tr_at
scene_b = sed.strips.new_image("場面B", str(pngs[1]), 2, tr_at, fit_method="FIT")
scene_b.right_handle = tr_at + 60
tr_tpl = templates.add()
tr_tpl.name = "場面転換テスト"
tr_tpl.data = json.dumps({"version": 1, "fps": 30.0, "items": [
    {"kind": "transition", "frame": 0, "length": 20, "layer": 2, "type": "push", "easing": "Expo", "mode": "InOut",
     "angle": 0.0}]})
scene.frame_current = tr_at
run(bpy.ops.tachie.insert_template, index=list(templates).index(tr_tpl))
assert scene_a.right_handle == tr_at + 20 and scene_b.channel == 3  # 前の場面を伸ばし、新しい場面は上のチャンネルへ
w_res = scene.render.resolution_x
assert keys_of(scene_a.transform, "offset_x") == [(tr_at, 0.0), (tr_at + 19, -w_res)]  # 左へ押し出す
assert keys_of(scene_b.transform, "offset_x") == [(tr_at, w_res), (tr_at + 19, 0.0)]  # 新しい場面は右から
assert not [x for x in sed.strips if x.type == "IMAGE" and x.name.startswith("場面転換")]  # 残像は作らない
# 範囲に何も映らない目印を置き（blender-tools と同じ）、付ける前の状態を記録する
from bl_ext.user_default.blender_tachie import transitions as tr_mod, templates as tpl_mod  # noqa: E402

marks = lambda: [x for x in sed.strips if x.get(tr_mod.MARK)]
tr_marker, = marks()
assert tr_marker.type == "COLOR" and tr_marker.blend_alpha == 0.0
assert (tr_marker.left_handle, tr_marker.right_handle) == (tr_at, tr_at + 20) and tr_marker.channel > scene_b.channel
assert bpy.ops.tachie.insert_template(index=list(templates).index(tr_tpl)) == {"CANCELLED"}  # 二重には付けない
# 目印を選んでテンプレートに登録すると場面転換になる
tr_data, _behind, _skipped = tpl_mod.template_from_strips(bpy.context, [tr_marker])
assert tr_data["items"] == [{"type": "push", "easing": "Expo", "mode": "InOut", "angle": 0.0, "kind": "transition",
                             "frame": 0, "length": 20, "layer": tr_marker.channel}], tr_data["items"]
# 記録は blender-tools の押し出しと同じ形（付け直す・外すは blender-tools が行う）
token = tr_marker[tr_mod.MARK]["token"]
tr_state = scene[tr_mod.REGISTRY][token].to_dict()
assert {k: tr_state[k] for k in ("kind", "a", "b", "left", "right", "a_right", "b_left", "b_channel", "attr",
                                 "base_a", "base_b", "marker")} == {
    "kind": "push", "a": "場面A", "b": "場面B", "left": tr_at, "right": tr_at + 20, "a_right": tr_at, "b_left": tr_at,
    "b_channel": 2, "attr": "offset_x", "base_a": 0.0, "base_b": 0.0, "marker": tr_marker.name}, tr_state
assert tr_state["b_channels"] == {"場面B": 2} and tr_state["crops"] == {} and "a_return" not in tr_state
# 動画の前の場面も伸ばして再生したまま。素材が足りない分は最後のコマで止まる（知らせる）。フェードは上にある方の不透明度
mv_at = tr_at + 100
movie_a = sed.strips.new_movie("場面C", str(out / "clip.mp4"), 2, mv_at - 40, fit_method="FIT")
movie_a.right_handle = mv_at
tr_tpl.data = json.dumps({"version": 1, "fps": 30.0, "items": [
    {"kind": "transition", "frame": 0, "length": 200, "layer": 2, "type": "fade", "easing": "Linear", "mode": "InOut",
     "angle": 0.0}]})
scene.frame_current = mv_at
run(bpy.ops.tachie.insert_template, index=list(templates).index(tr_tpl))
assert movie_a.right_handle == mv_at + 200
assert keys_of(movie_a, "blend_alpha") == [(mv_at, 1.0), (mv_at + 199, 0.0)]
# 速度の補正をかけた動画は動かせないので付けない（blender-tools と同じ）
sp_at = mv_at + 400
movie_s = sed.strips.new_movie("場面D", str(out / "clip.mp4"), 2, sp_at - 40, fit_method="FIT")
movie_s.right_handle = sp_at
sed.strips.new_effect("場面D 速度", "SPEED", 3, frame_start=sp_at - 40, input1=movie_s)
scene.frame_current = sp_at
assert bpy.ops.tachie.insert_template(index=list(templates).index(tr_tpl)) == {"CANCELLED"}
assert movie_s.right_handle == sp_at
# 前の場面が無ければ置かない（理由を知らせる）
scene.frame_current = sp_at + 300
assert bpy.ops.tachie.insert_template(index=list(templates).index(tr_tpl)) == {"CANCELLED"}
# 新しい場面を上に重ねて置き、前の場面がその下で続いているときは、続いている方を前の場面にする（伸ばさない）。
# 上で続いているもの（重ねた画像など）は前の場面にしない
ov_at = sp_at + 3000  # .ymmp のテスト（sp_at + 600 から）と重ならないところ
scene_e = sed.strips.new_image("場面E", str(pngs[0]), 2, ov_at - 30, fit_method="FIT")
scene_e.right_handle = ov_at + 300
scene_f = sed.strips.new_image("場面F", str(pngs[1]), 3, ov_at, fit_method="FIT")
scene_f.right_handle = ov_at + 400
overlay = sed.strips.new_image("重ねた画像", str(pngs[0]), 4, ov_at - 30, fit_method="FIT")
overlay.right_handle = ov_at + 300
scene.frame_current = ov_at
run(bpy.ops.tachie.insert_template, index=list(templates).index(tr_tpl))
assert scene_e.right_handle == ov_at + 300 and scene_f.channel == 3
assert keys_of(scene_f, "blend_alpha") == [(ov_at, 0.0), (ov_at + 199, 1.0)]  # 上にある新しい場面を濃くする
assert not keys_of(overlay, "blend_alpha") and not keys_of(scene_e, "blend_alpha")
# プッシュなら、押し出した前の場面は場面転換の終わりで元の位置に戻す（新しい場面が先に終わっても映る）
tr_tpl.data = json.dumps({"version": 1, "fps": 30.0, "items": [
    {"kind": "transition", "frame": 0, "length": 20, "layer": 2, "type": "push", "easing": "Linear", "mode": "InOut",
     "angle": 0.0}]})
scene.frame_current = ov_at + 50
sed.strips.remove(scene_f)
fade_marker, = [x for x in marks() if x.left_handle == ov_at]
fade_state = tr_mod.record_of(scene, fade_marker).to_dict()
assert (fade_state["kind"], fade_state["target"], fade_state["full"], fade_state["a"]) == ("fade", "b", 1.0, "場面E")
del scene[tr_mod.REGISTRY][fade_marker[tr_mod.MARK]["token"]]  # 先にフェードを外す（場面E に重ねては付けない）
sed.strips.remove(fade_marker)
scene_g = sed.strips.new_image("場面G", str(pngs[1]), 3, ov_at + 50, fit_method="FIT")
scene_g.right_handle = ov_at + 100
run(bpy.ops.tachie.insert_template, index=list(templates).index(tr_tpl))
assert keys_of(scene_e.transform, "offset_x") == [(ov_at + 50, 0.0), (ov_at + 69, -w_res), (ov_at + 70, 0.0)]
push_state = tr_mod.record_of(scene, [x for x in marks() if x.left_handle == ov_at + 50][0])
assert push_state["a_return"] and push_state["a_right"] == ov_at + 300
assert scene_e.right_handle == ov_at + 300
# .ymmp の読み込みの場面転換も同じ（前の場面のストリップを伸ばして動かす。残像は作らない）
from bl_ext.user_default.blender_tachie.tachie_studio.ymmp import Media as YMedia, Project as YProject, _transitions  # noqa: E402
from bl_ext.user_default.blender_tachie.ymmp_import import find_font  # noqa: E402

ym_at = sp_at + 600
ma, mb = (YMedia(kind="image", frame=0, length=30, layer=1, file=str(pngs[0])),
          YMedia(kind="image", frame=30, length=60, layer=1, file=str(pngs[1])))
proj = YProject(path=out / "x.ymmp", fps=30.0, length=90, media=[ma, mb], others=[
    {"$type": "YukkuriMovieMaker.Project.Items.TransitionItem, Y", "Frame": 30, "Layer": 2, "Length": 20,
     "TransitionType": "YukkuriMovieMaker.Transition.PushTransitionPlugin, Y", "TransitionParameter": {}}])
_transitions(proj, set())
placer = ymmp_media.MediaPlacer(bpy.context, proj, ymmp_media.MediaFinder(out / "x.ymmp"), lambda f: ym_at + f,
                                find_font)
placer.place()
ya, yb = (next(x for x in sed.strips if x.get("tachie_ymmp") and x.left_handle == ym_at + f) for f in (0, 30))
assert ya.right_handle == ym_at + 50 and placer.placed["transition"] == 1, (ya.right_handle, placer.placed)
assert keys_of(ya.transform, "offset_x")[-1] == (ym_at + 49, -w_res) and keys_of(yb.transform, "offset_x")[0][1] == w_res
for x in (ya, yb):
    sed.strips.remove(x)
# fps 合わせの速度の補正（アドオンが付けたもの）の動画も、元の動画を伸ばしてフレーム番号を同じ傾きで延ばす
ym2 = ym_at + 300
mv, mb2 = (YMedia(kind="video", frame=0, length=30, layer=1, file=str(out / "clip.mp4"), rate=50.0),
           YMedia(kind="image", frame=30, length=60, layer=1, file=str(pngs[1])))
proj = YProject(path=out / "x.ymmp", fps=30.0, length=90, media=[mv, mb2], others=[
    {"$type": "YukkuriMovieMaker.Project.Items.TransitionItem, Y", "Frame": 30, "Layer": 2, "Length": 20,
     "TransitionType": "YukkuriMovieMaker.Transition.PushTransitionPlugin, Y", "TransitionParameter": {}}])
_transitions(proj, set())
placer = ymmp_media.MediaPlacer(bpy.context, proj, ymmp_media.MediaFinder(out / "x.ymmp"), lambda f: ym2 + f, find_font)
placer.place()
ysp = next(x for x in sed.strips if x.type == "SPEED" and x.get("tachie_fps_speed") and x.left_handle == ym2)
assert placer.placed["transition"] == 1 and ysp.input_1.right_handle == ym2 + 50 and ysp.right_handle == ym2 + 50
assert keys_of(ysp, "speed_frame_number") == [(ym2, 0.0), (ym2 + 50, 25.0)]  # 半分の速さのまま延ばす
assert keys_of(ysp.transform, "offset_x")[-1] == (ym2 + 49, -w_res)
# 新しい場面が fps 合わせの補正の動画で前の場面と同じチャンネルなら、元の動画と補正の 2 本を並べたまま上へ移す
ym3 = ym2 + 300
mi, mv2 = (YMedia(kind="image", frame=0, length=30, layer=1, file=str(pngs[0])),
           YMedia(kind="video", frame=30, length=60, layer=1, file=str(out / "clip.mp4"), rate=50.0))
proj = YProject(path=out / "x.ymmp", fps=30.0, length=90, media=[mi, mv2], others=[
    {"$type": "YukkuriMovieMaker.Project.Items.TransitionItem, Y", "Frame": 30, "Layer": 2, "Length": 20,
     "TransitionType": "YukkuriMovieMaker.Transition.PushTransitionPlugin, Y", "TransitionParameter": {}}])
_transitions(proj, set())
placer = ymmp_media.MediaPlacer(bpy.context, proj, ymmp_media.MediaFinder(out / "x.ymmp"), lambda f: ym3 + f, find_font)
placer.place()
yi = next(x for x in sed.strips if x.type == "IMAGE" and x.get("tachie_ymmp") and x.left_handle == ym3)
ysp2 = next(x for x in sed.strips if x.type == "SPEED" and x.get("tachie_fps_speed") and x.left_handle == ym3 + 30)
assert placer.placed["transition"] == 1 and yi.right_handle == ym3 + 50, (placer.problems, yi.right_handle)
assert ysp2.input_1.channel == yi.channel + 1 and ysp2.channel == yi.channel + 2
for x in [x for x in sed.strips if x.left_handle >= ym2 and x.get("tachie_ymmp") is not None and x.type == "SPEED"]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.left_handle >= ym2 and x.get("tachie_ymmp") is not None]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.type == "SPEED" and x.name == "場面D 速度"]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.name in ("場面A", "場面B", "場面C", "場面D")]:
    sed.strips.remove(x)
templates.remove(list(templates).index(tr_tpl))
# 片付け
for x in [x for x in sed.strips if x.left_handle >= t_at and (x.get("tachie_id") or x.get("tachie_ymmp")
                                                             or x.get("tachie_still"))]:
    sed.strips.remove(x)
for i in reversed(range(len(templates))):
    if templates[i].name in ("テスト", "あいさつ", "立ち絵だけ"):
        templates.remove(i)
ops.refresh_faces(bpy.context)
scene.frame_end = frame_end_before

# AI で表情を割り当てる（偽の LLM サーバ。キャラごとの束の最初の行は -1 = 変えない、他は WANT の表情の番号）
import re  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from bl_ext.user_default.blender_tachie import faceassign as fa_mod  # noqa: E402

WANT = {}
llm_calls = []
# 割り当てで表情を 3 つ使い分ける。素材に preset.ini が無い（サンプルの「困り」「ドヤ」だけの）ときは 1 つ足す
if len(zunda.expressions) < 3:
    extra = zunda.expressions.add()
    extra.name, extra.part_eyebrow = "AI テスト", "怒り眉"


class _FakeLlm(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "fake"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        content = "."
        if body.get("max_tokens") != 1:
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            llm_calls.append(user)
            numbers = {m.group(2): int(m.group(1)) for m in re.finditer(r"^(-?\d+): (.+)$", system, re.M)}
            targets = re.findall(r"^\[(.+?) #(\d+)\]", user, re.M)
            content = json.dumps({"a": [{"i": int(i), "p": -1 if k == 0 else numbers[WANT[name]]}
                                        for k, (name, i) in enumerate(targets)]})
        self._send({"choices": [{"message": {"content": content}}]})


llm_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlm)
threading.Thread(target=llm_server.serve_forever, daemon=True).start()
prefs.llm_url, prefs.llm_model = f"http://127.0.0.1:{llm_server.server_port}", "fake"
run(bpy.ops.tachie.llm_test)


def no_faces(cid):
    sts = ops.line_strips(sed, cid)
    return not ops.face_segments(sed, ops.find_character(s, sts[0]["tachie_character"]),
                                 min(x.left_handle for x in sts), ops.line_end(sts))


fa_ids = [cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん"
          and no_faces(cid)][:2]
assert len(fa_ids) == 2, fa_ids
keep_expr = sound(fa_ids[0])["tachie_expression"]
old_expr = sound(fa_ids[1])["tachie_expression"]
WANT["ずんだもん"] = next(e.name for e in zunda.expressions if e.name != old_expr)
wav_before = sound(fa_ids[1]).sound.filepath
select_only([ops.line_subtitle(ops.line_strips(sed, fa_ids[0])), sound(fa_ids[1])])
prefs.faceassign_target = "LINE"
run(bpy.ops.tachie.assign_faces)
assert len(llm_calls) == 1 and llm_calls[0].count("#") == 2, llm_calls
assert all(x["tachie_expression"] == keep_expr for x in ops.line_strips(sed, fa_ids[0]))
assert all(x["tachie_expression"] == WANT["ずんだもん"] for x in ops.line_strips(sed, fa_ids[1]))
assert sound(fa_ids[1]).sound.filepath == wav_before  # 音声は合成し直さない
fa_start = lines()[fa_ids[1]][0]
assert expression_dir(still("ずんだもん"), fa_start) == cache_dir(zunda, WANT["ずんだもん"])
fa_report = bpy.data.texts[fa_mod.REPORT_TEXT].as_string()
assert fa_report.startswith("2 行のうち 1 行の表情を変えました"), fa_report
print(fa_report)
# 書き込み先を表情ストリップにすると、セリフの区間に表情ストリップを置く（セリフの表情はそのまま）
WANT["ずんだもん"] = next(e.name for e in zunda.expressions if e.name not in (WANT["ずんだもん"], keep_expr))
prefs.faceassign_target = "FACE_STRIP"
select_only([sound(fa_ids[0]), sound(fa_ids[1])])
faces_before = set(ops.face_strips(sed, "ずんだもん"))
run(bpy.ops.tachie.assign_faces)
new_faces = [f for f in ops.face_strips(sed, "ずんだもん") if f not in faces_before]
sts = ops.line_strips(sed, fa_ids[1])
assert [(f.left_handle, f.right_handle, f["tachie_expression"]) for f in new_faces] == [
    (min(x.left_handle for x in sts), ops.line_end(sts), WANT["ずんだもん"])], new_faces
assert sound(fa_ids[1])["tachie_expression"] != WANT["ずんだもん"]
assert expression_dir(still("ずんだもん"), fa_start) == cache_dir(zunda, WANT["ずんだもん"])
# 片付け
prefs.faceassign_target = "LINE"
for f in new_faces:
    sed.strips.remove(f)
ops.refresh_faces(bpy.context)
llm_server.shutdown()

# 書き出したキャラ設定を読み直すと同じになる
exported = out / "exported.toml"
zunda.talk_period = 0.5  # 動きの設定も書き出す（既定値のものは書かない）
run(bpy.ops.tachie.export_characters, filepath=str(exported))
from bl_ext.user_default.blender_tachie.tachie_studio.pipeline import load_characters  # noqa: E402

again = load_characters(exported)
assert again["ずんだもん"].expression.eye == "ジト目" and "channel" not in again["四国めたん"].layout  # チャンネルは書かない
assert again["ずんだもん"].voice == {"speedScale": 1.1} and again["四国めたん"].voice == {}, again["ずんだもん"].voice
assert abs(again["ずんだもん"].audio["after"] - 0.3) < 1e-6 and again["四国めたん"].audio["volume"] == 1.0
assert again["ずんだもん"].layout["period"] == 0.5 and "enter" not in again["ずんだもん"].layout
print(exported.read_text(encoding="utf-8"))

# 台本の読み込み（テキストエディタの台本、表情つき）
for st in [st for st in sed.strips if st.get("tachie_id") or st.get("tachie_still")]:
    sed.strips.remove(st)
scene.frame_end = 1  # 置いたセリフ・立ち絵の分だけ伸びる
text = bpy.data.texts.new("台本")
text.from_string((characters_toml.parent / "script.txt").read_text(encoding="utf-8"))
s.script = text
s.line_gap = 0.2
scene.frame_current = 1
run(bpy.ops.tachie.import_script, use_text=True)
dump("台本を読み込み")
placed = list(lines().values())
assert len(placed) == 5
assert [v[2][0].get("tachie_expression") for v in placed] == ["", "", "困り", "おこ", "ドヤ"]
assert all(next(x for x in v[2] if x.type == "SOUND")["tachie_kana"] for v in placed)
assert all(b[0] - a[1] == 6 for a, b in zip(placed, placed[1:])), "セリフの間隔が 0.2 秒（6 フレーム）でない"
# 立ち絵は台本の最後のセリフの終わりまで（あとで後ろにもう 1 本置く）
script_end = max(r for _, r, _ in lines().values())
s.still_seconds = (script_end - 1) / 30
for index in (0, 1):
    s.active_character, s.expression = index, ""
    scene.frame_current = 1
    run(bpy.ops.tachie.add_still)
assert still("ずんだもん").right_handle == script_end
assert len({mouth(still("ずんだもん"), f) for f in range(placed[0][0], placed[0][1])}) > 1


# 再生成はセリフの表情（パーツを変えたものも）を引き継ぐ
third = next(x for x in placed[2][2] if x.type == "TEXT")
ops.set_line_parts(bpy.context, third["tachie_id"], {**ops.preset_parts(zunda, "ドヤ"), "目": "なごみ目"}, "ドヤ")
activate(third)
run(bpy.ops.tachie.regenerate)
regen = list(lines().values())[2][2]
assert all(x["tachie_expression"] == "ドヤ" and ops.item_parts(x)["目"] == "なごみ目" for x in regen)
assert ops.expression_label(zunda, regen[0], "（立ち絵のまま）") == "ドヤ*"
third = next(x for x in regen if x.type == "TEXT")
ops.set_line_expression(bpy.context, third["tachie_id"], "困り")
activate(third)
run(bpy.ops.tachie.regenerate)
assert ops.item_parts(list(lines().values())[2][2][0]) == ops.preset_parts(zunda, "困り")

# 存在しない表情の台本はエラーで何も置かない
bad = bpy.data.texts.new("悪い台本")
bad.from_string("ずんだもん(泣き): えーん")
s.script = bad
try:
    bpy.ops.tachie.import_script(use_text=True)
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
assert len(lines()) == 5
# fps・解像度は置く前に決める。出力タブで変えたら知らせて元に戻せる（置いてあるものは変換しない）
from bl_ext.user_default.blender_tachie import timeline as timeline_mod  # noqa: E402

r = scene.render
fps_before = (r.fps, r.resolution_x, r.resolution_y)
assert timeline_mod.changed_from(scene) is None, timeline_mod.changed_from(scene)
# 出力タブの「動画を書き出す」は、まだ動画の形式でなければ MP4 にしてから書き出す（解像度・fps・% は変えない）
percent = r.resolution_percentage
r.image_settings.media_type = "IMAGE"
run(bpy.ops.tachie.render_video, start=False)
assert (r.fps, r.resolution_x, r.resolution_y, r.resolution_percentage) == fps_before + (percent,)
assert (r.image_settings.file_format, r.ffmpeg.format, r.ffmpeg.codec, r.ffmpeg.audio_codec, r.ffmpeg.audio_mixrate,
        r.use_sequencer) == ("FFMPEG", "MPEG4", "H264", "AAC", 48000, True)
r.ffmpeg.codec = "AV1"
run(bpy.ops.tachie.render_video, start=False)
assert r.ffmpeg.codec == "AV1"  # 動画の形式なら変えた設定のまま
r.ffmpeg.audio_codec = "NONE"
run(bpy.ops.tachie.render_video, start=False)
assert r.ffmpeg.audio_codec == "AAC" and r.ffmpeg.codec == "AV1"  # 音声なしだとセリフの声が入らない
r.image_settings.media_type = "IMAGE"
sound_lengths = {cid: sound(cid).duration for cid in lines()}
r.fps = 24
assert timeline_mod.changed_from(scene) == (30, 1920, 1080)
run(bpy.ops.tachie.timeline_revert)
assert r.fps == 30 and timeline_mod.changed_from(scene) is None
assert {cid: sound(cid).duration for cid in lines()} == sound_lengths  # 音声は Blender が秒から数え直して元の長さ
r.resolution_x, r.resolution_y = 3840, 2160
assert timeline_mod.changed_from(scene) is not None
run(bpy.ops.tachie.timeline_revert)
assert (r.fps, r.resolution_x, r.resolution_y) == fps_before
# 声のエンジン（VOICEVOX 互換）: キャラごとに URL を指定する（ここでは同じエンジンを別の URL 表記で）
# （VOICEVOX_URL で別のマシンを指定したときは、末尾に / を付けた表記にする）
alt_url = (prefs.voicevox_url.replace("127.0.0.1", "localhost") if "127.0.0.1" in prefs.voicevox_url
           else prefs.voicevox_url.rstrip("/") + "/")
zunda.engine, zunda.engine_url = "CUSTOM", alt_url
assert ops.engine_url(bpy.context, zunda) == alt_url
first_zunda = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
activate(sound(first_zunda))
run(bpy.ops.tachie.regenerate)
assert ops.config_of(zunda).engine.rstrip("/") == alt_url.rstrip("/")
cfg_engine = ops.config_of(zunda)
cfg_engine.engine = "http://127.0.0.1:10101"
ops.apply_config(zunda, cfg_engine)
assert zunda.engine == "AIVIS"  # 既知のエンジンの URL は名前で選ばれる
# 声のエンジンにつながらなくても、ダイアログの入力は次に開いたときに戻す（書き直さずに済むように）
zunda.engine, zunda.engine_url = "CUSTOM", "http://127.0.0.1:9"
s.active_character = list(s.characters).index(zunda)


def fails(op, **kwargs):
    try:
        op(**kwargs)
        raise AssertionError(f"{op.idname()} がつながらないエンジンで成功した")
    except RuntimeError as e:
        assert "もう一度開くと戻ります" in str(e), e


first_zunda = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
fails(bpy.ops.tachie.add_line, text="つながらないのだ")
assert ops._unsent.pop(("add_line", "ずんだもん")) == "つながらないのだ"
activate(sound(first_zunda))
fails(bpy.ops.tachie.edit_line_text, text="書き直したのだ", subtitle_only=False)
assert ops._unsent.pop(("edit_line_text", first_zunda)) == "書き直したのだ"
assert ops._unsent.pop((ops.EDIT_AUDIO_KEY, first_zunda))  # 次に開くと「字幕だけ変える」は外したまま
activate(sound(first_zunda))
fails(bpy.ops.tachie.edit_kana, text="カキナオシタノダ'")
assert ops._unsent.pop(("edit_kana", first_zunda)) == "カキナオシタノダ'"
# ダイアログの外を押して閉じても、入力するたびに覚えた入力が次に開いたときに戻る（欄の update）
activate(sound(first_zunda))
ops._keep_draft("edit_kana", ops.active_line_id, ops.TACHIE_OT_edit_kana.initial_kana)(
    type("P", (), {"text": "トジタノダ'"})(), bpy.context)
assert ops._unsent.pop(("edit_kana", first_zunda)) == "トジタノダ'"
ops._keep_draft("edit_kana", ops.active_line_id, ops.TACHIE_OT_edit_kana.initial_kana)(
    type("P", (), {"text": ops.TACHIE_OT_edit_kana.initial_kana(bpy.context)})(), bpy.context)
assert ("edit_kana", first_zunda) not in ops._unsent  # 最初の値に戻したら覚えない
zunda.engine = "DEFAULT"
assert scene.frame_end == max(r for _, r, _ in lines().values()) - 1, scene.frame_end

# 左右反転は置いてある立ち絵にすぐ反映される
metan_images = lambda: [x for x in sed.strips if x.type == "IMAGE"
                        and x.get("tachie_still") == "四国めたん"]
assert metan_images() and not any(x.use_flip_x for x in metan_images())
metan.flip_x = True
assert all(x.use_flip_x for x in metan_images())
# 字幕のスタイルはキャラクターごと。変えると置いてある字幕にすぐ反映される
subs = lambda name: [x for x in sed.strips if x.type == "TEXT" and x.get("tachie_character") == name]
assert all(abs(x.outline_color[0] - 0.75) < 1e-3 for x in subs("四国めたん")), "TOML の縁取りの色が入っていない"
from bl_ext.user_default.blender_tachie.properties import system_fonts  # noqa: E402

# 別のフォントに変える（Mac はヒラギノ丸ゴ、無ければ今と違う日本語フォント。Linux なら Noto CJK など）
fonts = system_fonts()
current = os.path.realpath(bpy.path.abspath(metan.font_path)) if metan.font_path else ""
maru = next((p for n, p in fonts if "丸ゴ" in n), None) or next(
    p for n, p in fonts if os.path.realpath(p) != current and any(k in n for k in ("CJK", "JP", "ゴシック", "Gothic")))
zunda.outline_color = (0.1, 0.45, 0.1, 1.0)
metan.font_path, metan.font_size = maru, 72
assert all(abs(x.outline_color[1] - 0.45) < 1e-3 for x in subs("ずんだもん"))
assert all(x.font_size == 72 and x.font and os.path.realpath(bpy.path.abspath(x.font.filepath)) == os.path.realpath(maru)
           for x in subs("四国めたん")), [(x.font_size, x.font.filepath if x.font else None) for x in subs("四国めたん")]
assert all(x.font_size == 64 for x in subs("ずんだもん"))
print("フォント:", {x.get("tachie_character"): x.font.name for x in sed.strips if x.type == "TEXT"})
zunda_images = [x for x in sed.strips if x.type == "IMAGE" and x.get("tachie_still") == "ずんだもん"]
assert not any(x.use_flip_x for x in zunda_images)
assert s.characters[0].speaker_name, "話者名が入っていない"

# 立ち絵を最後のセリフの後ろに置く（喋らないのでまばたきだけ）
last_end = max(r for _, r, _ in lines().values())
tail = lambda: [x for x in sed.strips if x.get("tachie_still") and x.left_handle >= last_end]
s.active_character, s.expression, s.still_seconds = 0, "困り", 2.0
scene.frame_current = last_end
run(bpy.ops.tachie.add_still)
(tail_still,) = tail()
assert (tail_still.left_handle, tail_still.right_handle, tail_still.channel) == (last_end, last_end + 60, zunda.channel)
assert tail_still["tachie_expression"] == "困り" and not tail_still.use_flip_x
assert scene.frame_end == tail_still.right_handle - 1
# 同じキャラの立ち絵に重ねては置けない
scene.frame_current = list(lines().values())[0][0]
try:
    bpy.ops.tachie.add_still()
    raise AssertionError("重なっているのに置けた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
# 伸ばして作り直すと、まばたきが伸ばした長さぶん入る
tail_still.right_handle = tail_still.left_handle + 90
tail_still["tachie_expression"] = ""
activate(tail_still)
run(bpy.ops.tachie.remake_still)
(tail_still,) = tail()
assert tail_still.right_handle - tail_still.left_handle == 90 and len(tail_still.elements) == 89, len(tail_still.elements)
assert tail_still["tachie_expression"] == ""
run(bpy.ops.tachie.regenerate_all, character_only=False)
assert [(x.left_handle, x.right_handle) for x in tail()] == [(last_end, last_end + 90)]
check_stills()
s.expression = ""

# チャンネル: キャラを並べ替えるとレーンが入れ替わり、置いたストリップとチャンネルのミュートもついてくる
def on_lanes():
    lay = chmod.scene_layout(scene)
    return all(x.channel == lay.channel(*chmod.lane_key(x)) or x.channel > lay.top
               for x in sed.strips if chmod.lane_key(x))


zsub = lane("subtitle", "ずんだもん")
sed.channels[zsub].mute = True
s.active_character = 0
run(bpy.ops.tachie.character_move, direction="DOWN")  # 並べ替えると zunda などの参照は別のキャラを指すので名前で取る
char = lambda name: next(c for c in s.characters if c.name == name)
assert s.characters[1].name == "ずんだもん" and char("ずんだもん").channel > char("四国めたん").channel and on_lanes()
assert sed.channels[lane("subtitle", "ずんだもん")].mute and not sed.channels[lane("subtitle", "四国めたん")].mute
assert all(x.channel == char("ずんだもん").channel for x in sed.strips if x.get("tachie_still") == "ずんだもん")
run(bpy.ops.tachie.character_move, direction="UP")
zunda, metan = s.characters[0], s.characters[1]
assert zunda.name == "ずんだもん" and lane("subtitle", "ずんだもん") == zsub and on_lanes()
sed.channels[zsub].mute = False
# 手で動かしたストリップは、並べ替え・再生成をしても動かさない（今のチャンネルに置き直す）。
# 「チャンネルを整理する」を押したときだけレーンに戻る
first_id = next(iter(lines()))
subtitle_of = lambda cid: ops.line_subtitle(ops.line_strips(sed, cid))
moved = subtitle_of(first_id)
moved.channel = 90
s.active_character = 0
run(bpy.ops.tachie.character_move, direction="DOWN")
run(bpy.ops.tachie.character_move, direction="UP")
zunda, metan = s.characters[0], s.characters[1]
assert subtitle_of(first_id).channel == 90, subtitle_of(first_id).channel
activate(sound(first_id))
sound(first_id).channel = 91
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
assert subtitle_of(first_id).channel == 90 and sound(first_id).channel == 91
run(bpy.ops.tachie.relayout)
moved = subtitle_of(first_id)
assert moved.channel == lane("subtitle", zunda.name) and sound(first_id).channel == lane("audio", zunda.name) and on_lanes()
# 古い .blend（レーンに並べ直す前）: 開いたときに並べ直す
s.applied_layout, s.layout_version = "", 0
moved.channel = 91
chmod._on_load(None)
assert s.layout_version == 1 and moved.channel == lane("subtitle", zunda.name) and on_lanes()
assert sed.channels[zunda.channel].name == "ずんだもん 立ち絵"

scene.render.image_settings.file_format = "PNG"
spans = [(l, r) for l, r, _ in lines().values()] + [(last_end, last_end + 90)]
for i, (l, r) in enumerate(spans):
    frame = (l + r) // 2
    scene.frame_set(frame)
    scene.render.filepath = str(out / f"frame{i + 1}_{frame:04d}.png")
    bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=str(out / "smoke.blend"))

# キャラクター追加の初期値: アドオン設定 → 素材の tachie.toml → 素材の preset.ini（YMM4 の表情プリセット）→ 固定値
from bl_ext.user_default.blender_tachie import operators as ops  # noqa: E402

material = out / "material" / "ずんだもん2"  # 素材を汚さないよう、パーツのフォルダだけリンクした素材
material.mkdir(parents=True)
for sub in Path(bpy.path.abspath(zunda.folder)).iterdir():
    if sub.is_dir():
        try:
            (material / sub.name).symlink_to(sub)
        except OSError:  # Windows は開発者モードか管理者でないとリンクを作れないのでコピーする
            __import__("shutil").copytree(sub, material / sub.name)
(material / "tachie.toml").write_text(
    f'name = "ずんだ（素材）"\nspeaker = 1\n\n[parts]\n"他" = ["左腕_基本", "右腕_基本"]\n\n'
    f'[layout]\nposition = -0.1\nchannel = {zunda.channel}\n', encoding="utf-8")
real_presets = Path(bpy.path.abspath(zunda.folder)) / "preset.ini"
if real_presets.is_file():
    __import__("shutil").copy(real_presets, material / "preset.ini")
else:
    (material / "preset.ini").write_text("[泣く]\n目=_.png\n他3=記号_涙.png\n", encoding="utf-8")
ops.character_defaults_path().write_text('["ずんだもん2"]\nspeaker = 7\n\n["ずんだもん2".layout]\nflip = true\n',
                                         encoding="utf-8")
run(bpy.ops.tachie.character_add, directory=str(material) + "/")
new = s.characters[-1]
assert new.name == "ずんだ（素材）" and new.speaker == 7 and new.part_other1 == "左腕_基本", (new.name, new.speaker)
assert abs(new.position_x + 0.1) < 1e-6 and new.flip_x
naku = next(e for e in new.expressions if e.name == "泣く")
assert naku.part_eye == "_" and naku.part_other3 == "記号_涙", (naku.part_eye, naku.part_other3)
# メニューから読み込み直す（同じ名前は上書き）
new.expressions.clear()
run(bpy.ops.tachie.import_presets, filepath=str(material / "preset.ini"))
assert {"泣く"} <= {e.name for e in new.expressions}
# スロットごと: 泣くは他1〜3 を全部、他2 だけのプリセットは他1 が基本のまま
s.active_character = len(s.characters) - 1
s.expression, s.text = "泣く", "えーん"
scene.frame_current = scene.frame_end + 10
run(bpy.ops.tachie.add_line)
cid = list(lines())[-1]  # いちばん後ろに置いたセリフ
assert ops.line_strips(sed, cid)[0]["tachie_expression"] == "泣く"
partial = next((e for e in new.expressions if e.part_other2 and not e.part_other1), None)
if partial is not None:
    e = ops.expression_of(new, partial.name)
    assert e.others[0] == "左腕_基本" and e.others[1] == partial.part_other2, e.others
s.expression = ""
assert new.channel == lane("image", new.name) and len({c.channel for c in s.characters}) == len(s.characters)
new.height = 0.7
run(bpy.ops.tachie.save_defaults, target="PREFS")
saved = ops.load_character_defaults()["ずんだもん2"]
assert saved["name"] == "ずんだ（素材）" and saved["speaker"] == 7 and abs(saved["layout"]["height"] - 0.7) < 1e-6
run(bpy.ops.tachie.save_defaults, target="MATERIAL")
from bl_ext.user_default.blender_tachie.tachie_studio.pipeline.config import load_material_table  # noqa: E402

assert load_material_table(material)["speaker"] == 7
run(bpy.ops.tachie.remove_defaults, key="ずんだもん2")
assert ops.load_character_defaults() == {}
# 立ち絵の無いキャラの初期値: キャラ名で保存し、同じ名前で追加すると使う。素材フォルダ名の初期値とは混ざらない
ops.character_defaults_path().write_text('["語り手"]\nspeaker = 9\n', encoding="utf-8")
run(bpy.ops.tachie.character_add_voice, name="語り手")
narr = s.characters[-1]
assert narr.speaker == 3 and narr.line_layout == "SUBTITLE"  # 素材フォルダ「語り手」の初期値は使わない
s.active_character = len(s.characters) - 1
narr.speaker, narr.speed_scale, narr.font_size, narr.line_layout = 8, 1.2, 40, "AUDIO"
run(bpy.ops.tachie.save_defaults, target="PREFS")
assert ops.load_character_defaults() == {"語り手": {"speaker": 9}}
voice = ops.load_voice_defaults()["語り手"]
assert voice["speaker"] == 8 and voice["speed"] == 1.2 and voice["subtitle"]["size"] == 40, voice
assert "parts" not in voice and set(voice["layout"]) == {"lines"}, voice  # チャンネルはレーンで決まるので保存しない
try:
    bpy.ops.tachie.save_defaults(target="MATERIAL")
    raise AssertionError("立ち絵の無いキャラの初期値を素材フォルダに書けた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
run(bpy.ops.tachie.character_add_voice, name="語り手")  # 同じ名前があれば番号を付け、初期値は元の名前で探す
narr2 = s.characters[-1]
assert narr2.name == "語り手 2" and narr2.speaker == 8 and abs(narr2.speed_scale - 1.2) < 1e-6
assert narr2.font_size == 40 and narr2.line_layout == "AUDIO"
assert lane("audio", narr2.name) != lane("audio", narr.name) and lane("image", narr2.name) is None  # 立ち絵のレーンは無い
run(bpy.ops.tachie.remove_defaults, key="語り手", voice=True)
assert ops.load_voice_defaults() == {} and ops.load_character_defaults() == {"語り手": {"speaker": 9}}
run(bpy.ops.tachie.remove_defaults, key="語り手")
zunda = next(c for c in s.characters if c.name == "ずんだもん")  # 追加でコレクションが作り直されて参照が無効になる

# シンプル立ち絵（画像を入れただけのフォルダ）: 口を閉じた画像と開いた画像を音量で切り替える
from bl_ext.user_default.blender_tachie.tachie_studio.tachie import Expression, compose, load_character  # noqa: E402

simple_dir = out / "material" / "シンプル"
simple_dir.mkdir(parents=True)
zc = load_character(bpy.path.abspath(zunda.folder))
compose(zc, Expression(mouth_frame=0)).save(simple_dir / "00 デフォルト.png")
compose(zc, Expression(mouth="ほあー")).save(simple_dir / "01 口開き.png")
run(bpy.ops.tachie.character_add, directory=str(simple_dir) + "/")
simple = s.characters[-1]
assert simple.name == "シンプル" and ops.load_material(simple.folder).simple
s.active_character, s.expression, s.text = len(s.characters) - 1, "", "シンプル立ち絵なのだ。"
s.still_seconds = 5.0
scene.frame_current = at = scene.frame_end + 10
run(bpy.ops.tachie.add_still)
scene.frame_current = at
run(bpy.ops.tachie.add_line)
img = still("シンプル")
names = {os.stat(Path(bpy.path.abspath(img.directory)) / e.filename).st_ino for e in img.elements}
assert len(names) == 2, names  # 閉じと開きの 2 枚だけを行き来する
# 素材を選び直す: 設定はそのままで、新しい素材に無いパーツ（シンプル立ち絵の画像名）は既定に戻す
simple.speaker, simple.part_body = 8, "00 デフォルト"
run(bpy.ops.tachie.change_material, directory=str(Path(bpy.path.abspath(zunda.folder))) + "/")
assert simple.folder.rstrip("/").endswith("ずんだもん") and not ops.load_material(simple.folder).simple
assert simple.speaker == 8 and simple.part_body == ""
(out / "空のフォルダ").mkdir()
try:  # 素材でないフォルダは選べない（素材はそのまま）
    bpy.ops.tachie.change_material(directory=str(out / "空のフォルダ") + "/")
    raise AssertionError("素材でないフォルダを選べた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
assert simple.folder.rstrip("/").endswith("ずんだもん")

# PSD 立ち絵: 口パク・まばたきの設定は動く立ち絵から作る（素材フォルダの PSD が無ければ飛ばす）
psd_src = next(iter(sorted(Path(bpy.path.abspath(zunda.folder)).glob("*.psd"))), None)
if psd_src is None:
    print("PSD が無いので PSD 立ち絵の確認は飛ばします")
else:
    import shutil  # noqa: E402

    from bl_ext.user_default.blender_tachie.tachie_studio.tachie.psd_animation import (  # noqa: E402
        animations_from_material, write_config)
    from bl_ext.user_default.blender_tachie.tachie_studio.tachie.psd import read_psd  # noqa: E402

    psd_dir = out / "material" / "psd"
    psd_dir.mkdir(parents=True)
    psd = psd_dir / "ずんだもんPSD.psd"
    shutil.copy(psd_src, psd)
    config, warnings_ = animations_from_material(zc, read_psd(psd))
    write_config(psd, config)
    run(bpy.ops.tachie.character_add, filepath=str(psd), directory=str(psd_dir) + "/")
    pch = s.characters[-1]
    pmat = ops.load_material(pch.folder)
    assert pch.name == "ずんだもんPSD" and pmat.is_psd and pmat.animations["mouth"] and pmat.animations["eye"]
    assert "!口/*" in pch.psd_layers, pch.psd_layers  # 口パクの組の閉じた口が基本になる
    # 表情: 目を選ぶ、記号を表示する、プリセットで口を変える
    ops._psd_option_items = [("!目/*ジト目", "ジト目", "")]
    s.active_character = len(s.characters) - 1
    run(bpy.ops.tachie.psd_choose, group="!目", option="!目/*ジト目")
    run(bpy.ops.tachie.psd_toggle, path="記号など/汗1")
    layers = ops.item_layers(pch)
    assert layers["!目/*ジト目"] is True and layers["記号など/汗1"] is True
    run(bpy.ops.tachie.expression_add)
    preset = pch.expressions[-1]
    ops._psd_option_items = [("!口/*お", "お", "")]
    run(bpy.ops.tachie.psd_choose, group="!口", preset=preset.name, option="!口/*お")
    assert ops.expression_of(pch, preset.name).layers["!口/*お"] is True
    s.expression, s.text = "", "PSD の立ち絵なのだ。"
    scene.frame_current = at = scene.frame_end + 10
    run(bpy.ops.tachie.add_still)
    scene.frame_current = at
    run(bpy.ops.tachie.add_line)
    img = still(pch.name)
    assert img.channel == pch.channel == lane("image", pch.name)
    inodes = {os.stat(Path(bpy.path.abspath(img.directory)) / e.filename).st_ino for e in img.elements}
    assert len(inodes) >= 3, inodes  # 口パク（3 段）とまばたき
    # まばたきは目のセット（フォルダ）ではなく中の部品のレイヤーで指す（YMM4 はフォルダの組を使わない）
    import json  # noqa: E402

    ymm_json = psd.with_name(psd.stem + "-ymm.json")
    eye_paths = [p for a in json.loads(ymm_json.read_text(encoding="utf-8-sig"))["EyeAnimations"] for p in a["LayerPaths"]]
    assert eye_paths and not any(p.endswith("*目セット\x1d0") for p in eye_paths)
    # 表情プリセットを YMM4 の表情プリセットとして書き出し（口パク・まばたきは残る）、読み込み直すと同じ表情
    run(bpy.ops.tachie.export_ymm_presets)
    ymm = json.loads(ymm_json.read_text(encoding="utf-8-sig"))
    written = next(p for p in ymm["Presets"] if p["Name"] == preset.name)
    assert ymm["MouthAnimations"] and ymm["EyeAnimations"] and len(written["Layers"]) == len(written["LayerPaths"])
    preset.psd_layers = ""
    run(bpy.ops.tachie.import_presets, filepath=str(ymm_json))
    reread = ops.expression_of(pch, preset.name).layers
    assert reread["!口/*お"] is True and reread.get("!目/*ジト目") is True, reread
    # 書き出して読み直してもレイヤーの上書きが残る
    exported2 = out / "exported_psd.toml"
    run(bpy.ops.tachie.export_characters, filepath=str(exported2))
    again2 = load_characters(exported2)
    assert again2["ずんだもんPSD"].expression.layers["!目/*ジト目"] is True
assert sed.use_prefetch, "プリフェッチが戻っていない"
print("OK")
