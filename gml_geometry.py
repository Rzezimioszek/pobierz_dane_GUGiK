# -*- coding: utf-8 -*-
"""
Bezpieczna konwersja elementu geometrii GML (QDomElement) na QgsGeometry
przez GDAL/OGR.

Dlaczego nie QgsOgcUtils.geometryFromGML:
  QgsOgcUtils obsługuje tylko przestrzeń nazw GML 2/3.1
  (http://www.opengis.net/gml). Dla GML 3.2 (usługi GUGiK) zwraca pustą
  geometrię, a gdy element ma srsName typu urn:ogc:def:crs:EPSG::2180
  (oś odwrócona), wywołuje swapXy() na pustej geometrii, co w QGIS 4
  kończy się crashem całego programu (access violation).

Kolejność osi: zgodnie z konwencją GML/WFS (jak w QGIS/GeoServer) osie są
zamieniane, gdy srsName jest w postaci URN / http://www.opengis.net/def/crs
i układ ma kolejność northing/easting lub lat/lon (np. EPSG:2180).
"""
from qgis.core import QgsGeometry
from qgis.PyQt.QtCore import QByteArray, QBuffer, QIODevice, QTextStream

try:
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
    _HAS_OGR = True
except Exception:  # pragma: no cover
    _HAS_OGR = False


def _element_to_string(elem) -> str:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ts = QTextStream(buf)
    elem.save(ts, 0)
    ts.flush()
    buf.close()
    return bytes(ba.data()).decode("utf-8", errors="replace")


def _axis_inverted(srs_name: str) -> bool:
    if not srs_name:
        return False
    s = srs_name.strip()
    if s.startswith("EPSG:") or s.startswith("http://www.opengis.net/gml/srs/"):
        return False  # konwencja: x/y (easting, northing)
    try:
        srs = osr.SpatialReference()
        srs.SetFromUserInput(s)
        return bool(srs.EPSGTreatsAsNorthingEasting() or srs.EPSGTreatsAsLatLong())
    except Exception:
        return False


def geometry_from_gml_element(elem):
    """Zwraca QgsGeometry albo None. Bezpieczne w wątku QgsTask."""
    if not _HAS_OGR or elem is None or elem.isNull():
        return None
    try:
        gml = _element_to_string(elem)
        og = ogr.CreateGeometryFromGML(gml)
        if og is None or og.IsEmpty():
            return None
        geom = QgsGeometry.fromWkt(og.ExportToIsoWkt())
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        if _axis_inverted(elem.attribute("srsName", "")):
            geom.get().swapXy()
        return geom
    except Exception:
        return None
