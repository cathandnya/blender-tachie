"""AI で表情を割り当てる（#27）。選択中のセリフのテキストを LLM に送り、キャラの表情プリセットを選ばせる。

LLM とのやり取りは `tachie_studio.faceassign`。ここは台本を作る（`build_jobs`）・別スレッドで送る（モーダル操作）・
結果を書く（`apply_outcomes`）の 3 つ。bpy に触るのはメインスレッドだけで、スレッドには台本と候補だけを渡す。
"""

import os
import threading
from dataclasses import dataclass, field

import bpy

from . import prefetch

from . import operators as ops
from .tachie_studio import faceassign as fa

REPORT_TEXT = "表情の割り当て結果"
BASE_LABEL = "基本の表情"  # LLM に見せる「プリセット無し」の名前
API_KEY_ENV = "TACHIE_LLM_API_KEY"


@dataclass
class Job:
    """1 キャラ分。`transcript` はシーンのセリフ全部で、このキャラの対象の行だけ ID を持つ。"""
    character: str
    transcript: list
    candidates: list  # LLM に見せる名前
    values: dict  # 名前 → 表情プリセット（基本は ""）


@dataclass
class Plan:
    scene: str = ""  # 台本を作ったシーン（送っている間にシーケンサーのシーンが変わったら書かない）
    jobs: list = field(default_factory=list)
    targets: dict = field(default_factory=dict)  # ID → tachie_id
    skipped: list = field(default_factory=list)  # (セリフ, 理由)


def endpoint(context) -> fa.Endpoint:
    p = ops.prefs(context)
    return fa.Endpoint(base_url=p.llm_url, model=p.llm_model.strip(),
                       api_key=os.environ.get(API_KEY_ENV) or p.llm_api_key, timeout=float(p.llm_timeout),
                       no_think=p.llm_no_think, reasoning_effort=p.llm_reasoning_effort.strip(),
                       include_reason=p.llm_include_reason)


def _label(strips) -> str:
    text = ops.line_text(strips).replace("\n", " ")
    return f"{strips[0]['tachie_character']}: {text if len(text) <= 24 else text[:23] + '…'}"


def build_jobs(context, ids: list[str]) -> Plan:
    """選んだセリフ（tachie_id）→ キャラごとの台本と候補。表情を付けられないセリフは `skipped` に。"""
    scene = ops.seq_scene(context)
    sed = scene.sequence_editor
    settings = scene.tachie
    lines: dict[str, list] = {}
    for s in sed.strips if sed else []:
        if s.get("tachie_id"):
            lines.setdefault(s["tachie_id"], []).append(s)
    order = sorted(lines, key=lambda cid: (min(s.left_handle for s in lines[cid]), cid))
    plan = Plan(scene=scene.name)
    chosen: dict[str, list[str]] = {}
    for cid in ids:
        strips = lines.get(cid)
        if not strips:
            continue
        name = strips[0]["tachie_character"]
        ch = next((c for c in settings.characters if c.name == name), None)
        reason = ("キャラクターがありません" if ch is None
                  else "立ち絵の無いキャラクターです" if not ch.folder
                  else "表情プリセットがありません" if not len(ch.expressions)
                  else "")
        if reason:
            plan.skipped.append((_label(strips), reason))
        else:
            chosen.setdefault(name, []).append(cid)
    for name, cids in chosen.items():
        ch = next(c for c in settings.characters if c.name == name)
        values = {BASE_LABEL: ""}
        for e in ch.expressions:
            values.setdefault(e.name, e.name)
        transcript = []
        for cid in order:
            line_id = None
            if cid in cids:
                line_id = len(plan.targets)
                plan.targets[line_id] = cid
            text = ops.line_text(lines[cid]).strip()
            if text or line_id is not None:
                transcript.append(fa.Line(line_id, lines[cid][0]["tachie_character"], text))
        plan.jobs.append(Job(name, transcript, list(values), values))
    return plan


def run_jobs(client: fa.Client, jobs: list, policy: str, max_targets: int, context_lines: int,
             progress=None, cancelled=None) -> list:
    """全キャラ分を順に送る → [(Job, [Outcome])]。最初にモデルを読み込ませる。"""
    if progress:
        progress("モデルを準備しています…")
    client.warm_up()
    results = []
    for n, job in enumerate(jobs, 1):
        def report(step, job=job, n=n):
            if progress:
                progress(f"{job.character}（{n}/{len(jobs)}）{step}")
        results.append((job, fa.assign(client, job.transcript, job.candidates, policy, max_targets=max_targets,
                                       context_lines=context_lines, progress=report, cancelled=cancelled)))
    return results


def _face_strip_for(sed, name: str, start: int, end: int):
    return next((f for f in ops.face_strips(sed, name) if f.left_handle == start and f.right_handle == end), None)


