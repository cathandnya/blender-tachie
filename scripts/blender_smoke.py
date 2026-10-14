"""Blender 内で動かす結合テスト。scripts/smoke.sh から呼ぶ。

キャラ設定を TOML から読み込み、セリフを置き、編集して再生成（後ろがずれること）、表情を変えて全再生成、
TOML への書き出しを確かめ、途中のフレームを PNG に書き出す。
引数: -- <キャラ設定 TOML> <出力フォルダ>
"""

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
bg = sed.strips.new_effect(name="背景", type="COLOR", channel=1, frame_start=1, length=900)
bg.color = (0.55, 0.7, 0.85)
s = scene.tachie


def run(op, **kwargs):
    result = op(**kwargs)
    assert result == {"FINISHED"}, (op, result)


def lines():
    """{tachie_id: (left, right, [ストリップ])}（始まり順）"""
    out = {}
    for st in sed.strips:
        cid = st.get("tachie_id")
        if cid:
            l, r, sts = out.get(cid, (10**9, -1, []))
            out[cid] = (min(l, st.left_handle), max(r, st.right_handle), sts + [st])
    return dict(sorted(out.items(), key=lambda kv: kv[1][0]))


def idle_spans():
    return sorted((st["tachie_idle"], st.channel, st.left_handle, st.right_handle)
                  for st in sed.strips if st.get("tachie_idle"))


def check_idle():
    """各キャラの立ち絵チャンネルが、シーン開始から最後のセリフの終わりまで隙間なく埋まっている。"""
    end = max(r for _, r, _ in lines().values())
    for ch in s.characters:
        spans = sorted((st.left_handle, st.right_handle) for st in sed.strips if st.channel == ch.channel)
        cursor = scene.frame_start
        for l, r in spans:
            assert l == cursor, (ch.name, spans)
            cursor = r
        assert cursor == end, (ch.name, cursor, end)


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
assert metan.flip_x and metan.channel == 4 and abs(metan.position_x + 0.28) < 1e-6
metan.flip_x = False
zunda.position_x = 0.28

scene.frame_current = 1
for index, text in [(0, "こんにちは、ずんだもんなのだ。"), (1, "四国めたんよ。"), (0, "今日は Blender で動画を作るのだ。")]:
    s.active_character, s.text = index, text
    run(bpy.ops.tachie.add_line)
dump("追加")
print("待機:", idle_spans())
check_idle()
before = list(lines().values())
assert len(before) == 3 and all(len(v[2]) == 3 for v in before)
assert before[0][1] == before[1][0] and before[1][1] == before[2][0], "隙間なく並んでいない"

# 2 本目を長くして再生成 → 3 本目が後ろにずれる
second_id = list(lines())[1]
target = next(x for x in lines()[second_id][2] if x.type == "TEXT")
target["tachie_text"] = "四国めたんよ。「動く立ち絵」の素材をそのまま読み込めるのね。"
sed.active_strip = target
run(bpy.ops.tachie.regenerate)
dump("2 本目を長くして再生成")
print("待機:", idle_spans())
check_idle()
after = list(lines().values())
delta = after[1][1] - before[1][1]
assert delta > 0 and after[2][0] == before[2][0] + delta, (delta, after[2][0], before[2][0])
assert after[1][0] == before[1][0]

# 表情を変えて全再生成（位置と長さは変わらない）
zunda.part_eye, zunda.part_eyebrow = "ジト目", "困り眉1"
spans = [(l, r) for l, r, _ in lines().values()]
run(bpy.ops.tachie.regenerate_all, character_only=True)
dump("ずんだもんの表情を変えて再生成")
check_idle()

# 待機を切ったキャラは待機ストリップが無くなる
s.characters[1].show_idle = False
run(bpy.ops.tachie.refresh_idle)
assert {name for name, *_ in idle_spans()} == {"ずんだもん"}, idle_spans()
s.characters[1].show_idle = True
run(bpy.ops.tachie.refresh_idle)
check_idle()
assert [(l, r) for l, r, _ in lines().values()] == spans

# セリフごとの声: 置いたときのキャラの既定値が音声ストリップにコピーされ、あとで既定値を変えても変わらない
from bl_ext.user_default.blender_tachie import operators as ops  # noqa: E402

