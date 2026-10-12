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

from .tachie_studio.subtitle import chars_per_line, wrap
from .tachie_studio.pipeline.config import VOICE_DEFAULTS
from .tachie_studio.voicevox import DEFAULT_URL

MOUTH_ITEMS = [
    ("auto", "自動", "母音の画像があればあいうえお式、なければ音量式"),
    ("vowel", "あいうえお", "音素の母音に合わせて口を動かす"),
    ("volume", "音量", "音声の大きさに合わせて口を動かす"),
]

# 素材のフォルダ名 → TachieCharacter のプロパティ名（他・後は 3 スロット）
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

# 声の調整: TachieCharacter のプロパティ名 → (audio_query のキー, 表示名, 最小, 最大, 説明)。範囲は VOICEVOX アプリに合わせる
VOICE_PROPS = {
    "speed_scale": ("speedScale", "読み上げ速度", 0.5, 2.0, "話す速さ（1 が標準）"),
    "pitch_scale": ("pitchScale", "声の高さ", -0.15, 0.15, "音高（0 が標準）"),
    "intonation_scale": ("intonationScale", "抑揚", 0.0, 2.0, "抑揚の大きさ（1 が標準）"),
    "volume_scale": ("volumeScale", "合成の音量", 0.0, 2.0, "VOICEVOX で合成するときの音量（1 が標準）"),
    "pre_phoneme": ("prePhonemeLength", "開始無音", 0.0, 1.5, "音声の前の無音（秒）"),
    "post_phoneme": ("postPhonemeLength", "終了無音", 0.0, 1.5, "音声の後ろの無音（秒）"),
    "pause_scale": ("pauseLengthScale", "間の長さ", 0.0, 2.0, "句読点の間の長さ（1 が標準）"),
}
# 音声ストリップ側: プロパティ名 → (表示名, 最小, 最大, 既定, 説明)。volume / pan はストリップの値をそのまま使う（すぐ反映）。
# fade_in / fade_out / after は wav と連番に焼き込む（再生成で反映）
AUDIO_PROPS = {
    "volume": ("音量", 0.0, 2.0, 1.0, "音声ストリップの音量（1 が元の大きさ）"),
    "pan": ("パン", -1.0, 1.0, 0.0, "左右の定位（-1 が左、1 が右）"),
    "fade_in": ("フェードイン", 0.0, 2.0, 0.0, "音声の先頭のフェード（秒）"),
    "fade_out": ("フェードアウト", 0.0, 2.0, 0.0, "音声の末尾のフェード（秒）"),
    "after": ("セリフ後の余韻", 0.0, 10.0, 0.0, "音声の後ろに、口を閉じた立ち絵（まばたきのみ）を足す長さ（秒）"),
}
BAKED_AUDIO = ("fade_in", "fade_out", "after")


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


class TachiePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    voicevox_url: StringProperty(name="VOICEVOX エンジン", default=DEFAULT_URL)
    output_dir: StringProperty(
        name="書き出し先", subtype="DIR_PATH", default="",
        description="音声と連番画像の置き場所。空なら .blend の隣の tachie フォルダ（未保存なら一時フォルダ）",
    )

    def draw(self, context):
        from .operators import character_defaults_path, load_character_defaults

        layout = self.layout
        layout.prop(self, "voicevox_url")
        layout.prop(self, "output_dir")
        box = layout.box()
        box.label(text="キャラクターを追加するときの初期値（素材の tachie.toml より優先）")
        try:
            defaults = load_character_defaults()
        except ValueError as e:
            box.label(text=str(e), icon="ERROR")
            defaults = {}
        for key, table in defaults.items():
            row = box.row()
            name = table.get("name")
            row.label(text=f"{key}（{name}）" if name else key, icon="USER")
            row.operator("tachie.remove_defaults", text="", icon="X").key = key
        if not defaults:
            box.label(text="なし。シーケンサーの立ち絵タブのメニューから保存できます")
        box.label(text=str(character_defaults_path()), icon="FILE")


