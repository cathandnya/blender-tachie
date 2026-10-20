"""セリフから LLM で表情プリセットを選ぶ（#27。cathandnya/ymm4faceassign を Python に移したもの）。

OpenAI 互換のチャット API（LM Studio / Ollama / Gemini など）に台本を送り、各セリフにキャラの表情の候補の番号を
返させる。Blender 同梱 Python で動くよう urllib だけで書く。

ymm4faceassign の実測（docs/measurements.md）で決めたこと:
- 出力は候補の番号（名前を書かせると遅い）。JSON スキーマの enum で実在する番号だけに縛る。-1 は「どれも合わない」
- 台本はキャラごとに束ね、他のキャラのセリフは ID 無しの文脈として混ぜる。文脈は束の手前 `context_lines` 行と、
  対象の間の直前 1 行だけ（全部送ると 1 行の推定に数十秒かかる）
- 返ってこなかった行は一度だけ聞き直し、駄目なら変えない。json_schema を断るサーバには、整数の enum を範囲
  （minimum / maximum）にしたスキーマ（apfel など。Apple の端末内モデルは文字列の enum しか縛れない）、それも
  断られたらスキーマ無しで送り直す
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable

KEEP = -1  # どの候補も合わない（変えない）
BRIDGE_LINES = 1  # 対象と対象の間に残す他のセリフの数
NO_ANSWER = "LLM が判定を返しませんでした"


class LlmError(RuntimeError):
    def __init__(self, message: str, body: str = "", schema_rejected: bool = False):
        super().__init__(message)
        self.body = body
        self.schema_rejected = schema_rejected

    def detail(self) -> str:
        body = self.body if len(self.body) <= 800 else self.body[:800] + "…"
        return f"{self}\n{body}" if body.strip() else str(self)


class Cancelled(Exception):
    pass


@dataclass
class Endpoint:
    base_url: str = "http://127.0.0.1:1234"
    model: str = "qwen/qwen3-4b"
    api_key: str = ""
    temperature: float = 0.0
    timeout: float = 180.0
    no_think: bool = True  # Qwen3 系の thinking を抑える /no_think をシステムプロンプトに足す
    reasoning_effort: str = "low"  # gpt-oss 系で効く。medium は low の約 2 倍遅い
    include_reason: bool = False  # 判断の理由も書かせる（25〜40% 遅くなる）

    def base(self) -> str:
        """パスの無い URL（http://127.0.0.1:1234）にだけ /v1 を足す。Gemini のようにパスを持つものはそのまま。"""
        b = self.base_url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(b)
        if parsed.scheme and parsed.netloc:
            return b + "/v1" if not parsed.path.strip("/") else b
        return b if b.lower().endswith("/v1") else b + "/v1"

    def chat_url(self) -> str:
        return f"{self.base()}/chat/completions"

    def models_url(self) -> str:
        return f"{self.base()}/models"


@dataclass(frozen=True)
class Line:
    """台本の 1 行。対象の行だけ `id` を持ち、他は文脈として送る。"""
    id: int | None
    character: str
    text: str


@dataclass(frozen=True)
class Outcome:
    """1 行の結果。`preset` が None なら変えない（`warning` が理由）。"""
    id: int
    preset: str | None
    reason: str = ""
    warning: str = ""


