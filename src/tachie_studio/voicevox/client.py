"""VOICEVOX エンジンの API（`/speakers`、`/audio_query`、`/accent_phrases`、`/synthesis`、`/user_dict*`）を呼ぶ。

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
            try:  # {"detail": {"text": "判別できない読み仮名があります: …"}} なら text だけ
                parsed = json.loads(detail).get("detail")
                detail = parsed.get("text", detail) if isinstance(parsed, dict) else (parsed or detail)
            except (ValueError, AttributeError):
                pass
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

    def accent_phrases(self, text: str, speaker: int, is_kana: bool = False) -> list[dict[str, Any]]:
        """`is_kana` なら `text` は AquesTalk 風記法（`キョ'ウワ/イ'イ/テ'ンキ`）。"""
        params = {"text": text, "speaker": speaker, "is_kana": "true" if is_kana else "false"}
        return json.loads(self._request("POST", "/accent_phrases", params))

    def user_dict(self) -> dict[str, dict[str, Any]]:
        """{単語の UUID: {surface, pronunciation, accent_type, priority, …}}"""
        return json.loads(self._request("GET", "/user_dict"))

    def add_user_dict_word(self, surface: str, pronunciation: str, accent_type: int, priority: int | None = None) -> str:
        """単語を登録して UUID を返す。`pronunciation` はカタカナ、`accent_type` は音が下がる直前のモーラ位置（0 は平板）。"""
        params: dict[str, Any] = {"surface": surface, "pronunciation": pronunciation, "accent_type": accent_type}
        if priority is not None:
            params["priority"] = priority
        return json.loads(self._request("POST", "/user_dict_word", params))

    def delete_user_dict_word(self, word_uuid: str) -> None:
        self._request("DELETE", f"/user_dict_word/{urllib.parse.quote(word_uuid)}")

    def synthesis(self, query: dict[str, Any], speaker: int) -> bytes:
        """wav のバイト列を返す。"""
        return self._request("POST", "/synthesis", {"speaker": speaker}, body=query)