def _add_part_props(cls, empty_description: str, slot_description: str):
    """PART_PROPS のパーツ用 StringProperty をクラスに足す（register の前に呼ぶ）。"""
    for category, props in PART_PROPS.items():
        for i, prop in enumerate(props):
            label = category if len(props) == 1 else f"{category}{i + 1}"
            desc = slot_description if category in SLOT_CATEGORIES else empty_description
            cls.__annotations__[prop] = StringProperty(name=label, default="", description=desc)
    return cls


class TachieExpression(bpy.types.PropertyGroup):
    """表情プリセット。空のパーツは基本の表情のまま。"""
    # name は PropertyGroup が持っている
    __annotations__ = {}


_add_part_props(TachieExpression, "空なら基本の表情のまま",
                "空のスロットは基本の表情のまま。消すときは素材の透明なパーツ（_ など）を選ぶ")


def character_image_strips(ch):
    """このキャラクターの立ち絵ストリップ（セリフと待機）。"""
    sed = ch.id_data.sequence_editor
    if sed is None:
        return []
    return [s for s in sed.strips if s.type == "IMAGE"
            and (s.get("tachie_character") == ch.name or s.get("tachie_idle") == ch.name)]


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
        if strip.type == "TEXT" and strip.get("tachie_character") == self.name:
            try:
                apply_subtitle(strip, self, strip.get("tachie_text", strip.text))
            except RuntimeError:
                pass  # フォントが読めないときは今の見た目のまま


def _update_placement(self, context):
    # 置いてあるストリップにもすぐ反映する（作り直さなくてよい）
    for strip in character_image_strips(self):
        apply_placement(strip, self)


class TachieCharacter(bpy.types.PropertyGroup):
    # name は PropertyGroup が持っている
    folder: StringProperty(name="素材フォルダ", subtype="DIR_PATH",
                           description="動く立ち絵の素材フォルダ（眉・目・口などのフォルダがある所）")
    speaker: IntProperty(name="話者 ID", default=3, min=0, description="VOICEVOX のスタイル ID")
    speaker_name: StringProperty(name="話者", default="")
    mouth: EnumProperty(name="口パク", items=MOUTH_ITEMS, default="auto")

    expressions: CollectionProperty(type=TachieExpression)
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


_add_part_props(TachieCharacter, "空なら素材の既定のパーツ", "空なら使わない")
for _prop, (_key, _label, _min, _max, _desc) in VOICE_PROPS.items():
    TachieCharacter.__annotations__[_prop] = FloatProperty(
        name=_label, default=VOICE_DEFAULTS[_key], min=_min, max=_max, step=1, precision=2,
        description=_desc + "。新しく置くセリフの既定値（置いたセリフは選択中のセリフで変える）")
for _prop, (_label, _min, _max, _default, _desc) in AUDIO_PROPS.items():
    TachieCharacter.__annotations__[f"audio_{_prop}"] = FloatProperty(
        name=_label, default=_default, min=_min, max=_max, step=1, precision=2,
        unit="TIME_ABSOLUTE" if _prop in BAKED_AUDIO else "NONE",
        description=_desc + "。新しく置くセリフの既定値")


class TachieSceneSettings(bpy.types.PropertyGroup):
    characters: CollectionProperty(type=TachieCharacter)
    active_character: IntProperty(name="キャラクター", default=0)
    text: StringProperty(name="セリフ", default="")
    expression: StringProperty(name="表情", default="", description="追加するセリフの表情プリセット。空なら基本の表情")
    script: PointerProperty(name="台本", type=bpy.types.Text,
                            description="テキストエディタの台本（1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」）")
    line_gap: FloatProperty(name="セリフの間隔", default=0.2, min=0.0, max=10.0, unit="TIME_ABSOLUTE",
                            description="台本を流し込むときのセリフとセリフの間（秒）")

    audio_channel: IntProperty(name="音声", default=2, min=1, max=128)
    subtitle_channel: IntProperty(name="字幕", default=10, min=1, max=128)
    still_seconds: FloatProperty(name="長さ", default=5.0, min=0.1, max=3600.0, unit="TIME_ABSOLUTE",
                                 description="「立ち絵だけを置く」で置く長さ（秒）")
    advance: BoolProperty(name="追加後に再生位置を進める", default=True)
    ripple: BoolProperty(name="再生成で長さが変わったら後ろをずらす", default=True)
    auto_idle: BoolProperty(name="待機を自動で更新", default=True,
                            description="セリフを追加・再生成したら、待機中の立ち絵を作り直す")

    def character(self):
        if 0 <= self.active_character < len(self.characters):
            return self.characters[self.active_character]
        return None


