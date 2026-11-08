# -*- coding: utf-8 -*-
"""
Odświeża słownik jednostek (województwa, powiaty, gminy, obręby) z PRG
przed wydaniem wtyczki → data/jednostki.json.gz.

Uruchomienie:
  • w QGIS: Wtyczki → Konsola Pythona → Pokaż edytor → otwórz ten plik → Uruchom
  • albo z wiersza poleceń:  python tools/odswiez_slowniki.py  [--force]

Pobierane są tylko kody i nazwy (bez geometrii), stronami po 5000.
Skrypt nie nadpisze słownika, gdy którejś warstwy przybyło lub ubyło
podejrzanie dużo (np. przerwane pobieranie) – wtedy --force.
Na końcu wypisuje zmiany względem poprzedniego słownika (do changelogu).
"""
import datetime
import gzip
import json
import os
import sys
import xml.etree.ElementTree as ET  # nosec – odpowiedź usługi GUGiK, bez DTD

import requests

def _plugin_dir() -> str:
    """
    Folder wtyczki: obok tego pliku, a gdy skrypt uruchomiono z edytora/konsoli
    QGIS (bez __file__ albo z kopii w folderze tymczasowym) – w profilu QGIS.
    """
    candidates = []
    try:
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except NameError:
        pass
    try:
        from qgis.core import QgsApplication
        candidates.append(os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins",
                                       "pobierz_dane_GUGiK"))
    except ImportError:
        pass
    for c in candidates:
        if os.path.isfile(os.path.join(c, "metadata.txt")) and os.path.isdir(os.path.join(c, "data")):
            return c
    raise RuntimeError("Nie znaleziono folderu wtyczki (metadata.txt + data/): " + ", ".join(candidates))


PLUGIN_DIR = _plugin_dir()

OUT = os.path.join(PLUGIN_DIR, "data", "jednostki.json.gz")
PRG_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
PAGE = 5000
LEVELS = [   # (klucz w pliku, warstwa PRG)
    ("wojewodztwa", "ms:A01_Granice_wojewodztw"),
    ("powiaty", "ms:A02_Granice_powiatow"),
    ("gminy", "ms:A03_Granice_gmin"),
    ("jednostki", "ms:A05_Granice_jednostek_ewidencyjnych"),
    ("obreby", "ms:A06_Granice_obrebow_ewidencyjnych"),
]
MAX_CHANGE = 0.10    # ±10% rekordów względem poprzedniego słownika = podejrzane


def normalize(level: str, code: str) -> str:
    """Kody jak w panelu: gmina 7 cyfr → '146501_1'; obręb bez zmian ('146501_1.0001')."""
    code = (code or "").strip()
    if level == "gminy" and len(code) == 7 and code.isdigit():
        return f"{code[:6]}_{code[6]}"
    return code


def parse_page(xml: bytes) -> list:
    """[(kod, nazwa)] z odpowiedzi GetFeature (atrybuty JPT_KOD_JE, JPT_NAZWA_)."""
    root = ET.fromstring(xml)  # nosec
    if root.tag.endswith("ExceptionReport"):
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise RuntimeError(f"Serwer zwrócił błąd: {text[:300]}")
    out = []
    for member in root:
        if not member.tag.endswith("member"):
            continue
        for feat in member:
            vals = {child.tag.split("}")[-1]: (child.text or "").strip() for child in feat}
            out.append((vals.get("JPT_KOD_JE", ""), vals.get("JPT_NAZWA_", "")))
    return out


def fetch_level(session, url: str, level: str, typename: str, log=print) -> dict:
    result, start = {}, 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typenames": typename, "propertyName": "ms:JPT_KOD_JE,ms:JPT_NAZWA_",
                  "count": PAGE, "startIndex": start}
        resp = session.get(url, params=params, timeout=300)
        resp.raise_for_status()
        rows = parse_page(resp.content)
        for code, name in rows:
            code = normalize(level, code)
            if code and name:
                result[code] = name
        log(f"  {level}: {start + len(rows)} rekordów")
        if len(rows) < PAGE:
            return result
        start += PAGE


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def diff(old: dict, new: dict) -> list:
    """Opis zmian per poziom: nowe, usunięte, zmienione nazwy (pierwsze przykłady)."""
    lines = []
    for level, _ in LEVELS:
        a, b = old.get(level, {}), new.get(level, {})
        added = sorted(set(b) - set(a))
        removed = sorted(set(a) - set(b))
        renamed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        lines.append(f"{level}: {len(b)} (nowe {len(added)}, usunięte {len(removed)}, "
                     f"zmienione nazwy {len(renamed)})")
        for k in added[:5]:
            lines.append(f"   + {k} {b[k]}")
        for k in removed[:5]:
            lines.append(f"   - {k} {a[k]}")
        for k in renamed[:5]:
            lines.append(f"   ~ {k} {a[k]} → {b[k]}")
    return lines


def suspicious(old: dict, new: dict) -> list:
    problems = []
    for level, _ in LEVELS:
        a, b = len(old.get(level, {})), len(new.get(level, {}))
        if not b:
            problems.append(f"{level}: pusto")
        elif not a:
            continue                       # poziom nowy w słowniku (np. jednostki)
        elif a and abs(b - a) / a > MAX_CHANGE:
            problems.append(f"{level}: {a} → {b}")
    return problems


def coverage(data: dict) -> list:
    """Gminy, dla których słownik nie ma żadnego obrębu (reguła jak w panelu)."""
    jedn = {t.split(".")[0] for t in data.get("obreby", {})}
    missing = []
    for g in data.get("gminy", {}):
        base, kind = g[:6], g[7:]
        cand = {f"{base}_4", f"{base}_5"} if kind == "3" else {g}
        if kind == "1":
            cand |= {j for j in jedn if j[:4] == g[:4] and j[7:] in ("8", "9")}
        if not cand & jedn:
            missing.append(g)
    return missing


def main(argv=None, url: str = PRG_URL, out: str = OUT, log=print) -> int:
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    log(f"Słownik: {out}")
    session = requests.Session()
    new = {"zrodlo": "PRG – " + url, "data": datetime.date.today().isoformat()}
    try:
        for level, typename in LEVELS:
            new[level] = fetch_level(session, url, level, typename, log)
    except Exception as exc:
        log(f"BŁĄD pobierania: {exc}")
        return 1
    finally:
        session.close()

    old = load(out)
    problems = suspicious(old, new)
    missing = coverage(new)
    if missing:
        problems.append(f"gminy bez obrębów: {len(missing)} (np. {', '.join(missing[:5])})")
    if problems and not force:
        log("NIE zapisano – podejrzane liczby rekordów: " + "; ".join(problems)
            + ". Sprawdź i uruchom z --force.")
        return 2
    tmp = out + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, out)
    log(f"Zapisano {out} ({os.path.getsize(out) // 1024} KB), stan z {new['data']}")
    for line in diff(old, new):
        log(line)
    return 0


if __name__ != "odswiez_slowniki":           # nie przy imporcie w testach
    # Edytor/konsola QGIS uruchamia plik różnie (exec, __name__ ≠ "__main__"),
    # więc skrypt startuje zawsze, a sys.exit tylko w wierszu poleceń.
    _code = main([a for a in sys.argv[1:] if a.startswith("--")])
    if "qgis.utils" not in sys.modules:
        sys.exit(_code)
