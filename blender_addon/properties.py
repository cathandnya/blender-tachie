import glob
import re
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

from . import prefetch
from .tachie_studio.subtitle import breaks, chars_per_line, wrap
from .tachie_studio.pipeline.config import VOICE_DEFAULTS
from .tachie_studio.voicevox import DEFAULT_URL

EFFECT_ITEMS = [
    ("NONE", "なし", ""),
    ("FADE", "フェード", "透明からだんだん見える（見えなくなる）"),
    ("SLIDE", "スライド", "近い方の画面の端から入る（出る）"),
]

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

# 声のエンジン（VOICEVOX と同じ API のもの）: 名前 → (表示名, URL)。URL は各エンジンの標準のポート
ENGINE_PRESETS = {
    "AIVIS": ("AivisSpeech", "http://127.0.0.1:10101"),
    "SHAREVOX": ("SHAREVOX", "http://127.0.0.1:50025"),
    "COEIROINK": ("COEIROINK（v1）", "http://127.0.0.1:50031"),
}
ENGINE_ITEMS = [("DEFAULT", "既定（アドオン設定のエンジン）", "アドオン設定の VOICEVOX エンジン")] + [
    (k, label, url) for k, (label, url) in ENGINE_PRESETS.items()] + [("CUSTOM", "URL を指定", "")]

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
             "C:/Windows/Fonts", "%LOCALAPPDATA%/Microsoft/Windows/Fonts",  # Windows のユーザーごとのフォント
             "/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts")
FONT_SUFFIXES = (".ttf", ".ttc", ".otf", ".otc")


def system_fonts() -> list[tuple[str, str]]:
    """(表示名, パス) の一覧（名前順）。"""
    import os

    found = {}
    for d in FONT_DIRS:
        d = os.path.expanduser(os.path.expandvars(d))
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
                    "C:/Windows/Fonts/meiryo*.ttc", "C:/Windows/Fonts/YuGoth*.ttc",
                    "/usr/share/fonts/**/NotoSansCJK*.tt[cf]", "/usr/share/fonts/**/NotoSansJP*.[ot]tf"):
        for path in sorted(glob.glob(pattern, recursive=True)):
            name = unicodedata.normalize("NFC", path)
            if any(key in name.lower() for key in ("ヒラギノ角ゴシック", "meiryo", "yugoth", "notosanscjk", "notosansjp")):
                return path
    return ""


class TachieSoundEffect(bpy.types.PropertyGroup):
    """登録した効果音（アドオン設定、全 .blend 共通）。"""

    name: StringProperty(name="名前")
    filepath: StringProperty(name="ファイル", subtype="FILE_PATH")
    volume: FloatProperty(name="音量", default=1.0, min=0.0, max=100.0, soft_max=2.0,
                          description="置くときの音量")


class TachieTemplate(bpy.types.PropertyGroup):
    """登録したテンプレート（アドオン設定、全 .blend 共通）。中身は `tachie_studio.templates` の JSON。"""

    name: StringProperty(name="名前")
    data: StringProperty(name="中身", default="", options={"HIDDEN"})
    behind: BoolProperty(name="立ち絵より奥に置く", default=False,
                         description="画像・動画・カラーを、アドオンのチャンネル（音声・立ち絵・字幕など）より下に置く（背景など）")


class TachiePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    voicevox_url: StringProperty(name="VOICEVOX エンジン", default=DEFAULT_URL)
    output_dir: StringProperty(
        name="書き出し先", subtype="DIR_PATH", default="",
        description="音声と連番画像の置き場所。空なら .blend の隣の tachie フォルダ（未保存なら一時フォルダ）",
    )
    sound_effects: CollectionProperty(type=TachieSoundEffect)
    templates: CollectionProperty(type=TachieTemplate)
    # AI で表情を割り当てる（#27、OpenAI 互換のチャット API）
    llm_url: StringProperty(name="接続先", default="http://127.0.0.1:1234",
                            description="OpenAI 互換のチャット API。LM Studio は http://127.0.0.1:1234、"
                                        "Ollama は http://127.0.0.1:11434、"
                                        "Gemini は https://generativelanguage.googleapis.com/v1beta/openai/")
    llm_model: StringProperty(name="モデル", default="qwen/qwen3-4b",
                              description="接続先でのモデル名。空のまま接続テストを押すと一覧の先頭が入る")
    llm_api_key: StringProperty(name="API キー", subtype="PASSWORD", default="",
                                description="クラウドの API を使うときだけ。アドオン設定（userpref.blend）に平文で残る。"
                                            "環境変数 TACHIE_LLM_API_KEY があればそちらを使う")
    llm_lines_per_request: IntProperty(name="1 回あたりの行数", default=12, min=1, max=100,
                                       description="多すぎるとモデルが後半を落とす（落とした行は自動で聞き直す）")
    llm_context_lines: IntProperty(name="文脈の行数", default=4, min=0, max=50,
                                   description="対象の手前を何行、文脈として送るか。増やすと遅くなる")
    llm_no_think: BoolProperty(name="/no_think を付ける", default=True, description="Qwen3 系の thinking を抑える")
    llm_reasoning_effort: StringProperty(name="reasoning_effort", default="low",
                                         description="gpt-oss 系で効く（medium は low の約 2 倍遅い）。"
                                                     "Gemini などで断られるなら空にする")
    llm_include_reason: BoolProperty(name="判断の理由を書かせる", default=False,
                                     description="結果のテキストに理由が出る。25〜40% 遅くなる")
    llm_timeout: IntProperty(name="タイムアウト（秒）", default=180, min=10, max=3600)
    faceassign_policy: StringProperty(name="作品の方針", default="",
                                      description="LLM に伝える方針（例: 明るいトーンの解説動画。怒りは控えめに）")
    faceassign_target: EnumProperty(
        name="書き込み先", default="LINE",
        items=[("LINE", "セリフの表情", "セリフの表情プリセットを変える（音声は合成し直さない）"),
               ("FACE_STRIP", "表情ストリップ", "セリフの区間に表情ストリップを置く（後から目で見て直しやすい）")])

    def draw(self, context):
        from .operators import character_defaults_path, load_character_defaults, load_voice_defaults

        layout = self.layout
        layout.prop(self, "voicevox_url")
        layout.prop(self, "output_dir")
        box = layout.box()
        box.label(text="キャラクターを追加するときの初期値（素材の tachie.toml より優先）")
        try:
            defaults, voices = load_character_defaults(), load_voice_defaults()
        except ValueError as e:
            box.label(text=str(e), icon="ERROR")
            defaults, voices = {}, {}
        for key, table in defaults.items():
            row = box.row()
            name = table.get("name")
            row.label(text=f"{key}（{name}）" if name else key, icon="USER")
            row.operator("tachie.remove_defaults", text="", icon="X").key = key
        for name in voices:
            row = box.row()
            row.label(text=f"{name}（立ち絵なし）", icon="SPEAKER")
            op = row.operator("tachie.remove_defaults", text="", icon="X")
            op.key, op.voice = name, True
        if not defaults and not voices:
            box.label(text="なし。シーケンサーの立ち絵タブのメニューから保存できます")
        box.label(text=str(character_defaults_path()), icon="FILE")
        box = layout.box()
        box.label(text="効果音（シーケンサーの立ち絵タブの「効果音」で置く）")
        draw_sound_effects(box, self)
        from .templates import draw_templates

        box = layout.box()
        box.label(text="テンプレート（シーケンサーの立ち絵タブの「テンプレート」で置く）")
        draw_templates(box, self, editable=True)
        box = layout.box()
        box.label(text="表情の割り当て（AI）。セリフを選んで「立ち絵」タブの「AI で表情を割り当てる」")
        box.prop(self, "llm_url")
        row = box.row(align=True)
        row.prop(self, "llm_model")
        row.operator("tachie.llm_test", text="接続テスト")
        box.prop(self, "llm_api_key")
        box.prop(self, "faceassign_policy")
        box.prop(self, "faceassign_target")
        col = box.column(heading="調整")
        col.prop(self, "llm_lines_per_request")
        col.prop(self, "llm_context_lines")
        col.prop(self, "llm_reasoning_effort")
        col.prop(self, "llm_no_think")
        col.prop(self, "llm_include_reason")
        col.prop(self, "llm_timeout")


def draw_sound_effects(layout, preferences) -> None:
    for i, se in enumerate(preferences.sound_effects):
        row = layout.row(align=True)
        row.prop(se, "name", text="")
        row.prop(se, "volume")
        row.operator("tachie.remove_sound_effect", text="", icon="X").index = i
    if not preferences.sound_effects:
        layout.label(text="なし")
    layout.operator("tachie.register_sound_effect", icon="ADD")


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
    __annotations__ = {
        # PSD 立ち絵: 基本の表情から変えるレイヤーの表示 {パス: 表示するか}（JSON）
        "psd_layers": StringProperty(name="レイヤー", default=""),
    }


