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
    wfs = "WFS=https://x.pl/gs/ows?service=WFS&request=GetCapabilities"
    record = {"teryt": "1261", "zbior": "Baza danych szczegółowych osnów geodezyjnych",
              "uslugi": {"pobierania": ["ATOM=https://x.pl/atom/feed.xml", wfs]}}
    payload = [{"metadata": {}, "data": [record]}]
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


def test_egib_landuse_and_contours_from_epodgik_capabilities(monkeypatch):
    """Powiat miński (ePODGiK): obok działek i budynków są użytki i kontury."""
    import os
    from conftest import FIXTURES
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    with open(os.path.join(FIXTURES, "egib_minsk_capabilities.xml"), "rb") as f:
        caps = f.read()
    c = client()
    monkeypatch.setattr(c, "_get_capabilities_layers", lambda url: c._extract_layer_names(caps))
    names = c.get_layer_names("https://wms.epodgik.pl/cgi-bin/minsk?", "egib")
    pick = task.EziudpWfsTask._pick_wfs_layer
    keys = task.AVAILABLE_LAYERS_BY_KEY
    got = {k: pick(names, keys[k]["layer_pattern"])
           for k in ("egib_dzi", "egib_bud", "egib_pkt", "egib_uzy", "egib_kon")}
    assert got == {"egib_dzi": "ms:dzialki", "egib_bud": "ms:budynki",
                   "egib_pkt": "ms:punkty_graniczne", "egib_uzy": "ms:uzytki",
                   "egib_kon": "ms:kontury_klasyfikacyjne"}
    assert keys["egib_uzy"]["optional"] and keys["egib_kon"]["optional"]


def test_optional_layer_missing_is_skipped_not_error():
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    rep = task.EziudpWfsReport()
    rep.skipped.append("TERYT 1465: usługa powiatu nie udostępnia warstwy „Użytki gruntowe (EGIB)”")
    text = rep.format_summary()
    assert "Pominięte" in text and "Błędy" not in text


def test_direct_client_pages_and_keeps_own_params():
    """Bez prostokąta zakresu: GetFeature stronami, własny parser, parametry usługi zachowane."""
    import os
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from fake_wfs import FakeWfs
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    srv = FakeWfs(total=1005)
    try:
        fields, geom_type, feats = task._read_direct(
            srv.url, "ms:uzytki", None, None, "1412", srv.url + "?map=minsk&service=WFS",
            lambda: False)
    finally:
        srv.close()
    assert fields is None and len(feats) == 1005 and geom_type.startswith("Multi")
    assert [int(r["startIndex"]) for r in srv.requests] == [0, 1000]
    assert srv.requests[0]["map"] == "minsk" and srv.requests[0]["typenames"] == "ms:uzytki"
    assert feats[0]["attrs"]["teryt_powiatu"] == "1412" and feats[0]["attrs"]["gml_id"] == "d.0"


def test_direct_client_tiles_with_rect_and_rejects_wrong_axes():
    import os
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    import pytest
    from fake_wfs import FakeWfs
    from qgis.core import QgsRectangle
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    srv = FakeWfs(total=2500, spatial=True, unstable=True)
    try:
        _, _, feats = task._read_direct(srv.url, "ms:dzialki", QgsRectangle(284000, 366000, 284390, 367990),
                                        None, "0226", srv.url, lambda: False)
        assert len(feats) == 2000 and len({f["attrs"]["gml_id"] for f in feats}) == 2000
        assert all(int(r["startIndex"]) == 0 and "EPSG::2180" in r["bbox"] for r in srv.requests)
        # serwer ignoruje BBOX i oddaje obiekty z innego miejsca → nie udajemy, że to wynik
        srv.spatial = False
        srv.total = 5
        with pytest.raises(RuntimeError, match="poza pytanym zakresem"):
            task._read_direct(srv.url, "ms:dzialki", QgsRectangle(600000, 500000, 600100, 500100),
                              None, "0226", srv.url, lambda: False)
    finally:
        srv.close()


