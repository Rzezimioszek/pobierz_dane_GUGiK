# -*- coding: utf-8 -*-
"""
Jeden parser GML dla całej wtyczki.

`parse_features(gml)` – obiekty jako {'geom': WKT, 'attrs': {nazwa: tekst}}:
    atrybuty = tekst każdego elementu-dziecka obiektu (jak w 1.x),
    geometria = GDAL/OGR (obsługuje GML 2/3/3.2, multi, łuki, powierzchnie).

`read_typed(gml)` – cała odpowiedź jako warstwa OGR w QGIS (typy pól z danych),
    dokładnie jak w 1.x; dla skorowidzów.

Kolejność osi: zamiana X/Y, gdy srsName jest w postaci URN lub
http://www.opengis.net/def/crs/... i układ ma kolejność N/E (np. EPSG:2180),
tak jak w QGIS i GeoServerze. Brak srsName = konwencja usług GUGiK (N/E).

Bez QgsOgcUtils.geometryFromGML: dla GML 3.2 z srsName URN zamyka QGIS 4.
"""
import os
import warnings
from functools import lru_cache
from typing import Optional

from qgis.core import QgsGeometry
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QTextStream
from qgis.PyQt.QtXml import QDomDocument, QDomElement, QDomNode

from . import log
from ..qt_compat import dom_set_content

try:
    # Bez UseExceptions(): to przełącznik globalny dla całego QGIS (inne wtyczki,
    # Processing). Błędy obsługujemy po zwracanych wartościach.
    from osgeo import ogr, osr
except ImportError:  # pragma: no cover – GDAL jest częścią każdej instalacji QGIS
    ogr = osr = None

WFS2_NS = "http://www.opengis.net/wfs/2.0"
GEOMETRY_PROPERTIES = ("geom", "msGeometry", "geometry", "GEOMETRIA")


# ---------------------------------------------------------------- geometria

def _element_to_string(elem: QDomElement) -> str:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    stream = QTextStream(buf)
    elem.save(stream, 0)
    stream.flush()
    buf.close()
    return bytes(ba.data()).decode("utf-8", errors="replace")


@lru_cache(maxsize=32)
def axis_swapped(srs_name: str) -> bool:
    """Czy współrzędne w GML mają kolejność N/E (lat/lon) dla tego srsName."""
    if not srs_name:
        return True  # usługi GUGiK: EPSG:2180, oś N pierwsza
    s = srs_name.strip()
    if s.upper().startswith("EPSG:") or s.startswith("http://www.opengis.net/gml/srs/"):
        return False  # postać krótka: x/y (E/N)
    if osr is None:
        return False
    try:
        with warnings.catch_warnings():  # GDAL ≥3.9: FutureWarning o domyślnym trybie wyjątków
            warnings.simplefilter("ignore", FutureWarning)
            srs = osr.SpatialReference()
            if srs.SetFromUserInput(s) != 0:
                return False
            return bool(srs.EPSGTreatsAsNorthingEasting() or srs.EPSGTreatsAsLatLong())
    except Exception:
        return False


def geometry_from_element(elem: QDomElement) -> Optional[QgsGeometry]:
    """Geometria z elementu GML (np. gml:Polygon) albo None."""
    if ogr is None or elem is None or elem.isNull():
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            og = ogr.CreateGeometryFromGML(_element_to_string(elem))
    except Exception as exc:
        og = None
        log.warning(f"[GML] Nieczytelna geometria {elem.localName()}: {exc}")
    if og is None or og.IsEmpty():
        return None
    geom = QgsGeometry.fromWkt(og.ExportToIsoWkt())
    if geom.isNull() or geom.isEmpty():
        return None
    if axis_swapped(elem.attribute("srsName", "")):
        geom.get().swapXy()
    return geom


# ---------------------------------------------------------------- obiekty

def _first_element_child(node: QDomNode) -> Optional[QDomElement]:
    child = node.firstChild()
    while not child.isNull():
        if child.nodeType() == QDomNode.NodeType.ElementNode:
            return child.toElement()
        child = child.nextSibling()
    return None


def parse_features(gml: str) -> list:
    """Obiekty z odpowiedzi WFS: [{'geom': WKT, 'attrs': {...}}]; bez geometrii – pomijane."""
    doc = QDomDocument()
    if not dom_set_content(doc, gml):
        log.warning("[GML] Odpowiedź nie jest poprawnym XML")
        return []

    root = doc.documentElement()
    members = root.elementsByTagNameNS(WFS2_NS, "member")
    if members.count() == 0:
        members = root.elementsByTagName("wfs:member")
    if members.count() == 0:  # WFS 1.x: gml:featureMember
        members = root.elementsByTagNameNS("http://www.opengis.net/gml", "featureMember")

    features = []
    for i in range(members.count()):
        feature = _first_element_child(members.item(i))
        if feature is None:
            continue
        attrs, geom = {}, None
        prop = feature.firstChild()
        while not prop.isNull():
            if prop.nodeType() == QDomNode.NodeType.ElementNode:
                elem = prop.toElement()
                name = elem.localName() or elem.tagName().split(":")[-1]
                if name in GEOMETRY_PROPERTIES:
                    geom_elem = _first_element_child(elem)
                    if geom_elem is not None and geom is None:
                        geom = geometry_from_element(geom_elem)
                else:
                    attrs[name] = elem.text()
            prop = prop.nextSibling()
        if geom is not None:
            features.append({"geom": geom.asWkt(), "attrs": attrs})
    return features


# ---------------------------------------------------------------- odczyt jako warstwa OGR

def service_exception(text: str) -> str:
    """Treść ows:ExceptionReport z odpowiedzi serwera albo ''."""
    if "ExceptionReport" not in text[:2000] and "ServiceException" not in text[:2000]:
        return ""
    doc = QDomDocument()
    if not dom_set_content(doc, text):
        return text[:300]
    for tag in ("ExceptionText", "ServiceException"):
        nodes = doc.elementsByTagNameNS("*", tag)
        if nodes.count() == 0:
            nodes = doc.elementsByTagName(tag)
        if nodes.count():
            return nodes.item(0).toElement().text().strip()
    return doc.documentElement().text().strip()[:300]


def read_typed(gml: str) -> list:
    """
    Cała odpowiedź wczytana jako warstwa OGR (plik tymczasowy, jak w 1.x).
    Zwraca [{'geom': QgsGeometry, 'attrs': {...}}] z typami pól z danych.
    Gdy serwer zwrócił wyjątek zamiast danych – RuntimeError z jego treścią.
    """
    import shutil
    import tempfile
    from qgis.core import QgsVectorLayer

    error = service_exception(gml)
    if error:
        raise RuntimeError(f"Serwer zwrócił błąd: {error}")

    folder = tempfile.mkdtemp(prefix="pd_gugik_")
    path = os.path.join(folder, "odpowiedz.gml")
    features = []
    vl = None
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(gml)
        vl = QgsVectorLayer(path, "tmp", "ogr")
        if not vl.isValid():
            log.warning("[GML] Odpowiedź nie dała się wczytać jako warstwa")
            return []
        names = vl.fields().names()
        for feat in vl.getFeatures():
            if not feat.hasGeometry():
                continue
            features.append({"geom": QgsGeometry(feat.geometry()),
                             "attrs": {n: feat[n] for n in names}})
    finally:
        del vl  # zwalnia plik (Windows) przed usunięciem
        shutil.rmtree(folder, ignore_errors=True)
    return features
