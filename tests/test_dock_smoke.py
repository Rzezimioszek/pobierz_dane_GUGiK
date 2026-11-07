# -*- coding: utf-8 -*-
"""
Nowy panel z prawdziwym QGIS (bez okna): wybór → zakres → Pobierz kończy się
warstwą, praca sieciowa idzie przez zadania w tle (udawany serwer WFS),
a przycisk Pobierz jest aktywny tylko przy pełnym wyborze.
"""
import dataclasses
import datetime
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
    from qgis.core import QgsApplication, QgsGeometry, QgsProject, QgsSettings
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
    d.shutdown()
    d.deleteLater()
    QgsApplication.processEvents()
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
    assert not dock.sources.years_row.isHidden() and sel.years == [str(datetime.date.today().year)]
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert not dock.btn_download.isEnabled()


def test_package_choice_for_powiat(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    dock.scope.teryt.set_teryt("1465")
    assert not dock.output.package_row.isHidden()
    dock.scope.teryt.set_teryt("14")
    assert dock.output.package_row.isHidden() and not dock.output.package_hint.isHidden()


def test_style_sheet_applied_and_step_states(dock):
    assert "QFrame#stepCard" in dock.styleSheet()
    dock.sources.select_labels([])
    assert dock.source_box.property("state") == "todo"
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("")
    assert dock.source_box.property("state") == "done"
    assert dock.scope_box.property("state") == "todo"
    assert dock.hint.property("state") == "problem"
    dock.scope.txt_ids.setPlainText("146501_1.0001.1")
    assert dock.scope_box.property("state") == "done" and dock.hint.property("state") == "ready"


def test_gpkg_without_path_blocks_download(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("146501_1.0001.1")
    dock.output.fw_gpkg.setFilePath("")
    dock.output.rb_gpkg.setChecked(True)
    assert not dock.btn_download.isEnabled() and dock.output_box.property("state") == "todo"
    dock.output.rb_memory.setChecked(True)
    assert dock.btn_download.isEnabled()


class _PluginIface(_Iface):
    """Interfejs QGIS na potrzeby cyklu życia wtyczki (pasek, menu, panele)."""

    def __init__(self):
        super().__init__()
        self.docks, self.menu_actions = [], []

    def addToolBar(self, name):
        return self._win.addToolBar(name)

    def addPluginToVectorMenu(self, menu, action):
        self.menu_actions.append(action)

    def removePluginVectorMenu(self, menu, action):
        self.menu_actions.remove(action)

    def removeToolBarIcon(self, action):
        pass

    def addDockWidget(self, area, dock):
        self._win.addDockWidget(area, dock)
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        self._win.removeDockWidget(dock)
        self.docks.remove(dock)


def test_unload_removes_everything_and_cancels_tasks():
    from qgis.PyQt.QtWidgets import QToolBar
    main = importlib.import_module(f"{PLUGIN_NAME}.pobierz_dane_GUGiK")
    iface = _PluginIface()
    plugin = main.PD_GUGiK(iface)
    plugin.initGui()
    plugin.run()
    dock = plugin.dockwidget
    assert iface.docks == [dock] and iface.menu_actions

    class FakeTask:
        canceled = False

        def cancel(self):
            self.canceled = True
    t = FakeTask()
    dock.running_tasks.append(t)
    dock._start_draw()
    tool = dock.map_tool
    assert dock.canvas.mapTool() is tool

    plugin.unload()
    assert t.canceled and iface.docks == [] and iface.menu_actions == []
    assert plugin.dockwidget is None and plugin.toolbar is None
    assert dock.canvas.mapTool() is not tool
    assert not [tb for tb in iface._win.findChildren(QToolBar) if tb.objectName() == "PobieranieGUGiK"
                and tb.parent() is iface._win and tb.isVisible()]

    # Ponowne wczytanie (jak przeładowanie wtyczki) – jeden panel, bez wyjątków
    plugin2 = main.PD_GUGiK(iface)
    plugin2.initGui()
    plugin2.run()
    assert len(iface.docks) == 1
    plugin2.unload()


def test_eziudp_gmina_boundary_in_background(dock, monkeypatch):
    import threading
    from qgis.core import QgsGeometry
    prg = importlib.import_module(f"{PLUGIN_NAME}.prg_client")
    ez = importlib.import_module(f"{PLUGIN_NAME}.eziudp_download_dialog")
    threads = []

    def boundary(self, teryt):
        threads.append(threading.current_thread() is threading.main_thread())
        return QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")
    monkeypatch.setattr(prg.PRGClient, "get_boundary_geometry", boundary)
    launched = []
    monkeypatch.setattr(ez.EziudpWidget, "_launch",
                        lambda self, keys, teryts, bbox, clip: launched.append((keys, teryts, bbox, clip)))

    dock.sources.select_labels(["Usługi powiatowe (EZiUDP)"])
    w = dock.eziudp
    w.radio_teryt.setChecked(True)
    w.teryt_scope.set_teryt("146501_1")
    next(iter(w._layer_checks.values())).setChecked(True)
    w._on_start()
    assert not launched, "start powinien czekać na granicę"
    from qgis.core import QgsApplication
    t0 = time.time()
    while not launched and time.time() - t0 < 10:
        QgsApplication.processEvents()
        time.sleep(0.02)
    assert threads == [False], "granica musi iść poza wątkiem interfejsu"
    keys, teryts, bbox, clip = launched[0]
    assert teryts == ["1465"] and bbox and clip.startswith("Polygon")


def test_enter_starts_only_when_ready(dock, monkeypatch):
    started = []
    monkeypatch.setattr(dock, "start", lambda: started.append(1))
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.OBREB)
    dock.scope.txt_obreb.setText("")
    dock.scope.txt_nr.returnPressed.emit()
    assert started == []
    name = next(n for n, ts in dock.units.obreby_by_name.items() if len(ts) == 1)
    dock.scope.txt_obreb.setText(name)
    dock.scope.txt_nr.setText("5")
    dock.scope.txt_nr.returnPressed.emit()
    assert started == [1]


def _band_vertices(dock):
    return dock.band.numberOfVertices()


def test_layer_multipolygon_uses_both_parts(dock):
    """Zgłoszony błąd: MultiPolygon w warstwie → TypeError w filters.spatial (QGIS 4)."""
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
    lyr = QgsVectorLayer("MultiPolygon?crs=epsg:2180", "multi", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(
        "MULTIPOLYGON(((284002 366002, 284008 366002, 284008 366008, 284002 366008, 284002 366002)),"
        "((284002 366402, 284008 366402, 284008 366408, 284002 366408, 284002 366402)))"))
    lyr.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(lyr)
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.LAYER)
    dock.scope.cmb_layer.setLayer(lyr)
    dock.scope.chk_selected.setChecked(False)
    dock.start()
    _wait(dock)
    ids = sorted(x["ID_DZIALKI"] for x in _layer("Dzialki").getFeatures())
    # działki 0 (N 366000) i 20 (N 366400) – po jednej z każdej części, nic pomiędzy
    assert ids == ["022602_1.0003.0", "022602_1.0003.20"], ids


