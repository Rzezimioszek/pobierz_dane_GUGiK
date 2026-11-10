# -*- coding: utf-8 -*-
"""Tłumaczenie angielskie: komplet napisów w .ts, aktualny .qm, panel po angielsku."""
import importlib
import os
import sys

from conftest import PLUGIN_NAME

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "tools"))


def test_every_string_has_english_translation():
    import tlumaczenia
    strings, problems = tlumaczenia.collect()
    assert not problems, problems
    ts = tlumaczenia.read_ts()
    missing = sorted(s for s in strings if not ts.get(s))
    assert not missing, f"uruchom tools/tlumaczenia.py i uzupełnij .ts: {missing[:5]}"
    assert len(strings) > 300


def test_qm_is_up_to_date_and_translates_panel():
    import tlumaczenia
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QCoreApplication, QTranslator
    i18n = importlib.import_module(f"{PLUGIN_NAME}.core.i18n")
    admin = importlib.import_module(f"{PLUGIN_NAME}.ui.admin_units")
    net = importlib.import_module(f"{PLUGIN_NAME}.core.net")
    assert i18n.tr("Pobierz") == "Pobierz"            # bez tłumacza – polski z kodu
    tr = QTranslator()
    assert tr.load(tlumaczenia.QM)
    QCoreApplication.installTranslator(tr)
    saved, admin._CACHE = admin._CACHE, None          # słownik buduje napisy przy wczytaniu
    try:
        ts = tlumaczenia.read_ts()
        stale = [s for s in tlumaczenia.collect()[0] if i18n.tr(s) != ts[s]]
        assert not stale, f".qm starszy niż .ts – uruchom tools/tlumaczenia.py: {stale[:3]}"

        from test_dock_smoke import _Iface
        mod = importlib.import_module(f"{PLUGIN_NAME}.ui.dock")
        dock = mod.PD_GUGiKDock(_Iface())
        try:
            assert dock.btn_download.text() == "Download"
            assert dock.source_box.title.text() == "What to download"
            texts = [dock.sources.tree.topLevelItem(i).text(0)
                     for i in range(dock.sources.tree.topLevelItemCount())]
            assert "County services (EZiUDP)" in texts and "EGiB – parcels and buildings" in texts
            dock.sources.select_labels(["eziudp:egib_dzi"])
            assert "Cadastral parcels (EGiB)" in dock.sources.selection().names()
            assert dock.scope.teryt.cmb_woj.itemText(0) == "- Select -"
            assert any(k.endswith("(county 1465)") for k in dock.units.name_to_teryt)
            dock.scope.teryt.set_teryt("1465")           # podpowiedzi po angielsku nadal dają kod
            assert dock.scope.scope().teryt == "1465"
            assert "voivodeship" in dock.units.check("99x")[1]
        finally:
            dock.shutdown()
            dock.deleteLater()
            QgsApplication.processEvents()
        # komunikat błędu usługi: rozpoznawany także po angielsku
        msg = i18n.server_error("boom")
        assert msg == "The server returned an error: boom" and net.friendly(RuntimeError(msg)) == msg
    finally:
        QCoreApplication.removeTranslator(tr)
        admin._CACHE = saved
    assert i18n.tr("Pobierz") == "Pobierz"


def test_other_languages_get_english_and_unload_removes_translator():
    from qgis.PyQt.QtCore import QSettings
    from test_dock_smoke import _PluginIface
    main = importlib.import_module(f"{PLUGIN_NAME}.pobierz_dane_GUGiK")
    i18n = importlib.import_module(f"{PLUGIN_NAME}.core.i18n")
    QSettings().setValue("locale/userLocale", "de_DE")
    try:
        plugin = main.PD_GUGiK(_PluginIface())
        assert i18n.tr("Pobierz") == "Download"
        plugin.unload()
        assert i18n.tr("Pobierz") == "Pobierz"
        QSettings().setValue("locale/userLocale", "pl_PL")
        plugin = main.PD_GUGiK(_PluginIface())
        assert plugin.translator is None and i18n.tr("Pobierz") == "Pobierz"
        plugin.unload()
    finally:
        QSettings().setValue("locale/userLocale", "pl")