zunda.speed_scale = 1.1
first_id = next(iter(lines()))
sound = lambda cid: next(x for x in ops.line_strips(sed, cid) if x.type == "SOUND")
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.0) < 1e-6
l0, r0, _ = lines()[first_id]
second_start = list(lines().values())[1][0]
# 1 本だけ速くして再生成 → 短くなり、後ろが詰まる
sound(first_id)["tachie_voice_speed_scale"] = 1.5
sed.active_strip = sound(first_id)
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
l1, r1, _ = lines()[first_id]
assert l1 == l0 and r1 < r0, (l0, r0, l1, r1)
assert list(lines().values())[1][0] == second_start - (r0 - r1)
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.5) < 1e-6
# スタイルを変える
ops._style_items = [("1", "ずんだもん（あまあま）", "", 1)]
run(bpy.ops.tachie.pick_speaker, style="1", target="LINE")
first_id = sed.active_strip["tachie_id"]
assert all(x["tachie_speaker"] == 1 for x in ops.line_strips(sed, first_id))
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.5) < 1e-6
# キャラクターを変える → そのキャラのチャンネル・話者・声になる
run(bpy.ops.tachie.pick_line_character, character="四国めたん")
first_id = sed.active_strip["tachie_id"]
image = next(x for x in ops.line_strips(sed, first_id) if x.type == "IMAGE")
assert image.channel == metan.channel and image["tachie_character"] == "四国めたん"
assert sound(first_id)["tachie_speaker"] == metan.speaker
assert abs(sound(first_id)["tachie_voice_speed_scale"] - 1.0) < 1e-6
run(bpy.ops.tachie.pick_line_character, character="ずんだもん")
assert abs(sound(sed.active_strip["tachie_id"])["tachie_voice_speed_scale"] - 1.1) < 1e-6
# キャラクターの声に戻す
first_id = sed.active_strip["tachie_id"]
sound(first_id)["tachie_voice_pitch_scale"] = 0.1
sed.active_strip = sound(first_id)
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
sed.active_strip = snd
run(bpy.ops.tachie.preview_line)
run(bpy.ops.tachie.regenerate)
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == edited and snd["tachie_kana_auto"] == auto
run(bpy.ops.tachie.reset_kana)
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == auto
snd["tachie_kana"] = "ア'"
snd["tachie_text"] = "こんにちは、ずんだもんだよ。"
sed.active_strip = snd
run(bpy.ops.tachie.regenerate)  # テキストを変えたので編集した発音は使わない
snd = sound(sed.active_strip["tachie_id"])
assert snd["tachie_kana"] == snd["tachie_kana_auto"] != "ア'"
# 読めない発音はエラーで、置いてあるセリフはそのまま
snd["tachie_kana"] = "abc"
sed.active_strip = snd
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
sed.active_strip = sound(first_id)
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
sed.active_strip = sound(first_id)
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
assert sound(first_id).get("tachie_accent") and lines()[first_id][1] == r1
run(bpy.ops.tachie.preview_line)
# 抑揚の編集を消すと元の長さに戻る
run(bpy.ops.tachie.clear_accent)
first_id = sed.active_strip["tachie_id"]
assert not sound(first_id).get("tachie_accent") and lines()[first_id][1] == r0
# 発音を変えると抑揚の編集は捨てる
ops.load_accent_edit(edit, ops._fresh_accent(bpy.context, text_, speaker_, kana_))
edit.clip_id = first_id
edit.phrases[0].moras[0].vowel_length += 0.2
run(bpy.ops.tachie.edit_accent)
first_id = sed.active_strip["tachie_id"]
assert sound(first_id).get("tachie_accent")
snd = sound(first_id)
snd["tachie_kana"] = edited
sed.active_strip = snd
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
sed.active_strip = snd
run(bpy.ops.tachie.regenerate)
assert not sound(sed.active_strip["tachie_id"]).get("tachie_accent")
check_idle()
# 音量・パン（すぐ反映）とフェード・余韻（再生成で反映）
first_id = sed.active_strip["tachie_id"]
snd = sound(first_id)
assert snd.volume == 1.0 and snd["tachie_after"] == 0.0
voice_end = snd.right_handle
image_end = next(x for x in ops.line_strips(sed, first_id) if x.type == "IMAGE").right_handle
snd.volume, snd.pan = 0.5, -0.5
snd["tachie_after"], snd["tachie_fade_out"] = 0.5, 0.2
sed.active_strip = snd
run(bpy.ops.tachie.regenerate)
first_id = sed.active_strip["tachie_id"]
snd = sound(first_id)
image = next(x for x in ops.line_strips(sed, first_id) if x.type == "IMAGE")
assert (snd.volume, snd.pan) == (0.5, -0.5) and snd.right_handle == voice_end
assert image.right_handle == image_end + 15, (image.right_handle, image_end)  # 0.5 秒 = 15 フレーム
from bl_ext.user_default.blender_tachie.tachie_studio.lipsync import read_wav  # noqa: E402

assert read_wav(bpy.path.abspath(snd.sound.filepath)).samples[-1] == 0
check_idle()
zunda.audio_after = 0.3

# 表情ストリップ: 重なる範囲だけ立ち絵の表情が変わり、音声はそのまま

def image_of(cid):
    return next(x for x in ops.line_strips(sed, cid) if x.type == "IMAGE")


def frame_inode(strip, i):
    return os.stat(Path(bpy.path.abspath(strip.directory)) / strip.elements[i].filename).st_ino


def cache_inodes(ch, preset):
    return {p.stat().st_ino for p in ops.character_cache(bpy.context, ch, preset).directory.iterdir()}


zunda_id = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
img = image_of(zunda_id)
orig_dir, wav_before = img.directory, sound(zunda_id).sound.filepath
l, r = img.left_handle, img.right_handle
mid = (l + r) // 2
s.active_character, s.expression = 0, "ドヤ"
scene.frame_current = mid
run(bpy.ops.tachie.add_face)
face = sed.active_strip
assert face["tachie_face"] == "ずんだもん" and face.mute and (face.left_handle, face.right_handle) == (mid, r)
assert face.channel > s.subtitle_channel
img = image_of(zunda_id)
assert img.directory != orig_dir and img["tachie_faced"]
doya, base = cache_inodes(zunda, "ドヤ"), cache_inodes(zunda, "")
assert frame_inode(img, mid - l) in doya and frame_inode(img, r - l - 1) in doya
assert frame_inode(img, 0) in base and frame_inode(img, mid - l - 1) in base
assert sound(zunda_id).sound.filepath == wav_before
# 動かして反映（画面ではハンドラが自動でやる）
face.content_start = l
run(bpy.ops.tachie.apply_faces)
img = image_of(zunda_id)
assert frame_inode(img, 0) in doya and frame_inode(img, r - l - 1) not in doya
# チャンネルが上の表情ストリップが優先
s.expression = "困り"
scene.frame_current = l
run(bpy.ops.tachie.add_face)
face2 = sed.active_strip
face2.duration = 3
run(bpy.ops.tachie.apply_faces)
assert face2.channel > face.channel
img = image_of(zunda_id)
komari = cache_inodes(zunda, "困り")
assert frame_inode(img, 0) in komari and frame_inode(img, 3) in doya
# 表情ストリップの表情を変える
sed.active_strip = face2
ops._expression_items = [("ドヤ", "ドヤ", "")]
run(bpy.ops.tachie.pick_expression, target="FACE", expression="ドヤ")
assert face2.name.startswith("表情 ずんだもん: ドヤ") and frame_inode(image_of(zunda_id), 0) in doya
# 再生成しても表情ストリップに従う
sed.active_strip = sound(zunda_id)
run(bpy.ops.tachie.regenerate)
zunda_id = sed.active_strip["tachie_id"]
assert frame_inode(image_of(zunda_id), 0) in doya
# 待機も表情ストリップの境目で分かれる
metan_id = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "四国めたん")
ml, mr, _ = lines()[metan_id]
idle_before = [x for x in sed.strips if x.get("tachie_idle") == "ずんだもん"]
s.expression = "困り"
scene.frame_current = ml + 2
run(bpy.ops.tachie.add_face)
face3 = sed.active_strip
face3.duration = 5
run(bpy.ops.tachie.apply_faces)
idle_after = [x for x in sed.strips if x.get("tachie_idle") == "ずんだもん"]
assert len(idle_after) == len(idle_before) + 2, (len(idle_before), len(idle_after))
covered = next(x for x in idle_after if x.left_handle == ml + 2)
assert covered.right_handle == ml + 7 and frame_inode(covered, 0) in komari
check_idle()
# キャラに無い表情の表情ストリップは無視する（待機の作り直しが失敗しない）
face3["tachie_expression"] = "無い表情"
run(bpy.ops.tachie.apply_faces)
assert len([x for x in sed.strips if x.get("tachie_idle") == "ずんだもん"]) == len(idle_before)
check_idle()
# 表情ストリップを消すと元に戻る
for f in [face, face2, face3]:
    sed.strips.remove(f)
