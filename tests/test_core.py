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
    assert [l.url for l in md.links()] == ["https://example.pl/wfs"]


def test_separate_layers_get_numbered_names():
    from qgis.core import QgsProject
    layers, catalog = mod("layers"), mod("catalog")
    out = layers.Output(separate=True)
    src = catalog.egib_dzialki()
    first = layers.write_source(_features(2), src, out, scope="TERYT 1465", tag="1465")
    second = layers.write_source(_features(2), src, out, scope="TERYT 1465", tag="1465")
    try:
        assert [l.name() for l in first + second] == ["Dzialki_1465_1", "Dzialki_1465_2"]
        assert layers.unique_name("Dzialki", "obręb 12/3") == "Dzialki_obreb_12_3_1"
    finally:
        QgsProject.instance().removeMapLayers([l.id() for l in first + second])


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
        QgsProject.instance().removeMapLayers([l.id() for l in a + sep])


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
