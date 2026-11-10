# -*- coding: utf-8 -*-
"""
Testy rdzenia bez interfejsu QGIS.

Uruchomienie (z folderu nadrzędnego wtyczki albo z dowolnego miejsca):
    QT_QPA_PLATFORM=offscreen python -m pytest pobierz_dane_GUGiK/tests
Wymaga Pythona z QGIS (np. OSGeo4W Shell na Windows albo pakiet python3-qgis).
"""
import os
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = os.path.basename(PLUGIN_DIR)
FIXTURES = os.path.join(PLUGIN_DIR, "tests", "fixtures")
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def qgis_app():
    from qgis.core import QgsApplication
    app = QgsApplication.instance()
    if app is None:
        app = QgsApplication([], False)
        app.initQgis()
    import importlib
    from qgis.PyQt.QtCore import QSettings
    QSettings().setValue("locale/userLocale", "pl")   # testy sprawdzają polskie napisy z kodu
    importlib.import_module(f"{PLUGIN_NAME}.core.uldk").ENABLED = False   # bez prawdziwego ULDK
    yield app


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()
