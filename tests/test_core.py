# -*- coding: utf-8 -*-
"""Katalog, filtry, zapis do warstw i zadanie pobierania (przez udawany serwer WFS)."""
import dataclasses
import importlib
import os

import pytest

from conftest import PLUGIN_NAME
from fake_wfs import FakeWfs

os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def mod(name):
    return importlib.import_module(f"{PLUGIN_NAME}.core.{name}")


# ---------------------------------------------------------------- katalog

def test_every_source_is_in_the_picker():
    from qgis.PyQt.QtCore import Qt
    catalog = mod("catalog")
    widgets = importlib.import_module(f"{PLUGIN_NAME}.ui.widgets")
    picker = widgets.SourcePicker()
    keys = {it.data(0, Qt.ItemDataRole.UserRole)[1] for it in picker._items}
    assert {s.label for s in catalog.SOURCES} <= keys
    assert len(catalog.SOURCES) == 64


def test_styles_exist():
    catalog, layers = mod("catalog"), mod("layers")
    missing = [s.style for s in catalog.SOURCES if s.style and layers.style_path(s.style) is None]
    assert missing == []


def test_opendata_urls_like_v130():
    catalog = mod("catalog")
    egib = catalog.source_for_label("dzialki (EGIB)")
    rcn = catalog.source_for_label("lokale (RCN)")
    assert catalog.opendata_package(egib, "14") == (
        "https://opendata.geoportal.gov.pl/InneDane/latest_exports/eziudp_wfs/PARQUET/14_dzialki.parquet",
        "14_dzialki.parquet")
    assert catalog.opendata_package(egib, "1465")[0].endswith("/eziudp_wfs/GPKG/1465.gpkg.zip")
    assert catalog.opendata_package(rcn, "02")[0].endswith(
        "/rcn_transakcje_ceny/PARQUET/02_transakcje_ceny_lokale.parquet")
    assert catalog.opendata_package(catalog.source_for_label("A08_Ulice (PRG)"), "14") is None


def test_prg_sea_layers_geometry():
    catalog = mod("catalog")
    assert catalog.source_for_label("W02_Morze_terytorialne_RP (PRG_W)").geom_type == "MultiPolygon"
    assert catalog.source_for_label("W06_Morskie_linie_brzegowe (PRG_W)").geom_type == "MultiLineString"


# ---------------------------------------------------------------- filtry

def test_bbox_axis_order_and_escape():
    f = mod("filters")
    xml = f.bbox("geom", 284000, 366000, 284100, 366100)
    assert "<gml:lowerCorner>366000 284000</gml:lowerCorner>" in xml
    assert "&amp;" in f.ids("id", ["a&b"])


def test_combine_is_valid_xml():
    from xml.etree import ElementTree as ET
    f = mod("filters")
    xml = f.combine([f.bbox("geom", 0, 0, 1, 1), f.attribute("ms:id_dzialki", "1465", like=True)])
    root = ET.fromstring(xml)
    assert root.tag.endswith("Filter") and root[0].tag.endswith("And")


# ---------------------------------------------------------------- zapis do warstw

def _features(n, start=0):
    gml = mod("gml")
    from fake_wfs import HEADER, parcel
    text = HEADER.format(matched=n, returned=n) + "".join(parcel(i) for i in range(start, start + n)) \
        + "</wfs:FeatureCollection>"
    return gml.parse_features(text)


def test_write_dedupes_and_appends():
    from qgis.core import QgsProject, QgsWkbTypes
    layers = mod("layers")
    project = QgsProject()
    vl = layers.write(_features(3), "Dzialki", "MultiPolygon", "ID_DZIALKI", project=project)
    layers.write(_features(4), "Dzialki", "MultiPolygon", "ID_DZIALKI", project=project)
    assert vl.featureCount() == 4
    assert QgsWkbTypes.isMultiType(vl.wkbType())
    assert len(project.mapLayersByName("Dzialki")) == 1


def test_missing_unique_field_does_not_drop_features():
    from qgis.core import QgsProject
    layers = mod("layers")
    project = QgsProject()
    vl = layers.write(_features(5), "X", "MultiPolygon", "boundedBy", project=project)
    assert vl.featureCount() == 5