run(bpy.ops.tachie.apply_faces)
img = image_of(zunda_id)
assert not img["tachie_faced"] and frame_inode(img, 0) in base
assert len([x for x in sed.strips if x.get("tachie_idle") == "ずんだもん"]) == len(idle_before)
s.expression = ""

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
select_only([image_of(zunda_ids[0]), sound(zunda_ids[1]), bface])
assert ops.selected_targets(sed)[0] == zunda_ids and ops.selected_targets(sed)[2] == [bface]
run(bpy.ops.tachie.batch_edit, use_expression=True, expression="ドヤ")
new_ids = [cid for cid in lines() if lines()[cid][2][0]["tachie_character"] == "ずんだもん"][:2]
assert all(lines()[cid][2][0]["tachie_expression"] == "ドヤ" for cid in new_ids)
assert bface["tachie_expression"] == "ドヤ"
# 音量だけ: 作り直さない
select_only([image_of(cid) for cid in new_ids])
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
# 何も変えなければ何もしない
assert bpy.ops.tachie.batch_edit() == {"CANCELLED"}
# 別キャラが混ざると、両方が持っている表情だけ選べる
metan_ids = [cid for cid in lines() if lines()[cid][2][0]["tachie_character"] == "四国めたん"][:1]
select_only([image_of(after_ids[0]), image_of(metan_ids[0])])
names = [i[0] for i in ops._batch_expressions(type("P", (), {"use_character": False, "character": ""})(), bpy.context)]
common = {e.name for e in zunda.expressions} & {e.name for e in metan.expressions}
assert names[0] == ops.NONE and set(names[1:]) == common and "ドヤ" not in names, names
# 候補が変わっても同じ表情は同じ番号（動的な Enum は番号で値を持つ）
mixed_items = ops._batch_expressions(type("P", (), {"use_character": False, "character": ""})(), bpy.context)
zunda_items = ops._batch_expressions(type("P", (), {"use_character": True, "character": "ずんだもん"})(), bpy.context)
zunda_numbers = {i[0]: i[3] for i in zunda_items}
assert all(zunda_numbers[i[0]] == i[3] for i in mixed_items) and len(set(zunda_numbers.values())) == len(zunda_items)
sed.strips.remove(bface)
run(bpy.ops.tachie.apply_faces)
check_idle()

# 立ち絵の動き: 喋っている間に跳ねる・揺れる、登場（フェード）・退場（スライド）、セリフごとの大きさ
from bl_ext.user_default.blender_tachie import motion as motion_mod  # noqa: E402


def keyed(strip, attr):
    bag = motion_mod._channelbag(scene)
    path = f'sequence_editor.strips_all["{strip.name}"].{attr}'
    fc = next((f for f in bag.fcurves if f.data_path == path), None) if bag else None
    return [tuple(k.co) for k in fc.keyframe_points] if fc else []


zunda.talk_bounce, zunda.talk_sway = 0.02, 3.0  # 変えるとすぐ作り直す
zid = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
zimg = image_of(zid)
ys = keyed(zimg, "transform.offset_y")
assert len(ys) > 4 and max(v for _, v in ys) > 0, ys
assert keyed(zimg, "transform.rotation")
peak = next(f for f, v in ys if v > 0)
scene.frame_set(int(peak))
assert zimg.transform.offset_y > 0
zunda.enter_effect, zunda.exit_effect = "FADE", "SLIDE"
zstrips = sorted([x for x in sed.strips if motion_mod.owner(x) == "ずんだもん" and x.channel == zunda.channel],
                 key=lambda x: x.left_handle)
