# -*- coding: utf-8 -*-
"""Algorytmy Processing na udawanym serwerze WFS (ten sam rdzeń co panel)."""
import dataclasses
import importlib
import os

import pytest

from conftest import PLUGIN_NAME
from fake_wfs import FakeWfs

os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def mod(name):
    return importlib.import_module(f"{PLUGIN_NAME}.{name}")


@pytest.fixture
def server():
    srv = FakeWfs(total=25, fail_filters=["ZLY"])
    yield srv
    srv.close()


@pytest.fixture
def _alg(server, monkeypatch):
    """Algorytm z działkami EGiB kierowanymi na udawany serwer (run() tworzy kopię algorytmu)."""
    catalog = mod("core.catalog")
    patched = [dataclasses.replace(s, url=server.url) if s.label == "dzialki (EGIB)" else s
               for s in catalog.SOURCES]
    monkeypatch.setattr(catalog, "SOURCES", patched)

    def make(cls_name):
        alg = getattr(mod("processing.algorithms"), cls_name)()
        alg.initAlgorithm()
        return alg, next(i for i, s in enumerate(alg.sources) if s.label == "dzialki (EGIB)")
    return make


def _run(alg, params):
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    params = dict(params, OUTPUT="memory:wynik")
    res, ok = alg.run(params, context, feedback)
    assert ok, feedback.textLog()
    return context.takeResultLayer(res["OUTPUT"]), feedback


def test_provider_lists_three_algorithms():
    from qgis.core import QgsApplication
    provider = mod("processing.provider").GugikProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    try:
        names = sorted(a.id() for a in provider.algorithms())
        assert names == ["pobierz_dane_gugik:pobierz_id", "pobierz_dane_gugik:pobierz_teryt",
                         "pobierz_dane_gugik:pobierz_warstwa"]
    finally:
        QgsApplication.processingRegistry().removeProvider(provider)


def test_ids_from_text_and_table(server, _alg):
    from qgis.core import QgsFeature, QgsVectorLayer
    alg, i = _alg("DownloadIds")
    table = QgsVectorLayer("None?field=id:string", "ids", "memory")
    f = QgsFeature(table.fields())
    f.setAttributes(["022602_1.0003.20"])
    table.dataProvider().addFeatures([f])
    out, _ = _run(alg, {"SOURCE": i, "IDS": "022602_1.0003.0; 022602_1.0003.0",
                        "TABLE": table, "FIELD": "id"})
    assert out.crs().authid() == "EPSG:2180" and out.featureCount() > 0
    flt = server.requests[0]["filter"]
    assert "022602_1.0003.0" in flt and "022602_1.0003.20" in flt and flt.count("022602_1.0003.0<") == 1


def test_layer_extent_is_clipped(server, _alg):
    from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
    alg, i = _alg("DownloadLayer")
    area = QgsVectorLayer("Polygon?crs=EPSG:2180", "zasieg", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(
        "POLYGON((284000 366000, 284015 366000, 284015 366015, 284000 366015, 284000 366000))"))
    area.dataProvider().addFeatures([f])
    out, _ = _run(alg, {"SOURCE": i, "INPUT": area})
    assert 0 < out.featureCount() < 25
    assert "Intersects" in server.requests[0]["filter"]


def test_teryt_uses_prg_boundary(server, _alg, monkeypatch):
    from qgis.core import QgsGeometry
    scope = mod("core.scope")
    monkeypatch.setattr(scope, "boundary", lambda teryt: QgsGeometry.fromWkt(
        "POLYGON((284000 366000, 284100 366000, 284100 366100, 284000 366000))"))
    alg, i = _alg("DownloadTeryt")
    out, _ = _run(alg, {"SOURCE": i, "TERYT": "1465", "PRECISE": False})
    assert out.featureCount() == 25
    flt = server.requests[0]["filter"]
    assert "BBOX" in flt and "1465*" in flt


def test_teryt_without_boundary_fails_clearly(server, _alg, monkeypatch):
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    scope = mod("core.scope")
    monkeypatch.setattr(scope, "boundary", lambda teryt: None)
    alg, i = _alg("DownloadTeryt")
    _, ok = alg.run({"SOURCE": i, "TERYT": "9999", "OUTPUT": "memory:"},
                    QgsProcessingContext(), QgsProcessingFeedback())
    assert not ok and server.requests == []