def test_split_lines_polygons():
    from conftest import fixture_text
    gml, layers = mod("gml"), mod("layers")
    parts = layers.split_lines_polygons(gml.parse_features(fixture_text("prg_ulice_mixed.gml")))
    assert len(parts["line"]) == 2 and len(parts["polygon"]) == 1


# ---------------------------------------------------------------- pobieranie

@pytest.fixture
def server():
    srv = FakeWfs(total=1005, fail_filters=["ZEPSUTE"])
    yield srv
    srv.close()


def _source(url):
    catalog = mod("catalog")
    return dataclasses.replace(catalog.egib_dzialki(), url=url)


def test_task_pages_until_short_page(server):
    tasks, filters = mod("tasks"), mod("filters")
    task = tasks.WfsDownloadTask(_source(server.url), [filters.bbox("geom", 0, 0, 1, 1)])
    assert task.run() is True
    assert len(task.result.features) == 1005
    assert [int(r["startIndex"]) for r in server.requests] == [0, 1000]
    assert task.result.errors == []


def test_failed_query_does_not_hang_or_stop_others(server):
    tasks = mod("tasks")
    task = tasks.WfsDownloadTask(_source(server.url), [
        tasks.Query("<fes:Filter>ZEPSUTE</fes:Filter>", label="zła"),
        tasks.Query("<fes:Filter>OK</fes:Filter>", label="dobra"),
    ])
    assert task.run() is True
    assert len(task.result.features) == 1005
    assert [e[0] for e in task.result.errors] == ["zła"]


def test_clip_keeps_only_intersecting(server):
    from qgis.core import QgsGeometry
    tasks = mod("tasks")
    clip = QgsGeometry.fromWkt("POLYGON((284000 366000, 284015 366000, 284015 366015, 284000 366015, 284000 366000))")
    task = tasks.WfsDownloadTask(_source(server.url), [tasks.Query("<x/>", clip_geom=clip)])
    task.run()
    assert 0 < len(task.result.features) < 10


def test_attribute_selection_only_for_egib_parcels(server):
    tasks, catalog = mod("tasks"), mod("catalog")
    budynki = dataclasses.replace(catalog.source_for_label("budynki (EGIB)"), url=server.url)
    assert tasks.WfsDownloadTask(budynki, ["<x/>"], attributes=["id_dzialki"]).attributes is None
    task = tasks.WfsDownloadTask(_source(server.url), ["<x/>"], attributes=["id_dzialki"])
    task.run()
    assert server.requests[0]["propertyName"] == "id_dzialki,geom"


def test_hits(server):
    tasks = mod("tasks")
    task = tasks.HitsTask(_source(server.url), "<x/>")
    assert task.run() and task.hits == 1005


# ---------------------------------------------------------------- filtr dla wielu części

MULTI = ("MULTIPOLYGON(((284000 366000, 284010 366000, 284010 366010, 284000 366000)),"
         "((290000 370000, 290010 370000, 290010 370010, 290000 370000)))")


def test_spatial_multipolygon_uses_every_part():
    from xml.etree import ElementTree as ET
    f = mod("filters")
    xml = f.spatial("geom", MULTI, use_bbox=False)
    root = ET.fromstring(xml)
    assert root[0].tag.endswith("Or") and len(root[0]) == 2
    assert "366000.0 284000.0" in xml and "370000.0 290000.0" in xml


def test_spatial_single_part_multipolygon_and_curve():
    f = mod("filters")
    one = "MULTIPOLYGON(((284000 366000, 284010 366000, 284010 366010, 284000 366000)))"
    assert "<fes:Or>" not in f.spatial("geom", one, use_bbox=False)
    curve = "CURVEPOLYGON(CIRCULARSTRING(284000 366000, 284010 366010, 284020 366000, 284010 365990, 284000 366000))"
    assert "<fes:Intersects>" in f.spatial("geom", curve, use_bbox=False)


def test_spatial_bbox_for_points_and_flag():
    f = mod("filters")
    assert "<fes:BBOX>" in f.spatial("geom", "POINT(284000 366000)", use_bbox=False)
    assert "<fes:BBOX>" in f.spatial("geom", MULTI, use_bbox=True)