class Client:
    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self._schema_level = 0  # 断られたスキーマの段は次から飛ばす（0: そのまま、1: 範囲、2: スキーマ無し）

    def _post(self, url: str, body: dict, timeout: float) -> str:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        try:
            req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        except ValueError as e:
            raise LlmError(f"URL が正しくありません（{self.endpoint.base_url}）: {e}") from None
        return self._open(req, timeout)

    def _open(self, req, timeout: float) -> str:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise LlmError(f"失敗しました（{e.code}）", e.read().decode("utf-8", errors="replace")) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            # タイムアウト（TimeoutError）も OSError。途中で切れた応答（IncompleteRead）は HTTPException、
            # ポート番号の誤りなど（InvalidURL）は ValueError
            reason = getattr(e, "reason", e)
            raise LlmError(f"{self.endpoint.base_url} に接続できません: {reason}") from None

    def list_models(self) -> list[str]:
        """接続テストを兼ねたモデルの一覧。"""
        headers = {"Authorization": f"Bearer {self.endpoint.api_key}"} if self.endpoint.api_key else {}
        try:
            req = urllib.request.Request(self.endpoint.models_url(), headers=headers)
        except ValueError as e:
            raise LlmError(f"URL が正しくありません（{self.endpoint.base_url}）: {e}") from None
        body = self._open(req, min(self.endpoint.timeout, 30))
        try:
            data = json.loads(body).get("data") or []
        except (ValueError, AttributeError):
            raise LlmError("モデルの一覧を読めません", body) from None
        return [d["id"] for d in data if isinstance(d, dict) and isinstance(d.get("id"), str)]

    def warm_up(self) -> bool:
        """モデルを読み込ませるだけの小さなリクエスト（LM Studio は待機中にモデルを降ろす）。失敗しても続ける。"""
        try:
            self._post(self.endpoint.chat_url(), {"model": self.endpoint.model, "stream": False, "max_tokens": 1,
                                                  "messages": [{"role": "user", "content": "."}]},
                       self.endpoint.timeout)
            return True
        except LlmError:
            return False

    def complete_json(self, system: str, user: str, schema: dict | None, name: str = "face_assignments") -> dict:
        """JSON スキーマで縛ってチャット補完を 1 回呼ぶ。スキーマを断られたら、整数の enum を範囲にしたスキーマ、
        それも断られたらスキーマ無しで送り直す。断られた段は同じ Client では次から送らない。"""
        attempts = [(0, schema)]
        if schema is not None:
            relaxed = range_schema(schema)
            attempts += [(1, relaxed), (2, None)] if relaxed != schema else [(2, None)]
            attempts = [a for a in attempts if a[0] >= self._schema_level] or attempts[-1:]
        for n, (_, attempt) in enumerate(attempts, 1):
            try:
                return parse_content(self._chat(system, user, attempt, name))
            except LlmError as e:
                if n == len(attempts) or not e.schema_rejected:
                    raise
                self._schema_level = attempts[n][0]

    def _chat(self, system: str, user: str, schema: dict | None, name: str) -> str:
        body: dict = {"model": self.endpoint.model, "temperature": self.endpoint.temperature, "stream": False,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.endpoint.reasoning_effort:
            body["reasoning_effort"] = self.endpoint.reasoning_effort  # 対応しないサーバは無視する
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": name, "strict": True, "schema": schema}}
        try:
            return self._post(self.endpoint.chat_url(), body, self.endpoint.timeout)
        except LlmError as e:
            if schema is not None and e.body and re.search("response_format|json_schema", e.body, re.I):
                e.schema_rejected = True
            raise


def parse_content(response: str) -> dict:
    try:
        content = json.loads(response)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LlmError("応答を読めません", response) from None
    if not isinstance(content, str) or not content.strip():
        raise LlmError("応答が空でした", response)
    try:
        data = json.loads(sanitize(content))
    except ValueError as e:
        raise LlmError(f"JSON を読めません: {e}", content) from None
    if not isinstance(data, dict):
        raise LlmError("JSON の形が違います", content)
    return data


def sanitize(content: str) -> str:
    """thinking（<think>…</think>）とコードフェンスを除き、最初の { から最後の } までを取る。"""
    s = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if 0 <= start < end else s


def range_schema(schema):
    """整数の enum を範囲（minimum / maximum）にしたスキーマ。整数の enum を縛れないサーバ向け（範囲の中の
    実在しない番号は `interpret` が候補に無い番号として扱う）。"""
    if isinstance(schema, list):
        return [range_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: range_schema(v) for k, v in schema.items()}
    values = out.get("enum")
    if out.get("type") == "integer" and isinstance(values, list) and values and all(isinstance(v, int) for v in values):
        del out["enum"]
        out["minimum"], out["maximum"] = min(values), max(values)
    return out


def build_schema(candidates: list[str], include_reason: bool) -> dict:
    """{"a": [{"i": ID, "p": 候補の番号}]}。番号は enum で実在する候補（と -1）に縛る。

    ymm4faceassign では配列の長さを minItems/maxItems で決めると、返る件数がかえって減った。
    """
    props: dict = {"i": {"type": "integer"}, "p": {"type": "integer", "enum": [KEEP, *range(len(candidates))]}}
    if include_reason:
        props["r"] = {"type": "string"}
    return {"type": "object", "additionalProperties": False, "required": ["a"],
            "properties": {"a": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["i", "p", "r"] if include_reason else ["i", "p"], "properties": props}}}}


def build_system_prompt(candidates: list[str], policy: str = "", no_think: bool = True,
                        include_reason: bool = False) -> str:
    lines = [
        "あなたは動画の台本に立ち絵の表情を割り当てる担当です。",
        "各セリフについて、下の候補から最もふさわしい表情の番号をひとつ選びます。",
        "",
        "判断の指針:",
        "- セリフの感情を優先する。直前の流れも考慮してよい。",
        "- 淡々とした説明や相槌には、平常に近い表情を選ぶ。連続して同じ表情になるのは構わない。",
        f"- 判断に迷う場合や、どの候補もしっくりこない場合は {KEEP} を返してよい。無理に選ばない。",
    ]
    if include_reason:
        lines.append("- r は40字以内の日本語で、なぜその表情かを簡潔に書く。")
    lines.append("")
    if policy.strip():
        lines += ["作品の方針:", policy.strip(), ""]
    lines.append("表情の候補 (番号: 名前):")
    lines += [f"{i}: {c}" for i, c in enumerate(candidates)]
    lines += [
        f"{KEEP}: どの候補も合わない",
        "",
        "入力の各行は [キャラクター名 #ID] セリフ の形式です。",
        "#ID が付いている行だけが対象で、付いていない行は文脈として読むだけにします。",
        "対象の行すべてに、過不足なくひとつずつ割り当てを返してください。",
        '出力は {"a":[{"i":ID,"p":番号,"r":"理由"}]} の形式の JSON のみ。' if include_reason
        else '出力は {"a":[{"i":ID,"p":番号}]} の形式の JSON のみ。',
    ]
    if no_think:
        lines += ["", "/no_think"]
    return "\n".join(lines)


