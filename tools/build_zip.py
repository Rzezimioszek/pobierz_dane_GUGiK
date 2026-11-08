# -*- coding: utf-8 -*-
"""
Pakuje wtyczkę do ZIP dla repozytorium QGIS (bez testów, narzędzi i kopii).

    python tools/build_zip.py            → dist/pobierz_dane_GUGiK-<wersja>.zip
"""
import configparser
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = os.path.basename(ROOT)
SKIP_DIRS = {"tests", "tools", "docs", "dist", "__pycache__", ".git", ".github", ".pytest_cache", "_sync"}
SKIP_FILES = {".gitignore", "pylintrc", ".flake8"}


def main():
    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    target = os.path.join(ROOT, "dist", f"{NAME}-{version}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f in SKIP_FILES or f.startswith("_backup") or f.endswith(".pyc"):
                    continue
                path = os.path.join(folder, f)
                info = zipfile.ZipInfo.from_file(path, os.path.join(NAME, os.path.relpath(path, ROOT)))
                info.external_attr = 0o644 << 16          # bez bitu wykonywania (skan repo QGIS)
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as src:
                    z.writestr(info, src.read())
    print(target)


if __name__ == "__main__":
    main()