_add_part_props(TachieExpression, "空なら基本の表情のまま",
                "空のスロットは基本の表情のまま。消すときは素材の透明なパーツ（_ など）を選ぶ")


def character_image_strips(ch):
    """このキャラクターの立ち絵ストリップ（セリフなしの立ち絵と、前の版で置いたセリフ・待機の立ち絵）。"""
    sed = ch.id_data.sequence_editor
    if sed is None:
        return []
    return [s for s in sed.strips if s.type == "IMAGE"
            and ch.name in (s.get("tachie_character"), s.get("tachie_idle"), s.get("tachie_still"))]


def still_flip(strip, ch) -> bool:
    """立ち絵の左右反転。立ち絵ごとの `tachie_flip`（YMM4 の立ち絵アイテムと同じ）が無ければキャラの設定。"""
    flip = strip.get("tachie_flip")
    return ch.flip_x if flip is None else bool(flip)


def apply_placement(strip, ch) -> None:
    """立ち位置・左右反転（`still_flip`）と、立ち絵ごとの位置・大きさ（tachie_pos_x / tachie_pos_y / tachie_scale）を設定する。

    use_flip_x はずらした位置ごと画面中央で反転するので、反転するときは位置の符号を逆にしておく。
    拡大・回転の中心は足元（下の中央）にする（大きさを変えても足の位置が動かず、揺れは足元が軸になる）。
    """
    render = ch.id_data.render
    flip = still_flip(strip, ch)
    strip.use_flip_x = flip
    offset = (ch.position_x + strip.get("tachie_pos_x", 0.0)) * render.resolution_x
    strip.transform.offset_x = -offset if flip else offset
    strip.transform.offset_y = (ch.position_y + strip.get("tachie_pos_y", 0.0)) * render.resolution_y
    scale = strip.get("tachie_scale", 1.0)
    strip.transform.scale_x = strip.transform.scale_y = scale
    strip.transform.origin = (0.5, 0.0)


def load_font(path: str):
    if not path:
        return None
    path = bpy.path.abspath(path)
    for font in bpy.data.fonts:
        if font.filepath and bpy.path.abspath(font.filepath) == path:
            return font
    return bpy.data.fonts.load(path, check_existing=True)


def text_offset(strip) -> tuple[float, float]:
    """セリフごとに字幕（とキャラ名）をずらす量（tachie_sub_x / tachie_sub_y、画面に対する割合）。"""
    return float(strip.get("tachie_sub_x", 0.0)), float(strip.get("tachie_sub_y", 0.0))


# テキストストリップの行の間隔（行間 space_line = 1 で文字の大きさの約 1.16 倍）
LINE_PITCH = 1.16


def name_height(ch, text: str) -> float:
    """キャラ名を置く高さ（字幕の上。字幕の行数から見積もる）。"""
    render = ch.id_data.render
    lines = wrap(breaks(text), chars_per_line(render.resolution_x, ch.font_size)).count("\n") + 1
    margin = ch.box_margin if ch.subtitle_box else 0.0
    # 1 行目の高さ + 2 行目からの行の間隔
    text_height = ch.font_size * (1.25 + (lines - 1) * LINE_PITCH * ch.line_spacing)
    return ch.subtitle_y + (text_height + ch.font_size * 0.6 * 0.3) / render.resolution_y + margin * 2


def place_text(strip, ch, text: str) -> None:
    """字幕・キャラ名のストリップを、キャラの字幕の高さ + セリフごとにずらした位置に置く。

    置いた位置を `tachie_placed_loc` に覚える（プレビューで動かしたかを見るため。.blend と undo の段に一緒に入る）。
    """
    dx, dy = text_offset(strip)
    if strip.get("tachie_role") == "name":
        strip.location = (0.5 + dx, min(0.98, name_height(ch, text) + dy))
    else:
        strip.location = (0.5 + dx, ch.subtitle_y + dy)
    strip["tachie_placed_loc"] = list(strip.location)