def apply_outcomes(context, plan: Plan, results: list, target: str) -> tuple[int, list[str]]:
    """結果を書く（LINE: セリフの表情 / FACE_STRIP: セリフの区間の表情ストリップ）。(変えた数, 結果の行) を返す。"""
    scene = ops.seq_scene(context)
    sed = scene.sequence_editor
    settings = scene.tachie
    changed, rows = 0, []
    ops._applying_faces = True
    try:
        for job, outcomes in results:
            for o in outcomes:
                cid = plan.targets[o.id]
                strips = ops.line_strips(sed, cid) if sed else []
                if not strips:
                    rows.append(f"{job.character}: セリフが見つかりません（送っている間に消した？）")
                    continue
                label = _label(strips)
                reason = f"（{o.reason}）" if o.reason else ""
                if strips[0].get("tachie_character") != job.character:
                    rows.append(f"{label} → 変えない: 送っている間にキャラクターが変わりました")
                    continue
                if o.preset is None:
                    rows.append(f"{label} → 変えない{reason}" + (f": {o.warning}" if o.warning else ""))
                    continue
                preset = job.values[o.preset]
                shown = preset or BASE_LABEL
                try:
                    if target == "FACE_STRIP":
                        start, end = min(s.left_handle for s in strips), ops.line_end(strips)
                        face = _face_strip_for(sed, job.character, start, end)
                        if face is not None:
                            same = face.get("tachie_expression", "") == preset
                            face["tachie_expression"] = preset
                            face.name = ops._face_name(job.character, preset)
                        else:
                            same = preset == strips[0].get("tachie_expression", "") and not ops.face_segments(
                                sed, ops.find_character(settings, job.character), start, end)
                            if not same:
                                face = sed.strips.new_effect(
                                    name=ops._face_name(job.character, preset), type="COLOR",
                                    channel=ops._free_face_channel(sed, settings, start, end, job.character),
                                    frame_start=start, length=end - start)
                                face.color = (0.95, 0.6, 0.25)
                                face.color_tag = "COLOR_02"
                                face.mute = True
                                face["tachie_face"] = job.character
                                face["tachie_expression"] = preset
                    else:
                        same = strips[0].get("tachie_expression", "") == preset
                        if not same:
                            ops.set_line_expression(context, cid, preset)
                            strips = ops.line_strips(sed, cid)
                            if ops.face_segments(sed, ops.find_character(settings, job.character),
                                                 min(s.left_handle for s in strips), ops.line_end(strips)):
                                reason += "（表情ストリップが重なる区間はそちらが優先）"
                except ops.ERRORS as e:
                    rows.append(f"{label} → {shown}: 書けません: {e}")
                    continue
                if not same:
                    changed += 1
                rows.append(f"{label} → {shown}{'（そのまま）' if same else ''}{reason}")
    finally:
        try:
            ops.refresh_faces(context)
        finally:
            ops._applying_faces = False
            ops._applied_signatures[scene.name] = ops.auto_signature(scene)
    return changed, rows


def write_report(plan: Plan, rows: list[str], header: str) -> None:
    lines = [header, ""] + rows
    if plan.skipped:
        lines += ["", "対象にしなかったセリフ:"] + [f"{label}: {reason}" for label, reason in plan.skipped]
    text = bpy.data.texts.get(REPORT_TEXT) or bpy.data.texts.new(REPORT_TEXT)
    text.clear()
    text.write("\n".join(lines) + "\n")


_running = False  # 送っている途中（もう一度押して 2 本走らせない）