# ---------------------------------------------------------------- czytelne wyniki

def test_friendly_network_errors():
    import requests
    net = mod("net")
    assert "proxy" in net.friendly(requests.exceptions.ProxyError("x")).lower()
    assert "czasie" in net.friendly(requests.Timeout("read timed out"))
    resp = requests.Response()
    resp.status_code = 503
    assert "503" in net.friendly(requests.HTTPError(response=resp))
    retry = requests.exceptions.RetryError("Max retries exceeded (Caused by ResponseError("
                                           "'too many 500 error responses'))")
    assert "500" in net.friendly(retry) and "niedostępna" in net.friendly(retry)
    assert "połączenia" in net.friendly(requests.ConnectionError("Max retries exceeded … "
                                                                 "NameResolutionError"))


def test_write_groups_layers_and_stamps_metadata():
    from qgis.core import QgsProject
    layers = mod("layers")
    project = QgsProject()
    info = {"url": "https://example.pl/wfs", "typename": "ms:dzialki", "scope": "TERYT 1465"}
    vl = layers.write(_features(3), "Dzialki", "MultiPolygon", "ID_DZIALKI", project=project, info=info)
    layers.write(_features(4), "Dzialki", "MultiPolygon", "ID_DZIALKI", project=project, info=info)
    group = project.layerTreeRoot().findGroup("GUGiK")
    assert group is not None and group.findLayer(vl.id()) is not None
    md = vl.metadata()
    assert "https://example.pl/wfs" in md.abstract() and "TERYT 1465" in md.abstract()
    assert len(md.history()) == 2 and md.history()[0].split(" · ")[1] == "3 obiektów"
    assert md.history()[1].split(" · ")[1] == "1 obiektów"
    assert [lyr.url for lyr in md.links()] == ["https://example.pl/wfs"]


def test_separate_layers_get_numbered_names():
    from qgis.core import QgsProject
    layers, catalog = mod("layers"), mod("catalog")
    out = layers.Output(separate=True)
    src = catalog.egib_dzialki()
    first = layers.write_source(_features(2), src, out, scope="TERYT 1465", tag="1465")
    second = layers.write_source(_features(2), src, out, scope="TERYT 1465", tag="1465")
    try:
        assert [lyr.name() for lyr in first + second] == ["Dzialki_1465_1", "Dzialki_1465_2"]
        assert layers.unique_name("Dzialki", "obręb 12/3") == "Dzialki_obreb_12_3_1"
    finally:
        QgsProject.instance().removeMapLayers([lyr.id() for lyr in first + second])


def test_task_reports_running_count(server):
    tasks, filters = mod("tasks"), mod("filters")
    task = tasks.WfsDownloadTask(_source(server.url), [filters.bbox("geom", 0, 0, 1, 1)])
    seen = []
    task.count_changed.connect(seen.append)
    task.run()
    assert seen == [1000, 1005]


def test_prefix_names_layers_and_groups_them():
    from qgis.core import QgsProject
    layers, catalog = mod("layers"), mod("catalog")
    assert layers.clean_prefix("  Projekt A/ł ") == "Projekt_A_ł"
    assert layers.clean_prefix("__") == ""
    out = layers.Output(prefix="ProjektA")
    src = catalog.egib_dzialki()
    a = layers.write_source(_features(2), src, out, scope="TERYT 1465", tag="1465")
    b = layers.write_source(_features(3), src, out, scope="TERYT 1465", tag="1465")
    sep = layers.write_source(_features(1), src, layers.Output(prefix="ProjektA", separate=True),
                              tag="1465")
    try:
        assert a[0] is b[0] and a[0].name() == "ProjektA_Dzialki" and a[0].featureCount() == 3
        assert sep[0].name() == "ProjektA_Dzialki_1465_1"
        sub = QgsProject.instance().layerTreeRoot().findGroup("GUGiK").findGroup("ProjektA")
        assert sub is not None and sub.findLayer(a[0].id()) and sub.findLayer(sep[0].id())
    finally:
        QgsProject.instance().removeMapLayers([lyr.id() for lyr in a + sep])


