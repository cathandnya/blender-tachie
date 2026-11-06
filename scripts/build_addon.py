"""Blender 拡張機能の zip を作る（必要なら Blender にインストールする）。

    uv run python scripts/build_addon.py            # dist/blender_tachie-<version>-<OS>.zip（OS ごと）
    uv run python scripts/build_addon.py --install  # 作ったうえで、この OS 用を Blender の user_default リポジトリに入れる

blender_addon/ と src/tachie_studio/ を build/blender_tachie/ に集め、Blender 同梱 Python 用の wheel（`PACKAGES`）を
OS ごとに取ってきて manifest の platforms / wheels に書き、`blender --command extension build --split-platforms` で
OS ごとの zip に固める。Blender は環境変数 BLENDER、PATH の blender、OS ごとの標準の場所の順で探す。
"""

from __future__ import annotations

import argparse
import glob
import os
import platform
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
# Blender の platforms → Pillow の wheel の platform タグ（Blender の Linux 版は x64 のみ）
PLATFORMS = {
    "macos-arm64": "macosx_11_0_arm64",
    "macos-x64": "macosx_10_13_x86_64",
    "windows-x64": "win_amd64",
    "windows-arm64": "win_arm64",
    "linux-x64": "manylinux_2_28_x86_64",
}
# 同梱する wheel（パッケージ → Blender の platforms ごとの platform タグ。None はその OS 用に取ってこない）。
# Brotli は YMM4 の .ymmp に保存された音声（VoiceCache）を展開するため。macOS は arm64 で取れる universal2 の 1 つで
# x64 も兼ねる（x86_64 用も取ると x64 の zip に 2 つ入る）。Windows arm64 用は配布されていないので、その OS では
# YMM4 の音声を使わずに VOICEVOX で合成する
# BudouX は字幕を文節の切れ目で折り返すため。どの OS でも同じ wheel（py3-none-any）なので 1 つだけ取る
PACKAGES = {
    "pillow": PLATFORMS,
    "budoux": {"macos-arm64": "macosx_11_0_arm64"},
    "brotli": {**PLATFORMS, "macos-x64": None, "linux-x64": "manylinux_2_17_x86_64", "windows-arm64": None},
}


def find_blender() -> str:
    candidates = [os.environ.get("BLENDER"), shutil.which("blender"),
                  "/Applications/Blender.app/Contents/MacOS/Blender"]
    # Windows の標準のインストール先（新しい版を先に）
    candidates += sorted(glob.glob("C:/Program Files/Blender Foundation/Blender */blender.exe"), reverse=True,
                         key=lambda p: [int(n) for n in re.findall(r"\d+", Path(p).parent.name)])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("blender が見つかりません（環境変数 BLENDER に実行ファイルのパスを指定してください）")


def this_platform() -> str:
    """この OS の Blender の platforms 名（macos-arm64 など）。"""
    system = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(platform.system().lower())
    if system is None:
        return platform.system().lower() or "unknown"  # Blender の拡張が対応していない OS（どの zip にも当たらない）
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def download_wheels() -> list[Path]:
    wheels = []
    for package, platforms in PACKAGES.items():
        ver = version(package)
        for platform in filter(None, platforms.values()):
            # wheel の名前のタグは指定と違うことがある（macOS arm64 の Brotli は universal2）ので、パッケージ・OS ごとのフォルダに置く
            folder = WHEEL_CACHE / package / ver / platform
            found = list(folder.glob("*.whl"))
            if not found:
                folder.mkdir(parents=True, exist_ok=True)
                subprocess.run(["uvx", "pip", "download", f"{package}=={ver}", "--only-binary=:all:", "--no-deps",
                                "--python-version", PYTHON_VERSION, "--platform", platform, "-d", str(folder)],
                               check=True)
                found = list(folder.glob("*.whl"))
            if found[0] not in wheels:
                wheels.append(found[0])
    return wheels


def assemble(wheels: list[Path]) -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    shutil.copytree(ROOT / "blender_addon", BUILD, ignore=shutil.ignore_patterns("tachie_studio", "__pycache__", ".DS_Store"))
    shutil.copytree(ROOT / "src" / "tachie_studio", BUILD / "tachie_studio", ignore=ignore)
    shutil.copy2(ROOT / "LICENSE", BUILD / "LICENSE")
    (BUILD / "wheels").mkdir()
    for w in wheels:
        shutil.copy2(w, BUILD / "wheels" / w.name)

    manifest = BUILD / "blender_manifest.toml"
    entries = ", ".join(f'"./wheels/{w.name}"' for w in wheels)
    platforms = ", ".join(f'"{p}"' for p in PLATFORMS)
    text = re.sub(r"^wheels = \[.*?\]", f"wheels = [{entries}]", manifest.read_text(encoding="utf-8"),
                  flags=re.M | re.S)
    text = re.sub(r"^platforms = \[.*?\]", f"platforms = [{platforms}]", text, flags=re.M | re.S)
    manifest.write_text(text, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--install", action="store_true", help="Blender の user_default リポジトリにインストールする")
    args = p.parse_args()

    blender = find_blender()
    assemble(download_wheels())
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("blender_tachie-*.zip"):
        old.unlink()
    subprocess.run([blender, "--command", "extension", "build", "--split-platforms", "--source-dir", str(BUILD),
                    "--output-dir", str(DIST)], check=True)
    zips = sorted(DIST.glob("blender_tachie-*.zip"))
    for z in zips:
        print(z)
    mine = [z for z in zips if this_platform().replace("-", "_") in z.name]
    if args.install:
        if not mine:
            sys.exit(f"この OS（{this_platform()}）用の zip がありません")
        subprocess.run([blender, "--command", "extension", "install-file", "-r", "user_default", "-e",
                        str(mine[0])], check=True)
    elif mine:
        print(mine[0])  # 最後の行はこの OS 用（scripts/smoke.sh が使う）


if __name__ == "__main__":
    main()
