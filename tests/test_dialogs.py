# -*- coding: utf-8 -*-
"""Wybór arkuszy skorowidza: warstwa na mapie, filtry, wybór kliknięciem, pobieranie."""
import importlib
import os

import pytest

from conftest import PLUGIN_NAME, fixture_text


def _point(x, y):
    from qgis.core import QgsPointXY
    return QgsPointXY(x, y)


@pytest.fixture
def canvas():
    from qgis.core import QgsCoordinateReferenceSystem
    from qgis.gui import QgsMapCanvas
    c = QgsMapCanvas()
    c.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2180"))
    yield c
    from qgis.core import QgsProject
    QgsProject.instance().removeAllMapLayers()


def _features():
    """Arkusz z 2025 (prawdziwa próbka) + kopia jako 2024 i drugi typ."""
    gml = importlib.import_module(f"{PLUGIN_NAME}.core.gml")
    from qgis.core import QgsGeometry
    real = gml.read_typed(fixture_text("skorowidz_orto_2025.gml"))[0]
    real["layer"] = "gugik:SkorowidzOrtofomapy2025"
    old = {"geom": QgsGeometry(real["geom"]), "layer": "gugik:SkorowidzOrtofomapy2024",
           "attrs": dict(real["attrs"], akt_rok=2024, kolor="CIR", wlk_pliku_mb=None,
                         url_do_pobrania=real["attrs"]["url_do_pobrania"].replace(".tif", "_24.tif"))}
    return [real, old]


def _dialog(canvas):
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.sheets")
    return mod, mod.SheetsDialog(_features(), canvas, "ortofotomapa")


def test_sheet_from_real_sample():
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.sheets")
    s = mod.Sheet.from_feature(_features()[0])
    assert (s.name, s.year, s.kind, s.size_mb) == ("M-34-64-D-d-1-4", "2025", "RGB", 1072.0)
    assert s.file_name == "83139_1483340_M-34-64-D-d-1-4.tif"
    assert mod.format_size(1072) == "1,0 GB" and mod.format_size(0) == ""


def test_layer_table_and_selection_stay_in_sync(canvas):
    from qgis.core import QgsProject
    from qgis.PyQt.QtCore import Qt
    mod, dlg = _dialog(canvas)
    layer = dlg.layer
    lid = layer.id()
    assert QgsProject.instance().mapLayer(lid) is not None
    assert layer.featureCount() == 2 and layer.selectedFeatureCount() == 2
    assert "Wybrano 2 z 2" in dlg.status.text() and "1,0 GB" in dlg.status.text()
    # odznaczenie w tabeli → zaznaczenie warstwy
    dlg.table.item(0, mod.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
    assert layer.selectedFeatureCount() == 1
    # zaznaczenie na warstwie (np. narzędziem QGIS) → tabela
    layer.selectAll()
    assert all(dlg.table.item(r, 0).checkState() == Qt.CheckState.Checked for r in range(2))
    dlg.close()
    assert QgsProject.instance().mapLayer(lid) is None


def test_year_filter_and_newest(canvas):
    mod, dlg = _dialog(canvas)
    assert dlg.cmb_year.isVisibleTo(dlg) and dlg.cmb_type.isVisibleTo(dlg)
    dlg.cmb_year.setCurrentIndex(dlg.cmb_year.findData("2024"))
    assert [s.year for s in dlg.selected()] == ["2024"]
    assert dlg.layer.featureCount() == 1            # na mapie tylko pasujące
    dlg.cmb_year.setCurrentIndex(0)
    dlg.select_visible()
    dlg.keep_newest()                                 # to samo godło → zostaje 2025
    assert [s.year for s in dlg.selected()] == ["2025"]
    dlg.close()


def test_click_on_map_toggles_sheet(canvas):
    mod, dlg = _dialog(canvas)
    dlg.layer.removeSelection()
    inside = dlg.sheets[0].geom.centroid().asPoint()
    dlg.toggle_at(inside)            # oba arkusze leżą w tym samym miejscu
    assert len(dlg.selected()) == 2
    dlg.toggle_at(inside)
    assert dlg.selected() == []
    dlg.toggle_at(_point(0, 0))      # poza arkuszami – nic
    assert dlg.selected() == []
    dlg.btn_pick.setChecked(True)
    assert canvas.mapTool() is dlg._tool
    dlg.close()
    assert canvas.mapTool() is not dlg._tool


def test_download_emits_choice_and_removing_layer_closes(canvas):
    from qgis.core import QgsProject
    mod, dlg = _dialog(canvas)
    got = []
    dlg.download_requested.connect(lambda sheets, skip: got.append((len(sheets), skip)))
    dlg.chk_skip.setChecked(True)
    dlg._download()
    assert got == [(2, True)] and dlg.layer is None
    mod, dlg2 = _dialog(canvas)
    dlg2.show()
    QgsProject.instance().removeMapLayer(dlg2.layer.id())
    assert dlg2.layer is None and not dlg2.isVisible()


def test_already_downloaded(tmp_path):
    mod = importlib.import_module(f"{PLUGIN_NAME}.ui.sheets")
    p = tmp_path / "a.tif"
    assert not mod.already_downloaded(str(p), 1)
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    assert mod.already_downloaded(str(p), 2) and mod.already_downloaded(str(p), 0)
    assert not mod.already_downloaded(str(p), 50)    # niepełny plik
    assert os.path.exists(p)
