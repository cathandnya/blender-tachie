"""ランダム間隔のまばたき。"""

from __future__ import annotations

import random


def blink_track(n_frames: int, fps: float, n_images: int, *, seed: int | None = None,
                interval: tuple[float, float] = (2.0, 6.0), step: float = 1 / 30,
                double_rate: float = 0.15) -> list[int]:
    """各フレームで使う目の画像の添字（`Part.frames` の添字）を返す。

    待機中は最後の画像（数字なし = 開き）。まばたきは `.0 → .1 → … → .N` をファイル順に
    1 枚 `step` 秒ずつ見せて開きに戻る。公式仕様（.0 = 閉じ）でも、
    `.0` が開きで後ろの番号が閉じの素材でも、この再生順ならまばたきに見える。
    `double_rate` の確率で続けてもう 1 回まばたきする。
    """
    open_index = n_images - 1
    out = [open_index] * n_frames
    if n_images <= 1:
        return out

    rng = random.Random(seed)
    hold = max(1, round(step * fps))
    sequence = [i for i in range(open_index) for _ in range(hold)]

    t = rng.uniform(*interval) * fps
    doubled = False
    while t < n_frames:
        f = int(t)
        for k, index in enumerate(sequence):
            if f + k < n_frames:
                out[f + k] = index
        if not doubled and rng.random() < double_rate:
            t = f + len(sequence) + max(1, round(0.12 * fps))
            doubled = True
        else:
            t = f + len(sequence) + rng.uniform(*interval) * fps
            doubled = False
    return out