def test_drawn_polygon_download_and_highlight(dock):
    from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock.scope.rb_polygon.setChecked(True)
    assert not dock.btn_download.isEnabled()
    dock._start_draw("polygon")
    tool = dock.map_tool
    from qgis.core import QgsPointXY
    tool.points = [QgsPointXY(284002, 366002), QgsPointXY(284008, 366002),
                   QgsPointXY(284008, 366048), QgsPointXY(284008, 366048)]   # powtórzony (dwuklik)
    tool._finish()
    assert dock.canvas.mapTool() is not tool
    assert dock.btn_download.isEnabled() and _band_vertices(dock) > 0
    dock.start()
    assert _band_vertices(dock) == 0, "podświetlenie powinno zniknąć po starcie"
    _wait(dock)
    assert 1 <= _layer("Dzialki").featureCount() <= 4


def test_rect_highlight_disappears_on_scope_change(dock):
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock._start_draw("rect")
    dock._on_rect(QgsRectangle(284000, 366000, 284100, 366100))
    assert _band_vertices(dock) > 0
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert _band_vertices(dock) == 0
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    assert _band_vertices(dock) > 0          # wraca, bo prostokąt nadal jest zakresem
    dock.scope.rb_extent.setChecked(True)
    assert _band_vertices(dock) == 0


def test_polygon_escape_cancels(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock.scope.rb_polygon.setChecked(True)
    dock._start_draw("polygon")
    tool = dock.map_tool
    dock._on_draw_cancel()
    assert dock.canvas.mapTool() is not tool and dock.scope.drawn_polygon is None


def test_step_cards_collapse_and_remember(dock):
    from qgis.core import QgsSettings
    box = dock.scope_box
    assert not box.is_collapsed() and box.arrow.text() == "▾"
    box.toggle()
    assert box.is_collapsed() and box.content.isHidden() and box.arrow.text() == "▸"
    assert QgsSettings().value("pobierz_dane_GUGiK/ui/collapsed/step2", False, type=bool)
    # zwinięty krok z brakami mówi, że trzeba go uzupełnić
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert "rozwiń" in box.summary.text()
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.dock")
    other = mod.PD_GUGiKDock(_Iface())
    try:
        assert other.scope_box.is_collapsed() and not other.source_box.is_collapsed()
    finally:
        other.shutdown()
        other.deleteLater()
    box.toggle()
    assert not box.is_collapsed()


def test_tasks_panel_collapses_and_opens_for_new_task(dock):
    dock.tasks.toggle()
    assert dock.tasks.is_collapsed()
    dock.tasks.add("próba")
    assert not dock.tasks.is_collapsed() and dock.tasks.title.text() == "Zadania (1)"


def test_skorowidz_years_reach_current_year():
    services = importlib.import_module(f"{PLUGIN_NAME}.skorowidz_services")
    year = datetime.date.today().year
    orto = services.SKOROWIDZ_BY_LABEL["ortofotomapa"]
    assert orto.year_range[0] == year
