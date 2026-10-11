"""Blender 内で動かす結合テスト。scripts/smoke.sh から呼ぶ。

キャラ設定を TOML から読み込み、セリフを置き、編集して再生成（後ろがずれること）、表情を変えて全再生成、
TOML への書き出しを確かめ、途中のフレームを PNG に書き出す。
引数: -- <キャラ設定 TOML> <出力フォルダ>
"""

import sys
from pathlib import Path

import addon_utils
import bpy

characters_toml, out = map(Path, sys.argv[sys.argv.index("--") + 1:])
out.mkdir(parents=True, exist_ok=True)

addon_utils.enable("bl_ext.user_default.ymm_studio", default_set=True)
prefs = bpy.context.preferences.addons["bl_ext.user_default.ymm_studio"].preferences
prefs.output_dir = str(out / "ymm")

scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1920, 1080, 50
scene.render.fps = 30
sed = scene.sequence_editor_create()
bg = sed.strips.new_effect(name="背景", type="COLOR", channel=1, frame_start=1, length=900)
bg.color = (0.55, 0.7, 0.85)
s = scene.ymm


def run(op, **kwargs):
    result = op(**kwargs)
    assert result == {"FINISHED"}, (op, result)


def lines():
    """{ymm_id: (left, right, [ストリップ])}（始まり順）"""
    out = {}
    for st in sed.strips:
        cid = st.get("ymm_id")
        if cid:
            l, r, sts = out.get(cid, (10**9, -1, []))
            out[cid] = (min(l, st.left_handle), max(r, st.right_handle), sts + [st])
    return dict(sorted(out.items(), key=lambda kv: kv[1][0]))


def idle_spans():
    return sorted((st["ymm_idle"], st.channel, st.left_handle, st.right_handle)
                  for st in sed.strips if st.get("ymm_idle"))


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
        print(f"  {l:4d}-{r:4d} {sts[0]['ymm_character']}: {sts[0]['ymm_text']}  "
              f"[{', '.join(f'{x.type}@{x.channel}' for x in sorted(sts, key=lambda x: x.channel))}]")


# キャラ設定の読み込み
run(bpy.ops.ymm.import_characters, filepath=str(characters_toml))
assert [c.name for c in s.characters] == ["ずんだもん", "四国めたん"], [c.name for c in s.characters]
zunda, metan = s.characters
assert zunda.part_other1 == "右腕_基本" and metan.part_body == "白ロリ服"
assert metan.flip_x and metan.channel == 4 and abs(metan.position_x + 0.28) < 1e-6
metan.flip_x = False
zunda.position_x = 0.28

scene.frame_current = 1
for index, text in [(0, "こんにちは、ずんだもんなのだ。"), (1, "四国めたんよ。"), (0, "今日は Blender で動画を作るのだ。")]:
    s.active_character, s.text = index, text
    run(bpy.ops.ymm.add_line)
dump("追加")
print("待機:", idle_spans())
check_idle()
before = list(lines().values())
assert len(before) == 3 and all(len(v[2]) == 3 for v in before)
assert before[0][1] == before[1][0] and before[1][1] == before[2][0], "隙間なく並んでいない"

# 2 本目を長くして再生成 → 3 本目が後ろにずれる
second_id = list(lines())[1]
target = next(x for x in lines()[second_id][2] if x.type == "TEXT")
target["ymm_text"] = "四国めたんよ。「動く立ち絵」の素材をそのまま読み込めるのね。"
sed.active_strip = target
run(bpy.ops.ymm.regenerate)
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
run(bpy.ops.ymm.regenerate_all, character_only=True)
dump("ずんだもんの表情を変えて再生成")
check_idle()

# 待機を切ったキャラは待機ストリップが無くなる
s.characters[1].show_idle = False
run(bpy.ops.ymm.refresh_idle)
assert {name for name, *_ in idle_spans()} == {"ずんだもん"}, idle_spans()
s.characters[1].show_idle = True
run(bpy.ops.ymm.refresh_idle)
check_idle()
assert [(l, r) for l, r, _ in lines().values()] == spans

# 書き出したキャラ設定を読み直すと同じになる
exported = out / "exported.toml"
run(bpy.ops.ymm.export_characters, filepath=str(exported))
from bl_ext.user_default.ymm_studio.ymm.pipeline import load_characters  # noqa: E402

