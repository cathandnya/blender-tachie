import bpy

from .tachie_studio.subtitle import breaks

from .operators import (STILL_SETTINGS, active_selected, item_strip, line_layout_of, line_sound, line_strips,
                        line_subtitle, load_material, output_dir, prefs, selected_targets, seq_scene)
from .properties import AUDIO_PROPS, BAKED_AUDIO, LINE_LAYOUTS, PART_PROPS, VOICE_PROPS, still_flip


class _Panel:
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "立ち絵"


class _StripPanel:
    """選んでいるストリップの設定のパネル。Blender のストリップの設定（音量・変形・テキストなど）と同じく、プロパティ
    エディターの「ストリップ」タブに置く（サイドバーは作る道具とキャラクター設定）。"""
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "strip"


class TACHIE_UL_characters(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon="USER")
        row.label(text=item.speaker_name or f"ID {item.speaker}")


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
        ch = seq_scene(context).tachie.character()
        col = self.layout.column()
        col.enabled = ch is None or bool(ch.folder)  # 立ち絵の無いキャラには素材フォルダが無い
        col.operator("tachie.save_defaults", text="素材フォルダに初期値を書き出す（tachie.toml）",
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
        col.separator()
        col.operator("tachie.character_move", text="", icon="TRIA_UP").direction = "UP"
        col.operator("tachie.character_move", text="", icon="TRIA_DOWN").direction = "DOWN"

        ch = s.character()
        tachie = ch is not None and bool(ch.folder)  # 立ち絵のあるキャラ
        row = layout.row(align=True)
        row.enabled = tachie
        draw_expression_row(row, ch, "ADD", s.expression or "（基本）", [_preset_look(ch, s.expression)])
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
        if not bpy.data.filepath and not context.preferences.addons[__package__].preferences.output_dir:
            layout.label(text=f"未保存のため {output_dir(context)} に書き出します", icon="INFO")


class TACHIE_PT_line(_Panel, bpy.types.Panel):
    """選んでいるストリップをまとめて扱う道具（1 本の設定はプロパティの「ストリップ」タブ、`TACHIE_PT_strip`）。"""
    bl_label = "選んだストリップ"
    bl_parent_id = "TACHIE_PT_main"

    def draw(self, context):
        layout = self.layout
        sed = seq_scene(context).sequence_editor
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
            layout.operator("tachie.line_faces", text=f"表情を表情ストリップにする（{len(ids)} 行）", icon="SEQ_STRIP_DUPLICATE")
        active = active_selected(context)
        if active is not None and item_strip(active):
            layout.label(text="このストリップの設定はプロパティの「ストリップ」タブ", icon="PROPERTIES")
        elif n:  # 選んでいるがアクティブなストリップがアイテムでない（ストリップタブには出ない）
            layout.label(text="1 本をクリックすると、プロパティの「ストリップ」タブで編集できます", icon="INFO")
        else:
            layout.label(text="セリフ・立ち絵・表情ストリップを選ぶと、プロパティの「ストリップ」タブで編集できます",
                         icon="INFO")
        layout.separator()
        layout.prop(seq_scene(context).tachie, "ripple")
        row = layout.row(align=True)
        row.operator("tachie.regenerate_all", text="全セリフを再生成").character_only = False
        row.operator("tachie.regenerate_all", text="このキャラだけ").character_only = True


class TACHIE_PT_strip(_StripPanel, bpy.types.Panel):
    """選んでいるセリフ・立ち絵・表情ストリップの設定（テキスト・表情・パーツ・声・発音・抑揚・字幕・立ち絵の位置など）。"""
    bl_label = "立ち絵アドオン"
    # 見出しの無いパネルは、ストリップタブの Blender のパネル（変形・音量など）より上、ストリップ名のすぐ下に並ぶ
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context):
        # Blender は選択を外してもアクティブなストリップを覚えているので、選ばれているときだけ（前の値を出したままにしない）
        strip = active_selected(context)
        return strip is not None and item_strip(strip)

    def draw(self, context):
        layout = self.layout
        sed = seq_scene(context).sequence_editor
        strip = active_selected(context)
        if strip is not None and strip.get("tachie_face"):
            layout.label(text=f"{strip['tachie_face']}の表情ストリップ", icon="SHADERFX")
            draw_item_expression(layout, context, strip, "FACE")
            layout.label(text="重なる範囲だけこの表情になります", icon="INFO")
            layout.operator("tachie.apply_faces", icon="FILE_REFRESH")
        elif strip is not None and strip.get("tachie_still"):
            layout.label(text=f"{strip['tachie_still']}の立ち絵", icon="USER")
            draw_item_expression(layout, context, strip, "STILL")
            owner = seq_scene(context).tachie.characters.get(strip["tachie_still"])
            flipped = bool(owner and still_flip(strip, owner))
            row = layout.row(align=True)
            row.label(text="左右反転")
            row.operator("tachie.flip_still", text="反転" if flipped else "反転しない", icon="MOD_MIRROR",
                         depress=flipped)
            if "tachie_flip" not in strip:
                row.label(text="（キャラの設定）")
            col = layout.column(align=True)
            for prop, label, *_ in STILL_SETTINGS.values():
                if prop in strip:
                    col.prop(strip, f'["{prop}"]', text=label)
            if strip.get("tachie_jump"):
                layout.label(text="跳ねながら登場退場（YMM4 の映像エフェクト）", icon="ANIM")
            draw_pose(layout, strip)
            layout.operator("tachie.remake_still", icon="FILE_REFRESH")
        else:
            layout.label(text=f"{strip.get('tachie_character', '?')}のセリフ", icon="SPEAKER")
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
            # テキストは折り返して見せ、押すと「セリフを編集」のダイアログ（入力欄は 1 行しか作れない）
            draw_wrapped(layout, context, breaks(strip.get("tachie_text", "")), "tachie.edit_line_text")
            if sound is not None and "tachie_kana" in sound:
                row = layout.row(align=True)
                row.label(text="発音")
                row.operator("tachie.edit_kana", text="発音を編集", icon="GREASEPENCIL")
                row.operator("tachie.preview_line", text="", icon="PLAY")
                row.operator("tachie.reset_kana", text="", icon="FILE_REFRESH")
                from .operators import input_text

                # テキストを置いたときから書き換えていれば、前の発音は使わない（再生成で新しいテキストの自動の読み）
                changed = input_text(strip.get("tachie_text", "")) != input_text(sound.get("tachie_placed_text",
                                                                                           strip.get("tachie_text", "")))
                draw_kana(layout, context, "" if changed else sound.get("tachie_kana", ""),
                          empty="（テキストを変えたので、再生成すると自動の読み）" if changed else "（自動の読み）")
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
                                   text=next(n for k, n, *_ in LINE_LAYOUTS if k == shape))
            if owner is not None and owner.folder:  # 重なる立ち絵がセリフの間だけこの表情になる
                draw_item_expression(layout, context, sound or strip, "LINE")
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
            draw_text_pose(layout, line_subtitle(strips), seq_scene(context).tachie)
            row = layout.row(align=True)
            row.scale_y = 1.2
            row.operator("tachie.regenerate", icon="FILE_REFRESH")
            row.prop(seq_scene(context).tachie, "ripple", text="後ろをずらす")


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


