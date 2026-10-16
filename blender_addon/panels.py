import bpy

from .operators import (line_image, line_layout_of, line_sound, line_strips, line_subtitle, load_material, output_dir,
                        prefs, selected_targets, seq_scene)
from .properties import AUDIO_PROPS, BAKED_AUDIO, LINE_LAYOUTS, PART_PROPS, VOICE_PROPS


class _Panel:
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "立ち絵"


class TACHIE_UL_characters(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon="USER")
        row.label(text=item.speaker_name or f"ID {item.speaker}")


class TACHIE_UL_expressions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "name", text="", emboss=False, icon="SHADERFX")


class TACHIE_MT_characters(bpy.types.Menu):
    bl_label = "キャラクター設定"

    def draw(self, context):
        self.layout.operator("tachie.character_add_voice", icon="SPEAKER")
        self.layout.separator()
        self.layout.operator("tachie.import_characters", icon="IMPORT")
        self.layout.operator("tachie.export_characters", icon="EXPORT")
        self.layout.operator("tachie.import_presets", icon="SHADERFX")
        self.layout.operator("tachie.export_ymm_presets", icon="EXPORT")
        self.layout.separator()
        self.layout.operator("tachie.save_defaults", text="初期値としてアドオン設定に保存", icon="PREFERENCES").target = "PREFS"
        self.layout.operator("tachie.save_defaults", text="素材フォルダに初期値を書き出す（tachie.toml）",
                             icon="FILE_FOLDER").target = "MATERIAL"
        self.layout.separator()
        self.layout.operator("tachie.reload_character", icon="FILE_REFRESH")


class TACHIE_PT_main(_Panel, bpy.types.Panel):
    bl_label = "Blender Tachie"

    def draw(self, context):
        layout = self.layout
        s = seq_scene(context).tachie

        row = layout.row()
        row.template_list("TACHIE_UL_characters", "", s, "characters", s, "active_character", rows=3)
        col = row.column(align=True)
        col.operator("tachie.character_add", text="", icon="ADD")
        col.operator("tachie.character_remove", text="", icon="REMOVE")
        col.separator()
        col.menu("TACHIE_MT_characters", text="", icon="DOWNARROW_HLT")

        ch = s.character()
        layout.prop(s, "text", text="")
        tachie = ch is not None and bool(ch.folder)  # 立ち絵のあるキャラ
        row = layout.row(align=True)
        row.enabled = tachie
        row.label(text="表情")
        row.operator("tachie.pick_expression", text=s.expression or "（基本）").target = "ADD"
        row = layout.row()
        row.scale_y = 1.4
        row.enabled = ch is not None
        row.operator("tachie.add_line", text=f"{ch.name}のセリフを追加" if ch else "セリフを追加", icon="ADD")
        row = layout.row(align=True)
        row.enabled = tachie
        row.operator("tachie.add_still", text="立ち絵だけを置く", icon="IMAGE_DATA")
        row.prop(s, "still_seconds")
        row = layout.row(align=True)
        row.enabled = tachie
        row.operator("tachie.add_face", text="表情を置く", icon="SHADERFX")
        row.prop(s, "face_seconds")
        layout.prop(s, "advance")
        row = layout.row(align=True)
        row.prop(s, "auto_idle")
        row.operator("tachie.refresh_idle", text="", icon="FILE_REFRESH")
        if not bpy.data.filepath and not context.preferences.addons[__package__].preferences.output_dir:
            layout.label(text=f"未保存のため {output_dir(context)} に書き出します", icon="INFO")


