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


@pytest.fixture
def county(monkeypatch):
    """Udawana usługa powiatowa: EZiUDP wskazuje lokalny serwer z warstwą ms:dzialki."""
    ez = importlib.import_module(f"{PLUGIN_NAME}.eziudp_client")
    srv = FakeWfs(total=2500, spatial=True, unstable=True)
    state = {"srv": srv, "asked": [], "fail": set()}

    def urls(self, teryt, zbior):
        state["asked"].append((teryt, zbior))
        if teryt in state["fail"]:
            return ez.EziudpResult(success=False, teryt=teryt, zbior=zbior, error_message="brak usługi")
        return ez.EziudpResult(success=True, teryt=teryt, zbior=zbior,
                               records=[ez.EziudpRecord(url_wfs=srv.url + "?map=x")])
    monkeypatch.setattr(ez.EziudpClient, "get_wfs_urls", urls)
    monkeypatch.setattr(ez.EziudpClient, "get_layer_names", lambda self, url, zbior: ["ms:dzialki"])
    yield state
    srv.close()


def test_eziudp_uses_steps_and_task_list(dock, county):
    """Usługi powiatowe: zakres z kroku 2, zapis z kroku 3, wiersz na liście zadań, kafle."""
    from qgis.core import QgsRectangle
    ui_ez = importlib.import_module(f"{PLUGIN_NAME}.ui.eziudp")
    dock.sources.select_labels(["eziudp:egib_dzi"])
    assert not dock.scope_box.isHidden() and not dock.btn_download.isHidden()
    assert dock.sources.selection().eziudp == ["egib_dzi"] and not dock.sources.selection().sources
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("146501_1.0001.1")
    assert not dock.btn_download.isEnabled() and "powiatowych" in dock.hint.text()

    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock._start_draw("rect")
    rect = QgsRectangle(284000, 366000, 284390, 367990)     # 20 × 100 działek = 2000
    dock._on_rect(rect)
    from qgis.core import QgsGeometry
    teryts = ui_ez.powiaty_for(QgsGeometry.fromRect(rect))
    assert len(teryts) == 1
    dock.output.ed_prefix.setText("Proj")
    assert dock.btn_download.isEnabled()
    dock.start()
    _wait(dock)
    vl = _layer(f"Proj_Dzialki_{teryts[0]}")
    assert vl is not None and vl.featureCount() == 2000, "kafle nie mogą zgubić obiektów"
    reqs = county["srv"].requests
    assert all("bbox" in r and r["map"] == "x" for r in reqs)
    assert max(int(r["startIndex"]) for r in reqs) == 0, "kafle zamiast stronicowania"
    row = _rows(dock)[0]
    assert "2 000" in row.result.text() and not row.btn_show.isHidden()


def test_eziudp_failed_county_offers_retry(dock, county):
    ui_ez = importlib.import_module(f"{PLUGIN_NAME}.ui.eziudp")
    county["fail"].add("0226")
    out = dock.output.output()
    ui_ez.launch(dock, ["egib_dzi"], ["0226"], None, None, out, "powiat")
    _wait(dock)
    row = _rows(dock)[0]
    assert "brak usługi" in row.result.text() and not row.btn_retry.isHidden()
    county["fail"].clear()
    county["srv"].total = 30
    row.btn_retry.click()
    _wait(dock)
    assert _layer("Dzialki_0226").featureCount() == 30


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
    from qgis.core import QgsApplication
    registry = QgsApplication.processingRegistry()
    assert registry.providerById("pobierz_dane_gugik") is not None

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
    assert registry.providerById("pobierz_dane_gugik") is None
    assert dock.canvas.mapTool() is not tool
    assert not [tb for tb in iface._win.findChildren(QToolBar) if tb.objectName() == "PobieranieGUGiK"
                and tb.parent() is iface._win and tb.isVisible()]

    # Ponowne wczytanie (jak przeładowanie wtyczki) – jeden panel, bez wyjątków
    plugin2 = main.PD_GUGiK(iface)
    plugin2.initGui()
    plugin2.run()
    assert len(iface.docks) == 1
    assert registry.providerById("pobierz_dane_gugik") is not None
    plugin2.unload()