def draw_text_pose(layout, strip, settings) -> None:
    """セリフごとの字幕（とキャラ名）の位置（px）・文字の大きさ・折り返し幅（0 はキャラの設定）。"""
    from .operators import TEXT_STYLE_PROPS

    if strip is None or "tachie_sub_x" not in strip:
        return
    col = layout.column(align=True)
    if settings.characters.get(strip.get("tachie_character", "")) is not None:
        col.prop(settings, "line_sub_pos_x")
        col.prop(settings, "line_sub_pos_y")
    else:  # 消したキャラのセリフは px に換算できないので、ずらす量（割合）をそのまま見せる
        from .operators import TEXT_POSE_PROPS

        for key, (label, _) in TEXT_POSE_PROPS.items():
            col.prop(strip, f'["{key}"]', text=label)
    if "tachie_sub_size" in strip:
        col = layout.column(align=True)
        for key, (label, *_) in TEXT_STYLE_PROPS.items():
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


def draw_kana(layout, context, kana: str, max_lines: int = 3, empty: str = "（自動の読み）") -> None:
    """発音を折り返して見せる（句の区切りで改行）。押すと「発音を編集」のダイアログを開く。"""
    draw_wrapped(layout, context, kana, "tachie.edit_kana", max_lines, empty, breaks_at="/、")


def draw_wrapped(layout, context, text: str, operator: str, max_lines: int = 3, empty: str = "",
                 breaks_at: str = "") -> None:
    """テキストを折り返して見せる（改行と、幅に入りきらないところで。`breaks_at` の文字の後ろで折り返しやすい）。
    各行は押すと `operator`（編集のダイアログ）を開く枠の無いボタン。表示だけなので複数行にできる。"""
    width = context.region.width if context.region else 300
    per_line = max(8, int(width / (14 * context.preferences.system.ui_scale)) - 2)  # 全角の文字数（だいたい）
    lines = []
    for paragraph in text.split("\n") if text else []:
        current = ""
        for ch in paragraph:
            current += ch
            if (ch in breaks_at and len(current) >= per_line * 0.6) or len(current) >= per_line:
                lines.append(current)
                current = ""
        if current or not paragraph:  # 空の行（字幕の空行）も 1 行として見せる
            lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + ["".join(lines[max_lines - 1:])]
    box = layout.box().column(align=True)
    box.scale_y = 0.8
    for line in lines or [empty]:
        row = box.row()
        row.alignment = "LEFT"  # 文字の幅のボタンにして左に寄せる（表示と同じ見た目）
        row.operator(operator, text=line or " ", emboss=False)


