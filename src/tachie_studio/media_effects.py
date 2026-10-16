"""YMM4 の映像エフェクトのうち、画像に焼き込めるもの（Blender 非依存、Pillow のみ）。"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageFilter, ImageMorph

_DILATE = {name: ImageMorph.MorphOp(op_name=name) for name in ("dilation4", "dilation8")}


def _dilate(mask: Image.Image, radius: int, angular: bool) -> Image.Image:
    """2 値のマスク（L）を `radius` ピクセル太らせる。丸い角は 4 近傍と 8 近傍を交互にして八角形で近づける。"""
    for i in range(radius):
        op = _DILATE["dilation8" if angular or i % 2 else "dilation4"]
        _, mask = op.apply(mask)
    return mask


def outline_image(src: Path, out_dir: Path, thickness: float, color: tuple, *, angular: bool = False,
                  blur: float = 0.0, only: bool = False) -> Path:
    """画像の外側に縁取りを付けた PNG を `out_dir` に書いて、そのパスを返す（同じ中身なら書き直さない）。
    太さは元の画像のピクセル（YMM4 の縁取りと同じく、このあと拡大縮小される）。画像は太さの分だけ大きくなる。"""
    t = max(1, round(thickness))
    stat = src.stat()
    key = hashlib.sha1(repr((str(src), stat.st_size, stat.st_mtime_ns, t, tuple(color), angular, blur, only))
                       .encode()).hexdigest()[:12]
    dst = out_dir / f"{src.stem}_outline_{key}.png"
    if dst.is_file():
        return dst
    image = Image.open(src).convert("RGBA")
    pad = t + math.ceil(blur * 2) + 1  # 太らせる処理は画像の一番外側のピクセルを扱わないので 1 つ余分に
    canvas = Image.new("RGBA", (image.width + pad * 2, image.height + pad * 2))
    canvas.paste(image, (pad, pad))
    alpha = canvas.getchannel("A").point(lambda v: 255 if v > 0 else 0)
    mask = _dilate(alpha, t, angular)
    if blur > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    r, g, b, a = (round(c * 255) for c in color)
    stroke = Image.new("RGBA", canvas.size, (r, g, b, 0))
    stroke.putalpha(mask.point(lambda v: v * a // 255))
    if not only:
        stroke.alpha_composite(canvas)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.png")
    stroke.save(tmp)
    tmp.replace(dst)
    return dst