class TACHIE_PT_line(_Panel, bpy.types.Panel):
    bl_label = "選択中のセリフ"
    bl_parent_id = "TACHIE_PT_main"

    def draw(self, context):
        layout = self.layout
        sed = seq_scene(context).sequence_editor
        strip = sed.active_strip if sed else None
        ids, stills, faces = selected_targets(sed)
        n = len(ids) + len(stills) + len(faces)
        if n > 1:
            row = layout.row()
            row.scale_y = 1.2
            row.operator("tachie.batch_edit", text=f"選択中の {n} 個をまとめて変える", icon="MODIFIER")
        if ids:
            preferences = context.preferences.addons[__package__].preferences
            row = layout.row(align=True)
            row.operator("tachie.assign_faces", text=f"AI で表情を割り当てる（{len(ids)} 行）", icon="SHADERFX")
            row.prop(preferences, "faceassign_target", text="")
        if n > 1 or ids:
            layout.separator()
        if strip is not None and strip.get("tachie_face"):
            layout.label(text=f"{strip['tachie_face']}の表情ストリップ", icon="SHADERFX")
            row = layout.row(align=True)
            row.label(text="表情")
            row.operator("tachie.pick_expression", text=strip.get("tachie_expression") or "（基本）").target = "FACE"
            layout.label(text="重なる範囲だけこの表情になります", icon="INFO")
            layout.operator("tachie.apply_faces", icon="FILE_REFRESH")
        elif strip is not None and strip.get("tachie_still"):
            layout.label(text=f"{strip['tachie_still']}の立ち絵（セリフなし）", icon="USER")
            row = layout.row(align=True)
            row.label(text="表情")
            row.operator("tachie.pick_expression", text=strip.get("tachie_expression") or "（基本）").target = "STILL"
            draw_pose(layout, strip)
            layout.operator("tachie.remake_still", icon="FILE_REFRESH")
        elif strip is None or not strip.get("tachie_id"):
            layout.label(text="セリフのストリップを選ぶと編集できます")
        else:
            sound = line_sound(line_strips(sed, strip["tachie_id"]))
            row = layout.row(align=True)
            row.label(text="キャラクター")
            row.operator("tachie.pick_line_character", text=strip.get("tachie_character", "?"), icon="USER")
            row = layout.row(align=True)
            row.label(text="スタイル")
            speaker = (sound or strip).get("tachie_speaker", "?")
            owner = next((c for c in seq_scene(context).tachie.characters
                          if c.name == strip.get("tachie_character")), None)
            name = owner.speaker_name if owner and owner.speaker == speaker and owner.speaker_name else f"ID {speaker}"
            row.operator("tachie.pick_speaker", text=name, icon="VIEWZOOM").target = "LINE"
            layout.prop(strip, '["tachie_text"]', text="")
            if sound is not None and "tachie_kana" in sound:
                split = layout.split(factor=0.18, align=True)
                split.label(text="発音")
                row = split.row(align=True)
                row.prop(sound, '["tachie_kana"]', text="")
                row.operator("tachie.preview_line", text="", icon="PLAY")
                row.operator("tachie.reset_kana", text="", icon="FILE_REFRESH")
                row = layout.row(align=True)
                edited = bool(sound.get("tachie_accent"))
                row.operator("tachie.edit_accent", text="抑揚を編集（編集済み）" if edited else "抑揚を編集",
                             icon="IPO_EASE_IN_OUT")
                if edited:
                    row.operator("tachie.clear_accent", text="", icon="X")
            strips = line_strips(sed, strip["tachie_id"])
            shape = line_layout_of(strips)
            row = layout.row(align=True)
            row.label(text="形")
            row.operator_menu_enum("tachie.pick_line_layout", "line_layout",
                                   text=next(n for k, n, _ in LINE_LAYOUTS if k == shape))
            if shape == "FULL":
                row = layout.row(align=True)
                row.label(text="表情")
                row.operator("tachie.pick_expression", text=strip.get("tachie_expression") or "（基本）").target = "LINE"
            col = layout.column(align=True)
            if sound is not None and "tachie_voice_speed_scale" in sound:
                for prop, (_, label, *_) in VOICE_PROPS.items():
                    col.prop(sound, f'["tachie_voice_{prop}"]', text=label)
                col = layout.column(align=True)
                col.prop(sound, "volume", text=AUDIO_PROPS["volume"][0])
                col.prop(sound, "pan", text=AUDIO_PROPS["pan"][0])
                for key in BAKED_AUDIO:
                    if f"tachie_{key}" in sound:
                        col.prop(sound, f'["tachie_{key}"]', text=AUDIO_PROPS[key][0])
                col = layout.column(align=True)
                col.operator("tachie.reset_line_voice", icon="LOOP_BACK")
            else:
                col.label(text="声はキャラクターの既定値（再生成すると編集できます）")
            draw_pose(layout, line_image(strips))
            draw_text_pose(layout, line_subtitle(strips))
            row = layout.row()
            row.scale_y = 1.2
            row.operator("tachie.regenerate", icon="FILE_REFRESH")
        layout.prop(seq_scene(context).tachie, "ripple")
        row = layout.row(align=True)
        row.operator("tachie.regenerate_all", text="全セリフを再生成").character_only = False
        row.operator("tachie.regenerate_all", text="このキャラだけ").character_only = True