def _preset_look(ch, name: str) -> dict | None:
    """表情プリセットのパーツ（見本用。無いプリセット・立ち絵の無いキャラは None）。"""
    from .operators import preset_parts

    try:
        return preset_parts(ch, name) if ch is not None else None
    except ValueError:
        return None


def draw_expression_row(row, ch, target: str, text: str, layers: list | None) -> None:
    """表情の欄（ラベル・表情の見本・選ぶボタン tachie.pick_expression）。`layers` は見本の表情（基本に重ねる
    パーツの並び。None なら見本なし）。"""
    from .operators import draw_swatch, expression_icon

    row.label(text="表情")
    icon = expression_icon(ch, *layers) if layers is not None and None not in layers else 0
    draw_swatch(row.row(align=True), icon).operator("tachie.pick_expression", text=text).target = target


# アイテムの種類ごとの (空の表情の表示, パーツの欄の見出し)
ITEM_TEXTS = {"STILL": ("（基本）", "パーツ（この立ち絵だけ）"), "LINE": ("（立ち絵のまま）", "パーツ（このセリフだけ）"),
              "FACE": ("（立ち絵のまま）", "パーツ（この表情ストリップだけ）")}


def draw_item_expression(layout, context, strip, item: str) -> None:
    """選択中の立ち絵・セリフ・表情ストリップの表情の欄と、パーツの欄（YMM4 と同じく、プリセットを選ぶとパーツが
    それになり、パーツを 1 つずつ変えられる）。セリフ・表情ストリップのパーツは重なる立ち絵の上に重ねる。"""
    from .operators import expression_label, item_character, strip_parts

    settings = seq_scene(context).tachie
    ch = settings.characters.get(item_character(strip))
    empty, title = ITEM_TEXTS[item]
    parts = strip_parts(strip, ch) if ch is not None else {}
    label = expression_label(ch, strip, empty)
    draw_expression_row(layout.row(align=True), ch, item, label,
                        [parts] if item == "STILL" or parts else None)
    changed = label.endswith("*") or label == "（カスタム）"
    row = layout.row(align=True)
    row.prop(settings, "show_still_parts", text=title + ("：変更あり" if changed else ""),
             icon="TRIA_DOWN" if settings.show_still_parts else "TRIA_RIGHT", emboss=False)
    if changed:
        row.operator("tachie.clear_item_parts", text="", icon="LOOP_BACK")
    if not settings.show_still_parts or ch is None:
        return
    rows = part_rows(ch)
    if not rows:
        layout.label(text="PSD 立ち絵は表情プリセットで変えてください", icon="INFO")
        return
    material = material_of(ch)
    col = layout.column(align=True)
    keep = "（基本のまま）" if item == "STILL" else "（立ち絵のまま）"
    for category, props, row_title in rows:
        for i, prop in enumerate(props):
            value = parts.get(category)
            if isinstance(value, list):
                value = value[i] if i < len(value) else ""
            elif len(props) > 1:
                value = ""
            op = draw_part_row(col, material, category, row_title if len(props) == 1 else f"{category}{i + 1}",
                               value or "", value or keep, depress=bool(value))
            op.prop, op.item = prop, item


def draw_part_row(col, material, category: str, label: str, value: str, text: str, depress: bool = False):
    """パーツの欄の 1 行（ラベル・パーツの見本・選ぶボタン）。ボタン（tachie.pick_part）を返す。"""
    from .operators import draw_swatch, part_icon

    row = col.row(align=True)
    row.label(text=label)
    part = material.parts.get(category, {}).get(value) if material is not None and value else None
    icon = part_icon(part.path) if part is not None else 0
    return draw_swatch(row.row(align=True), icon).operator("tachie.pick_part", text=text, depress=depress)


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
                label = title if len(props) == 1 else f"{category}{i + 1}"
                value = getattr(ch, prop)
                empty = "（なし）" if len(props) > 1 else "（既定）"
                op = draw_part_row(col, material, category, label, value, value or empty)
                op.prop = prop
        layout.label(text="変えたら「全セリフを再生成」で反映", icon="INFO")


