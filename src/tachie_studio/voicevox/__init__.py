"""VOICEVOX エンジンの HTTP クライアント（Blender 非依存、標準ライブラリのみ）。"""

from .client import DEFAULT_URL, VoicevoxClient, VoicevoxError

__all__ = ["DEFAULT_URL", "VoicevoxClient", "VoicevoxError"]