class TACHIE_PT_dictionary(_Panel, bpy.types.Panel):
    bl_label = "ユーザー辞書"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        from . import operators

        layout = self.layout
        d = context.window_manager.tachie_dict
        col = layout.column(align=True)
        col.prop(d, "surface")
        col.prop(d, "pronunciation")
        col.prop(d, "accent_type")
        layout.operator("tachie.dict_add", icon="ADD")
        row = layout.row()
        row.label(text="登録済み" if operators._dict_words is not None else "既定のエンジン（アドオン設定）の辞書")
        row.operator("tachie.dict_refresh", text="", icon="FILE_REFRESH")
        if operators._dict_words:
            box = layout.box().column(align=True)
            for uuid, surface, pronunciation, accent in operators._dict_words:
                row = box.row(align=True)
                row.label(text=f"{surface}  {pronunciation}（{accent}）")
                row.operator("tachie.dict_remove", text="", icon="X").uuid = uuid
        elif operators._dict_words is not None:
            layout.label(text="登録された単語はありません")
        layout.label(text="置いてあるセリフには再生成で反映", icon="INFO")


class TACHIE_PT_character(_Panel, bpy.types.Panel):
    bl_label = "キャラクター設定"
    bl_parent_id = "TACHIE_PT_main"

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        row = layout.row(align=True)
        row.prop(ch, "folder")
        row.operator("tachie.change_material", text="", icon="FILE_REFRESH")
        if not ch.folder:
            layout.label(text="立ち絵の無いキャラクター（声と字幕だけ）", icon="SPEAKER")
        layout.prop(ch, "line_layout")
        layout.prop(ch, "engine")
        if ch.engine == "CUSTOM":
            layout.prop(ch, "engine_url")
        row = layout.row(align=True)
        row.prop(ch, "speaker")
        row.operator("tachie.pick_speaker", text=ch.speaker_name or "選ぶ", icon="VIEWZOOM")
        if ch.folder:
            layout.prop(ch, "mouth")
            layout.prop(ch, "show_idle")


class _WithTachie:
    """立ち絵のあるキャラクター（素材フォルダがある）のときだけ出すパネル。"""

    @classmethod
    def poll(cls, context):
        ch = seq_scene(context).tachie.character()
        return ch is not None and bool(ch.folder)


class TACHIE_PT_motion(_WithTachie, _Panel, bpy.types.Panel):
    bl_label = "動き"
    bl_parent_id = "TACHIE_PT_character"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        col = layout.column(align=True)
        col.prop(ch, "talk_bounce")
        col.prop(ch, "talk_sway")
        col.prop(ch, "talk_period")
        col = layout.column(align=True)
        col.prop(ch, "enter_effect")
        col.prop(ch, "exit_effect")
        col.prop(ch, "effect_seconds")
        layout.label(text="置いてある立ち絵にすぐ反映します", icon="INFO")


def draw_pose(layout, strip) -> None:
    """セリフごと（立ち絵ごと）の位置・大きさ。"""
    from .operators import POSE_PROPS

    if strip is None or "tachie_scale" not in strip:
        return
    col = layout.column(align=True)
    for key, (label, *_) in POSE_PROPS.items():
        col.prop(strip, f'["{key}"]', text=label)