again = load_characters(exported)
assert again["ずんだもん"].expression.eye == "ジト目" and again["四国めたん"].layout["channel"] == 4
print(exported.read_text(encoding="utf-8"))

# 台本の流し込み（テキストエディタの台本、表情つき）
for st in [st for st in sed.strips if st.get("ymm_id") or st.get("ymm_idle")]:
    sed.strips.remove(st)
text = bpy.data.texts.new("台本")
text.from_string((characters_toml.parent / "script.txt").read_text(encoding="utf-8"))
s.script = text
s.line_gap = 0.2
scene.frame_current = 1
run(bpy.ops.ymm.import_script, use_text=True)
dump("台本を流し込み")
placed = list(lines().values())
assert len(placed) == 5
assert [v[2][0].get("ymm_expression") for v in placed] == ["", "", "困り", "おこ", "ドヤ"]
assert all(b[0] - a[1] == 6 for a, b in zip(placed, placed[1:])), "セリフの間隔が 0.2 秒（6 フレーム）でない"


# 選択中のセリフの表情を変えて再生成
third = next(x for x in placed[2][2] if x.type == "TEXT")
third["ymm_expression"] = "ドヤ"
sed.active_strip = third
run(bpy.ops.ymm.regenerate)
assert list(lines().values())[2][2][0]["ymm_expression"] == "ドヤ"
third = next(x for x in list(lines().values())[2][2] if x.type == "TEXT")
third["ymm_expression"] = "困り"
sed.active_strip = third
run(bpy.ops.ymm.regenerate)

# 存在しない表情の台本はエラーで何も置かない
bad = bpy.data.texts.new("悪い台本")
bad.from_string("ずんだもん(泣き): えーん")
s.script = bad
try:
    bpy.ops.ymm.import_script(use_text=True)
except RuntimeError as e:
    print("期待どおり失敗:", str(e).strip().splitlines()[-1])
assert len(lines()) == 5
assert scene.frame_end == max(r for _, r, _ in lines().values()) - 1, scene.frame_end

# 左右反転は置いてあるストリップ（セリフと待機）にすぐ反映される
metan_images = lambda: [x for x in sed.strips if x.type == "IMAGE"
                        and "四国めたん" in (x.get("ymm_character"), x.get("ymm_idle"))]
assert metan_images() and not any(x.use_flip_x for x in metan_images())
metan.flip_x = True
assert all(x.use_flip_x for x in metan_images())
# 字幕のスタイルはキャラクターごと。変えると置いてある字幕にすぐ反映される
subs = lambda name: [x for x in sed.strips if x.type == "TEXT" and x.get("ymm_character") == name]
assert all(abs(x.outline_color[0] - 0.75) < 1e-3 for x in subs("四国めたん")), "TOML の縁取りの色が入っていない"
from bl_ext.user_default.ymm_studio.properties import system_fonts  # noqa: E402

maru = next(p for n, p in system_fonts() if "丸ゴ" in n)
zunda.outline_color = (0.1, 0.45, 0.1, 1.0)
metan.font_path, metan.font_size = maru, 72
assert all(abs(x.outline_color[1] - 0.45) < 1e-3 for x in subs("ずんだもん"))
assert all(x.font_size == 72 and x.font and "Maru" in x.font.name for x in subs("四国めたん")), \
    [(x.font_size, x.font.name if x.font else None) for x in subs("四国めたん")]
assert all(x.font_size == 64 for x in subs("ずんだもん"))
print("フォント:", {x.get("ymm_character"): x.font.name for x in sed.strips if x.type == "TEXT"})
zunda_images = [x for x in sed.strips if x.type == "IMAGE" and "ずんだもん" in (x.get("ymm_character"), x.get("ymm_idle"))]
assert not any(x.use_flip_x for x in zunda_images)
assert s.characters[0].speaker_name, "話者名が入っていない"

scene.render.image_settings.file_format = "PNG"
spans = [(l, r) for l, r, _ in lines().values()]
for i, (l, r) in enumerate(spans):
    frame = (l + r) // 2
    scene.frame_set(frame)
    scene.render.filepath = str(out / f"frame{i + 1}_{frame:04d}.png")
    bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=str(out / "smoke.blend"))
print("OK")
