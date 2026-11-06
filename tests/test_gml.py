# -*- coding: utf-8 -*-
"""Parser GML: zgodność z 1.3.0 i kolejność osi."""
import json
import os

import pytest

from conftest import FIXTURES, PLUGIN_NAME, fixture_text

PLUGIN = PLUGIN_NAME
EXPECTED = json.load(open(os.path.join(FIXTURES, "expected_v130.json"), encoding="utf-8"))


def _gml():
    import importlib
    return importlib.import_module(f"{PLUGIN}.core.gml")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_same_as_v130(name):
    from qgis.core import QgsGeometry
    feats = _gml().parse_features(fixture_text(name))
    expected = EXPECTED[name]
    assert len(feats) == len(expected)
    for got, exp in zip(feats, expected):
        assert got["attrs"] == exp["attrs"]
        g, e = QgsGeometry.fromWkt(got["geom"]), QgsGeometry.fromWkt(exp["geom"])
        assert g.wkbType() == e.wkbType(), (g.asWkt(), e.asWkt())
        assert g.asWkt(4) == e.asWkt(4)


def test_known_parcel_lands_in_poland_2180():
    """Działka z próbki EGiB: E≈284 tys., N≈366 tys. (oś N pierwsza w GML)."""
    from qgis.core import QgsGeometry
    feats = _gml().parse_features(fixture_text("egib_dzialki.gml"))
    c = QgsGeometry.fromWkt(feats[0]["geom"]).centroid().asPoint()
    assert 284100 < c.x() < 284300
    assert 366300 < c.y() < 366400


def test_short_epsg_form_is_not_swapped():
    gml = _gml()
    assert gml.axis_swapped("urn:ogc:def:crs:EPSG::2180") is True
    assert gml.axis_swapped("http://www.opengis.net/def/crs/EPSG/0/2180") is True
    assert gml.axis_swapped("EPSG:2180") is False
    assert gml.axis_swapped("") is True  # konwencja usług GUGiK


def test_invalid_xml_returns_empty():
    assert _gml().parse_features("<to nie jest xml") == []


def test_read_typed_reads_all_features():
    feats = _gml().read_typed(fixture_text("egib_dzialki.gml"))
    assert len(feats) == 2
    c = feats[0]["geom"].centroid().asPoint()
    assert 284100 < c.x() < 284300 and 366300 < c.y() < 366400
