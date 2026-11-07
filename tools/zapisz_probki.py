# -*- coding: utf-8 -*-
"""
Zapisuje prawdziwe odpowiedzi usług GUGiK jako próbki do testów wtyczki.

Uruchomienie w QGIS: Wtyczki → Konsola Pythona → Pokaż edytor → otwórz ten plik → Uruchom
(albo w konsoli:  exec(open(r"<ścieżka>\\tools\\zapisz_probki.py", encoding="utf-8").read()) )

Wynik: tests/fixtures/live/*.gml / *.json + _podsumowanie.txt w folderze wtyczki.
Testy (tests/test_live_fixtures.py) sprawdzają na nich parser, m.in. czy
współrzędne po odczycie leżą w Polsce w EPSG:2180 (wykrywa zamienione osie).

Zapytania są małe (prostokąt ok. 300 × 300 m w Krakowie, pojedyncze jednostki),
cały skrypt trwa zwykle kilkadziesiąt sekund.
"""
import json
import os
import time
import traceback

try:
    PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() w konsoli – brak __file__
    from qgis.core import QgsApplication
    PLUGIN_DIR = os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins",
                              "pobierz_dane_GUGiK")

PKG = os.path.basename(PLUGIN_DIR)
_mod = __import__(f"{PKG}.core", fromlist=["catalog", "filters", "net", "wfs"])
catalog, filters, net = _mod.catalog, _mod.filters, _mod.net
WfsClient = __import__(f"{PKG}.core.wfs", fromlist=["WfsClient"]).WfsClient

OUT = os.path.join(PLUGIN_DIR, "tests", "fixtures", "live")
TERYT_POWIAT = "1261"           # Kraków
# Prostokąt w EPSG:2180 (E, N) – ok. 300 × 300 m w centrum Krakowa
BBOX = (566300, 244200, 566600, 244500)

WFS_SAMPLES = [
    "dzialki (EGIB)", "budynki (EGIB)",
    "dzialki (RCN)", "budynki (RCN)", "lokale (RCN)",
    "A07_Punkty_adresowe (PRG)", "A08_Ulice (PRG)",
    "M1_UrzedoweNazwyMiejscowosci (PRNG)",
]


def short(exc: Exception) -> str:
    """Błąd bez długiego adresu zapytania."""
    text = str(exc)
    if "Caused by" in text:
        text = text[text.index("Caused by"):]
    return text[:160]


def save(name: str, text: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main() -> None:
    lines = [f"Próbki zapisane {time.strftime('%Y-%m-%d %H:%M')}", ""]

    for label in WFS_SAMPLES:
        src = catalog.source_for_label(label)
        name = src.layer_name.lower() + ".gml"
        try:
            client = WfsClient(src.url, src.typename, src.geom_field)
            text = client.page(filters.bbox(src.geom_field, *BBOX), 0, 50)
            save(name, text)
            lines.append(f"OK    {label:40s} {len(text):>9} B  → {name}")
        except Exception as exc:
            lines.append(f"BŁĄD  {label:40s} {short(exc)}")

    # Granica PRG (gmina) i skorowidz ortofotomapy
    try:
        prg = __import__(f"{PKG}.prg_client", fromlist=["PRGClient"]).PRGClient()
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typenames": "ms:A02_Granice_powiatow",
                  "outputFormat": "application/gml+xml; version=3.2",
                  "filter": ('<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
                             'xmlns:ms="http://mapserver.gis.umn.edu/mapserver"><fes:PropertyIsEqualTo>'
                             '<fes:ValueReference>ms:JPT_KOD_JE</fes:ValueReference>'
                             f'<fes:Literal>{TERYT_POWIAT}</fes:Literal></fes:PropertyIsEqualTo></fes:Filter>')}
        text = net.get(prg.session, prg.url, params, timeout=60).text
        save("prg_powiat.gml", text)
        lines.append(f"OK    {'granica PRG ' + TERYT_POWIAT:40s} {len(text):>9} B  → prg_powiat.gml")
    except Exception as exc:
        lines.append(f"BŁĄD  granica PRG: {short(exc)}")

    try:
        svc = __import__(f"{PKG}.skorowidz_services", fromlist=["SKOROWIDZ_SERVICES"]).SKOROWIDZ_SERVICES[0]
        idx = __import__(f"{PKG}.wfs_index_client", fromlist=["WFSIndexClient"]).WFSIndexClient(svc.url)
        layer = (f"{svc.layer_name}{svc.year_range[0]}{svc.layer_suffix}"
                 if svc.year_range else svc.layer_name)
        text = idx.download(filters.bbox(idx.geom_field, *BBOX), layer)
        save("skorowidz.gml", text)
        lines.append(f"OK    {'skorowidz ' + layer:40s} {len(text):>9} B  → skorowidz.gml")
    except Exception as exc:
        lines.append(f"BŁĄD  skorowidz: {short(exc)}")

    # EZiUDP – API JSON
    try:
        session = net.make_session()
        url = "https://integracja.gugik.gov.pl/eziudp/api.php"
        resp = net.get(session, url, {"teryt": TERYT_POWIAT, "rodzaj": "powiaty",
                                      "usluga": "pobierania", "format": "json"}, timeout=60)
        save("eziudp_api.json", json.dumps(resp.json(), ensure_ascii=False, indent=1))
        lines.append(f"OK    {'EZiUDP API ' + TERYT_POWIAT:40s} {len(resp.content):>9} B  → eziudp_api.json")
    except Exception as exc:
        lines.append(f"BŁĄD  EZiUDP API: {short(exc)}")

    summary = "\n".join(lines)
    save("_podsumowanie.txt", summary)
    print(summary)
    print(f"\nFolder: {OUT}")


try:
    main()
except Exception:
    traceback.print_exc()
