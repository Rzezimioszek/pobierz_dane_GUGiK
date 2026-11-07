# -*- coding: utf-8 -*-
"""
Prawdziwe odpowiedzi usług (tools/zapisz_probki.py → tests/fixtures/live/).
Bez próbek testy są pomijane.
"""
import glob
import importlib
import json
import os

import pytest

from conftest import FIXTURES, PLUGIN_NAME

LIVE = os.path.join(FIXTURES, "live")
GML = sorted(glob.glob(os.path.join(LIVE, "*.gml")))

# Zasięg Polski w EPSG:2180 z zapasem (E, N)
E_RANGE, N_RANGE = (140_000, 900_000), (120_000, 800_000)


def mod(name):
    return importlib.import_module(f"{PLUGIN_NAME}.{name}")


@pytest.mark.skipif(not GML, reason="brak próbek – uruchom tools/zapisz_probki.py w QGIS")
@pytest.mark.parametrize("path", GML, ids=[os.path.basename(p) for p in GML])
def test_live_gml_parses_and_lands_in_poland(path):
    from qgis.core import QgsGeometry
    gml = mod("core.gml")
    text = open(path, encoding="utf-8").read()
    if gml.service_exception(text):
        pytest.skip(f"usługa zwróciła błąd: {gml.service_exception(text)[:100]}")
    if 'numberReturned="0"' in text[:3000]:
        pytest.skip("pusta odpowiedź – w prostokącie próbki nie ma obiektów tej warstwy")
    reader = gml.read_typed if os.path.basename(path).startswith("skorowidz") else gml.parse_features
    feats = reader(text)
    assert feats, "odpowiedź bez obiektów z geometrią"
    for f in feats[:50]:
        g = f["geom"] if isinstance(f["geom"], QgsGeometry) else QgsGeometry.fromWkt(f["geom"])
        c = g.centroid().asPoint()
        assert E_RANGE[0] < c.x() < E_RANGE[1] and N_RANGE[0] < c.y() < N_RANGE[1], \
            f"współrzędne poza Polską (zamienione osie?): {c.x():.0f}, {c.y():.0f}"


@pytest.mark.skipif(not os.path.exists(os.path.join(LIVE, "eziudp_api.json")),
                    reason="brak próbki EZiUDP")
def test_live_eziudp_api_has_wfs():
    client = mod("eziudp_client").EziudpClient()
    payload = json.load(open(os.path.join(LIVE, "eziudp_api.json"), encoding="utf-8"))
    recs = client.parse_api(payload)
    assert recs and any(r.url_wfs for r in recs)
