# -*- coding: utf-8 -*-
"""
Filtry FES 2.0 dla zapytań WFS (EPSG:2180).

Kolejność osi: EPSG:2180 w postaci URN ma oś N (Y) przed E (X), dlatego
w Envelope piszemy '{ymin} {xmin}', a w posList '{y} {x}'. Zawsze z URN:
krótkie „EPSG:2180” GeoServer (np. PRG – adresy i ulice) czyta jako X Y,
a MapServer jako Y X – ten sam wielokąt trafiał wtedy w inne miejsce.
"""
from xml.sax.saxutils import escape  # nosec B406 – tylko escape tekstu do XML, bez parsowania

try:
    import defusedxml.ElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET  # nosec – parsujemy tylko własne filtry

from qgis.core import QgsGeometry, QgsWkbTypes

from . import log
from .i18n import tr
from ..qt_compat import GEOM_POLYGON

FES = 'xmlns:fes="http://www.opengis.net/fes/2.0"'
GML = 'xmlns:gml="http://www.opengis.net/gml/3.2"'


def _wrap(inner: str, with_gml: bool = False) -> str:
    ns = f"{FES} {GML}" if with_gml else FES
    return f"<fes:Filter {ns}>{inner}</fes:Filter>"


def bbox(geom_field: str, xmin: float, ymin: float, xmax: float, ymax: float) -> str:
    xmin, ymin, xmax, ymax = (round(v, 2) for v in (xmin, ymin, xmax, ymax))
    return _wrap(
        f"<fes:BBOX><fes:ValueReference>{geom_field}</fes:ValueReference>"
        f'<gml:Envelope srsName="urn:ogc:def:crs:EPSG::2180">'
        f"<gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner>"
        f"<gml:upperCorner>{ymax} {xmax}</gml:upperCorner>"
        f"</gml:Envelope></fes:BBOX>",
        with_gml=True,
    )


def _polygon_parts(geom: QgsGeometry) -> list:
    """Zewnętrzne pierścienie wszystkich części poligonu (krzywe zamienione na odcinki)."""
    if QgsWkbTypes.isCurvedType(geom.wkbType()):
        geom = QgsGeometry(geom.constGet().segmentize())
    parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
    rings = []
    for part in parts:
        if part.isEmpty() or QgsWkbTypes.geometryType(part.wkbType()) != GEOM_POLYGON:
            continue
        poly = part.asPolygon()
        if poly and poly[0]:
            rings.append(poly[0])
    return rings


def _intersects(geom_field: str, ring: list, n: int) -> str:
    coords = [f"{pt.y()} {pt.x()}" for pt in ring]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return (
        f"<fes:Intersects><fes:ValueReference>{geom_field}</fes:ValueReference>"
        f'<gml:Polygon gml:id="polygon_filter_{n}" srsName="urn:ogc:def:crs:EPSG::2180">'
        f"<gml:exterior><gml:LinearRing>"
        f'<gml:posList srsDimension="2">{" ".join(coords)}</gml:posList>'
        f"</gml:LinearRing></gml:exterior></gml:Polygon></fes:Intersects>"
    )


def spatial(geom_field: str, wkt: str, use_bbox: bool = True) -> str:
    """
    BBOX albo Intersects z poligonem. Poligon wieloczęściowy = osobne Intersects
    dla każdej części połączone przez Or (żadna część nie jest pomijana).
    Dziury w poligonach nie trafiają do zapytania – dokładne przycięcie robi
    zadanie pobierania (Query.clip_geom).
    """
    geom = QgsGeometry.fromWkt(wkt)
    if geom is None or geom.isNull() or geom.isEmpty():
        raise ValueError(tr("Nieprawidłowa geometria WKT: ") + wkt[:80])

    rect = geom.boundingBox()
    rings = [] if use_bbox or QgsWkbTypes.geometryType(geom.wkbType()) != GEOM_POLYGON \
        else _polygon_parts(geom)
    if not rings:
        return bbox(geom_field, rect.xMinimum(), rect.yMinimum(),
                    rect.xMaximum(), rect.yMaximum())
    conditions = [_intersects(geom_field, ring, i + 1) for i, ring in enumerate(rings)]
    inner = conditions[0] if len(conditions) == 1 else "<fes:Or>" + "".join(conditions) + "</fes:Or>"
    return _wrap(inner, with_gml=True)


def ids(id_field: str, values: list) -> str:
    """Równość (albo LIKE, gdy wartość ma * lub ?) dla listy identyfikatorów."""
    conditions = []
    for value in values:
        value = (value or "").strip()
        if not value:
            continue
        lit = escape(value)
        if "*" in value or "?" in value:
            conditions.append(
                f'<fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\">'
                f"<fes:ValueReference>{id_field}</fes:ValueReference>"
                f"<fes:Literal>{lit}</fes:Literal></fes:PropertyIsLike>")
        else:
            conditions.append(
                f"<fes:PropertyIsEqualTo><fes:ValueReference>{id_field}</fes:ValueReference>"
                f"<fes:Literal>{lit}</fes:Literal></fes:PropertyIsEqualTo>")
    if not conditions:
        return ""
    inner = conditions[0] if len(conditions) == 1 else "<fes:Or>" + "".join(conditions) + "</fes:Or>"
    return _wrap(inner)


def attribute(field: str, value: str, like: bool = False) -> str:
    """Równość albo LIKE z '*' na końcu; prefiks przestrzeni nazw jest usuwany."""
    clean = field.split(":")[-1]
    lit = escape(value)
    if like:
        return _wrap(
            f'<fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\">'
            f"<fes:ValueReference>{clean}</fes:ValueReference>"
            f"<fes:Literal>{lit}*</fes:Literal></fes:PropertyIsLike>")
    return _wrap(
        f"<fes:PropertyIsEqualTo><fes:ValueReference>{clean}</fes:ValueReference>"
        f"<fes:Literal>{lit}</fes:Literal></fes:PropertyIsEqualTo>")


def combine(filters: list) -> str:
    """Łączy filtry operatorem AND (puste pomija)."""
    parts = []
    for f in filters:
        if not f:
            continue
        try:
            root = ET.fromstring(f)  # nosec – własny XML
            parts.append("".join(ET.tostring(child, encoding="unicode") for child in root))
        except ET.ParseError as exc:
            log.warning(f"[FES] combine – błąd parsowania XML: {exc}")
            parts.append(f)
    if not parts:
        return ""
    inner = parts[0] if len(parts) == 1 else "<fes:And>" + "".join(parts) + "</fes:And>"
    return _wrap(inner, with_gml=True)
