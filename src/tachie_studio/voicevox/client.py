"""VOICEVOX エンジンの API（`/speakers`、`/audio_query`、`/synthesis`）を呼ぶ。

Blender 同梱 Python でも動くよう、requests は使わず urllib だけで書く。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:50021"


class VoicevoxError(RuntimeError):
    pass


class VoicevoxClient:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: Any = None) -> bytes:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise VoicevoxError(f"{method} {path} が {e.code} を返しました: {detail}") from e
        except urllib.error.URLError as e:
            raise VoicevoxError(f"VOICEVOX エンジンに接続できません（{self.base_url}）: {e.reason}") from e

    def version(self) -> str:
        return json.loads(self._request("GET", "/version"))

    def speakers(self) -> list[dict[str, Any]]:
        return json.loads(self._request("GET", "/speakers"))

    def styles(self) -> list[tuple[int, str]]:
        """(スタイル ID, 「話者名（スタイル名）」) の一覧。"""
        return [
            (style["id"], f"{speaker['name']}（{style['name']}）")
            for speaker in self.speakers()
            for style in speaker["styles"]
            if style.get("type", "talk") == "talk"
        ]

    def audio_query(self, text: str, speaker: int) -> dict[str, Any]:
        return json.loads(self._request("POST", "/audio_query", {"text": text, "speaker": speaker}))

    def synthesis(self, query: dict[str, Any], speaker: int) -> bytes:
        """wav のバイト列を返す。"""
        return self._request("POST", "/synthesis", {"speaker": speaker}, body=query)
