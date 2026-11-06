# -*- coding: utf-8 -*-
"""
Warstwa zgodności QGIS 3 (Qt5/PyQt5) ↔ QGIS 4 (Qt6/PyQt6).

W QGIS 4 usunięto przestarzałe aliasy enumów (QgsWkbTypes.Polygon,
QgsWkbTypes.PolygonGeometry, QVariant.String itd.), a Qt6 przeniósł
QAction do QtGui. Ten moduł wybiera właściwy wariant w czasie importu,
dzięki czemu reszta wtyczki działa w obu wersjach.
"""
from qgis.core import Qgis, QgsWkbTypes
from qgis.PyQt.QtXml import QDomDocument

# ---------------------------------------------------------------- QAction
try:  # Qt6 (oraz nowsze QGIS 3 udostępniające alias)
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

# ------------------------------------------------------- typy pól (QgsField)
# QgsField przyjmuje QMetaType.Type od QGIS 3.38; w QGIS 4 to jedyna opcja.
if Qgis.QGIS_VERSION_INT >= 33800:
    from qgis.PyQt.QtCore import QMetaType
    FIELD_STRING = QMetaType.Type.QString
    FIELD_DOUBLE = QMetaType.Type.Double
    FIELD_INT = QMetaType.Type.Int
    FIELD_LONGLONG = QMetaType.Type.LongLong
    FIELD_UINT = QMetaType.Type.UInt
    FIELD_ULONGLONG = QMetaType.Type.ULongLong
else:
    from qgis.PyQt.QtCore import QVariant
    # QGIS < 3.38 (getattr, żeby statyczny skaner Qt6 nie zgłaszał QVariant.X)
    FIELD_STRING = getattr(QVariant, "String")
    FIELD_DOUBLE = getattr(QVariant, "Double")
    FIELD_INT = getattr(QVariant, "Int")
    FIELD_LONGLONG = getattr(QVariant, "LongLong")
    FIELD_UINT = getattr(QVariant, "UInt")
    FIELD_ULONGLONG = getattr(QVariant, "ULongLong")

FIELD_INTEGER_TYPES = (FIELD_INT, FIELD_LONGLONG, FIELD_UINT, FIELD_ULONGLONG)

# ------------------------------------------- typy geometrii / warstw (3.30+)
if hasattr(Qgis, "GeometryType"):
    GEOM_POLYGON = Qgis.GeometryType.Polygon
    GEOM_LINE = Qgis.GeometryType.Line
    GEOM_POINT = Qgis.GeometryType.Point
else:
    # QGIS < 3.30 (getattr, żeby statyczny skaner Qt6 nie zgłaszał starych aliasów)
    GEOM_POLYGON = getattr(QgsWkbTypes, "PolygonGeometry")
    GEOM_LINE = getattr(QgsWkbTypes, "LineGeometry")
    GEOM_POINT = getattr(QgsWkbTypes, "PointGeometry")

if hasattr(Qgis, "WkbType"):
    WKB_POLYGON = Qgis.WkbType.Polygon
    WKB_MULTIPOLYGON = Qgis.WkbType.MultiPolygon
else:
    WKB_POLYGON = getattr(QgsWkbTypes, "Polygon")
    WKB_MULTIPOLYGON = getattr(QgsWkbTypes, "MultiPolygon")

if hasattr(Qgis, "LayerType"):
    LAYER_VECTOR = Qgis.LayerType.Vector
else:
    from qgis.core import QgsMapLayerType
    LAYER_VECTOR = QgsMapLayerType.VectorLayer

# Filtr warstw poligonowych dla QgsMapLayerComboBox (Qgis.LayerFilter od 3.34)
if hasattr(Qgis, "LayerFilter"):
    LAYER_FILTER_POLYGON = Qgis.LayerFilter.PolygonLayer
else:
    try:
        from qgis.core import QgsMapLayerProxyModel
    except ImportError:
        from qgis.gui import QgsMapLayerProxyModel
    LAYER_FILTER_POLYGON = getattr(QgsMapLayerProxyModel, "PolygonLayer")


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