class TACHIE_OT_assign_faces(bpy.types.Operator):
    """選択中のセリフのテキストを LLM に送り、表情プリセットを選んで書き込む（Esc で取り消し）"""

    bl_idname = "tachie.assign_faces"
    bl_label = "AI で表情を割り当てる"
    bl_options = {"REGISTER", "UNDO"}

    _thread = None
    _timer = None
    _state = None

    @classmethod
    def poll(cls, context):
        if _running:
            cls.poll_message_set("表情を割り当てている途中です（Esc で取り消し）")
            return False
        sed = ops.seq_scene(context).sequence_editor
        return sed is not None and any(s.select and s.get("tachie_id") for s in sed.strips)

    def invoke(self, context, event):
        global _running
        p = ops.prefs(context)
        if not p.llm_model.strip():
            self.report({"ERROR"}, "アドオン設定でモデル名を入れてください（接続テストで一覧の先頭が入ります）")
            return {"CANCELLED"}
        ids, _, _ = ops.selected_targets(ops.seq_scene(context).sequence_editor)
        plan = build_jobs(context, ids)
        if not plan.jobs:
            write_report(plan, [], "表情を付けられるセリフがありません")
            reason = plan.skipped[0][1] if plan.skipped else "セリフを選んでください"
            self.report({"WARNING"}, f"表情を付けられるセリフがありません（{reason}）")
            return {"CANCELLED"}
        state = {"plan": plan, "message": "", "results": None, "error": None, "cancel": False}
        client = fa.Client(endpoint(context))
        args = (p.faceassign_policy, p.llm_lines_per_request, p.llm_context_lines)

        def work():
            try:
                state["results"] = run_jobs(client, plan.jobs, *args,
                                            progress=lambda m: state.__setitem__("message", m),
                                            cancelled=lambda: state["cancel"])
            except fa.Cancelled:
                pass
            except Exception as e:  # スレッドの例外は画面に出ないので持ち帰る
                state["error"] = e

        self._state = state
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        _running = True
        self._status(context, "LLM に送っています…")
        return {"RUNNING_MODAL"}

    def _status(self, context, message: str | None) -> None:
        if context.workspace:
            context.workspace.status_text_set(
                None if message is None else f"AI で表情を割り当てる: {message}（Esc で取り消し）")

    def _finish(self, context) -> None:
        global _running
        _running = False
        context.window_manager.event_timer_remove(self._timer)
        self._status(context, None)

    def modal(self, context, event):
        state = self._state
        if event.type == "ESC" and event.value == "PRESS":
            state["cancel"] = True  # 送っている途中の 1 本は待たずに抜ける（結果は捨てる）
            self._finish(context)
            self.report({"INFO"}, "取り消しました")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._thread.is_alive():
            self._status(context, state["message"] or "LLM に送っています…")
            return {"RUNNING_MODAL"}
        self._finish(context)
        if state["error"] is not None:
            self.report({"ERROR"}, f"表情を割り当てられません: {state['error']}")
            return {"CANCELLED"}
        if state["results"] is None:
            return {"CANCELLED"}
        if ops.seq_scene(context).name != state["plan"].scene:
            self.report({"ERROR"}, f"シーケンサーのシーンが変わったので書き込みません（{state['plan'].scene} で実行し直してください）")
            return {"CANCELLED"}
        return self.finish_results(context, state["plan"], state["results"])

    @prefetch.guarded  # 書き込む間だけ止める（LLM を待っている間の modal では止めない）
    def finish_results(self, context, plan, results):
        target = ops.prefs(context).faceassign_target
        changed, rows = apply_outcomes(context, plan, results, target)
        total = sum(len(o) for _, o in results)
        failed = sum(1 for _, outs in results for o in outs if o.warning)
        header = f"{total} 行のうち {changed} 行の表情を変えました" + (
            f"（{failed} 行は LLM の答えが得られず変えていません）" if failed else "")
        write_report(plan, rows, header)
        self.report({"WARNING"} if failed else {"INFO"}, f"{header}。詳しくはテキスト「{REPORT_TEXT}」")
        return {"FINISHED"}

    def execute(self, context):
        if not bpy.app.background and context.window is not None:
            # 画面があれば（Shift+R の繰り返しなど）別スレッドで送る。画面を固めない
            return self.invoke(context, None)
        # バックグラウンド（モーダルを回せない）では送り終わるまで待つ
        ids, _, _ = ops.selected_targets(ops.seq_scene(context).sequence_editor)
        plan = build_jobs(context, ids)
        if not plan.jobs:
            self.report({"WARNING"}, "表情を付けられるセリフがありません")
            return {"CANCELLED"}
        p = ops.prefs(context)
        try:
            results = run_jobs(fa.Client(endpoint(context)), plan.jobs, p.faceassign_policy,
                               p.llm_lines_per_request, p.llm_context_lines)
        except fa.LlmError as e:
            self.report({"ERROR"}, f"表情を割り当てられません: {e}")
            return {"CANCELLED"}
        return self.finish_results(context, plan, results)


class TACHIE_OT_llm_test(bpy.types.Operator):
    """接続先のモデルの一覧を取る（モデル名が空なら一覧の先頭を入れる）"""

    bl_idname = "tachie.llm_test"
    bl_label = "接続テスト"

    def execute(self, context):
        p = ops.prefs(context)
        try:
            models = fa.Client(endpoint(context)).list_models()
        except fa.LlmError as e:
            self.report({"ERROR"}, e.detail())
            return {"CANCELLED"}
        if not p.llm_model.strip() and models:
            p.llm_model = models[0]
            context.preferences.is_dirty = True
        shown = "、".join(models[:10]) + ("…" if len(models) > 10 else "")
        mark = "" if not p.llm_model.strip() or p.llm_model.strip() in models else "（モデル名が一覧にありません）"
        self.report({"INFO"}, f"繋がりました{mark}。モデル {len(models)} 個: {shown}")
        return {"FINISHED"}


_classes = (TACHIE_OT_assign_faces, TACHIE_OT_llm_test)


def register():
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