assert [v for _, v in keyed(zstrips[0], "blend_alpha")][:2] == [0.0, 1.0]
assert len(keyed(zstrips[-1], "transform.offset_x")) == 2
# セリフごとの大きさ（足元が中心）。再生成しても残る
zimg["tachie_scale"] = 1.5
ops.refresh_motion_now(scene)
assert abs(zimg.transform.scale_x - 1.5) < 1e-6 and tuple(zimg.transform.origin) == (0.5, 0.0)
sed.active_strip = sound(zid)
run(bpy.ops.tachie.regenerate)
zid = sed.active_strip["tachie_id"]
assert abs(image_of(zid)["tachie_scale"] - 1.5) < 1e-6 and keyed(image_of(zid), "transform.offset_y")
# 動きを切るとキーフレームは消える
zunda.talk_bounce = zunda.talk_sway = 0.0
zunda.enter_effect = zunda.exit_effect = "NONE"
image_of(zid)["tachie_scale"] = 1.0
ops.refresh_motion_now(scene)
assert not keyed(image_of(zid), "transform.offset_y") and not keyed(zstrips[0], "blend_alpha") \
    if zstrips[0].name in {x.name for x in sed.strips} else True
assert abs(image_of(zid).transform.scale_x - 1.0) < 1e-6

# 字幕の帯・キャラ名・フェードイン、見出しテロップ
metan.subtitle_box, metan.box_roundness = True, 0.5
msubs = [x for x in sed.strips if x.type == "TEXT" and x.get("tachie_character") == "四国めたん"]
assert msubs and all(x.use_box and abs(x.box_roundness - 0.5) < 1e-6 for x in msubs)
metan.show_name, metan.subtitle_fade = True, 0.2
mid = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "四国めたん")
sed.active_strip = sound(mid)
run(bpy.ops.tachie.regenerate)
mid = sed.active_strip["tachie_id"]
mstrips = ops.line_strips(sed, mid)
name_strip = next(x for x in mstrips if x.get("tachie_role") == "name")
msub = next(x for x in mstrips if x.type == "TEXT" and x.get("tachie_role") != "name")
assert name_strip.text == "四国めたん" and name_strip.channel == s.name_channel
assert name_strip.location[1] > msub.location[1] and name_strip.left_handle == msub.left_handle
assert [v for _, v in keyed(msub, "blend_alpha")] == [0.0, 1.0] and keyed(name_strip, "blend_alpha")
assert all(c.text != "四国めたん" for c in ops.subtitle_cues(scene))  # キャラ名は字幕ファイルに入らない
s.headline_text, s.headline_seconds = "第 1 章 Blender で動画", 2.0
scene.frame_current = 1
run(bpy.ops.tachie.add_headline)
head = sed.active_strip
assert head["tachie_headline"] and head.text == "第 1 章 Blender で動画" and head.use_box
head.text = "第 1 章 Blender で動画を作る"  # ストリップで直した文字はスタイルを変えても残す
s.headline_size = 120
assert head.font_size == 120 and head.text == "第 1 章 Blender で動画を作る"  # スタイルはすぐ反映
metan.show_name, metan.subtitle_fade, metan.subtitle_box = False, 0.0, False
sed.active_strip = sound(mid)
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
reserved = {s.audio_channel, s.subtitle_channel, s.name_channel, s.headline_channel} | {c.channel for c in s.characters}
assert se_strip["tachie_se"] == "se" and se_strip.left_handle == 5 and abs(se_strip.volume - 0.7) < 1e-6
assert se_strip.channel not in reserved
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
assert all(x.channel not in reserved for x in sed.strips if x.get("tachie_se"))
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
assert role(zsts, SOUND).channel == s.audio_channel and role(zsts, SUB).channel == s.subtitle_channel
reserved = {s.audio_channel, s.subtitle_channel, s.name_channel, s.headline_channel} | {c.channel for c in s.characters}
msound, msub, mname = role(msts, SOUND), role(msts, SUB), role(msts, NAME)
assert msound.channel != s.audio_channel and msound.channel not in reserved, msound.channel
assert msub.channel > s.subtitle_channel and msub.channel not in reserved, msub.channel
assert mname.channel == s.name_channel or (mname.channel > msub.channel and mname.channel not in reserved)
assert msound.left_handle == t0 + 5 and msound.left_handle < role(zsts, SOUND).right_handle
scene.frame_current = t0 + 10  # 同じキャラは重ねられない
s.active_character, s.text = 0, "かぶせるのだ。"
try:
    bpy.ops.tachie.add_line()
    raise AssertionError("同じキャラなのに重ねて置けた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
# 縮めて再生成: 同時に喋るめたんのセリフは動かさず、その後ろのめたんのセリフはそこに重ならないところまで詰める
zl, zr, _ = next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋るのだ。")
ml, mr, _ = next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋りますわね、ずんだもんさん。")
assert zl < ml < zr < mr, (zl, zr, ml, mr)
scene.frame_current = mr
s.active_character, s.text = 1, "同時に続けますわ。"
run(bpy.ops.tachie.add_line)
s.ripple = True
sed.active_strip = role(zsts, SOUND)
sed.active_strip["tachie_text"] = "同時に。"
run(bpy.ops.tachie.regenerate)
(cl, cr, csts), = [v for v in lines().values() if v[2][0]["tachie_text"] == "同時に続けますわ。"]
assert cl >= mr and role(csts, ("IMAGE", None)).channel == metan.channel, (cl, mr)
assert next(v for v in lines().values() if v[2][0]["tachie_text"] == "同時に喋りますわね、ずんだもんさん。")[:2] == (ml, mr)
# 字幕をセリフごとにずらす（キャラ名も一緒に動く）。再生成・スタイル変更でも残る
name_y = mname.location[1]
msub["tachie_sub_x"], msub["tachie_sub_y"] = -0.1, 0.3
ops.refresh_motion_now(scene)
assert abs(msub.location[0] - 0.4) < 1e-6 and abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6
assert abs(mname.location[1] - min(0.98, name_y + 0.3)) < 1e-6 and abs(mname.location[0] - 0.4) < 1e-6
sed.active_strip = msound
run(bpy.ops.tachie.regenerate)
msts = ops.line_strips(sed, sed.active_strip["tachie_id"])
msub = role(msts, SUB)
assert abs(msub["tachie_sub_y"] - 0.3) < 1e-6 and abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6
assert role(msts, SOUND).channel not in reserved and msub.channel not in reserved
metan.font_size += 2
assert abs(msub.location[1] - (metan.subtitle_y + 0.3)) < 1e-6 and abs(role(msts, NAME).location[0] - 0.4) < 1e-6
metan.font_size -= 2
cue_texts = [c.text for c in ops.subtitle_cues(scene)]
assert any("同時に。" in t for t in cue_texts) and any("同時に喋りますわね" in t for t in cue_texts)
assert any(a <= t0 and b >= role(msts, SOUND).right_handle for a, b in ducking.voice_spans(sed, 0))
for x in [x for x in sed.strips if x.get("tachie_text", "").startswith("同時に")]:
    sed.strips.remove(x)
metan.show_name = False
ops.refresh_idle(bpy.context)
scene.frame_end = frame_end_before

# セリフの形: 音声と字幕・音声だけ（立ち絵なし）。喋っている間はそのキャラの立ち絵（待機）も消す
import tomllib  # noqa: E402

from bl_ext.user_default.blender_tachie.tachie_studio.pipeline.config import (  # noqa: E402
    dump_characters, parse_characters)

frame_end_before = scene.frame_end
t1 = max(r for _, r, _ in lines().values()) + 30
by_text = lambda text: next(sts for _, (_, _, sts) in lines().items() if sts[0]["tachie_text"] == text)
span = lambda sts: (min(x.left_handle for x in sts), max(x.right_handle for x in sts))
cue_has = lambda text: any(text in c.text.replace("\n", "") for c in ops.subtitle_cues(scene))
zunda.line_layout = "SUBTITLE"
scene.frame_current = t1
s.active_character, s.expression, s.text = 0, "", "形を変えるのだ。"
run(bpy.ops.tachie.add_line)
fsts = by_text("形を変えるのだ。")
fl, fr = span(fsts)
assert sorted(x.type for x in fsts) == ["SOUND", "TEXT"] and all(x["tachie_layout"] == "SUBTITLE" for x in fsts)
assert not any(x.channel == zunda.channel and x.left_handle < fr and fl < x.right_handle for x in sed.strips)
assert any(x.get("tachie_idle") == "四国めたん" and x.left_handle < fr and fl < x.right_handle for x in sed.strips)
assert cue_has("形を変えるのだ")
sed.active_strip = role(fsts, SOUND)
run(bpy.ops.tachie.pick_line_layout, line_layout="AUDIO")
fsts = by_text("形を変えるのだ。")
assert [x.type for x in fsts] == ["SOUND"] and not cue_has("形を変えるのだ") and span(fsts)[0] == fl
assert ops.line_end(fsts) == fr  # 音声だけでも余韻を含む長さ（再生成で後ろがずれない）
sed.active_strip = fsts[0]
run(bpy.ops.tachie.pick_line_layout, line_layout="FULL")
fsts = by_text("形を変えるのだ。")
assert role(fsts, ("IMAGE", None)).channel == zunda.channel and span(fsts) == (fl, fr)
check_idle()  # 立ち絵が戻れば待機も元どおり
zunda.line_layout = "FULL"
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
sed.active_strip = role(nsts, SOUND)
run(bpy.ops.tachie.regenerate)
nsts = by_text("ナレーションです。")
assert sorted(x.type for x in nsts) == ["SOUND", "TEXT"] and span(nsts)[0] == t1
again = parse_characters(tomllib.loads(dump_characters({nar.name: ops.config_of(nar)})), out)[nar.name]
assert again.folder is None and again.layout.get("lines") == "SUBTITLE"
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
ops.refresh_idle(bpy.context)
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
addon_before = (s.se_channel, s.audio_channel, s.subtitle_channel, s.name_channel, s.headline_channel,
                zunda.channel, metan.channel)
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
         "TachieItemParameter": {"Eye": metan_dir + "\\目\\普通白目_普通目.png",
                                 "Mouth": metan_dir + "\\口\\ほほえみ.png"}, "X": -770.0, "Y": 600.0,
         "Zoom": 80.0, "Frame": ymm_base, "Layer": 1, "Length": 700},
        {"$type": "YukkuriMovieMaker.Project.Items.TachieFaceItem, YukkuriMovieMaker", "CharacterName": "ずんだもん",
         "TachieFaceParameter": {"Eyebrow": YMM_CHAR + "ずんだもん\\眉\\怒り眉.png"}, "Frame": ymm_base + 600,
         "Layer": 4, "Length": 60},
        *ymm_media,
    ]}]}, ensure_ascii=False), encoding="utf-8-sig")
