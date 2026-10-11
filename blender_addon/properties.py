import glob
import unicodedata

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from .ymm.subtitle import chars_per_line, wrap
from .ymm.voicevox import DEFAULT_URL

MOUTH_ITEMS = [
    ("auto", "自動", "母音の画像があればあいうえお式、なければ音量式"),
    ("vowel", "あいうえお", "音素の母音に合わせて口を動かす"),
    ("volume", "音量", "音声の大きさに合わせて口を動かす"),
]

# 素材のフォルダ名 → YMMCharacter のプロパティ名（他・後は 3 スロット）
PART_PROPS = {
    "眉": ["part_eyebrow"],
    "目": ["part_eye"],
    "口": ["part_mouth"],
    "髪": ["part_hair"],
    "顔色": ["part_complexion"],
    "体": ["part_body"],
    "他": ["part_other1", "part_other2", "part_other3"],
    "後": ["part_back1", "part_back2", "part_back3"],
}
SLOT_CATEGORIES = ("他", "後")


FONT_DIRS = ("/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts", "~/Library/Fonts",
             "C:/Windows/Fonts", "/usr/share/fonts", "~/.local/share/fonts")
FONT_SUFFIXES = (".ttf", ".ttc", ".otf", ".otc")


def system_fonts() -> list[tuple[str, str]]:
    """(表示名, パス) の一覧（名前順）。"""
    import os

    found = {}
    for d in FONT_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(FONT_SUFFIXES):
                    name = unicodedata.normalize("NFC", os.path.splitext(f)[0])
                    found.setdefault(name, os.path.join(root, f))
    return sorted(found.items())


def default_font() -> str:
    for pattern in ("/System/Library/Fonts/*W6.ttc", "/System/Library/Fonts/*W3.ttc",
                    "C:/Windows/Fonts/meiryo*.ttc", "/usr/share/fonts/**/NotoSansCJK*.tt[cf]"):
        for path in sorted(glob.glob(pattern, recursive=True)):
            name = unicodedata.normalize("NFC", path)
            if "ヒラギノ角ゴシック" in name or "meiryo" in name.lower() or "NotoSansCJK" in name:
                return path
    return ""


class YMMPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    voicevox_url: StringProperty(name="VOICEVOX エンジン", default=DEFAULT_URL)
    output_dir: StringProperty(
        name="書き出し先", subtype="DIR_PATH", default="",
        description="音声と連番画像の置き場所。空なら .blend の隣の ymm フォルダ（未保存なら一時フォルダ）",
    )

    def draw(self, context):
        self.layout.prop(self, "voicevox_url")
        self.layout.prop(self, "output_dir")


def _add_part_props(cls, empty_description: str, slot_description: str):
    """PART_PROPS のパーツ用 StringProperty をクラスに足す（register の前に呼ぶ）。"""
    for category, props in PART_PROPS.items():
        for i, prop in enumerate(props):
            label = category if len(props) == 1 else f"{category}{i + 1}"
            desc = slot_description if category in SLOT_CATEGORIES else empty_description
            cls.__annotations__[prop] = StringProperty(name=label, default="", description=desc)
    return cls


class YMMExpression(bpy.types.PropertyGroup):
    """表情プリセット。空のパーツは基本の表情のまま。"""
    # name は PropertyGroup が持っている
    __annotations__ = {}


_add_part_props(YMMExpression, "空なら基本の表情のまま",
                "他（後）をどれか 1 つでも指定すると、他（後）は丸ごとこのプリセットのものになる")


def character_image_strips(ch):
    """このキャラクターの立ち絵ストリップ（セリフと待機）。"""
    sed = ch.id_data.sequence_editor
    if sed is None:
        return []
    return [s for s in sed.strips if s.type == "IMAGE"
            and (s.get("ymm_character") == ch.name or s.get("ymm_idle") == ch.name)]


def apply_placement(strip, ch) -> None:
    """立ち位置と左右反転をストリップに設定する。

    use_flip_x はずらした位置ごと画面中央で反転するので、反転するときは位置の符号を逆にしておく。
    """
    strip.use_flip_x = ch.flip_x
    offset = ch.position_x * ch.id_data.render.resolution_x
    strip.transform.offset_x = -offset if ch.flip_x else offset


def load_font(path: str):
    if not path:
        return None
    path = bpy.path.abspath(path)
    for font in bpy.data.fonts:
        if font.filepath and bpy.path.abspath(font.filepath) == path:
            return font
    return bpy.data.fonts.load(path, check_existing=True)


def apply_subtitle(strip, ch, text: str) -> None:
    """字幕ストリップにキャラクターの字幕スタイルを設定する（改行も入れ直す）。"""
    strip.text = wrap(text, chars_per_line(ch.id_data.render.resolution_x, ch.font_size))
    strip.font_size = ch.font_size
    font = load_font(ch.font_path)
    if font is not None:
        strip.font = font
    strip.location = (0.5, ch.subtitle_y)
    strip.anchor_y = "BOTTOM"
    strip.color = ch.text_color
    strip.use_outline = ch.outline_width > 0
    strip.outline_width = ch.outline_width
    strip.outline_color = ch.outline_color


