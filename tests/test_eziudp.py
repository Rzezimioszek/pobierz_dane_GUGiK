# -*- coding: utf-8 -*-
"""Odczyt adresów usług z odpowiedzi EZiUDP (dawny i zmieniony układ strony)."""
import importlib

from conftest import PLUGIN_NAME


def client():
    return importlib.import_module(f"{PLUGIN_NAME}.eziudp_client").EziudpClient()


OLD = """<table><tr class="row"><td>PL.1</td><td>Starosta</td><td>BDSOG</td><td>1261</td><td>WMS, WFS</td>
<td><a href="https://ows.example.pl/wms?service=WMS&amp;request=GetCapabilities">WMS</a></td>
<td><a href="https://ows.example.pl/bdsog/wfs?service=WFS&amp;request=GetCapabilities">WFS</a></td></tr></table>"""

NEW = """<div><table class="table"><thead><tr><th>TERYT</th><th>Usługi</th></tr></thead>
<tbody><tr><td>1261</td><td><a href="https://x.pl/geoserver/wms?SERVICE=WMS">podgląd</a>
<a href="https://x.pl/geoserver/ows?service=WFS&request=GetCapabilities&version=2.0.0">pobieranie (WFS)</a>
</td></tr></tbody></table></div>"""


def test_old_layout():
    recs = client()._parse_html(OLD)
    assert [r.url_wfs for r in recs] == ["https://ows.example.pl/bdsog/wfs"]
    assert recs[0].teryt == "1261"


def test_changed_layout_found_by_links():
    recs = [r for r in client()._parse_html(NEW) if r.url_wfs]
    assert len(recs) == 1
    assert recs[0].url_wfs == "https://x.pl/geoserver/ows"
    assert recs[0].url_wms == "https://x.pl/geoserver/wms"
    assert recs[0].teryt == "1261"


def test_no_services():
    assert [r for r in client()._parse_html("<html><p>Brak danych</p></html>") if r.url_wfs] == []


def test_rcn_layers_like_collective_rcn():
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    keys = {e["key"]: e for e in task.AVAILABLE_LAYERS}
    assert "rcn_tran" not in keys
    assert {keys[k]["layer_name"] for k in ("rcn_dzi", "rcn_bud", "rcn_lok")} == \
        {"Dzialki_RCN", "Budynki_RCN", "Lokale_RCN"}


# ---------------------------------------------------------------- API JSON

DOC_EXAMPLE = [{"metadata": {"count": 1, "limit": None, "index": None}, "data": [{
    "numer": "200", "idiip": "PL.PZGiK.200", "teryt": "PL",
    "zbior": "Państwowy rejestr granic i powierzchni jednostek podziałów terytorialnych kraju (PRG)",
    "organ": "Główny Geodeta Kraju",
    "uslugi": {
        "wyszukiwania": ["http://mapy.geoportal.gov.pl/wss/service/CSW/guest/CSWStartup"],
        "przegladania": ["WMS=http://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WMS/AdministrativeBoundaries"],
        "pobierania": ["https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries",
                       "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaNumeracjiAdresowej"],
        "przeksztalcania": [], "uruchamiania": []}}]}]


def test_api_documentation_example():
    recs = client().parse_api(DOC_EXAMPLE)
    assert len(recs) == 1
    r = recs[0]
    assert r.url_wfs == "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
    assert r.url_wms == "http://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WMS/AdministrativeBoundaries"
    assert r.organ == "Główny Geodeta Kraju" and r.teryt == "PL"


def test_api_prefixed_entries_prefer_wfs_over_atom():
    payload = [{"metadata": {}, "data": [{"teryt": "1261", "zbior": "Baza danych szczegółowych osnów geodezyjnych",
        "uslugi": {"pobierania": ["ATOM=https://x.pl/atom/feed.xml",
                                  "WFS=https://x.pl/gs/ows?service=WFS&request=GetCapabilities"]}}]}]
    assert client().parse_api(payload)[0].url_wfs == "https://x.pl/gs/ows"


def test_api_atom_only_gives_no_wfs():
    payload = {"data": [{"teryt": "1261", "zbior": "X",
                         "uslugi": {"pobierania": ["https://x.pl/atom/feed.xml"]}}]}
    assert client().parse_api(payload)[0].url_wfs == ""


def test_get_wfs_urls_api_then_name_fallback(monkeypatch):
    c = client()
    calls = []
    every = [{"metadata": {}, "data": [
        {"teryt": "1261", "zbior": "Powiatowa baza GESUT", "uslugi": {"pobierania": ["https://a.pl/wfs"]}},
        {"teryt": "1261", "zbior": "Baza danych szczegółowych osnów geodezyjnych",
         "uslugi": {"pobierania": ["WFS=https://b.pl/osnowa/wfs"]}}]}]

    def fake(teryt, zbior_text):
        calls.append(zbior_text)
        return [] if zbior_text else c.parse_api(every)
    monkeypatch.setattr(c, "_api_records", fake)
    res = c.get_wfs_urls("1261", "bdsog")
    assert res.success and [r.url_wfs for r in res.records] == ["https://b.pl/osnowa/wfs"]
    assert calls == ["bdsog", None]


def test_get_wfs_urls_falls_back_to_html(monkeypatch):
    c = client()
    monkeypatch.setattr(c, "_api_records", lambda t, z: None)
    monkeypatch.setattr(c, "_fetch", lambda url: OLD)
    res = c.get_wfs_urls("1261", "bdsog")
    assert res.success and res.records[0].url_wfs == "https://ows.example.pl/bdsog/wfs"