def test_eziudp_gmina_boundary_in_background(dock, monkeypatch):
    import threading
    from qgis.core import QgsGeometry
    prg = importlib.import_module(f"{PLUGIN_NAME}.prg_client")
    ez = importlib.import_module(f"{PLUGIN_NAME}.ui.eziudp")
    threads = []

    def boundary(self, teryt):
        threads.append(threading.current_thread() is threading.main_thread())
        return QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")
    monkeypatch.setattr(prg.PRGClient, "get_boundary_geometry", boundary)
    launched = []
    monkeypatch.setattr(ez, "launch", lambda dock, keys, teryts, rect, clip, out, label, row=None, pairs=None:
                        (launched.append((keys, teryts, rect, clip)), row.finish("ok")))

    dock.sources.select_labels(["eziudp:egib_dzi"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    dock.scope.teryt.set_teryt("146501_1")
    dock.start()
    assert not launched, "start powinien czekać na granicę"
    from qgis.core import QgsApplication
    t0 = time.time()
    while not launched and time.time() - t0 < 10:
        QgsApplication.processEvents()
        time.sleep(0.02)
    assert threads == [False], "granica musi iść poza wątkiem interfejsu"
    keys, teryts, bbox, clip = launched[0]
    assert teryts == ["1465"] and not bbox.isEmpty() and clip.asWkt().startswith("Polygon")


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
    from qgis.core import QgsCoordinateReferenceSystem
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


def test_clear_button_removes_drawn_shape(dock):
    from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.MAP)
    dock.scope.rb_polygon.setChecked(True)
    assert not dock.scope.btn_clear_drawn.isEnabled()
    dock._on_polygon(QgsGeometry.fromWkt(
        "POLYGON((284002 366002, 284008 366002, 284008 366048, 284002 366002))"))
    assert dock.scope.btn_clear_drawn.isEnabled() and _band_vertices(dock) > 0
    assert dock.btn_download.isEnabled()
    dock.scope.btn_clear_drawn.click()
    assert dock.scope.drawn_polygon is None and _band_vertices(dock) == 0
    assert not dock.btn_download.isEnabled() and not dock.scope.btn_clear_drawn.isEnabled()


def test_failed_query_shows_count_and_retry(dock, monkeypatch):
    """Błąd serwera: czytelny komunikat i „Ponów” tylko dla nieudanych zapytań."""
    net = importlib.import_module(f"{PLUGIN_NAME}.core.net")
    catalog = importlib.import_module(f"{PLUGIN_NAME}.core.catalog")
    monkeypatch.setattr(net, "MAX_RETRIES", 1)
    srv = FakeWfs(total=3, fail_filters=["ZLY"])
    try:
        monkeypatch.setitem(catalog.SOURCES_BY_LABEL, "dzialki (EGIB)", dataclasses.replace(
            catalog.SOURCES_BY_LABEL["dzialki (EGIB)"], url=srv.url))
        dock.sources.select_labels(["dzialki (EGIB)"])
        dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
        dock.scope.txt_ids.setPlainText("ZLY_1")
        dock.start()
        _wait(dock)
        row = dock.tasks.rows.itemAt(0).widget()
        assert "⚠" in row.result.text() and "500" in row.result.text()
        assert not row.btn_retry.isHidden()
        srv.fail_filters = []
        row.btn_retry.click()
        assert row.btn_retry.isHidden()
        _wait(dock)
        again = dock.tasks.rows.itemAt(0).widget()
        assert again is not row and "ponowienie" in again.title.text()
        assert "✓" in again.result.text() and _layer("Dzialki").featureCount() == 3
    finally:
        srv.close()


def test_separate_layer_option_names_each_download(dock):
    dock.output.chk_separate.setChecked(True)
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("022602_1.0003.0")
    for _ in range(2):
        dock.start()
        _wait(dock)
    from qgis.core import QgsProject
    names = sorted(lyr.name() for lyr in QgsProject.instance().mapLayers().values())
    assert names == ["Dzialki_ID_1", "Dzialki_ID_2"]
    dock.output.chk_separate.setChecked(False)


def test_prefix_field_names_download_and_summary(dock):
    from qgis.core import QgsProject, QgsSettings
    dock.output.ed_prefix.setText("Projekt A")
    assert QgsSettings().value("pobierz_dane_GUGiK/layer_prefix") == "Projekt_A"
    assert "Projekt_A_Dzialki" in dock.output.prefix_hint.text()
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    dock.scope.txt_ids.setPlainText("022602_1.0003.0")
    assert "Projekt_A" in dock.output_box.summary.text()
    dock.start()
    _wait(dock)
    assert [lyr.name() for lyr in QgsProject.instance().mapLayers().values()] == ["Projekt_A_Dzialki"]
    dock.output.ed_prefix.clear()
    assert not dock.output.prefix_hint.isVisible()


class _Files:
    """Serwer plików: /ok.tif zwraca bajty, /zly.tif – 404."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                if "zly" in self.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = b"II*\x00" + b"0" * 2000
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_sheet_downloads_skip_existing_and_leave_no_partial_files(dock, tmp_path, monkeypatch):
    sheets = importlib.import_module(f"{PLUGIN_NAME}.ui.sheets")
    net = importlib.import_module(f"{PLUGIN_NAME}.core.net")
    monkeypatch.setattr(net, "MAX_RETRIES", 0)
    monkeypatch.setattr(dock, "_load_file_as_layer", lambda path, name: True)
    srv = _Files()
    try:
        (tmp_path / "jest.tif").write_bytes(b"x" * 100)

        def sheet(name):
            return sheets.Sheet(name, f"{srv.url}/{name}", "2025", "RGB", 0, None, {})
        dock._load_rasters([sheet("jest.tif"), sheet("ok.tif"), sheet("zly.tif")], True,
                           folder=str(tmp_path))
        for _ in range(3):
            _wait(dock)
    finally:
        srv.close()
    assert srv.hits == ["/ok.tif", "/zly.tif"]          # istniejący plik pominięty
    assert sorted(p.name for p in tmp_path.iterdir()) == ["jest.tif", "ok.tif"]
    results = [dock.tasks.rows.itemAt(i).widget().result.text() for i in range(dock.tasks.rows.count())]
    assert any("404" in r for r in results)


def test_sheets_window_is_modeless_and_single(dock):
    from test_dialogs import _features
    first = dock._show_sheets(_features(), "ortofotomapa")
    assert not first.isModal() and first.isVisible()
    second = dock._show_sheets(_features(), "ortofotomapa")
    assert not first.isVisible() and dock._sheets is second
    from qgis.core import QgsProject
    assert len(QgsProject.instance().mapLayers()) == 1
    second.close()
    assert dock._sheets is None and not QgsProject.instance().mapLayers()


def test_new_teryt_allowed_bad_teryt_blocked(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    dock.scope.teryt.set_teryt("146501_1.9998")          # obręb spoza słownika
    dock.scope.teryt.teryt_changed.emit("146501_1.9998")
    assert dock.btn_download.isEnabled()
    assert "nie ma w słowniku" in dock.scope.unit_hint.text()
    assert dock.scope.unit_hint.property("state") == "warn"
    dock.scope.teryt.set_teryt("14650")
    dock.scope.teryt.teryt_changed.emit("14650")
    assert not dock.btn_download.isEnabled() and "Nieprawidłowy" in dock.hint.text()
    dock.scope.teryt.set_teryt("1465011")                # gmina bez „_”
    dock.scope.teryt.teryt_changed.emit("1465011")
    assert dock.scope.scope().teryt == "146501_1"


def test_obreb_code_typed_instead_of_name(dock):
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.OBREB)
    dock.scope.txt_obreb.setText("146501_1.9998")
    dock.scope.txt_nr.setText("12/3")
    assert dock.btn_download.isEnabled()
    assert "bez sprawdzania" in dock.scope.obreb_hint.text()
    dock.scope.txt_obreb.setText("Nieistniejący obręb xyz")
    assert "wpisz jego kod" in dock.hint.text()


def test_gmina_lists_obreby_and_uldk_updates_them(dock):
    u = dock.units
    mw = next(g for g in u.gminy if g.endswith("_3"))
    w = dock.scope.teryt
    dock.sources.select_labels(["dzialki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    w.set_teryt(mw)                                   # wpisany kod gminy → kaskada + obręby
    assert w.cmb_gmina.currentData() == mw
    known = u.obreby_for_gmina(mw)
    assert w.cmb_obreb.count() == len(known) + 1 and w.cmb_obreb.isEnabled()
    w.cmb_obreb.setCurrentIndex(1)
    assert dock.scope.scope().teryt == known[0][0]
    # aktualna lista z ULDK: jeden nowy obręb, wybrany zostaje
    fresh = known + [(mw[:6] + "_5.9999", "Nowy obręb")]
    dock.scope._uldk_done(mw, fresh, [])
    assert w.cmb_obreb.count() == len(fresh) + 1 and w.cmb_obreb.currentData() == known[0][0]
    assert "ULDK" in w.lbl_obreby.text() and "1 nowych" in w.lbl_obreby.text()
    assert u.check(mw[:6] + "_5.9999") == ("obręb", "")      # znany już w tej sesji
    # ULDK nie odpowiada → zostaje słownik z uwagą
    dock.scope._uldk_cache.clear()
    dock.scope._uldk_done(mw, [], ["020104_4: brak połączenia"])
    assert "nie odpowiada" in w.lbl_obreby.text()


def test_typed_obreb_code_selects_whole_cascade(dock):
    u = dock.units
    code = next(c for c in u.obreby_teryt_to_info if c.split(".")[0].endswith("_9"))   # Kraków/Łódź
    w = dock.scope.teryt
    dock.scope.tabs.setCurrentIndex(ScopeTab.UNIT)
    w.txt_teryt.setText(code)
    assert w.cmb_gmina.currentData() == u.gmina_for(code.split(".")[0])
    assert w.cmb_obreb.currentData() == code


def test_large_download_asks_before_fetching_everything(dock, monkeypatch):
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.dock")
    catalog = importlib.import_module(f"{PLUGIN_NAME}.core.catalog")
    monkeypatch.setattr(mod, "LARGE_DOWNLOAD", 2)
    srv = FakeWfs(total=5)
    try:
        monkeypatch.setitem(catalog.SOURCES_BY_LABEL, "dzialki (EGIB)", dataclasses.replace(
            catalog.SOURCES_BY_LABEL["dzialki (EGIB)"], url=srv.url))
        dock.sources.select_labels(["dzialki (EGIB)"])
        dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
        dock.scope.txt_ids.setPlainText("022602_1.0003.0")
        dock.start()
        _wait(dock)
        row = dock.tasks.rows.itemAt(0).widget()
        assert "5 obiektów" in row.result.text() and "opendata" in row.result.text()
        assert row.btn_retry.text() == "Pobierz mimo to" and _layer("Dzialki") is None
        row.btn_retry.click()
        _wait(dock)
        done = dock.tasks.rows.itemAt(0).widget()
        assert "✓" in done.result.text() and _layer("Dzialki").featureCount() == 5
        # „Pokaż” przybliża mapę do wyniku
        assert not done.btn_show.isHidden()
        dock.canvas.setExtent(dock.canvas.fullExtent())
        done.btn_show.click()
        assert dock.canvas.extent().intersects(_layer("Dzialki").extent())
    finally:
        srv.close()


def test_area_and_layer_hints(dock):
    from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject,
                           QgsRectangle, QgsVectorLayer)
    widgets = importlib.import_module(f"{PLUGIN_NAME}.ui.widgets")
    assert widgets.format_area(2500) == "2500 m²" and widgets.format_area(125000) == "12,5 ha"
    assert widgets.format_area(3_450_000) == "3,45 km²"
    dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    dock._on_rect(QgsRectangle(284000, 366000, 285000, 367500))
    assert "1,50 km²" in dock.scope.map_hint.text()
    vl = QgsVectorLayer("Polygon?crs=EPSG:2180", "zasieg", "memory")
    feats = []
    for i in range(3):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(i, 0, i + 1, 1)))
        feats.append(f)
    vl.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(vl)
    dock.scope.tabs.setCurrentIndex(ScopeTab.LAYER)
    dock.scope.cmb_layer.setLayer(vl)
    assert "Obiektów: 3" in dock.scope.layer_hint.text()
    dock.scope.chk_selected.setChecked(True)
    assert "Obiektów: 0" in dock.scope.layer_hint.text()


def test_summary_message_after_last_task_and_id_field_hint(dock):
    dock.sources.select_labels(["dzialki (EGIB)", "budynki (EGIB)"])
    dock.scope.tabs.setCurrentIndex(ScopeTab.IDS)
    assert "ID_DZIALKI" in dock.scope.ids_hint.text() or "id_dzialki" in dock.scope.ids_hint.text().lower()
    assert "budynki" in dock.scope.ids_hint.text().lower()
    dock.scope.txt_ids.setPlainText("022602_1.0003.0")
    dock.iface.bar.messages.clear()
    dock.start()
    _wait(dock)
    last = dock.iface.bar.messages[-1]
    assert "Zakończono pobieranie" in last[1] and "Dzialki" in last[1]


def test_cancel_all_and_parallel_files(dock, tmp_path, monkeypatch):
    sheets = importlib.import_module(f"{PLUGIN_NAME}.ui.sheets")
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.dock")
    started = []

    class SlowTask(mod.tasks.FileDownloadTask):
        def run(self):
            started.append(self.url)
            while not self.isCanceled():
                time.sleep(0.02)
            return False
    monkeypatch.setattr(mod.tasks, "FileDownloadTask", SlowTask)
    many = [sheets.Sheet(f"a{i}.tif", f"http://127.0.0.1:9/a{i}.tif", "2025", "", 0, None, {})
            for i in range(5)]
    dock._load_rasters(many, True, folder=str(tmp_path))
    t0 = time.time()
    while not started and time.time() - t0 < 10:
        QgsApplication_process()
    # 3 pliki zlecone naraz (ile wątków je faktycznie uruchomi, zależy od procesora), reszta czeka
    assert dock._files_running() == 3 and len(dock._raster_queue) == 2
    assert not dock.tasks.btn_cancel_all.isHidden()
    dock.tasks.btn_cancel_all.click()
    _wait(dock)
    assert dock._raster_queue == [] and len(started) <= 3
    assert dock.tasks.btn_cancel_all.isHidden()


def QgsApplication_process():
    from qgis.core import QgsApplication
    QgsApplication.processEvents()
    time.sleep(0.02)


def test_raster_gets_prefix_and_subgroup(dock, tmp_path):
    from qgis.core import QgsProject
    import numpy  # noqa: F401 – GDAL w środowisku testów
    from osgeo import gdal
    path = str(tmp_path / "arkusz.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1)
    ds.SetGeoTransform([284000, 1, 0, 366004, 0, -1])
    ds = None
    dock.output.ed_prefix.setText("ProjektA")
    assert dock._load_file_as_layer(path, "arkusz.tif")
    lyr = QgsProject.instance().mapLayersByName("ProjektA_arkusz.tif")[0]
    group = QgsProject.instance().layerTreeRoot().findGroup("GUGiK").findGroup("ProjektA")
    assert group.findLayer(lyr.id()) is not None
    dock.output.ed_prefix.clear()