def test_prefix_in_gpkg_table_names(tmp_path):
    from qgis.core import QgsProject, QgsVectorLayer
    layers, catalog = mod("layers"), mod("catalog")
    path = str(tmp_path / "wynik.gpkg")
    out = layers.Output(gpkg_path=path, prefix="ProjektB")
    vl = layers.write_source(_features(2), catalog.egib_dzialki(), out)[0]
    try:
        assert QgsVectorLayer(f"{path}|layername=ProjektB_Dzialki", "x", "ogr").featureCount() == 2
    finally:
        QgsProject.instance().removeMapLayer(vl.id())


def test_output_from_settings_reads_prefix():
    from qgis.core import QgsSettings
    layers = mod("layers")
    QgsSettings().setValue("pobierz_dane_GUGiK/layer_prefix", "ProjektC")
    try:
        out = layers.Output.from_settings()
        assert out.prefix == "ProjektC" and out.layer_name("Dzialki_RCN_1465") == "ProjektC_Dzialki_RCN_1465"
    finally:
        QgsSettings().remove("pobierz_dane_GUGiK/layer_prefix")


def test_server_page_limit_below_page_size_still_gets_everything():
    tasks, filters = mod("tasks"), mod("filters")
    srv = FakeWfs(total=1250, max_count=500)
    try:
        task = tasks.WfsDownloadTask(_source(srv.url), [filters.bbox("geom", 0, 0, 1, 1)])
        task.run()
    finally:
        srv.close()
    assert len(task.result.features) == 1250
    assert [int(r["startIndex"]) for r in srv.requests] == [0, 500, 1000]


def test_exception_report_with_http_200_is_an_error_not_empty_result():
    tasks = mod("tasks")
    srv = FakeWfs(total=10, exception_filters=["ZLY"])
    try:
        task = tasks.WfsDownloadTask(_source(srv.url), [tasks.Query("<fes:Filter>ZLY</fes:Filter>", label="zły")])
        task.run()
    finally:
        srv.close()
    assert task.result.features == [] and task.result.errors
    assert "błędny filtr" in task.result.errors[0][1]


def test_gml_feature_id_is_kept_outside_attributes():
    from conftest import fixture_text
    gml = mod("gml")
    feats = gml.parse_features(fixture_text("prg_ulice_mixed.gml"))
    assert all("id" in f for f in feats) and "gml_id" not in feats[0]["attrs"]


def test_proxy_from_qgis_network_manager(monkeypatch):
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtNetwork import QNetworkProxy
    net = mod("net")
    nam = QgsNetworkAccessManager.instance()
    old = nam.fallbackProxy()
    try:
        nam.setFallbackProxyAndExcludes(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "proxy.urzad.pl",
                                                      8080, "jan kowalski", "p@ss"),
                                        ["https://intranet.urzad.pl"], [])
        proxies = net._qgis_proxies()
        assert proxies["https"] == "http://jan%20kowalski:p%40ss@proxy.urzad.pl:8080"
        assert proxies["no_proxy"] == "intranet.urzad.pl"
        assert net.make_session().proxies["http"] == proxies["http"]
        nam.setFallbackProxyAndExcludes(QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy), [], [])
        assert net._qgis_proxies() == {}
    finally:
        nam.setFallbackProxyAndExcludes(old, [], [])


def test_size_limit_stops_after_first_page_and_progress_uses_number_matched(server):
    tasks, filters = mod("tasks"), mod("filters")
    flt = [filters.bbox("geom", 0, 0, 1, 1)]
    task = tasks.WfsDownloadTask(_source(server.url), flt, size_limit=1000)
    assert task.run() is True
    assert task.result.too_large == 1005 and task.result.features == []
    assert len(server.requests) == 1                       # tylko pierwsza strona
    server.requests.clear()
    task = tasks.WfsDownloadTask(_source(server.url), flt)   # bez limitu – wszystko
    seen = []
    task.progressChanged.connect(seen.append)
    task.run()
    assert len(task.result.features) == 1005 and task.result.too_large == 0
    assert 99 <= seen[0] < 100 and seen[-1] == 100         # 1000/1005 po pierwszej stronie


