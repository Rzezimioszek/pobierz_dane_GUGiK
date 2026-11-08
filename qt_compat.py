# -*- coding: utf-8 -*-
"""
Wspólne stałe i drobne różnice QGIS 3.40+ (Qt5) ↔ QGIS 4 (Qt6).

Od wersji 1.9.10 minimalna wersja to QGIS 3.40 LTR, więc enumy Qgis.*
i QMetaType działają tak samo w obu wersjach. Zostały tylko dwie
różnice samego Qt: miejsce QAction i sygnatura QDomDocument.setContent.
"""
from qgis.core import Qgis
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtXml import QDomDocument

# ---------------------------------------------------------------- QAction
try:  # Qt6 (oraz nowsze QGIS 3 udostępniające alias)
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

# ------------------------------------------------------- typy pól (QgsField)
# Od QGIS 3.40 (minimalna wersja wtyczki) QgsField przyjmuje QMetaType.Type,
# a enumy geometrii i warstw są w przestrzeni Qgis.* – tak samo jak w QGIS 4.
FIELD_STRING = QMetaType.Type.QString
FIELD_DOUBLE = QMetaType.Type.Double
FIELD_INT = QMetaType.Type.Int
FIELD_LONGLONG = QMetaType.Type.LongLong
FIELD_UINT = QMetaType.Type.UInt
FIELD_ULONGLONG = QMetaType.Type.ULongLong
FIELD_INTEGER_TYPES = (FIELD_INT, FIELD_LONGLONG, FIELD_UINT, FIELD_ULONGLONG)

# ------------------------------------------------------- geometrie i warstwy
GEOM_POLYGON = Qgis.GeometryType.Polygon
GEOM_LINE = Qgis.GeometryType.Line
GEOM_POINT = Qgis.GeometryType.Point
WKB_POLYGON = Qgis.WkbType.Polygon
WKB_MULTIPOLYGON = Qgis.WkbType.MultiPolygon
LAYER_VECTOR = Qgis.LayerType.Vector
LAYER_FILTER_POLYGON = Qgis.LayerFilter.PolygonLayer


# ------------------------------------------------------------- QDomDocument
def dom_set_content(doc: QDomDocument, content) -> bool:
    """
    Wczytuje XML do QDomDocument z przetwarzaniem przestrzeni nazw.
    Zwraca True przy powodzeniu.

    PyQt5 / starsze Qt6: setContent(data, True) -> (bool, msg, line, col)
    Qt >= 6.5:           setContent(data, ParseOption) -> ParseResult
    """
    parse_option = getattr(QDomDocument, "ParseOption", None)
    if parse_option is not None and hasattr(parse_option, "UseNamespaceProcessing"):
        res = doc.setContent(content, parse_option.UseNamespaceProcessing)
    else:
        res = doc.setContent(content, True)
    if isinstance(res, tuple):
        return bool(res[0])
    if hasattr(res, "errorMessage"):
        return not res.errorMessage
    return bool(res)