def draw_text_pose(layout, strip) -> None:
    """セリフごとの字幕（とキャラ名）の位置。"""
    from .operators import TEXT_POSE_PROPS

    if strip is None or "tachie_sub_x" not in strip:
        return
    col = layout.column(align=True)
    for key, (label, _) in TEXT_POSE_PROPS.items():
        col.prop(strip, f'["{key}"]', text=label)


class TACHIE_PT_voice(_Panel, bpy.types.Panel):
    bl_label = "声"
    bl_parent_id = "TACHIE_PT_character"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        col = layout.column(align=True)
        for prop in VOICE_PROPS:
            col.prop(ch, prop)
        col = layout.column(align=True)
        for key in AUDIO_PROPS:
            col.prop(ch, f"audio_{key}")
        layout.label(text="新しく置くセリフの既定値", icon="INFO")
        op = layout.operator("tachie.regenerate_all", text="置いたセリフにも当て直す", icon="FILE_REFRESH")
        op.character_only, op.use_character_voice = True, True


# シンプル立ち絵は画像 1 枚: 体 = 口を閉じた画像、口 = セリフ中（口を開いた）画像
SIMPLE_LABELS = {"体": "立ち絵（口を閉じる）", "口": "セリフ中（口を開く）"}


def material_of(ch):
    try:
        return load_material(ch.folder) if ch.folder else None
    except (ValueError, FileNotFoundError, KeyError, OSError):
        return None


def draw_psd_layers(layout, ch, preset: str = "") -> None:
    """PSD 立ち絵の表情: * のフォルダは 1 つ選ぶ、その他のレイヤーは表示・非表示（プリセットは基本との差だけ持つ）。"""
    from .operators import item_layers, psd_layer_state
    from .tachie_studio.tachie.psd import is_on, is_shown, radio_groups, toggles

    try:
        doc, _, state = psd_layer_state(ch, preset)
    except (ValueError, FileNotFoundError, KeyError, OSError) as e:
        layout.label(text=str(e), icon="ERROR")
        return
    own = item_layers(find_preset(ch, preset)) if preset else {}
    character = load_material(ch.folder)
    anims = character.animations
    if anims.get("mouth") or anims.get("eye"):
        layout.label(text=f"口パク {len(anims.get('mouth', []))} 組・まばたき {len(anims.get('eye', []))} 組", icon="CHECKMARK")
    else:
        layout.label(text="口パク・まばたきの設定（<PSD 名>-ymm.json）がありません", icon="ERROR")
    col = layout.column(align=True)
    for group in radio_groups(doc):
        chosen = next((c for c in group.children if c.radio and is_on(c, state)), None)
        changed = any(c.path() in own for c in group.children if c.radio)
        row = col.row(align=True)
        row.active = is_shown(group, state)  # 選ばれていないフォルダの中は効かない
        depth = group.path().count("/") + (1 if group.parent is not None else 0)
        row.label(text="　" * depth + (group.label if group.parent is not None else "（いちばん外）"))
        text = chosen.label if chosen else "（なし）"
        if preset and not changed:
            text = f"（基本のまま: {text}）"
        op = row.operator("tachie.psd_choose", text=text)
        op.group, op.preset = group.path(), preset
    col = layout.column(align=True)
    for node in toggles(doc):
        row = col.row(align=True)
        row.alignment = "LEFT"
        row.active = node.parent is None or node.parent.parent is None or is_shown(node.parent, state)
        depth = node.path().count("/")
        on = is_on(node, state)
        op = row.operator("tachie.psd_toggle", text="　" * depth + node.label,
                          icon="CHECKBOX_HLT" if on else "CHECKBOX_DEHLT", emboss=False)
        op.path, op.preset, op.mode = node.path(), preset, "TOGGLE"
        if preset and node.path() in own:
            op = row.operator("tachie.psd_toggle", text="", icon="LOOP_BACK")
            op.path, op.preset, op.mode = node.path(), preset, "RESET"


def find_preset(ch, name: str):
    return next((e for e in ch.expressions if e.name == name), None)