def apply_subtitle(strip, ch, text: str) -> None:
    """字幕ストリップにキャラクターの字幕スタイルを設定する（改行も入れ直す）。"""
    strip.text = wrap(breaks(text), chars_per_line(ch.id_data.render.resolution_x, ch.font_size))
    strip.font_size = ch.font_size
    font = load_font(ch.font_path)
    if font is not None:
        strip.font = font
    place_text(strip, ch, text)
    strip.anchor_y = "BOTTOM"
    strip.color = ch.text_color
    strip.use_outline = ch.outline_width > 0
    strip.outline_width = ch.outline_width
    strip.outline_color = ch.outline_color
    strip.space_line = ch.line_spacing
    strip.use_box = ch.subtitle_box
    strip.box_color = ch.box_color
    strip.box_margin = ch.box_margin
    strip.box_roundness = ch.box_roundness


def apply_name(strip, ch, text: str) -> None:
    """キャラ名のストリップ: 字幕の上（行数から高さを見積もる）に、キャラの縁取りの色の帯で名前を出す。"""
    strip["tachie_role"] = "name"
    strip.text = ch.name
    strip.font_size = ch.font_size * 0.6
    font = load_font(ch.font_path)
    if font is not None:
        strip.font = font
    place_text(strip, ch, text)
    strip.anchor_y = "BOTTOM"
    strip.color = ch.text_color
    strip.use_outline = False
    strip.use_box = True
    strip.box_color = (*ch.outline_color[:3], 1.0)
    strip.box_margin = 0.006
    strip.box_roundness = 0.5


def _update_subtitle_fade(self, context):
    _update_motion(self, context)


def _update_subtitle(self, context):
    sed = self.id_data.sequence_editor
    if sed is None:
        return
    with prefetch.paused():
        for strip in sed.strips:
            if strip.type == "TEXT" and strip.get("tachie_character") == self.name:
                text = strip.get("tachie_placed_text", strip.get("tachie_text", strip.text))
                try:
                    if strip.get("tachie_role") == "name":
                        apply_name(strip, self, text)
                    else:
                        apply_subtitle(strip, self, text)
                except RuntimeError:
                    pass  # フォントが読めないときは今の見た目のまま


def _update_motion(self, context):
    from .operators import refresh_motion_now

    with prefetch.paused():  # プロパティの即反映もストリップ・F カーブを書き換える
        refresh_motion_now(self.id_data)


def _update_ducking(self, context):
    from .ducking import refresh_ducking

    with prefetch.paused():
        refresh_ducking(self.id_data)


def _update_placement(self, context):
    # 置いてあるストリップにもすぐ反映する（作り直さなくてよい）。登場・退場のスライドも位置に合わせて作り直す
    with prefetch.paused():
        for strip in character_image_strips(self):
            apply_placement(strip, self)
        _update_motion(self, context)


# セリフの形（セリフごとにストリップの tachie_layout に持つ。キャラの line_layout は新しく置くセリフの既定）
# 立ち絵はセリフとは別に置く（「立ち絵だけを置く」）。重なるそのキャラのセリフの間だけ口パクする
# 番号は前の版（0 = FULL、1 = SUBTITLE、2 = AUDIO）と同じにする（.blend の EnumProperty は番号で保存される）
LINE_LAYOUTS = [
    ("SUBTITLE", "音声と字幕", "音声と字幕を置く", 1),
    ("AUDIO", "音声だけ", "字幕を出さない", 2),
]


def clear_text_input(settings) -> None:
    """セリフの入力欄を空にする（追加したあと）。"""
    settings.text = ""


def _update_lanes(self, context):
    """キャラ × 役割のレーンを当て直す（いちばん下のチャンネル・キャラ名を出すかを変えたとき）。"""
    from .channels import ensure_layout

    with prefetch.paused():
        for problem in ensure_layout(self.id_data):
            print("チャンネルを整理できません:", problem)


