# -*- coding: utf-8 -*-
"""
Jeden parser GML dla całej wtyczki.

`parse_features(gml)` – obiekty jako {'geom': WKT, 'attrs': {nazwa: tekst}}:
    atrybuty = tekst każdego elementu-dziecka obiektu (jak w 1.x),
    geometria = GDAL/OGR (obsługuje GML 2/3/3.2, multi, łuki, powierzchnie).

`read_typed(gml)` – cała odpowiedź przez sterownik GML w GDAL (typy pól z danych);
    dla skorowidzów i serwerów, których odpowiedzi DOM nie rozbiera.

Kolejność osi: zamiana X/Y, gdy srsName jest w postaci URN lub
http://www.opengis.net/def/crs/... i układ ma kolejność N/E (np. EPSG:2180),
tak jak w QGIS i GeoServerze. Brak srsName = konwencja usług GUGiK (N/E).

Bez QgsOgcUtils.geometryFromGML: dla GML 3.2 z srsName URN zamyka QGIS 4.
"""
import uuid
from functools import lru_cache
from typing import Optional

from qgis.core import QgsGeometry
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QTextStream
from qgis.PyQt.QtXml import QDomDocument, QDomElement, QDomNode

from . import log
from ..qt_compat import dom_set_content

try:
    from osgeo import gdal, ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
except ImportError:  # pragma: no cover – GDAL jest częścią każdej instalacji QGIS
    gdal = ogr = osr = None

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
        srs = osr.SpatialReference()
        srs.SetFromUserInput(s)
        return bool(srs.EPSGTreatsAsNorthingEasting() or srs.EPSGTreatsAsLatLong())
    except Exception:
        return False


def geometry_from_element(elem: QDomElement) -> Optional[QgsGeometry]:
    """Geometria z elementu GML (np. gml:Polygon) albo None."""
    if ogr is None or elem is None or elem.isNull():
        return None
    try:
        og = ogr.CreateGeometryFromGML(_element_to_string(elem))
    except Exception as exc:
        log.warning(f"[GML] Nieczytelna geometria {elem.localName()}: {exc}")
        return None
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


# ---------------------------------------------------------------- odczyt przez GDAL

def read_typed(gml: str) -> list:
    """
    Cała odpowiedź przez sterownik GML w GDAL, w pamięci (/vsimem/, bez plików
    tymczasowych). Zwraca [{'geom': QgsGeometry, 'attrs': {...}}] z typami pól GDAL.
    """
    if gdal is None:
        return []
    path = f"/vsimem/pd_gugik_{uuid.uuid4().hex}.gml"
    gdal.FileFromMemBuffer(path, gml.encode("utf-8") if isinstance(gml, str) else gml)
    features = []
    ds = None
    # Opcja tylko dla tego wątku: odpowiedź nie ma odnośników xlink do rozwiązywania
    gdal.SetThreadLocalConfigOption("GML_SKIP_RESOLVE_ELEMS", "ALL")
    try:
        ds = ogr.Open(path)
        if ds is None:
            return []
        for li in range(ds.GetLayerCount()):
            layer = ds.GetLayer(li)
            defn = layer.GetLayerDefn()
            names = [defn.GetFieldDefn(k).GetName() for k in range(defn.GetFieldCount())]
            for f in layer:
                og = f.GetGeometryRef()
                if og is None or og.IsEmpty():
                    continue
                geom = QgsGeometry.fromWkt(og.ExportToIsoWkt())
                if geom.isNull():
                    continue
                features.append({"geom": geom,
                                 "attrs": {n: f.GetField(n) for n in names}})
    except Exception as exc:
        log.warning(f"[GML] Odczyt przez GDAL nie powiódł się: {exc}")
    finally:
        ds = None
        gdal.SetThreadLocalConfigOption("GML_SKIP_RESOLVE_ELEMS", None)
        for leftover in gdal.ReadDir("/vsimem/") or []:
            if leftover.startswith(path[len("/vsimem/"):-4]):
                gdal.Unlink("/vsimem/" + leftover)
    return features