def test_task_falls_back_to_qgis_provider(monkeypatch):
    """Własny klient zawiódł → dostawca WFS QGIS; obie drogi zawiodły → błąd i para do ponowienia."""
    from qgis.core import QgsFields, QgsGeometry
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    layers = importlib.import_module(f"{PLUGIN_NAME}.core.layers")
    ez = importlib.import_module(f"{PLUGIN_NAME}.eziudp_client")
    monkeypatch.setattr(ez.EziudpClient, "get_wfs_urls", lambda self, t, z: ez.EziudpResult(
        success=True, teryt=t, zbior=z, records=[ez.EziudpRecord(url_wfs="http://127.0.0.1:9/wfs")]))
    monkeypatch.setattr(ez.EziudpClient, "get_layer_names", lambda self, u, z: ["ms:dzialki"])

    def boom(*a, **k):
        raise RuntimeError("zły GML")
    monkeypatch.setattr(task, "_read_direct", boom)
    feat = {"geom": QgsGeometry.fromWkt("POINT(1 2)"), "attrs": {"teryt_powiatu": "0226"}}
    monkeypatch.setattr(task.EziudpWfsTask, "_read_with_provider",
                        lambda self, *a: (QgsFields(), "Point", [feat], "1.1.0"))
    t = task.EziudpWfsTask(["0226"], ["egib_dzi"], output=layers.Output(None, False))
    assert t.run() and t._report.total_features == 1 and not t._report.failed_pairs
    assert t._pending[0]["name"] == "Dzialki_0226"

    def boom2(self, *a):
        raise RuntimeError("nie WFS")
    monkeypatch.setattr(task.EziudpWfsTask, "_read_with_provider", boom2)
    t = task.EziudpWfsTask(["0226"], ["egib_dzi"], output=layers.Output(None, False))
    t.run()
    assert t._report.failed_pairs == [("0226", "egib_dzi")] and "zły GML" in t._report.errors[0]


def test_whole_county_empty_result_falls_back_to_provider(monkeypatch):
    """Cały powiat idzie kaflami po prostokącie powiatu; 0 obiektów (np. inna kolejność osi) → dostawca QGIS."""
    from qgis.core import QgsFields, QgsGeometry, QgsRectangle
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    layers = importlib.import_module(f"{PLUGIN_NAME}.core.layers")
    ez = importlib.import_module(f"{PLUGIN_NAME}.eziudp_client")
    monkeypatch.setattr(ez.EziudpClient, "get_wfs_urls", lambda self, t, z: ez.EziudpResult(
        success=True, teryt=t, zbior=z, records=[ez.EziudpRecord(url_wfs="http://127.0.0.1:9/wfs")]))
    monkeypatch.setattr(ez.EziudpClient, "get_layer_names", lambda self, u, z: ["ms:dzialki"])
    rects = []

    def direct(base, name, rect, *a, **k):
        rects.append(rect)
        return None, "", []
    monkeypatch.setattr(task, "_read_direct", direct)
    feat = {"geom": QgsGeometry.fromWkt("POINT(1 2)"), "attrs": {"teryt_powiatu": "0226"}}
    provider = []
    monkeypatch.setattr(task.EziudpWfsTask, "_read_with_provider",
                        lambda self, *a: provider.append(a) or (QgsFields(), "Point", [feat], "1.1.0"))
    t = task.EziudpWfsTask(["0226"], ["egib_dzi"], output=layers.Output(None, False),
                           powiat_rects={"0226": QgsRectangle(0, 0, 10, 10)})
    assert t.run()
    assert rects[0] == QgsRectangle(0, 0, 10, 10) and provider and t._report.total_features == 1


def test_county_client_uses_hits_and_works_without_it():
    import os
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    from fake_wfs import FakeWfs
    from qgis.core import QgsRectangle
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    rect = QgsRectangle(284000, 366000, 284390, 367990)          # 2000 działek
    for server, hits_expected in (({"max_count": 400}, True), ({"hits": False}, False)):
        srv = FakeWfs(total=2500, spatial=True, unstable=True, unknown_matched=True, **server)
        try:
            _, _, feats = task._read_direct(srv.url, "ms:dzialki", rect, None, "0226",
                                            srv.url + "?map=x", lambda: False)
        finally:
            srv.close()
        hits = [r for r in srv.requests if r.get("resultType") == "hits"]
        assert len({f["attrs"]["gml_id"] for f in feats}) == 2000, server
        assert (len(hits) > 1) == hits_expected and all(r["map"] == "x" and "bbox" in r for r in hits)