s.ymmp_material_root = str(Path(zunda.folder).parent if Path(zunda.folder).is_absolute()
                           else Path(bpy.path.abspath(zunda.folder)).parent)
zunda_presets = {e.name for e in zunda.expressions}
n_chars = len(s.characters)
run(bpy.ops.tachie.import_ymmp, filepath=str(ymmp), match_video=False)
report = bpy.data.texts["YMM4 の読み込み結果"].as_string()
print(report)
assert "セリフ 4 / 4 個" in report and "missing.png" in report and "存在しないフォント" in report, report
assert "画像 1" in report and "音声 1" in report and "テキスト 1" in report, report
# 立ち絵より奥（レイヤー 0）は、アドオンのチャンネルを上へずらした下に置く（置いてあったセリフも一緒にずれる）
shift = 2 if has_ffmpeg else 1  # レイヤー 0 に動画（スピードコントロールで 2 本）
assert (s.se_channel, s.audio_channel, s.subtitle_channel, s.name_channel, s.headline_channel, zunda.channel,
        metan.channel) == tuple(c + shift for c in addon_before)
assert first_sound.channel == first_sound_channel + shift
ymm = {x.get("tachie_ymmp"): x for x in sed.strips if x.get("tachie_ymmp") and x.type != "MOVIE"}
bg = ymm["image"]
assert bg.channel < min(addon_before) + shift and bg.left_handle == scene.frame_start + ymm_base // 2
assert bg.right_handle == bg.left_handle + 50 and abs(bg.transform.scale_x - 0.5) < 1e-6
assert (bg.transform.offset_x, bg.transform.offset_y) == (100.0, 50.0)
assert [v for _, v in keyed(bg, "blend_alpha")] == [1.0, 0.0]
bgm_strip = ymm["audio"]
assert [round(v, 4) for _, v in keyed(bgm_strip, "volume")] == [0.1, 0.0] and bgm_strip.right_handle - bgm_strip.left_handle == 150
title = ymm["text"]
assert title.text == "タイトル\n2 行目" and title.use_bold and title.anchor_x == "CENTER" and title.anchor_y == "TOP"
assert abs(title.location[0] - (0.5 - 100 / 1920)) < 1e-6 and abs(title.location[1] - (0.5 + 500 / 1080)) < 1e-6
assert title.channel > s.headline_channel
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
zpreset = zsts[0]["tachie_expression"]
assert zpreset not in zunda_presets and next(e for e in zunda.expressions if e.name == zpreset).part_eye == "なごみ目"
yfaces = [x for x in sed.strips if x.get("tachie_face") == "ずんだもん" and x.left_handle == zl + 300]
assert yfaces and next(e for e in zunda.expressions
                       if e.name == yfaces[0]["tachie_expression"]).part_eyebrow == "怒り眉"