def test_write_100k_features_is_fast_enough():
    """Zapis dzieje się w głównym wątku – nie może zamrażać QGIS na długie sekundy."""
    import time
    from qgis.core import QgsProject
    layers = mod("layers")
    base = _features(100)
    feats = []
    for k in range(200):
        for f in base:
            attrs = dict(f["attrs"], ID_DZIALKI=f"{k}_{f['attrs']['ID_DZIALKI']}",
                         **{f"pole{j}": "x" for j in range(8)})
            feats.append({"geom": f["geom"], "attrs": attrs})
    project = QgsProject()
    start = time.time()
    vl = layers.write(feats, "Duza", "MultiPolygon", "ID_DZIALKI", project=project, load_style=False)
    assert vl.featureCount() == 20000 and time.time() - start < 3.0


def test_prepared_in_background_matches_main_thread_write():
    from qgis.core import QgsProject
    layers, catalog = mod("layers"), mod("catalog")
    src = catalog.egib_dzialki()
    feats = _features(50)
    prepared = layers.prepare(feats, src.geom_type, src.numeric_fields, src.unique_field)
    assert len(prepared.features) == 50 and prepared.keys[0] == feats[0]["attrs"]["ID_DZIALKI"]
    project = QgsProject()
    fast = layers.write(feats, "A", src.geom_type, src.unique_field, project=project,
                        load_style=False, prepared=prepared)
    slow = layers.write(feats, "B", src.geom_type, src.unique_field, project=project, load_style=False)
    assert fast.featureCount() == slow.featureCount() == 50
    a = {f["ID_DZIALKI"]: f.geometry().asWkt() for f in fast.getFeatures()}
    b = {f["ID_DZIALKI"]: f.geometry().asWkt() for f in slow.getFeatures()}
    assert a == b
    # dopisanie z tymi samymi ID – szybka ścieżka też pomija duplikaty
    more = _features(60)
    layers.write(more, "A", src.geom_type, src.unique_field, project=project, load_style=False,
                 prepared=layers.prepare(more, src.geom_type, src.numeric_fields, src.unique_field))
    assert fast.featureCount() == 60


def test_download_task_prepares_features(server):
    tasks, filters = mod("tasks"), mod("filters")
    task = tasks.WfsDownloadTask(_source(server.url), [filters.bbox("geom", 0, 0, 1, 1)])
    task.run()
    assert task.result.prepared is not None and len(task.result.prepared.features) == 1005


# ---------------------------------------------------------------- kafle zamiast stronicowania

def test_unstable_paging_loses_features_but_tiles_get_all():
    """Serwer bez stałego porządku: stronicowanie gubi obiekty, kafle – nie."""
    from qgis.core import QgsGeometry
    tasks, scope = mod("tasks"), mod("scope")
    srv = FakeWfs(total=3000, spatial=True, unstable=True)
    try:
        src = _source(srv.url)
        area = QgsGeometry.fromWkt("POLYGON((283990 365990, 284600 365990, 284600 368000, "
                                   "283990 368000, 283990 365990))")
        plain = tasks.WfsDownloadTask(src, [tasks.Query(mod("filters").spatial("geom", area.asWkt()))])
        plain.run()
        ids = {f["attrs"]["ID_DZIALKI"] for f in plain.result.features}
        assert len(ids) < 3000                                  # stronicowanie zgubiło część
        srv.requests.clear()
        tiled = tasks.WfsDownloadTask(src, [scope.rect_query(src, area, "prostokąt")])
        tiled.run()
        ids = [f["attrs"]["ID_DZIALKI"] for f in tiled.result.features]
        assert len(set(ids)) == 3000 and len(ids) == 3000       # wszystkie, bez duplikatów
        assert all(int(r["count"]) <= 1000 and int(r.get("startIndex", 0)) == 0 for r in srv.requests)
    finally:
        srv.close()


