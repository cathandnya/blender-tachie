"""Blender 拡張機能の zip を作る（必要なら Blender にインストールする）。

    uv run python scripts/build_addon.py            # dist/blender_tachie-<version>.zip
    uv run python scripts/build_addon.py --install  # 作ったうえで Blender の user_default リポジトリに入れる

blender_addon/ と src/tachie_studio/ を build/blender_tachie/ に集め、Blender 同梱 Python 用の Pillow の wheel を
取ってきて manifest の wheels に書き、`blender --command extension build` で固める。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "blender_tachie"
DIST = ROOT / "dist"
WHEEL_CACHE = ROOT / "build" / "wheels"

# Blender 5.2 同梱 Python は 3.13
PYTHON_VERSION = "3.13"
PLATFORMS = {"macos-arm64": "macosx_11_0_arm64"}


def find_blender() -> str:
    for candidate in (shutil.which("blender"), "/Applications/Blender.app/Contents/MacOS/Blender"):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("blender が見つかりません")


def download_wheels() -> list[Path]:
    pillow = version("pillow")
    wheels = []
    for platform in PLATFORMS.values():
        WHEEL_CACHE.mkdir(parents=True, exist_ok=True)
        found = list(WHEEL_CACHE.glob(f"pillow-{pillow}-cp{PYTHON_VERSION.replace('.', '')}-*{platform}*.whl"))
        if not found:
            subprocess.run(["uvx", "pip", "download", f"pillow=={pillow}", "--only-binary=:all:", "--no-deps",
                            "--python-version", PYTHON_VERSION, "--platform", platform, "-d", str(WHEEL_CACHE)],
                           check=True)
            found = list(WHEEL_CACHE.glob(f"pillow-{pillow}-*{platform}*.whl"))
        wheels.append(found[0])
    return wheels


def assemble(wheels: list[Path]) -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    shutil.copytree(ROOT / "blender_addon", BUILD, ignore=shutil.ignore_patterns("tachie_studio", "__pycache__", ".DS_Store"))
    shutil.copytree(ROOT / "src" / "tachie_studio", BUILD / "tachie_studio", ignore=ignore)
    (BUILD / "wheels").mkdir()
    for w in wheels:
        shutil.copy2(w, BUILD / "wheels" / w.name)

    manifest = BUILD / "blender_manifest.toml"
    entries = ", ".join(f'"./wheels/{w.name}"' for w in wheels)
    text = re.sub(r"^wheels = \[.*?\]", f"wheels = [{entries}]", manifest.read_text(encoding="utf-8"),
                  flags=re.M | re.S)
    manifest.write_text(text, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--install", action="store_true", help="Blender の user_default リポジトリにインストールする")
    args = p.parse_args()

    blender = find_blender()
    assemble(download_wheels())
    DIST.mkdir(exist_ok=True)
    subprocess.run([blender, "--command", "extension", "build", "--source-dir", str(BUILD),
                    "--output-dir", str(DIST)], check=True)
    zips = sorted(DIST.glob("blender_tachie-*.zip"), key=lambda p: p.stat().st_mtime)
    print(zips[-1])
    if args.install:
        subprocess.run([blender, "--command", "extension", "install-file", "-r", "user_default", "-e",
                        str(zips[-1])], check=True)


if __name__ == "__main__":
    main()
