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

# 書き出したキャラ設定を読み直すと同じになる
exported = out / "exported.toml"
run(bpy.ops.tachie.export_characters, filepath=str(exported))
from bl_ext.user_default.blender_tachie.tachie_studio.pipeline import load_characters  # noqa: E402

again = load_characters(exported)
assert again["ずんだもん"].expression.eye == "ジト目" and again["四国めたん"].layout["channel"] == 4
assert again["ずんだもん"].voice == {"speedScale": 1.1} and again["四国めたん"].voice == {}, again["ずんだもん"].voice
assert abs(again["ずんだもん"].audio["after"] - 0.3) < 1e-6 and again["四国めたん"].audio["volume"] == 1.0
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

maru = next(p for n, p in system_fonts() if "丸ゴ" in n)
zunda.outline_color = (0.1, 0.45, 0.1, 1.0)
metan.font_path, metan.font_size = maru, 72
assert all(abs(x.outline_color[1] - 0.45) < 1e-3 for x in subs("ずんだもん"))
assert all(x.font_size == 72 and x.font and "Maru" in x.font.name for x in subs("四国めたん")), \
    [(x.font_size, x.font.name if x.font else None) for x in subs("四国めたん")]
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
        (material / sub.name).symlink_to(sub)
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
print("OK")
