# -*- coding: utf-8 -*-
"""Okno wyników skorowidza: buduje się z odpowiedzi WFS, zwraca zaznaczone adresy."""
import importlib

from conftest import PLUGIN_NAME, fixture_text


def test_skorowidz_results_dialog(monkeypatch):
    import qgis.utils
    from test_dock_smoke import _Iface
    from qgis.core import QgsProject
    monkeypatch.setattr(qgis.utils, "iface", _Iface(), raising=False)
    from qgis.PyQt.QtCore import Qt
    gml = importlib.import_module(f"{PLUGIN_NAME}.core.gml")
    dlg_mod = importlib.import_module(f"{PLUGIN_NAME}.wfs_index_dialog")
    feats = gml.read_typed(fixture_text("skorowidz_orto.gml"))
    dlg = dlg_mod.WfsIndexResultsDialog(feats)
    assert dlg.table.rowCount() == 1
    assert dlg.table.item(0, 0).checkState() == Qt.CheckState.Checked
    dlg.accept_selection()
    assert [u for _, u in dlg.selected_urls] == [feats[0]["attrs"]["url_do_pobrania"]]
    dlg.close()
    QgsProject.instance().removeAllMapLayers()
