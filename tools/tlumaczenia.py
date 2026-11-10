# -*- coding: utf-8 -*-
"""
Tłumaczenie angielskie wtyczki.

  python tools/tlumaczenia.py            zbiera napisy z kodu, uzupełnia i18n/PD_GUGiK_en.ts
                                         i buduje i18n/PD_GUGiK_en.qm (lrelease)
  python tools/tlumaczenia.py --check    tylko sprawdza: kod 1 = brakuje tłumaczeń

Napisy do tłumaczenia to argumenty tr("…") i mark("…") – zawsze zwykły tekst
(bez f-stringów; wartości wstawia się przez .format albo dokleja).
Angielskie odpowiedniki wpisuje się w pliku .ts (Qt Linguist albo edytor tekstu):
wpisy bez tłumaczenia mają type="unfinished".
"""
import ast
import os
import shutil
import subprocess  # nosec B404 – tylko lrelease z Qt
import sys
import xml.etree.ElementTree as ET  # nosec B405 – własny plik .ts
from xml.sax.saxutils import escape  # nosec B406

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT = "PobierzDaneGUGiK"
TS = os.path.join(ROOT, "i18n", "PD_GUGiK_en.ts")
QM = os.path.join(ROOT, "i18n", "PD_GUGiK_en.qm")
SKIP_DIRS = {"tests", "tools", "docs", "dist", "__pycache__", ".git", "i18n"}
FUNCS = {"tr", "mark"}


def collect(root: str = ROOT) -> tuple:
    """({napis: [plik:linia, …]}, [błędy]) – wszystkie tr("…") i mark("…") w kodzie wtyczki."""
    found, problems = {}, []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if func not in FUNCS or len(node.args) != 1:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.strip():
                        found.setdefault(arg.value, []).append(f"{rel}:{node.lineno}")
                elif isinstance(arg, ast.JoinedStr) or func == "mark":
                    problems.append(f"{rel}:{node.lineno}: {func}() wymaga zwykłego napisu (bez f-stringa)")
    return found, problems


def read_ts(path: str = TS) -> dict:
    """{napis: tłumaczenie} z pliku .ts (puste, gdy nieprzetłumaczone)."""
    if not os.path.exists(path):
        return {}
    out = {}
    for msg in ET.parse(path).getroot().iter("message"):  # nosec B314
        src, tra = msg.find("source"), msg.find("translation")
        if src is not None and src.text:
            out[src.text] = (tra.text or "") if tra is not None else ""
    return out


def write_ts(strings: dict, translations: dict, path: str = TS) -> None:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<!DOCTYPE TS>",
             '<TS version="2.1" language="en" sourcelanguage="pl">', "<context>",
             f"    <name>{CONTEXT}</name>"]
    for text in sorted(strings):
        lines.append("    <message>")
        for place in strings[text][:3]:
            fname, line = place.rsplit(":", 1)
            lines.append(f'        <location filename="../{fname}" line="{line}"/>')
        lines.append(f"        <source>{escape(text)}</source>")
        tra = translations.get(text, "")
        lines.append(f"        <translation>{escape(tra)}</translation>" if tra
                     else '        <translation type="unfinished"></translation>')
        lines.append("    </message>")
    lines += ["</context>", "</TS>", ""]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def lrelease() -> str:
    for exe in ("lrelease", "lrelease6", "lrelease-qt5", "lrelease-qt6"):
        if shutil.which(exe):
            return exe
    return ""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    strings, problems = collect()
    translations = read_ts()
    missing = sorted(s for s in strings if not translations.get(s))
    for p in problems:
        print("BŁĄD", p)
    if "--check" in argv:
        for s in missing:
            print("brak tłumaczenia:", repr(s), strings[s][0])
        return 1 if (missing or problems) else 0
    write_ts(strings, translations)
    print(f"{TS}: {len(strings)} napisów, bez tłumaczenia: {len(missing)}")
    for s in missing:
        print("  ", repr(s), strings[s][0])
    exe = lrelease()
    if not exe:
        print("Nie znaleziono lrelease (Qt Linguist) – plik .qm nie został zbudowany.")
        return 2
    subprocess.run([exe, "-silent", TS, "-qm", QM], check=True)  # nosec B603
    print(f"{QM}: {os.path.getsize(QM) // 1024} KB")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