def test_tiles_work_when_server_hides_number_matched():
    """MapServer usług zbiorczych EGiB/RCN: numberMatched="unknown" – kafle i tak, liczba z resultType=hits."""
    from qgis.core import QgsGeometry
    tasks, scope = mod("tasks"), mod("scope")
    srv = FakeWfs(total=3000, spatial=True, unstable=True, unknown_matched=True)
    try:
        src = _source(srv.url)
        area = QgsGeometry.fromWkt("POLYGON((283990 365990, 284600 365990, 284600 368000, "
                                   "283990 368000, 283990 365990))")
        task = tasks.WfsDownloadTask(src, [scope.rect_query(src, area, "prostokąt")])
        totals = []
        task.setProgress = lambda p: totals.append(p)
        task.run()
        ids = [f["attrs"]["ID_DZIALKI"] for f in task.result.features]
        assert len(set(ids)) == 3000 and len(ids) == 3000
        pages = [r for r in srv.requests if r.get("resultType") != "hits"]
        assert all(int(r.get("startIndex", 0)) == 0 for r in pages), "kafle, nie stronicowanie"
        assert sum(1 for r in srv.requests if r.get("resultType") == "hits") == 1
        assert sum(1 for r in pages if r["count"] == "1") == 1, "bez liczby nie pytamy kafli o 1 obiekt"
        assert 0 < totals[len(totals) // 2] < 99, "postęp z liczby z resultType=hits"
        # ochrona przed dużym pobraniem działa też bez numberMatched
        srv.requests.clear()
        big = tasks.WfsDownloadTask(src, [scope.rect_query(src, area, "prostokąt")], size_limit=100)
        big.run()
        assert big.result.too_large == 3000 and not big.result.features and len(srv.requests) <= 3
    finally:
        srv.close()


def test_paging_stops_when_server_ignores_start_index():
    """Bez liczby obiektów i z ignorowanym startIndex stronicowanie nie może kręcić się w kółko."""
    tasks, filters = mod("tasks"), mod("filters")
    srv = FakeWfs(total=2500, unknown_matched=True, ignore_start=True)
    try:
        task = tasks.WfsDownloadTask(_source(srv.url), [filters.bbox("geom", 0, 0, 1, 1)])
        task.run()
        assert len(task.result.features) == 1000 and len(srv.requests) == 2
    finally:
        srv.close()


def test_polygon_tiles_skip_quadrants_outside_and_clip():
    from qgis.core import QgsGeometry
    tasks, scope = mod("tasks"), mod("scope")
    srv = FakeWfs(total=3000, spatial=True, unstable=True)
    try:
        src = _source(srv.url)
        # „L”: lewa kolumna + dolny pas
        poly = QgsGeometry.fromWkt("POLYGON((284000 366000, 284600 366000, 284600 366300, 284200 366300, "
                                   "284200 368000, 284000 368000, 284000 366000))")
        task = tasks.WfsDownloadTask(src, scope.geometry_queries(src, [poly], "L"))
        task.run()
        got = {f["attrs"]["ID_DZIALKI"] for f in task.result.features}
        expected = {f"022602_1.0003.{i}" for i in range(3000)
                    if poly.intersects(QgsGeometry.fromWkt(
                        f"POLYGON(({284000 + (i // 100) * 20} {366000 + (i % 100) * 20}, "
                        f"{284010 + (i // 100) * 20} {366000 + (i % 100) * 20}, "
                        f"{284010 + (i // 100) * 20} {366010 + (i % 100) * 20}, "
                        f"{284000 + (i // 100) * 20} {366000 + (i % 100) * 20}))"))}
        assert got == expected
    finally:
        srv.close()


def test_polygon_filter_uses_urn_and_falls_back_to_bbox():
    from qgis.core import QgsGeometry
    tasks, scope, filters = mod("tasks"), mod("scope"), mod("filters")
    poly = QgsGeometry.fromWkt("POLYGON((284000 366000, 284100 366000, 284100 366100, 284000 366000))")
    assert 'srsName="urn:ogc:def:crs:EPSG::2180"' in filters.spatial("geom", poly.asWkt(), use_bbox=False)
    srv = FakeWfs(total=3000, spatial=True, reject_intersects=True)
    try:
        src = _source(srv.url)
        task = tasks.WfsDownloadTask(src, scope.geometry_queries(src, [poly], "trójkąt"))
        task.run()
        assert task.result.errors == [] and task.result.features
        assert all(poly.intersects(QgsGeometry.fromWkt(f["geom"])) for f in task.result.features)
    finally:
        srv.close()