def part_rows(ch):
    """表情の欄に出す (カテゴリ, [プロパティ], 表示名) の並び。シンプル立ち絵は 2 つだけ、PSD は無し。"""
    material = material_of(ch)
    if material is not None and material.is_psd:
        return []
    simple = material is not None and material.simple
    if simple:
        return [(c, PART_PROPS[c], SIMPLE_LABELS[c]) for c in ("体", "口")]
    return [(c, props, c) for c, props in PART_PROPS.items()]


class TACHIE_PT_expression(_WithTachie, _Panel, bpy.types.Panel):
    bl_label = "表情"
    bl_parent_id = "TACHIE_PT_character"

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        material = material_of(ch)
        if material is not None and material.is_psd:
            draw_psd_layers(layout, ch)
            layout.label(text="変えたら「全セリフを再生成」で反映", icon="INFO")
            return
        col = layout.column(align=True)
        for category, props, title in part_rows(ch):
            for i, prop in enumerate(props):
                row = col.row(align=True)
                label = title if len(props) == 1 else f"{category}{i + 1}"
                row.label(text=label)
                value = getattr(ch, prop)
                empty = "（なし）" if len(props) > 1 else "（既定）"
                op = row.operator("tachie.pick_part", text=value or empty)
                op.prop = prop
        layout.label(text="変えたら「全セリフを再生成」で反映", icon="INFO")


class TACHIE_PT_presets(_WithTachie, _Panel, bpy.types.Panel):
    bl_label = "表情プリセット"
    bl_parent_id = "TACHIE_PT_character"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        row = layout.row()
        row.template_list("TACHIE_UL_expressions", "", ch, "expressions", ch, "active_expression", rows=3)
        col = row.column(align=True)
        col.operator("tachie.expression_add", text="", icon="ADD")
        col.operator("tachie.expression_remove", text="", icon="REMOVE")
        preset = ch.expression()
        if preset is None:
            layout.label(text="台本では「キャラ名(表情): セリフ」で使えます", icon="INFO")
            return
        material = material_of(ch)
        if material is not None and material.is_psd:
            draw_psd_layers(layout, ch, preset.name)
            return
        col = layout.column(align=True)
        for category, props, title in part_rows(ch):
            for i, prop in enumerate(props):
                row = col.row(align=True)
                row.label(text=title if len(props) == 1 else f"{category}{i + 1}")
                op = row.operator("tachie.pick_part", text=getattr(preset, prop) or "（基本のまま）")
                op.prop = prop
                op.preset = preset.name


def draw_ymmp_pending(layout, s) -> None:
    """見つからない素材・ファイルがあって読み込みを待っている YMM4 のプロジェクト。ここで場所を指定する
    （読み込みのファイル選択の中からはフォルダを選べないので、パネルで）。"""
    from pathlib import Path

    from .ymmp_import import _abs, material_problem

    box = layout.box()
    box.label(text=f"{Path(s.ymmp_pending).name}: 見つからないものを指定してください", icon="ERROR")
    if len(s.ymmp_pending_chars):
        box.label(text="キャラクターの素材フォルダ:")
        for c in s.ymmp_pending_chars:
            problem = material_problem(_abs(c.folder))
            row = box.row(align=True)
            row.label(text=c.name, icon="ERROR" if problem else "CHECKMARK")
            row.prop(c, "folder", text="")
            if problem and c.folder:
                box.label(text=f"    {problem}")
        box.prop(s, "ymmp_material_root", text="探す場所")
    if s.ymmp_import_media and s.ymmp_pending_files:
        missing = [n for n in s.ymmp_pending_missing.split("\n") if n]
        box.label(text=f"画像・動画など: {s.ymmp_pending_files - len(missing)} / {s.ymmp_pending_files} 個見つかりました",
                  icon="ERROR" if missing else "CHECKMARK")
        box.prop(s, "ymmp_media_root", text="探す場所")
        if s.ymmp_pending_wide:
            box.label(text="    探す場所が広すぎるので途中までしか探していません", icon="INFO")
        col = box.column(align=True)
        for n in missing[:5]:
            col.label(text=f"  {n}")
        if len(missing) > 5:
            col.label(text=f"  ほか {len(missing) - 5} 個")
    box.prop(s, "ymmp_match_video")
    box.prop(s, "ymmp_import_media")
    row = box.row(align=True)
    row.scale_y = 1.2
    row.operator("tachie.import_ymmp", text="読み込む", icon="IMPORT").pending = True
    row.operator("tachie.import_ymmp_cancel", icon="CANCEL")
    box.label(text="見つからないキャラ・ファイルは飛ばして読み込みます", icon="INFO")