ynar = s.characters[n_chars + 1]  # 立ち絵の無いキャラは素材を探さずに追加し、声と字幕だけで置く
assert ynar.name == "ナレーター（YMM4）" and ynar.folder == "" and ynar.line_layout == "SUBTITLE"
assert ynar.speaker == 2 and ynar.font_size == 50
_, nsts = ylines["読み込んだナレーション"]
assert sorted(x.type for x in nsts) == ["SOUND", "TEXT"] and nsts[0]["tachie_layout"] == "SUBTITLE"
# 片付け（あとのテストに影響しないように）
for x in [x for x in sed.strips if (x.get("tachie_text", "").startswith("読み込んだ") or x in yfaces
                                    or x.get("tachie_character") in (ymetan.name, ynar.name))]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.get("tachie_ymmp") and x.type == "SPEED"]:
    sed.strips.remove(x)
for x in [x for x in sed.strips if x.get("tachie_ymmp")]:
    sed.strips.remove(x)
for x in sorted(sed.strips, key=lambda x: x.channel):  # ずらしたチャンネルを戻す（下から）
    x.channel -= shift
for prop in ("se_channel", "audio_channel", "subtitle_channel", "name_channel", "headline_channel"):
    setattr(s, prop, getattr(s, prop) - shift)
for c in s.characters:
    c.channel -= shift
s.characters.remove(n_chars + 1)
s.characters.remove(n_chars)
for i in reversed(range(len(zunda.expressions))):
    if zunda.expressions[i].name not in zunda_presets:
        zunda.expressions.remove(i)
ops.refresh_idle(bpy.context)
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
# 立ち絵より奥（背景）にすると、アドオンのチャンネルをずらして下に置く
channels_before = (s.se_channel, s.audio_channel, s.subtitle_channel, s.name_channel, s.headline_channel,
                   *(c.channel for c in s.characters))
tpl.behind = True
t_at2 = max(x.right_handle for x in sed.strips) + 20
scene.frame_current = t_at2
run(bpy.ops.tachie.insert_template, index=list(templates).index(tpl))
p_img2 = next(x for x in sed.strips if x.get("tachie_ymmp") == "image" and x.left_handle >= t_at2)
assert p_img2.channel < min(s.se_channel, s.audio_channel, *(c.channel for c in s.characters))
tpl_shift = s.se_channel - channels_before[0]
# YMM4 のテンプレート（ItemSettings.json）を読み込む: セリフと画像は置ける、トランジション・立ち絵アイテムは読まない
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
         "Length": 60}]},
]}, ensure_ascii=False), encoding="utf-8-sig")
n_templates = len(templates)
run(bpy.ops.tachie.import_ymm4_templates, filepath=str(item_settings))
tpl_report = bpy.data.texts["YMM4 のテンプレートの読み込み結果"].as_string()
assert "2 個のうち 1 個" in tpl_report and "トランジション 1" in tpl_report and "立ち絵アイテム 1" in tpl_report, tpl_report
assert len(templates) == n_templates + 1
zunda_presets_t = {e.name for e in zunda.expressions}
t_at3 = max(x.right_handle for x in sed.strips) + 20
scene.frame_current = t_at3
run(bpy.ops.tachie.insert_template, index=list(templates).index(next(t for t in templates if t.name == "あいさつ")))
greet = [sts for _, (l, _, sts) in lines().items() if l >= t_at3]
assert [g[0]["tachie_text"] for g in greet] == ["テンプレなのだ", "続きなのだ"]
assert max(x.right_handle for x in greet[0]) <= min(x.left_handle for x in greet[1])
gpreset = greet[0][0]["tachie_expression"]
assert gpreset and next(e for e in zunda.expressions if e.name == gpreset).part_eye == "なごみ目"
# 片付け
for x in [x for x in sed.strips if x.left_handle >= t_at and (x.get("tachie_id") or x.get("tachie_ymmp"))]:
    sed.strips.remove(x)
for i in reversed(range(len(templates))):
    if templates[i].name in ("テスト", "あいさつ"):
        templates.remove(i)
for i in reversed(range(len(zunda.expressions))):  # 読み込みで作った表情プリセット
    if zunda.expressions[i].name not in zunda_presets_t:
        zunda.expressions.remove(i)
