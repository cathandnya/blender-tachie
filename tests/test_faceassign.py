import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tachie_studio import faceassign as fa
from tachie_studio.faceassign import Client, Endpoint, Line


class FakeLlm:
    """OpenAI 互換の偽のサーバ。`reply(body) -> (status, content 文字列 or 応答本文)` で返す中身を決める。"""

    def __init__(self, reply):
        self.reply = reply
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body: str):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                self._send(200, json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if body.get("max_tokens") == 1:  # 暖機
                    return self._send(200, json.dumps({"choices": [{"message": {"content": "."}}]}))
                status, content = outer.reply(body)
                if status != 200:
                    return self._send(status, content)
                self._send(200, json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def chats(self):
        return [r for r in self.requests if r["path"].endswith("/chat/completions") and r["body"].get("max_tokens") != 1]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def llm():
    servers = []

    def make(reply):
        s = FakeLlm(reply)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def ids_in(body) -> list[int]:
    user = body["messages"][1]["content"]
    return [int(x) for x in __import__("re").findall(r"#(\d+)\]", user)]


def test_urls():
    assert Endpoint("http://127.0.0.1:1234").chat_url() == "http://127.0.0.1:1234/v1/chat/completions"
    assert Endpoint("http://127.0.0.1:1234/v1/").models_url() == "http://127.0.0.1:1234/v1/models"
    gemini = "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert Endpoint(gemini).chat_url() == gemini.rstrip("/") + "/chat/completions"
    assert Endpoint("127.0.0.1:11434").base() == "127.0.0.1:11434/v1"


def test_prompt_and_schema():
    system = fa.build_system_prompt(["基本", "笑顔"], "明るい作品", no_think=True, include_reason=False)
    assert "0: 基本\n1: 笑顔\n-1: どの候補も合わない" in system
    assert "作品の方針:\n明るい作品" in system
    assert system.endswith("/no_think") and '"r"' not in system
    schema = fa.build_schema(["基本", "笑顔"], include_reason=True)
    item = schema["properties"]["a"]["items"]
    assert item["properties"]["p"]["enum"] == [-1, 0, 1]
    assert item["required"] == ["i", "p", "r"]
    user = fa.build_user_prompt([Line(None, "めたん", "前の\nセリフ"), Line(3, "ずんだもん", "やったのだ")])
    assert user == "[めたん] 前の セリフ\n[ずんだもん #3] やったのだ\n"


def test_chunk_context():
    t = [Line(i if i in (5, 6, 20) else None, "z" if i in (5, 6, 20) else "m", f"s{i}") for i in range(25)]
    chunks = fa.chunk(t, max_targets=12, context_lines=4)
    assert len(chunks) == 1
    texts = [line.text for line in chunks[0]]
    # 手前 4 行 + 5 + 6 + 20 の直前 1 行 + 20
    assert texts == ["s1", "s2", "s3", "s4", "s5", "s6", "s19", "s20"]
    assert [line.id for line in chunks[0]] == [None] * 4 + [5, 6, None, 20]
    # 束を分けると、手前の文脈にある前の束の対象は ID を外す
    chunks = fa.chunk(t, max_targets=1, context_lines=2)
    assert [[line.id for line in c] for c in chunks] == [[None, None, 5], [None, None, 6], [None, None, 20]]
    assert [line.text for line in chunks[1]] == ["s4", "s5", "s6"]


def test_sanitize():
    assert fa.sanitize('<think>考え中 {x}</think>\n```json\n{"a": []}\n```') == '{"a": []}'
    assert fa.sanitize('はい: {"a": [1]} です') == '{"a": [1]}'


def test_interpret():
    out = fa.interpret({"a": [{"i": 1, "p": 1, "r": "嬉しい"}, {"i": 2, "p": -1}, {"i": 3, "p": 9},
                              {"i": "4", "p": "0"}, {"i": 99, "p": 0}, {"i": 1, "p": 0}]},
                       [1, 2, 3, 4, 5], ["基本", "笑顔"])
    assert out == [fa.Outcome(1, "笑顔", "嬉しい"), fa.Outcome(2, None), fa.Outcome(3, None, "", "候補に無い番号を返しました: 9"),
                   fa.Outcome(4, "基本"), fa.Outcome(5, None, "", fa.NO_ANSWER)]


def test_assign_retries_missing(llm):
    def reply(body):
        ids = ids_in(body)
        # 1 回目は最後の行を落とす
        answer = ids[:-1] if len(ids) > 1 else ids
        return 200, json.dumps({"a": [{"i": i, "p": 1} for i in answer]})

    server = llm(reply)
    client = Client(Endpoint(server.url, model="m", api_key="k"))
    assert client.warm_up()
    transcript = [Line(None, "めたん", "こんにちは"), Line(0, "ずんだもん", "やったのだ"), Line(1, "ずんだもん", "うれしいのだ")]
    out = fa.assign(client, transcript, ["基本", "笑顔"])
    assert [o.preset for o in out] == ["笑顔", "笑顔"]
    chats = server.chats()
    assert len(chats) == 2 and ids_in(chats[1]["body"]) == [1]
    body = chats[0]["body"]
    assert body["response_format"]["type"] == "json_schema" and body["reasoning_effort"] == "low"
    assert server.requests[0]["auth"] == "Bearer k"
    assert client.list_models() == ["m1", "m2"]


def test_assign_without_schema_support(llm):
    def reply(body):
        if "response_format" in body:
            return 400, json.dumps({"error": "response_format json_schema is not supported"})
        return 200, "<think>…</think>```json\n" + json.dumps({"a": [{"i": 0, "p": 0}]}) + "\n```"

    server = llm(reply)
    out = fa.assign(Client(Endpoint(server.url)), [Line(0, "z", "はい")], ["基本"])
    assert out == [fa.Outcome(0, "基本")]
    assert len(server.chats()) == 3  # enum のスキーマ → 範囲のスキーマ → スキーマ無し


def test_assign_with_range_schema(llm):
    """整数の enum を断るサーバ（apfel: Apple の端末内モデル）には、番号を範囲で縛ったスキーマで送り直す。"""
    def reply(body):
        schema = body.get("response_format", {}).get("json_schema", {}).get("schema")
        p = schema["properties"]["a"]["items"]["properties"]["p"] if schema else None
        if p is None or "enum" in p:
            return 400, json.dumps({"error": {"message": 'Invalid response_format.json_schema: "enum" at '
                                              '#/properties/a/items/properties/p cannot be enforced'}})
        assert (p["minimum"], p["maximum"]) == (-1, 1)
        return 200, json.dumps({"a": [{"i": 0, "p": 1}]})

    server = llm(reply)
    client = Client(Endpoint(server.url))
    out = fa.assign(client, [Line(0, "z", "はい")], ["基本", "笑顔"])
    assert out == [fa.Outcome(0, "笑顔")]
    assert len(server.chats()) == 2
    fa.assign(client, [Line(0, "z", "はい")], ["基本", "笑顔"])
    assert len(server.chats()) == 3  # 断られた enum のスキーマは次から送らない


def test_range_schema():
    schema = fa.build_schema(["基本", "笑顔", "怒り"], include_reason=False)
    p = fa.range_schema(schema)["properties"]["a"]["items"]["properties"]["p"]
    assert p == {"type": "integer", "minimum": -1, "maximum": 2}
    assert "enum" in schema["properties"]["a"]["items"]["properties"]["p"]  # 元のスキーマは変えない


def test_assign_failure_keeps_lines(llm):
    server = llm(lambda body: (500, "boom"))
    out = fa.assign(Client(Endpoint(server.url)), [Line(0, "z", "はい"), Line(1, "z", "いいえ")], ["基本"])
    assert [o.preset for o in out] == [None, None]
    assert "500" in out[0].warning and "boom" in out[0].warning


def test_assign_cancel(llm):
    server = llm(lambda body: (200, json.dumps({"a": []})))
    with pytest.raises(fa.Cancelled):
        fa.assign(Client(Endpoint(server.url)), [Line(0, "z", "はい")], ["基本"], cancelled=lambda: True)


def test_connection_error():
    with pytest.raises(fa.LlmError, match="接続できません"):
        Client(Endpoint("http://127.0.0.1:9", timeout=2)).list_models()


def test_bad_url_is_llm_error():
    # 送る側（_post）でも URL の誤りは LlmError にする（行ごとの失敗として結果に出し、例外で止めない）
    for url in ("", "http://127.0.0.1:abc"):
        with pytest.raises(fa.LlmError):
            Client(Endpoint(url, timeout=2)).complete_json("s", "u", None)
        out = fa.assign(Client(Endpoint(url, timeout=2)), [Line(0, "z", "はい")], ["基本"])
        assert out[0].preset is None and out[0].warning