class TachieCharacter(bpy.types.PropertyGroup):
    # name は PropertyGroup が持っている
    folder: StringProperty(name="素材フォルダ", subtype="DIR_PATH",
                           description="立ち絵の素材フォルダ（PSD 立ち絵は PSD ファイル）。別の素材にするときは右のボタン"
                                       "（素材に無いパーツは既定に戻す）")
    # AivisSpeech のスタイル ID は負の数にもなる（32 bit の符号付き）
    speaker: IntProperty(name="話者 ID", default=3, description="VOICEVOX（互換エンジン）のスタイル ID")
    engine: EnumProperty(name="エンジン", items=ENGINE_ITEMS, default="DEFAULT",
                         description="声のエンジン（VOICEVOX と同じ API のもの）。変えたら話者を選び直す")
    engine_url: StringProperty(name="URL", default="http://127.0.0.1:50021", description="エンジンの URL")
    speaker_name: StringProperty(name="話者", default="")
    mouth: EnumProperty(name="口パク", items=MOUTH_ITEMS, default="auto")

    expressions: CollectionProperty(type=TachieExpression)
    psd_layers: StringProperty(name="レイヤー", default="",
                               description="PSD 立ち絵: PSD の表示状態から変えるレイヤー {パス: 表示するか}（JSON）")
    active_expression: IntProperty(default=0)

    position_x: FloatProperty(name="立ち位置", default=0.3, min=-1.0, max=1.0, update=_update_placement,
                              description="画面中央を 0、右端を 0.5 とした横位置")
    position_y: FloatProperty(name="上下の位置", default=0.0, min=-1.0, max=1.0, update=_update_placement,
                              description="立ち絵を上下にずらす量（画面の高さに対する割合、上が正）")
    flip_x: BoolProperty(name="左右反転", default=False, update=_update_placement,
                         description="立ち絵を左右反転する")
    height: FloatProperty(name="高さ", default=0.9, min=0.1, max=3.0,
                          description="画面の高さに対する立ち絵の高さ")
    # 立ち絵のレーン（チャンネル）。キャラ × 役割のレーンから自動で決まる（channels.ensure_layout が書く）
    channel: IntProperty(name="立ち絵チャンネル", default=3, min=1, max=128)
    talk_bounce: FloatProperty(name="跳ねる高さ", default=0.0, min=0.0, max=0.2, step=0.1, precision=3,
                               subtype="FACTOR", update=_update_motion,
                               description="喋っている間に上下に跳ねる高さ（画面の高さに対する割合）。0 なら跳ねない")
    talk_sway: FloatProperty(name="揺れる角度", default=0.0, min=0.0, max=20.0, step=10, precision=1,
                             update=_update_motion, description="喋っている間に左右に揺れる角度（度）。0 なら揺れない")
    talk_period: FloatProperty(name="動きの速さ", default=0.3, min=0.1, max=2.0, step=1, precision=2,
                               unit="TIME_ABSOLUTE", update=_update_motion,
                               description="跳ねる・揺れる 1 往復の長さ（秒）")
    enter_effect: EnumProperty(name="登場", items=EFFECT_ITEMS, default="NONE", update=_update_motion,
                               description="立ち絵が現れるところ（最初と、途切れたあと）の効果")
    exit_effect: EnumProperty(name="退場", items=EFFECT_ITEMS, default="NONE", update=_update_motion,
                              description="立ち絵が消えるところ（最後と、途切れる前）の効果")
    effect_seconds: FloatProperty(name="登場・退場の長さ", default=0.3, min=0.05, max=3.0, step=1, precision=2,
                                  unit="TIME_ABSOLUTE", update=_update_motion)
    line_layout: EnumProperty(name="セリフの形", items=LINE_LAYOUTS, default="SUBTITLE",
                              description="新しく置くセリフの形（置いたセリフはセリフごとに変えられる）")

    font_path: StringProperty(name="フォント", subtype="FILE_PATH", default=default_font(), update=_update_subtitle)
    font_size: FloatProperty(name="文字サイズ", default=64, min=8, max=400, update=_update_subtitle)
    text_color: FloatVectorProperty(name="文字色", subtype="COLOR", size=4, min=0, max=1,
                                    default=(1.0, 1.0, 1.0, 1.0), update=_update_subtitle)
    outline_color: FloatVectorProperty(name="縁取り", subtype="COLOR", size=4, min=0, max=1,
                                       default=(0.0, 0.0, 0.0, 1.0), update=_update_subtitle)
    outline_width: FloatProperty(name="縁取りの太さ", default=0.15, min=0.0, max=1.0, update=_update_subtitle)
    line_spacing: FloatProperty(name="行間", default=1.0, min=0.3, max=3.0, step=5, update=_update_subtitle,
                                description="字幕の行の間隔（文字の大きさに対する割合。1 が通常。YMM4 の行間 % の 1/100）")
    subtitle_box: BoolProperty(name="背景の帯", default=False, update=_update_subtitle,
                               description="字幕の後ろに帯（箱）を敷く")
    box_color: FloatVectorProperty(name="帯の色", subtype="COLOR", size=4, min=0, max=1,
                                   default=(0.0, 0.0, 0.0, 0.6), update=_update_subtitle)
    box_margin: FloatProperty(name="帯の余白", default=0.012, min=0.0, max=0.1, step=0.1, precision=3,
                              update=_update_subtitle)
    box_roundness: FloatProperty(name="帯の角の丸み", default=0.4, min=0.0, max=1.0, subtype="FACTOR",
                                 update=_update_subtitle)
    show_name: BoolProperty(name="キャラ名を出す", default=False, update=_update_lanes,
                            description="字幕の上にキャラ名（縁取りの色の帯）を出す。変えたら全セリフを再生成で反映")
    subtitle_fade: FloatProperty(name="字幕のフェードイン", default=0.0, min=0.0, max=2.0, step=1, precision=2,
                                 unit="TIME_ABSOLUTE", update=_update_subtitle_fade,
                                 description="字幕（とキャラ名）が出るときにフェードする長さ（秒）。0 ならすぐ出る")
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


