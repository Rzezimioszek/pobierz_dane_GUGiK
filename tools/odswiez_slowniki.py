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

try:
    PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() w konsoli QGIS – brak __file__
    from qgis.core import QgsApplication
    PLUGIN_DIR = os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins",
                              "pobierz_dane_GUGiK")

OUT = os.path.join(PLUGIN_DIR, "data", "jednostki.json.gz")
PRG_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
PAGE = 5000
LEVELS = [   # (klucz w pliku, warstwa PRG)
    ("wojewodztwa", "ms:A01_Granice_wojewodztw"),
    ("powiaty", "ms:A02_Granice_powiatow"),
    ("gminy", "ms:A03_Granice_gmin"),
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
        elif a and abs(b - a) / a > MAX_CHANGE:
            problems.append(f"{level}: {a} → {b}")
    return problems


def main(argv=None, url: str = PRG_URL, out: str = OUT, log=print) -> int:
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
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


if __name__ == "__main__":
    _code = main([a for a in sys.argv[1:] if a.startswith("--")])
    if "qgis.utils" not in sys.modules:     # wiersz poleceń; w konsoli QGIS bez sys.exit
        sys.exit(_code)