def _update_subtitle(self, context):
    sed = self.id_data.sequence_editor
    if sed is None:
        return
    for strip in sed.strips:
        if strip.type == "TEXT" and strip.get("ymm_character") == self.name:
            try:
                apply_subtitle(strip, self, strip.get("ymm_text", strip.text))
            except RuntimeError:
                pass  # フォントが読めないときは今の見た目のまま


def _update_placement(self, context):
    # 置いてあるストリップにもすぐ反映する（作り直さなくてよい）
    for strip in character_image_strips(self):
        apply_placement(strip, self)


class YMMCharacter(bpy.types.PropertyGroup):
    # name は PropertyGroup が持っている
    folder: StringProperty(name="素材フォルダ", subtype="DIR_PATH",
                           description="動く立ち絵の素材フォルダ（眉・目・口などのフォルダがある所）")
    speaker: IntProperty(name="話者 ID", default=3, min=0, description="VOICEVOX のスタイル ID")
    speaker_name: StringProperty(name="話者", default="")
    mouth: EnumProperty(name="口パク", items=MOUTH_ITEMS, default="auto")

    expressions: CollectionProperty(type=YMMExpression)
    active_expression: IntProperty(default=0)

    position_x: FloatProperty(name="立ち位置", default=0.3, min=-1.0, max=1.0, update=_update_placement,
                              description="画面中央を 0、右端を 0.5 とした横位置")
    flip_x: BoolProperty(name="左右反転", default=False, update=_update_placement,
                         description="立ち絵を左右反転する")
    height: FloatProperty(name="高さ", default=0.9, min=0.1, max=3.0,
                          description="画面の高さに対する立ち絵の高さ")
    channel: IntProperty(name="立ち絵チャンネル", default=3, min=1, max=128)
    show_idle: BoolProperty(name="待機中も表示", default=True,
                            description="喋っていない間も、まばたきだけする立ち絵を表示する")

    font_path: StringProperty(name="フォント", subtype="FILE_PATH", default=default_font(), update=_update_subtitle)
    font_size: FloatProperty(name="文字サイズ", default=64, min=8, max=400, update=_update_subtitle)
    text_color: FloatVectorProperty(name="文字色", subtype="COLOR", size=4, min=0, max=1,
                                    default=(1.0, 1.0, 1.0, 1.0), update=_update_subtitle)
    outline_color: FloatVectorProperty(name="縁取り", subtype="COLOR", size=4, min=0, max=1,
                                       default=(0.0, 0.0, 0.0, 1.0), update=_update_subtitle)
    outline_width: FloatProperty(name="縁取りの太さ", default=0.15, min=0.0, max=1.0, update=_update_subtitle)
    subtitle_y: FloatProperty(name="字幕の高さ", default=0.12, min=0.0, max=1.0, subtype="FACTOR",
                              update=_update_subtitle)

    def expression(self):
        if 0 <= self.active_expression < len(self.expressions):
            return self.expressions[self.active_expression]
        return None


_add_part_props(YMMCharacter, "空なら素材の既定のパーツ", "空なら使わない")


class YMMSceneSettings(bpy.types.PropertyGroup):
    characters: CollectionProperty(type=YMMCharacter)
    active_character: IntProperty(name="キャラクター", default=0)
    text: StringProperty(name="セリフ", default="")
    expression: StringProperty(name="表情", default="", description="追加するセリフの表情プリセット。空なら基本の表情")
    script: PointerProperty(name="台本", type=bpy.types.Text,
                            description="テキストエディタの台本（1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」）")
    line_gap: FloatProperty(name="セリフの間隔", default=0.2, min=0.0, max=10.0, unit="TIME_ABSOLUTE",
                            description="台本を流し込むときのセリフとセリフの間（秒）")

    audio_channel: IntProperty(name="音声", default=2, min=1, max=128)
    subtitle_channel: IntProperty(name="字幕", default=10, min=1, max=128)
    advance: BoolProperty(name="追加後に再生位置を進める", default=True)
    ripple: BoolProperty(name="再生成で長さが変わったら後ろをずらす", default=True)
    auto_idle: BoolProperty(name="待機を自動で更新", default=True,
                            description="セリフを追加・再生成したら、待機中の立ち絵を作り直す")

    def character(self):
        if 0 <= self.active_character < len(self.characters):
            return self.characters[self.active_character]
        return None


_classes = (YMMPreferences, YMMExpression, YMMCharacter, YMMSceneSettings)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.ymm = PointerProperty(type=YMMSceneSettings)


def unregister():
    del bpy.types.Scene.ymm
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