def apply_headline(strip, settings) -> None:
    """見出しテロップのスタイル（シーンで共通）。文字はストリップで直したものを残すので触らない。"""
    strip.font_size = settings.headline_size
    font = load_font(settings.headline_font_path)
    if font is not None:
        strip.font = font
    strip.location = (0.5, settings.headline_y)
    strip.anchor_y = "CENTER"
    strip.color = settings.headline_color
    strip.use_outline = True
    strip.outline_width = 0.15
    strip.outline_color = settings.headline_outline_color
    strip.use_box = settings.headline_box
    strip.box_color = settings.headline_box_color
    strip.box_margin = 0.02
    strip.box_roundness = 0.3


def _update_headlines(self, context):
    sed = self.id_data.sequence_editor
    with prefetch.paused():
        for strip in sed.strips if sed else []:
            if strip.type == "TEXT" and strip.get("tachie_headline"):
                try:
                    apply_headline(strip, self)
                except RuntimeError:
                    pass


def _update_ymmp_folder(self, context):
    from .ymmp_import import fill_pending_folders
    fill_pending_folders(self.id_data.tachie, self)


def _update_ymmp_media(self, context):
    if self.ymmp_pending:
        from .ymmp_import import rescan_pending_media
        rescan_pending_media(self)


class TachieYmmpCharacter(bpy.types.PropertyGroup):
    """YMM4 のプロジェクトの読み込みを待っている間の、キャラクターごとの素材フォルダ。"""
    folder: StringProperty(name="素材フォルダ", subtype="DIR_PATH", default="", update=_update_ymmp_folder)
    folder_name: StringProperty()  # YMM4 の素材フォルダの名前


