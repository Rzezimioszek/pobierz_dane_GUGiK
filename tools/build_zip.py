# -*- coding: utf-8 -*-
"""
Pakuje wtyczkę do ZIP dla repozytorium QGIS (bez testów, narzędzi i kopii).

    python tools/build_zip.py            → dist/pobierz_dane_GUGiK-<wersja>.zip
"""
import configparser
import os
import subprocess  # nosec B404 – tylko git ls-files
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = os.path.basename(ROOT)
SKIP_DIRS = {"tests", "tools", "docs", "dist", "__pycache__", ".git", ".github", ".pytest_cache", "_sync"}
SKIP_FILES = {".gitignore", "pylintrc", ".flake8"}


def _files() -> list:
    """
    Pliki do paczki: tylko te śledzone przez git (bez przypadkowych folderów w katalogu
    wtyczki, np. wyników innych narzędzi); bez gita – przegląd folderu z listą wykluczeń.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,  # nosec B603 B607
                             check=True).stdout.decode("utf-8").split("\0")
        tracked = [os.path.join(ROOT, p) for p in out if p]
    except (OSError, subprocess.CalledProcessError):
        tracked = [os.path.join(folder, f) for folder, dirs, files in os.walk(ROOT)
                   if not (set(os.path.relpath(folder, ROOT).split(os.sep)) & SKIP_DIRS) for f in files]
    keep = []
    for path in tracked:
        rel = os.path.relpath(path, ROOT)
        parts = rel.split(os.sep)
        f = parts[-1]
        if set(parts[:-1]) & SKIP_DIRS or f in SKIP_FILES or f.startswith("_backup") \
                or f.endswith((".pyc", ".ts")) or not os.path.isfile(path):
            continue
        keep.append(path)
    return sorted(keep)


def main():
    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    target = os.path.join(ROOT, "dist", f"{NAME}-{version}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in _files():
            info = zipfile.ZipInfo.from_file(path, os.path.join(NAME, os.path.relpath(path, ROOT)))
            info.external_attr = 0o644 << 16          # bez bitu wykonywania (skan repo QGIS)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as src:
                z.writestr(info, src.read())
    print(target)


if __name__ == "__main__":
    main()
