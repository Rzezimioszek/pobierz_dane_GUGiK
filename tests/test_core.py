# -*- coding: utf-8 -*-
"""Katalog, filtry, zapis do warstw i zadanie pobierania (przez udawany serwer WFS)."""
import dataclasses
import importlib
import os

import pytest

from conftest import PLUGIN_DIR, PLUGIN_NAME
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