if tpl_shift:
    for x in sorted(sed.strips, key=lambda x: x.channel):
        x.channel -= tpl_shift
    for prop in ("se_channel", "audio_channel", "subtitle_channel", "name_channel", "headline_channel"):
        setattr(s, prop, getattr(s, prop) - tpl_shift)
    for c in s.characters:
        c.channel -= tpl_shift
ops.refresh_idle(bpy.context)
scene.frame_end = frame_end_before

# AI で表情を割り当てる（偽の LLM サーバ。キャラごとの束の最初の行は -1 = 変えない、他は WANT の表情の番号）
import re  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from bl_ext.user_default.blender_tachie import faceassign as fa_mod  # noqa: E402

WANT = {}
llm_calls = []


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
          and ops.line_layout_of(sts) == "FULL" and no_faces(cid)][:2]
assert len(fa_ids) == 2, fa_ids
keep_expr = image_of(fa_ids[0])["tachie_expression"]
old_expr = image_of(fa_ids[1])["tachie_expression"]
WANT["ずんだもん"] = next(e.name for e in zunda.expressions if e.name != old_expr)
wav_before = sound(fa_ids[1]).sound.filepath
select_only([image_of(fa_ids[0]), sound(fa_ids[1])])
prefs.faceassign_target = "LINE"
run(bpy.ops.tachie.assign_faces)
assert len(llm_calls) == 1 and llm_calls[0].count("#") == 2, llm_calls
assert all(x["tachie_expression"] == keep_expr for x in ops.line_strips(sed, fa_ids[0]))
assert all(x["tachie_expression"] == WANT["ずんだもん"] for x in ops.line_strips(sed, fa_ids[1]))
assert sound(fa_ids[1]).sound.filepath == wav_before  # 音声は合成し直さない
img = image_of(fa_ids[1])
assert frame_inode(img, 0) in cache_inodes(zunda, WANT["ずんだもん"])
fa_report = bpy.data.texts[fa_mod.REPORT_TEXT].as_string()
assert fa_report.startswith("2 行のうち 1 行の表情を変えました"), fa_report
print(fa_report)
# 書き込み先を表情ストリップにすると、セリフの区間に表情ストリップを置く（セリフの表情はそのまま）
WANT["ずんだもん"] = next(e.name for e in zunda.expressions if e.name not in (WANT["ずんだもん"], keep_expr))
prefs.faceassign_target = "FACE_STRIP"
select_only([image_of(fa_ids[0]), image_of(fa_ids[1])])
faces_before = set(ops.face_strips(sed, "ずんだもん"))
run(bpy.ops.tachie.assign_faces)
new_faces = [f for f in ops.face_strips(sed, "ずんだもん") if f not in faces_before]
sts = ops.line_strips(sed, fa_ids[1])
assert [(f.left_handle, f.right_handle, f["tachie_expression"]) for f in new_faces] == [
    (min(x.left_handle for x in sts), ops.line_end(sts), WANT["ずんだもん"])], new_faces
assert image_of(fa_ids[1])["tachie_expression"] != WANT["ずんだもん"]
assert frame_inode(image_of(fa_ids[1]), 0) in cache_inodes(zunda, WANT["ずんだもん"])
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
assert again["ずんだもん"].expression.eye == "ジト目" and again["四国めたん"].layout["channel"] == 4
assert again["ずんだもん"].voice == {"speedScale": 1.1} and again["四国めたん"].voice == {}, again["ずんだもん"].voice
assert abs(again["ずんだもん"].audio["after"] - 0.3) < 1e-6 and again["四国めたん"].audio["volume"] == 1.0
assert again["ずんだもん"].layout["period"] == 0.5 and "enter" not in again["ずんだもん"].layout
print(exported.read_text(encoding="utf-8"))

# 台本の流し込み（テキストエディタの台本、表情つき）
for st in [st for st in sed.strips if st.get("tachie_id") or st.get("tachie_idle")]:
    sed.strips.remove(st)
text = bpy.data.texts.new("台本")
text.from_string((characters_toml.parent / "script.txt").read_text(encoding="utf-8"))
s.script = text
s.line_gap = 0.2
scene.frame_current = 1
run(bpy.ops.tachie.import_script, use_text=True)
dump("台本を流し込み")
placed = list(lines().values())
assert len(placed) == 5
assert [v[2][0].get("tachie_expression") for v in placed] == ["", "", "困り", "おこ", "ドヤ"]
assert all(next(x for x in v[2] if x.type == "SOUND")["tachie_kana"] for v in placed)
assert all(b[0] - a[1] == 6 for a, b in zip(placed, placed[1:])), "セリフの間隔が 0.2 秒（6 フレーム）でない"


# 選択中のセリフの表情を変えて再生成
third = next(x for x in placed[2][2] if x.type == "TEXT")
third["tachie_expression"] = "ドヤ"
sed.active_strip = third
run(bpy.ops.tachie.regenerate)
assert list(lines().values())[2][2][0]["tachie_expression"] == "ドヤ"
third = next(x for x in list(lines().values())[2][2] if x.type == "TEXT")
third["tachie_expression"] = "困り"
sed.active_strip = third
run(bpy.ops.tachie.regenerate)