class TachieSceneSettings(bpy.types.PropertyGroup):
    characters: CollectionProperty(type=TachieCharacter)
    active_character: IntProperty(name="キャラクター", default=0)
    text: StringProperty(name="セリフ", default="",
                         description="追加するセリフ。字幕の改行は \\n（¥n）と書く（Blender の入力欄には改行を入れられない）")
    expression: StringProperty(name="表情", default="", description="追加するセリフの表情プリセット。空なら基本の表情")
    script: PointerProperty(name="台本", type=bpy.types.Text,
                            description="テキストエディタの台本（1 行に「キャラ名: セリフ」、表情は「キャラ名(表情): セリフ」）")
    line_gap: FloatProperty(name="セリフの間隔", default=0.2, min=0.0, max=10.0, unit="TIME_ABSOLUTE",
                            description="台本を流し込むときのセリフとセリフの間（秒）")

    # レーン（キャラ × 役割のチャンネル）のいちばん下。下の番号は動画・背景・効果音・BGM の場所
    channel_base: IntProperty(name="いちばん下のチャンネル", default=10, min=1, max=128, update=_update_lanes,
                              description="アドオンが使うチャンネル（キャラ × 役割のレーン）を、この番号から上に並べる。"
                                          "下の番号は動画・背景・効果音・BGM を置く場所")
    applied_layout: StringProperty(default="")  # 前に当てたレーン（JSON）
    layout_version: IntProperty(default=0)  # 0 は古い .blend（レーンに並べ直す前）
    # 以下の *_channel は古い .blend の設定で、使わない（見出しの headline_channel だけレーンから自動で書く）
    audio_channel: IntProperty(name="音声", default=2, min=1, max=128)
    name_channel: IntProperty(name="キャラ名", default=11, min=1, max=128,
                              description="字幕の上に出すキャラ名のチャンネル（キャラ名を出すキャラのみ）")
    headline_channel: IntProperty(name="見出し", default=12, min=1, max=128)
    headline_text: StringProperty(name="見出し", default="")
    headline_seconds: FloatProperty(name="長さ", default=3.0, min=0.1, max=3600.0, unit="TIME_ABSOLUTE",
                                    description="「見出しを置く」で置く長さ（秒）")
    headline_font_path: StringProperty(name="フォント", subtype="FILE_PATH", default=default_font(),
                                       update=_update_headlines)
    headline_size: FloatProperty(name="文字サイズ", default=96, min=8, max=600, update=_update_headlines)
    headline_color: FloatVectorProperty(name="文字色", subtype="COLOR", size=4, min=0, max=1,
                                        default=(1.0, 1.0, 1.0, 1.0), update=_update_headlines)
    headline_outline_color: FloatVectorProperty(name="縁取り", subtype="COLOR", size=4, min=0, max=1,
                                                default=(0.1, 0.1, 0.3, 1.0), update=_update_headlines)
    headline_box: BoolProperty(name="背景の帯", default=True, update=_update_headlines)
    headline_box_color: FloatVectorProperty(name="帯の色", subtype="COLOR", size=4, min=0, max=1,
                                            default=(0.15, 0.3, 0.7, 0.85), update=_update_headlines)
    headline_y: FloatProperty(name="高さ", default=0.85, min=0.0, max=1.0, subtype="FACTOR", update=_update_headlines)
    subtitle_channel: IntProperty(name="字幕", default=10, min=1, max=128)
    ymmp_material_root: StringProperty(name="素材フォルダを探す場所", subtype="DIR_PATH", default="",
                                       description="YMM4 のプロジェクトを読み込むとき、無いキャラクターの素材フォルダ"
                                                   "（YMM4 と同じ名前）をこの中から探す",
                                       update=_update_ymmp_media)
    ymmp_media_root: StringProperty(name="画像・動画などを探す場所", subtype="DIR_PATH", default="",
                                    description="YMM4 のプロジェクトの画像・動画・音声が元の場所と .ymmp のフォルダに"
                                                "無いとき、この中（下の階層も）から同じ名前のファイルを探す",
                                    update=_update_ymmp_media)
    # 見つからない素材・ファイルがあったときに、パネルで場所を指定してから読み込む（ymmp_pending が空なら待っていない）
    ymmp_pending: StringProperty(subtype="FILE_PATH", default="")
    ymmp_pending_chars: CollectionProperty(type=TachieYmmpCharacter)
    ymmp_pending_files: IntProperty(default=0)  # 画像・動画などのファイルの数
    ymmp_pending_missing: StringProperty(default="")  # 見つからないファイル名（改行区切り）
    ymmp_pending_wide: BoolProperty(default=False)  # 探す場所が広すぎて途中までしか探していない
    ymmp_match_video: BoolProperty(name="解像度・フレームレートを合わせる", default=True)
    ymmp_import_media: BoolProperty(name="画像・動画・音声・テキストも読み込む", default=True,
                                    update=_update_ymmp_media)
    se_channel: IntProperty(name="効果音", default=1, min=1, max=128,
                            description="効果音を置くチャンネル。他のストリップがあれば、上の空いているチャンネル"
                                        "（音声・字幕・キャラ名・見出し・立ち絵のチャンネルは除く）")
    se_at_lines: BoolProperty(name="選んだセリフの頭に置く", default=False,
                              description="オンなら選んでいるセリフそれぞれの頭に、オフなら再生位置に置く")
    duck_volume: FloatProperty(name="セリフ中の音量", default=0.3, min=0.0, max=1.0, subtype="FACTOR",
                               update=_update_ducking,
                               description="セリフの音声が鳴っている間の BGM の音量（BGM の音量に対する割合）。1 なら下げない")
    duck_fade: FloatProperty(name="下げる・戻す時間", default=0.3, min=0.0, max=5.0, unit="TIME_ABSOLUTE",
                             update=_update_ducking, description="BGM の音量を下げる・戻すのにかける時間（秒）")
    still_seconds: FloatProperty(name="長さ", default=5.0, min=0.1, max=3600.0, unit="TIME_ABSOLUTE",
                                 description="「立ち絵だけを置く」で置く長さ（秒）")
    face_seconds: FloatProperty(name="長さ", default=2.0, min=0.1, max=3600.0, unit="TIME_ABSOLUTE",
                                description="「表情を置く」の長さ（秒）。再生位置にそのキャラのセリフがあれば、その終わりまで")
    advance: BoolProperty(name="追加後に再生位置を進める", default=True)
    ripple: BoolProperty(name="再生成で長さが変わったら後ろをずらす", default=True)

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