class TACHIE_PT_script(_Panel, bpy.types.Panel):
    bl_label = "台本の流し込み"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = seq_scene(context).tachie
        layout.label(text="1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」")
        layout.label(text="YMM4 の台本（キャラ名「セリフ」、CSV・TSV）も読めます")
        layout.label(text="発音はセリフの最後に {キョ'ウワ/イ'イ/テ'ンキ}")
        layout.prop(s, "line_gap")
        row = layout.row(align=True)
        row.prop(s, "script", text="")
        sub = row.row(align=True)
        sub.enabled = s.script is not None
        sub.operator("tachie.import_script", text="流し込む", icon="PLAY").use_text = True
        layout.operator("tachie.import_script", text="ファイルから流し込む", icon="FILE_TEXT").use_text = False
        layout.operator("tachie.import_ymmp", icon="IMPORT")
        if s.ymmp_pending:
            draw_ymmp_pending(layout, s)


class TACHIE_PT_placement(_WithTachie, _Panel, bpy.types.Panel):
    bl_label = "配置"
    bl_parent_id = "TACHIE_PT_character"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(ch, "position_x")
        row.prop(ch, "flip_x", text="", icon="MOD_MIRROR")
        col.prop(ch, "position_y")
        col.prop(ch, "height")
        col.prop(ch, "channel")


class TACHIE_PT_subtitle(_Panel, bpy.types.Panel):
    bl_label = "字幕"
    bl_parent_id = "TACHIE_PT_character"

    def draw(self, context):
        import os
        import unicodedata

        layout = self.layout
        ch = seq_scene(context).tachie.character()
        row = layout.row(align=True)
        row.label(text="フォント")
        name = (unicodedata.normalize("NFC", os.path.splitext(os.path.basename(ch.font_path))[0])
                if ch.font_path else "（Blender の既定）")
        row.operator("tachie.pick_font", text=name, icon="FILE_FONT")
        col = layout.column(align=True)
        col.prop(ch, "font_size")
        col.prop(ch, "subtitle_y")
        row = layout.row(align=True)
        row.prop(ch, "text_color", text="文字")
        row.prop(ch, "outline_color", text="縁")
        layout.prop(ch, "outline_width")
        layout.prop(ch, "line_spacing")
        col = layout.column(align=True)
        col.prop(ch, "subtitle_box")
        sub = col.column(align=True)
        sub.active = ch.subtitle_box
        sub.prop(ch, "box_color")
        sub.prop(ch, "box_margin")
        sub.prop(ch, "box_roundness")
        layout.prop(ch, "show_name")
        layout.prop(ch, "subtitle_fade")
        layout.prop(ch, "font_path", text="")


class TACHIE_PT_headline(_Panel, bpy.types.Panel):
    bl_label = "見出し"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = seq_scene(context).tachie
        layout.prop(s, "headline_text", text="")
        row = layout.row(align=True)
        row.operator("tachie.add_headline", icon="FONT_DATA")
        row.prop(s, "headline_seconds")
        col = layout.column(align=True)
        col.prop(s, "headline_size")
        col.prop(s, "headline_y")
        row = layout.row(align=True)
        row.prop(s, "headline_color", text="文字")
        row.prop(s, "headline_outline_color", text="縁")
        row = layout.row(align=True)
        row.prop(s, "headline_box")
        row.prop(s, "headline_box_color", text="")
        layout.prop(s, "headline_font_path", text="")
        layout.label(text="スタイルは置いてある見出しにもすぐ反映します", icon="INFO")