class TACHIE_PT_presets(_WithTachie, _Panel, bpy.types.Panel):
    bl_label = "表情プリセット"
    bl_parent_id = "TACHIE_PT_character"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        ch = seq_scene(context).tachie.character()
        from .operators import draw_swatch, expression_icon

        # 一覧はパーツの欄と同じ大きさの見本を名前の左に並べる（UIList の行はアイコンが小さい）
        row = layout.row()
        col = row.column(align=True)
        if not ch.expressions:
            col.label(text="（表情プリセットはありません）")
        for i, e in enumerate(ch.expressions):
            draw_swatch(col.row(align=True), expression_icon(ch, _preset_look(ch, e.name))).operator(
                "tachie.expression_select", text=e.name, depress=i == ch.active_expression).index = i
        col = row.column(align=True)
        col.operator("tachie.expression_add", text="", icon="ADD")
        col.operator("tachie.expression_remove", text="", icon="REMOVE")
        preset = ch.expression()
        if preset is None:
            layout.label(text="台本では「キャラ名(表情): セリフ」で使えます", icon="INFO")
            return
        layout.prop(preset, "name", text="名前")
        material = material_of(ch)
        if material is not None and material.is_psd:
            draw_psd_layers(layout, ch, preset.name)
            return
        col = layout.column(align=True)
        for category, props, title in part_rows(ch):
            for i, prop in enumerate(props):
                value = getattr(preset, prop)
                op = draw_part_row(col, material, category, title if len(props) == 1 else f"{category}{i + 1}",
                                   value, value or "（基本のまま）")
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
        draw_char_lanes(self.layout, seq_scene(context), ch.name)


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
        col.prop(ch, "subtitle_width")
        col = layout.column(align=True)
        col.prop(ch, "subtitle_pos_x")
        col.prop(ch, "subtitle_pos_y")
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
        # タイムライン（解像度・fps）と書き出し（大きさの % と形式）は別。書き出し設定はタイムラインを変えない
        layout.label(text=f"タイムライン: {r.resolution_x}×{r.resolution_y} / {fps:g}fps", icon="SEQUENCE")
        layout.operator_menu_enum("tachie.timeline_preset", "preset", text="タイムラインの解像度・fps", icon="PRESET")
        summary = f"書き出し: {r.resolution_x * r.resolution_percentage // 100}×{r.resolution_y * r.resolution_percentage // 100}" \
                  " / " + (f"{r.ffmpeg.format}（{r.ffmpeg.codec}）" if video else r.image_settings.file_format)
        layout.label(text=summary, icon="OUTPUT")
        layout.operator_menu_enum("tachie.output_preset", "preset", text="書き出し設定を選ぶ", icon="PRESET")
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
        from .channels import scene_layout

        scene = seq_scene(context)
        s = scene.tachie
        layout = self.layout
        layout.prop(s, "channel_base")
        try:
            lay = scene_layout(scene)
        except ValueError as e:
            layout.label(text=str(e), icon="ERROR")
            return
        if s.applied_layout != lay.to_json():
            layout.label(text="置いてあるストリップがチャンネルの並びと合っていません", icon="ERROR")
        sed = scene.sequence_editor
        col = layout.column(align=True)
        for lane in reversed(lay.lanes):  # 上から（シーケンサーと同じ向き）
            row = col.row(align=True)
            row.label(text=f"{lane.channel}  {lane.label}", icon=LANE_ICONS.get(lane.role, "DOT"))
            if sed is not None:
                row.prop(sed.channels[lane.channel], "mute", text="", emboss=False)
                row.prop(sed.channels[lane.channel], "lock", text="", emboss=False)
        if sed is not None:
            over = sum(1 for x in sed.strips if x.channel > lay.top and _addon_strip(x))
            if over:
                layout.label(text=f"重なってチャンネルからあふれたストリップ {over} 本（{lay.top + 1} 番から上）", icon="INFO")
        layout.label(text=f"1〜{s.channel_base - 1} 番は動画・背景・効果音・BGM の場所です", icon="INFO")
        row = layout.row()
        row.scale_y = 1.2
        row.operator("tachie.relayout", icon="SORTALPHA")


LANE_ICONS = {"audio": "SPEAKER", "image": "IMAGE_DATA", "face": "SHADERFX", "subtitle": "FONT_DATA",
              "name": "USER", "headline": "SMALL_CAPS"}


def _addon_strip(strip) -> bool:
    from .channels import lane_key

    return lane_key(strip) is not None


def draw_char_lanes(layout, scene, name: str) -> None:
    """キャラのレーン（チャンネル）の表示（キャラ × 役割で自動で決まる）。"""
    from .channels import scene_layout

    try:
        lay = scene_layout(scene)
    except ValueError:
        return
    lanes = [lane for lane in lay.lanes if lane.char == name]
    if lanes:
        layout.label(text="チャンネル: " + "、".join(f"{lane.label.split(' ')[-1]} {lane.channel}" for lane in lanes),
                     icon="SEQ_STRIP_DUPLICATE")


_classes = (
    TACHIE_UL_characters,
    TACHIE_MT_characters,
    TACHIE_PT_main,
    TACHIE_PT_line,
    TACHIE_PT_strip,
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