def build_user_prompt(lines: list[Line]) -> str:
    out = []
    for line in lines:
        text = " ".join(line.text.replace("\r", "").split("\n")).strip()
        out.append(f"[{line.character} #{line.id}] {text}" if line.id is not None else f"[{line.character}] {text}")
    return "\n".join(out) + "\n"


def chunk(transcript: list[Line], max_targets: int, context_lines: int) -> list[list[Line]]:
    """対象の行を `max_targets` 行ずつに束ね、束の手前 `context_lines` 行と、対象の間の直前 1 行を文脈に足す。

    対象どうしが離れていても束は分けない（間に残すのは 1 行だけなので、分けても送る量は減らず往復が増える）。
    """
    max_targets, context_lines = max(1, max_targets), max(0, context_lines)
    targets = [i for i, line in enumerate(transcript) if line.id is not None]
    groups = [targets[i:i + max_targets] for i in range(0, len(targets), max_targets)]
    chunks = []
    for group in groups:
        first = group[0]
        # 束の手前の文脈。前の束の対象が入ることがあるので ID を外す
        lines = [replace(transcript[i], id=None) for i in range(max(0, first - context_lines), first)]
        previous = first - 1
        for index in group:
            lines += [replace(transcript[i], id=None)
                      for i in range(max(previous + 1, index - BRIDGE_LINES), index)]
            lines.append(transcript[index])
            previous = index
        chunks.append(lines)
    return chunks


def interpret(data: dict | None, target_ids: list[int], candidates: list[str], failure: str = "") -> list[Outcome]:
    """返った JSON → 対象の行ごとの結果。返ってこなかった行・読めない番号は変えない。"""
    found: dict[int, Outcome] = {}
    entries = data.get("a") if isinstance(data, dict) and not failure else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        i = _int(entry.get("i"))
        if i is None or i not in target_ids or i in found:
            continue
        reason = entry.get("r") if isinstance(entry.get("r"), str) else ""
        p = _int(entry.get("p"))
        if p is None:
            found[i] = Outcome(i, None, reason, "表情の番号を読めませんでした")
        elif p == KEEP:
            found[i] = Outcome(i, None, reason)
        elif 0 <= p < len(candidates):
            found[i] = Outcome(i, candidates[p], reason)
        else:
            found[i] = Outcome(i, None, reason, f"候補に無い番号を返しました: {p}")
    return [found.get(i) or Outcome(i, None, "", failure or NO_ANSWER) for i in target_ids]


def _int(value) -> int | None:
    """スキーマが効けば数値、効かないサーバは文字列で返すこともある。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def assign(client: Client, transcript: list[Line], candidates: list[str], policy: str = "", *,
           max_targets: int = 12, context_lines: int = 4,
           progress: Callable[[str], None] | None = None,
           cancelled: Callable[[], bool] | None = None) -> list[Outcome]:
    """1 キャラ分の対象の行に表情の候補を選ぶ。束は順に送る（並列にしても速くならないことが多い）。

    `cancelled` が True を返したら `Cancelled` を投げる（送っている途中の 1 本は待つ）。
    """
    if not candidates:
        raise ValueError("表情の候補がありません")
    endpoint = client.endpoint
    system = build_system_prompt(candidates, policy, endpoint.no_think, endpoint.include_reason)
    schema = build_schema(candidates, endpoint.include_reason)
    chunks = chunk(transcript, max_targets, context_lines)
    results: list[Outcome] = []
    for n, lines in enumerate(chunks, 1):
        if cancelled and cancelled():
            raise Cancelled()
        if progress:
            progress(f"{n}/{len(chunks)}")
        results += _ask(client, system, schema, lines, candidates, cancelled)
    return results


def _ask(client: Client, system: str, schema: dict, lines: list[Line], candidates: list[str],
         cancelled: Callable[[], bool] | None) -> list[Outcome]:
    targets = [line.id for line in lines if line.id is not None]
    try:
        outcomes = interpret(client.complete_json(system, build_user_prompt(lines), schema), targets, candidates)
    except LlmError as e:
        return interpret(None, targets, candidates, failure=e.detail())
    # まとめて送るとモデルによっては後半を落とす。落とした行だけを同じ文脈で一度だけ聞き直す
    missing = {o.id for o in outcomes if o.warning == NO_ANSWER}
    if not missing or len(missing) == len(targets) or (cancelled and cancelled()):
        return outcomes
    retry = [line if line.id in missing else replace(line, id=None) for line in lines]
    try:
        again = interpret(client.complete_json(system, build_user_prompt(retry), schema), sorted(missing), candidates)
    except LlmError:
        return outcomes
    better = {o.id: o for o in again if o.warning != NO_ANSWER}
    return [better.get(o.id, o) for o in outcomes]