class TachieDictionary(bpy.types.PropertyGroup):
    """ユーザー辞書に登録する単語の入力欄（保存しない）。"""
    surface: StringProperty(name="表記", description="セリフに出てくる書き方（例: 立ち絵）")
    pronunciation: StringProperty(name="読み", description="カタカナ（ひらがなはカタカナにする）")
    accent_type: IntProperty(name="アクセント", default=0, min=0, max=50,
                             description="音が下がる直前のモーラの位置。0 は平板（下がらない）")


class TachieMora(bpy.types.PropertyGroup):
    """抑揚の編集で 1 モーラ分（name はモーラの文字）。"""
    consonant: StringProperty()
    vowel: StringProperty()
    voiced: BoolProperty(default=True)  # 無声化・促音は音高 0 のまま
    pitch: FloatProperty(name="音高", min=0.0, max=10.0, soft_min=3.0, soft_max=6.5, step=1, precision=2)
    consonant_length: FloatProperty(name="子音", min=0.0, max=1.0, soft_max=0.3, step=0.1, precision=3,
                                    unit="TIME_ABSOLUTE", description="子音の長さ（秒）")
    vowel_length: FloatProperty(name="母音", min=0.0, max=1.0, soft_max=0.3, step=0.1, precision=3,
                                unit="TIME_ABSOLUTE", description="母音の長さ（秒）")


def _shift_phrase(self, context):
    delta = self.shift - self.shift_applied
    for m in self.moras:
        if m.voiced:
            m.pitch = max(0.0, m.pitch + delta)
    self.shift_applied = self.shift


class TachiePhrase(bpy.types.PropertyGroup):
    """抑揚の編集で 1 アクセント句分。moras 以外の値（accent / pause_mora / is_interrogative）は rest に JSON で持つ。"""
    moras: CollectionProperty(type=TachieMora)
    rest: StringProperty()
    shift: FloatProperty(name="句の高さ", default=0.0, soft_min=-1.0, soft_max=1.0, step=1, precision=2,
                         update=_shift_phrase, description="句全体の音高をずらす")
    shift_applied: FloatProperty(default=0.0)


class TachieAccentEdit(bpy.types.PropertyGroup):
    """抑揚の編集ダイアログの中身（保存しない）。"""
    phrases: CollectionProperty(type=TachiePhrase)
    tab: EnumProperty(items=[("PITCH", "イントネーション", "モーラごとの音高"),
                             ("LENGTH", "長さ", "モーラごとの子音・母音の長さ")])
    clip_id: StringProperty()
    text: StringProperty()
    kana: StringProperty()
    speaker: IntProperty()


_classes = (TachiePreferences, TachieExpression, TachieCharacter, TachieSceneSettings, TachieDictionary,
            TachieMora, TachiePhrase, TachieAccentEdit)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.tachie = PointerProperty(type=TachieSceneSettings)
    bpy.types.WindowManager.tachie_dict = PointerProperty(type=TachieDictionary)
    bpy.types.WindowManager.tachie_accent = PointerProperty(type=TachieAccentEdit)


def unregister():
    del bpy.types.WindowManager.tachie_accent
    del bpy.types.WindowManager.tachie_dict
    del bpy.types.Scene.tachie
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