class TACHIE_PT_bgm(_Panel, bpy.types.Panel):
    bl_label = "BGM"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        from .ducking import bgm_strips

        layout = self.layout
        scene = seq_scene(context)
        s = scene.tachie
        layout.operator("tachie.set_bgm", icon="SOUND")
        col = layout.column(align=True)
        col.prop(s, "duck_volume")
        col.prop(s, "duck_fade")
        bgms = bgm_strips(scene.sequence_editor)
        for strip in bgms:
            row = layout.row(align=True)
            row.prop(strip, '["tachie_bgm_volume"]', text=strip.name)
            row.operator("tachie.unset_bgm", text="", icon="X").strip = strip.name
        if bgms:
            layout.label(text="BGM の音量はここで変えます", icon="INFO")


class TACHIE_PT_sound_effects(_Panel, bpy.types.Panel):
    bl_label = "効果音"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = seq_scene(context).tachie
        effects = prefs(context).sound_effects
        layout.prop(s, "se_at_lines")
        grid = layout.grid_flow(columns=2, even_columns=True, align=True)
        for i, se in enumerate(effects):
            grid.operator("tachie.add_sound_effect", text=se.name, icon="PLAY_SOUND").index = i
        if not effects:
            layout.label(text="効果音を登録してください（アドオン設定でも編集できます）")
        layout.operator("tachie.register_sound_effect", icon="ADD")


class TACHIE_PT_templates(_Panel, bpy.types.Panel):
    bl_label = "テンプレート"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        from .templates import draw_templates

        layout = self.layout
        layout.label(text="再生位置に置きます（名前の変更・削除はアドオン設定）")
        draw_templates(layout.column(align=True), prefs(context))
        layout.operator("tachie.register_template", icon="ADD")
        layout.operator("tachie.import_ymm4_templates", icon="IMPORT")


class TACHIE_PT_export(_Panel, bpy.types.Panel):
    bl_label = "書き出し"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        r = seq_scene(context).render
        fps = r.fps / r.fps_base
        video = r.image_settings.file_format == "FFMPEG"
        summary = f"{r.resolution_x * r.resolution_percentage // 100}×{r.resolution_y * r.resolution_percentage // 100}" \
                  f" / {fps:g}fps / " + (f"{r.ffmpeg.format}（{r.ffmpeg.codec}）" if video else r.image_settings.file_format)
        layout.label(text=summary, icon="OUTPUT")
        layout.operator_menu_enum("tachie.render_preset", "preset", text="書き出し設定を選ぶ", icon="PRESET")
        row = layout.row()
        row.scale_y = 1.2
        op = row.operator("render.render", text="動画を書き出す", icon="RENDER_ANIMATION")
        op.animation = True
        # シーケンサーに映っているシーン（設定を表示しているもの）を書き出す。ウィンドウのシーンと違うことがある
        op.use_sequencer_scene = getattr(context, "sequencer_scene", None) is not None
        layout.operator("tachie.export_subtitles", text="字幕を書き出す（SRT / WebVTT）", icon="TEXT")


class TACHIE_PT_channels(_Panel, bpy.types.Panel):
    bl_label = "チャンネル"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = seq_scene(context).tachie
        row = self.layout.row(align=True)
        row.prop(s, "subtitle_channel")
        row.prop(s, "audio_channel")
        row = self.layout.row(align=True)
        row.prop(s, "name_channel")
        row.prop(s, "headline_channel")
        self.layout.prop(s, "se_channel")
        self.layout.label(text="立ち絵のチャンネルはキャラクターごとに設定")


_classes = (
    TACHIE_UL_characters,
    TACHIE_UL_expressions,
    TACHIE_MT_characters,
    TACHIE_PT_main,
    TACHIE_PT_line,
    TACHIE_PT_script,
    TACHIE_PT_dictionary,
    TACHIE_PT_character,
    TACHIE_PT_voice,
    TACHIE_PT_motion,
    TACHIE_PT_expression,
    TACHIE_PT_presets,
    TACHIE_PT_placement,
    TACHIE_PT_subtitle,
    TACHIE_PT_headline,
    TACHIE_PT_bgm,
    TACHIE_PT_sound_effects,
    TACHIE_PT_templates,
    TACHIE_PT_export,
    TACHIE_PT_channels,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