# 存在しない表情の台本はエラーで何も置かない
bad = bpy.data.texts.new("悪い台本")
bad.from_string("ずんだもん(泣き): えーん")
s.script = bad
try:
    bpy.ops.tachie.import_script(use_text=True)
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
assert len(lines()) == 5
# 字幕を SRT / WebVTT で書き出す
srt = out / "subtitles.srt"
run(bpy.ops.tachie.export_subtitles, filepath=str(srt), with_speaker=True)
srt_text = srt.read_text(encoding="utf-8")
first_l = min(v[0] for v in lines().values())
assert srt_text.count(" --> ") == 5 and srt_text.startswith("1\n"), srt_text[:200]
assert ": " in srt_text.splitlines()[2]  # キャラ名付き
run(bpy.ops.tachie.export_subtitles, filepath=str(srt), format="VTT")
assert (out / "subtitles.vtt").read_text(encoding="utf-8").startswith("WEBVTT")
print(srt_text.splitlines()[:3])
# 書き出し設定のプリセット（セリフを置いたあとで fps を変えると再生成を案内する）
fps_before = (scene.render.fps, scene.render.resolution_x, scene.render.resolution_y)
run(bpy.ops.tachie.render_preset, preset="FHD60")
r = scene.render
assert (r.resolution_x, r.resolution_y, r.fps, r.image_settings.file_format, r.ffmpeg.codec, r.ffmpeg.audio_codec) == \
    (1920, 1080, 60, "FFMPEG", "H264", "AAC")
r.fps, r.resolution_x, r.resolution_y = fps_before  # 以降のテストは元の設定で
r.image_settings.media_type = "IMAGE"
# 声のエンジン（VOICEVOX 互換）: キャラごとに URL を指定する（ここでは同じエンジンを別の URL 表記で）
# （VOICEVOX_URL で別のマシンを指定したときは、末尾に / を付けた表記にする）
alt_url = (prefs.voicevox_url.replace("127.0.0.1", "localhost") if "127.0.0.1" in prefs.voicevox_url
           else prefs.voicevox_url.rstrip("/") + "/")
zunda.engine, zunda.engine_url = "CUSTOM", alt_url
assert ops.engine_url(bpy.context, zunda) == alt_url
first_zunda = next(cid for cid, (_, _, sts) in lines().items() if sts[0]["tachie_character"] == "ずんだもん")
sed.active_strip = sound(first_zunda)
run(bpy.ops.tachie.regenerate)
assert ops.config_of(zunda).engine.rstrip("/") == alt_url.rstrip("/")
cfg_engine = ops.config_of(zunda)
cfg_engine.engine = "http://127.0.0.1:10101"
ops.apply_config(zunda, cfg_engine)
assert zunda.engine == "AIVIS"  # 既知のエンジンの URL は名前で選ばれる
zunda.engine = "DEFAULT"
assert scene.frame_end == max(r for _, r, _ in lines().values()) - 1, scene.frame_end

# 左右反転は置いてあるストリップ（セリフと待機）にすぐ反映される
metan_images = lambda: [x for x in sed.strips if x.type == "IMAGE"
                        and "四国めたん" in (x.get("tachie_character"), x.get("tachie_idle"))]
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
zunda_images = [x for x in sed.strips if x.type == "IMAGE" and "ずんだもん" in (x.get("tachie_character"), x.get("tachie_idle"))]
assert not any(x.use_flip_x for x in zunda_images)
assert s.characters[0].speaker_name, "話者名が入っていない"

# セリフなしの立ち絵を最後のセリフの後ろに置く → 他のキャラの待機もそこまで伸びる
stills = lambda: [x for x in sed.strips if x.get("tachie_still")]
last_end = max(r for _, r, _ in lines().values())
s.active_character, s.expression, s.still_seconds = 0, "困り", 2.0
scene.frame_current = last_end
run(bpy.ops.tachie.add_still)
(still,) = stills()
assert (still.left_handle, still.right_handle, still.channel) == (last_end, last_end + 60, zunda.channel)
assert still["tachie_expression"] == "困り" and not still.use_flip_x
assert scene.frame_end == still.right_handle - 1
metan_idle = [(x.left_handle, x.right_handle) for x in sed.strips if x.get("tachie_idle") == "四国めたん"]
assert max(r for _, r in metan_idle) == still.right_handle, metan_idle
# セリフに重ねては置けない
scene.frame_current = list(lines().values())[0][0]
try:
    bpy.ops.tachie.add_still()
    raise AssertionError("重なっているのに置けた")
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
# 伸ばして作り直すと、まばたきが伸ばした長さぶん入る（待機の作り直しでは消えない）
still.right_handle = still.left_handle + 90
still["tachie_expression"] = ""
sed.active_strip = still
run(bpy.ops.tachie.remake_still)
(still,) = stills()
assert still.right_handle - still.left_handle == 90 and len(still.elements) == 90, len(still.elements)
assert still["tachie_expression"] == ""
run(bpy.ops.tachie.refresh_idle)
run(bpy.ops.tachie.regenerate_all, character_only=False)
assert [(x.left_handle, x.right_handle) for x in stills()] == [(last_end, last_end + 90)]
s.expression = ""

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
assert new.channel not in {c.channel for c in s.characters if c != new} | {s.audio_channel, s.subtitle_channel}
new.height = 0.7
run(bpy.ops.tachie.save_defaults, target="PREFS")
saved = ops.load_character_defaults()["ずんだもん2"]
assert saved["name"] == "ずんだ（素材）" and saved["speaker"] == 7 and abs(saved["layout"]["height"] - 0.7) < 1e-6
run(bpy.ops.tachie.save_defaults, target="MATERIAL")
from bl_ext.user_default.blender_tachie.tachie_studio.pipeline.config import load_material_table  # noqa: E402

assert load_material_table(material)["speaker"] == 7
run(bpy.ops.tachie.remove_defaults, key="ずんだもん2")
assert ops.load_character_defaults() == {}

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
scene.frame_current = scene.frame_end + 10
run(bpy.ops.tachie.add_line)
cid = list(lines())[-1]
img = image_of(cid)
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
    scene.frame_current = scene.frame_end + 10
    run(bpy.ops.tachie.add_line)
    cid = list(lines())[-1]
    img = image_of(cid)
    assert img.channel == pch.channel
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
print("OK")
