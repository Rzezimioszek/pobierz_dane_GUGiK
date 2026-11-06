# -*- coding: utf-8 -*-
"""
Panel z prawdziwym QGIS (bez okna): ścieżki pobierania kończą się warstwą,
a praca sieciowa idzie przez zadania w tle (udawany serwer WFS).
"""
import dataclasses
import importlib
import os
import time

import pytest

from conftest import PLUGIN_NAME
from fake_wfs import FakeWfs

os.environ["NO_PROXY"] = "127.0.0.1,localhost"


class _Bar:
    def pushMessage(self, *a, **k):
        pass


class _Iface:
    def __init__(self):
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtWidgets import QMainWindow
        self._win = QMainWindow()
        self._canvas = QgsMapCanvas()

    def mapCanvas(self):
        return self._canvas

    def mainWindow(self):
        return self._win

    def messageBar(self):
        return _Bar()


def _wait(dock, timeout=30):
    from qgis.core import QgsApplication
    t0 = time.time()
    while time.time() - t0 < timeout:
        QgsApplication.processEvents()
        if not dock.running_tasks:
            QgsApplication.processEvents()
            return
        time.sleep(0.02)
    raise TimeoutError("zadanie nie skończyło się")


@pytest.fixture
def dock(monkeypatch):
    import qgis.utils
    from qgis.core import QgsProject
    monkeypatch.setattr(qgis.utils, "iface", _Iface(), raising=False)
    server = FakeWfs(total=25)
    catalog = importlib.import_module(f"{PLUGIN_NAME}.core.catalog")
    for label in ("dzialki (EGIB)", "budynki (EGIB)"):
        monkeypatch.setitem(catalog.SOURCES_BY_LABEL, label,
                            dataclasses.replace(catalog.SOURCES_BY_LABEL[label], url=server.url))
    mod = importlib.import_module(f"{PLUGIN_NAME}.pobierz_dane_GUGiK_dockwidget")
    d = mod.PD_GUGiKDockWidget()
    d.messages = []
    d.show_info = lambda m: d.messages.append(("info", m))
    d.show_error = lambda m: d.messages.append(("error", m))
    d._confirm_start = lambda: True
    QgsProject.instance().removeAllMapLayers()
    yield d
    server.close()
    QgsProject.instance().removeAllMapLayers()


def _layer(name):
    from qgis.core import QgsProject
    found = QgsProject.instance().mapLayersByName(name)
    return found[0] if found else None


def test_id_download_uses_selected_source(dock):
    dock.cmbObjType.setCurrentText("budynki (EGIB)")
    dock.txt_ids.setPlainText("\n".join(f"146501_1.0001.{i}" for i in range(12)))
    dock.run_id_download()
    assert dock.running_tasks, "pobieranie powinno iść w tle"
    _wait(dock)
    assert _layer("Budynki") is not None and _layer("Dzialki") is None
    assert dock.messages[-1][0] == "info", dock.messages


def test_extent_download_creates_parcels(dock):
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    dock.cmbObjType.setCurrentText("dzialki (EGIB)")
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock.canvas.setExtent(QgsRectangle(284000, 366000, 284500, 366500))
    dock.run_extent_download()
    _wait(dock)
    assert _layer("Dzialki").featureCount() == 25


def test_layer_download_clips_and_uses_layer_crs(dock):
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
    dock.cmbObjType.setCurrentText("dzialki (EGIB)")
    poly = QgsVectorLayer("Polygon?crs=epsg:2180", "zakres", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(
        "POLYGON((284000 366000, 284015 366000, 284015 366015, 284000 366015, 284000 366000))"))
    poly.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(poly)
    dock.cmb_layer.setLayer(poly)
    dock.run_layer_download()
    _wait(dock)
    assert _layer("Dzialki") is not None


def test_precise_search_goes_through_hits(dock):
    dock.obreby_by_name = {"Testowo": ["022602_1.0003"]}
    dock.search_by_obreb_and_nr("Testowo", "59")
    _wait(dock)
    _wait(dock)
    assert _layer("Dzialki") is not None, dock.messages