# 抑揚の編集の棒（YMM4 の縦のスライダーの代わり）。上がいちばん高い。値の範囲はダイアログごと（TachieAccentEdit の lo / hi）
ACCENT_LEVELS = 12
_LEVEL_ITEMS = [(str(i), "", "", i) for i in reversed(range(ACCENT_LEVELS))]  # 値（get / set の番号）は段の高さ


def _level_get(attr: str, range_attr: str):
    def get(self):
        edit = self.id_data.tachie_accent
        lo, hi = getattr(edit, f"{range_attr}_lo"), getattr(edit, f"{range_attr}_hi")
        t = (getattr(self, attr) - lo) / (hi - lo) if hi > lo else 0.0
        return max(0, min(ACCENT_LEVELS - 1, round(t * (ACCENT_LEVELS - 1))))
    return get


def _level_set(attr: str, range_attr: str):
    def set_(self, value):
        edit = self.id_data.tachie_accent
        lo, hi = getattr(edit, f"{range_attr}_lo"), getattr(edit, f"{range_attr}_hi")
        setattr(self, attr, round(lo + (hi - lo) * value / (ACCENT_LEVELS - 1), 4))
    return set_


def _mora_place(mora) -> tuple:
    """抑揚の編集のモーラの (句の番号, モーラの番号)。"""
    m = re.search(r"phrases\[(\d+)\]\.moras\[(\d+)\]", mora.path_from_id())
    return int(m[1]), int(m[2])


def _is_accent_get(self):
    p, k = _mora_place(self)
    return self.id_data.tachie_accent.phrases[p].accent == k + 1


def _is_accent_set(self, value):
    if value:  # 押したモーラをアクセントの位置にする（句の accent の update が音高を計算し直す）
        p, k = _mora_place(self)
        self.id_data.tachie_accent.phrases[p].accent = k + 1


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
    is_accent: BoolProperty(name="アクセント", get=_is_accent_get, set=_is_accent_set,
                            description="ここをアクセントにする（ここまで高く、次から下がる。音高を計算し直す）")
    # 棒（押した高さが値になる）
    pitch_level: EnumProperty(items=_LEVEL_ITEMS, get=_level_get("pitch", "pitch"), set=_level_set("pitch", "pitch"),
                              description="音高（押した高さにする。細かい値は上の数値で）")
    length_level: EnumProperty(items=_LEVEL_ITEMS, get=_level_get("vowel_length", "length"),
                               set=_level_set("vowel_length", "length"),
                               description="母音の長さ（押した高さにする。細かい値は上の数値で）")


def _shift_phrase(self, context):
    delta = self.shift - self.shift_applied
    for m in self.moras:
        if m.voiced:
            m.pitch = max(0.0, m.pitch + delta)
    self.shift_applied = self.shift


_loading_accent = False


def _accent_changed(self, context):
    """句のアクセントの位置を変えたら、その句の音高をエンジンで計算し直す。"""
    if _loading_accent:
        return
    if 0 < len(self.moras) < self.accent:  # 句のモーラ数より後ろには置けない（代入でもう一度呼ばれる）
        self.accent = len(self.moras)
        return
    from .operators import refresh_phrase_pitch

    refresh_phrase_pitch(context, self)


class TachiePhrase(bpy.types.PropertyGroup):
    """抑揚の編集で 1 アクセント句分。moras と accent 以外の値（pause_mora / is_interrogative）は rest に JSON で持つ。"""
    moras: CollectionProperty(type=TachieMora)
    rest: StringProperty()
    accent: IntProperty(name="アクセント", default=1, min=1, max=64, update=_accent_changed,
                        description="アクセントのあるモーラ（ここまで高く、次から下がる）。変えると音高を計算し直す")
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
    character: StringProperty()  # エンジン（キャラごと）を選ぶ
    changed: BoolProperty(default=False)  # アクセントの位置・句の区切りを変えた（発音を句から作り直す）
    width: IntProperty(default=600)  # ダイアログの幅（句を何モーラずつ並べるか）
    pitch_lo: FloatProperty(default=3.0)
    pitch_hi: FloatProperty(default=6.5)
    length_lo: FloatProperty(default=0.0)
    length_hi: FloatProperty(default=0.3)


_classes = (TachieSoundEffect, TachieTemplate, TachiePreferences, TachieExpression, TachieCharacter, TachieYmmpCharacter,
            TachieSceneSettings, TachieDictionary,
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
