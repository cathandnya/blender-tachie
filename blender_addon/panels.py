import bpy

from .operators import output_dir, seq_scene
from .properties import PART_PROPS


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
        self.layout.operator("tachie.import_characters", icon="IMPORT")
        self.layout.operator("tachie.export_characters", icon="EXPORT")
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
        row = layout.row(align=True)
        row.enabled = ch is not None
        row.label(text="表情")
        row.operator("tachie.pick_expression", text=s.expression or "（基本）").target = "ADD"
        row = layout.row()
        row.scale_y = 1.4
        row.enabled = ch is not None
        row.operator("tachie.add_line", text=f"{ch.name}のセリフを追加" if ch else "セリフを追加", icon="ADD")
        row = layout.row(align=True)
        row.enabled = ch is not None
        row.operator("tachie.add_still", text="立ち絵だけを置く", icon="IMAGE_DATA")
        row.prop(s, "still_seconds")
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
        if strip is not None and strip.get("tachie_still"):
            layout.label(text=f"{strip['tachie_still']}の立ち絵（セリフなし）", icon="USER")
            row = layout.row(align=True)
            row.label(text="表情")
            row.operator("tachie.pick_expression", text=strip.get("tachie_expression") or "（基本）").target = "STILL"
            layout.operator("tachie.remake_still", icon="FILE_REFRESH")
        elif strip is None or not strip.get("tachie_id"):
            layout.label(text="セリフのストリップを選ぶと編集できます")
        else:
            layout.label(text=f"{strip.get('tachie_character', '?')}（話者 {strip.get('tachie_speaker', '?')}）",
                         icon="USER")
            layout.prop(strip, '["tachie_text"]', text="")
            row = layout.row(align=True)
            row.label(text="表情")
            row.operator("tachie.pick_expression", text=strip.get("tachie_expression") or "（基本）").target = "LINE"
            row = layout.row()
            row.scale_y = 1.2
            row.operator("tachie.regenerate", icon="FILE_REFRESH")
        layout.prop(seq_scene(context).tachie, "ripple")
        row = layout.row(align=True)
        row.operator("tachie.regenerate_all", text="全セリフを再生成").character_only = False
        row.operator("tachie.regenerate_all", text="このキャラだけ").character_only = True


class TACHIE_PT_character(_Panel, bpy.types.Panel):
    bl_label = "キャラクター設定"
    bl_parent_id = "TACHIE_PT_main"

    @classmethod
    def poll(cls, context):
        return seq_scene(context).tachie.character() is not None

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        layout.prop(ch, "folder")
        row = layout.row(align=True)
        row.prop(ch, "speaker")
        row.operator("tachie.pick_speaker", text=ch.speaker_name or "選ぶ", icon="VIEWZOOM")
        layout.prop(ch, "mouth")
        layout.prop(ch, "show_idle")


class TACHIE_PT_expression(_Panel, bpy.types.Panel):
    bl_label = "表情"
    bl_parent_id = "TACHIE_PT_character"

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        col = layout.column(align=True)
        for category, props in PART_PROPS.items():
            for i, prop in enumerate(props):
                row = col.row(align=True)
                label = category if len(props) == 1 else f"{category}{i + 1}"
                row.label(text=label)
                value = getattr(ch, prop)
                empty = "（なし）" if len(props) > 1 else "（既定）"
                op = row.operator("tachie.pick_part", text=value or empty)
                op.prop = prop
        layout.label(text="変えたら「全セリフを再生成」で反映", icon="INFO")


class TACHIE_PT_presets(_Panel, bpy.types.Panel):
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
        col = layout.column(align=True)
        for category, props in PART_PROPS.items():
            for i, prop in enumerate(props):
                row = col.row(align=True)
                row.label(text=category if len(props) == 1 else f"{category}{i + 1}")
                op = row.operator("tachie.pick_part", text=getattr(preset, prop) or "（基本のまま）")
                op.prop = prop
                op.preset = preset.name


class TACHIE_PT_script(_Panel, bpy.types.Panel):
    bl_label = "台本の流し込み"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = seq_scene(context).tachie
        layout.label(text="1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」")
        layout.prop(s, "line_gap")
        row = layout.row(align=True)
        row.prop(s, "script", text="")
        sub = row.row(align=True)
        sub.enabled = s.script is not None
        sub.operator("tachie.import_script", text="流し込む", icon="PLAY").use_text = True
        layout.operator("tachie.import_script", text="ファイルから流し込む", icon="FILE_TEXT").use_text = False


class TACHIE_PT_placement(_Panel, bpy.types.Panel):
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
        layout.prop(ch, "font_path", text="")


class TACHIE_PT_channels(_Panel, bpy.types.Panel):
    bl_label = "チャンネル"
    bl_parent_id = "TACHIE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = seq_scene(context).tachie
        row = self.layout.row(align=True)
        row.prop(s, "subtitle_channel")
        row.prop(s, "audio_channel")
        self.layout.label(text="立ち絵のチャンネルはキャラクターごとに設定")


_classes = (
    TACHIE_UL_characters,
    TACHIE_UL_expressions,
    TACHIE_MT_characters,
    TACHIE_PT_main,
    TACHIE_PT_line,
    TACHIE_PT_script,
    TACHIE_PT_character,
    TACHIE_PT_expression,
    TACHIE_PT_presets,
    TACHIE_PT_placement,
    TACHIE_PT_subtitle,
    TACHIE_PT_channels,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
