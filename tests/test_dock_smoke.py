# -*- coding: utf-8 -*-
"""
Nowy panel z prawdziwym QGIS (bez okna): wybór → zakres → Pobierz kończy się
warstwą, praca sieciowa idzie przez zadania w tle (udawany serwer WFS),
a przycisk Pobierz jest aktywny tylko przy pełnym wyborze.
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
    def __init__(self):
        self.messages = []

    def pushMessage(self, *a, **k):
        self.messages.append(a)


class _Iface:
    def __init__(self):
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtWidgets import QMainWindow
        self._win = QMainWindow()
        self._canvas = QgsMapCanvas()
        self.bar = _Bar()

    def mapCanvas(self):
        return self._canvas

    def mainWindow(self):
        return self._win

    def messageBar(self):
        return self.bar

    def openMessageLog(self):
        pass


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
    from qgis.core import QgsGeometry, QgsProject, QgsSettings
    QgsSettings().remove("pobierz_dane_GUGiK")
    server = FakeWfs(total=25)
    catalog = importlib.import_module(f"{PLUGIN_NAME}.core.catalog")
    for label in ("dzialki (EGIB)", "budynki (EGIB)"):
        monkeypatch.setitem(catalog.SOURCES_BY_LABEL, label,
                            dataclasses.replace(catalog.SOURCES_BY_LABEL[label], url=server.url))
    # Granica PRG bez sieci: kwadrat w obszarze udawanego serwera
    prg = importlib.import_module(f"{PLUGIN_NAME}.prg_client")
    monkeypatch.setattr(prg.PRGClient, "get_boundary_geometry", lambda self, t: QgsGeometry.fromWkt(
        "POLYGON((284000 366000, 284500 366000, 284500 366500, 284000 366500, 284000 366000))"))
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.dock")
    d = mod.PD_GUGiKDock(_Iface())
    d.ask = lambda text: True
    QgsProject.instance().removeAllMapLayers()
    yield d
    server.close()
    QgsProject.instance().removeAllMapLayers()


def _layer(name):
    from qgis.core import QgsProject
    found = QgsProject.instance().mapLayersByName(name)
    return found[0] if found else None


def _rows(dock):
    lay = dock.tasks.rows
    return [lay.itemAt(i).widget() for i in range(lay.count())]


def test_button_needs_complete_choice(dock):
    dock.sources.select_labels([])
    assert not dock.btn_download.isEnabled() and "kroku 1" in dock.hint.text()
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert not dock.btn_download.isEnabled()
    dock.scope.txt_ids.setPlainText("146501_1.0001.1")
    assert dock.btn_download.isEnabled()


class ScopeTab:
    UNIT, MAP, LAYER, IDS, OBREB = range(5)


def test_two_sources_by_ids(dock):
    dock.sources.select_labels(["dzialki (EGIB)", "budynki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("\n".join(f"146501_1.0001.{i}" for i in range(12)))
    dock.start()
    assert dock.running_tasks, "pobieranie powinno iść w tle"
    _wait(dock)
    assert _layer("Dzialki") is not None and _layer("Budynki") is not None
    assert all(not r.running for r in _rows(dock))


def test_unit_scope_fetches_boundary_then_parcels(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    dock.scope.teryt.set_teryt("146501_1")
    assert dock.btn_download.isEnabled(), dock.hint.text()
    dock.start()
    _wait(dock)
    _wait(dock)
    assert _layer("Dzialki").featureCount() == 25
    assert "25" in _rows(dock)[0].result.text()


def test_extent_to_gpkg(dock, tmp_path):
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock.output.rb_gpkg.setChecked(True)
    dock.output.fw_gpkg.setFilePath(str(tmp_path / "w.gpkg"))
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock.canvas.setExtent(QgsRectangle(284000, 366000, 284500, 366500))
    dock.start()
    _wait(dock)
    vl = _layer("Dzialki")
    assert vl.providerType() == "ogr" and vl.featureCount() == 25


def test_layer_selected_only(dock):
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
    poly = QgsVectorLayer("Polygon?crs=epsg:2180", "zakres", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(
        "POLYGON((284000 366000, 284015 366000, 284015 366015, 284000 366015, 284000 366000))"))
    poly.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(poly)
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.LAYER)
    dock.scope.cmb_layer.setLayer(poly)
    dock.scope.chk_selected.setChecked(True)
    assert not dock.btn_download.isEnabled()      # nic nie zaznaczono
    poly.selectAll()
    dock._validate()
    assert dock.btn_download.isEnabled()
    dock.start()
    _wait(dock)
    assert _layer("Dzialki") is not None


def test_obreb_requires_parcels_and_finds_unit(dock):
    name = next(n for n, ts in dock.units.obreby_by_name.items() if len(ts) == 1)
    dock.sources.select_labels(["budynki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.OBREB)
    dock.scope.txt_obreb.setText(name)
    dock.scope.txt_nr.setText("59")
    assert not dock.btn_download.isEnabled() and "działek" in dock.hint.text()
    dock.sources.select_labels(["dzialki (EGIB)"])
    assert dock.btn_download.isEnabled()
    dock.start()
    _wait(dock)
    assert _layer("Dzialki") is not None


def test_eziudp_replaces_steps(dock):
    dock.sources.select_labels(["Usługi powiatowe (EZiUDP)"])
    assert not dock.eziudp.isHidden() and dock.scope_box.isHidden() and dock.btn_download.isHidden()
    assert dock.eziudp._dock() is dock


def test_skorowidz_excludes_wfs_and_shows_years(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    from qgis.PyQt.QtCore import Qt
    item = next(i for i in dock.sources._items if i.text(0) == "ortofotomapa")
    item.setCheckState(0, Qt.CheckState.Checked)
    sel = dock.sources.selection()
    assert sel.sources == [] and sel.skorowidz.label == "ortofotomapa"
    assert not dock.sources.years_row.isHidden() and sel.years == ["2025"]
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert not dock.btn_download.isEnabled()


def test_package_choice_for_powiat(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    dock.scope.teryt.set_teryt("1465")
    assert not dock.output.package_row.isHidden()
    dock.scope.teryt.set_teryt("14")
    assert dock.output.package_row.isHidden() and not dock.output.package_hint.isHidden()
