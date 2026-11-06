# -*- coding: utf-8 -*-
"""Narzędzia mapy: rysowanie prostokąta i podświetlenie zakresu."""
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject, QgsRectangle
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtGui import QColor

from ..qt_compat import GEOM_POLYGON

FILL = QColor(255, 200, 0, 60)
EDGE = QColor(230, 150, 0, 220)


def make_band(canvas) -> QgsRubberBand:
    band = QgsRubberBand(canvas, GEOM_POLYGON)
    band.setFillColor(FILL)
    band.setStrokeColor(EDGE)
    band.setWidth(2)
    return band


def show_geometry(band: QgsRubberBand, geom_2180: QgsGeometry, canvas, zoom: bool = True) -> None:
    """Rysuje geometrię z EPSG:2180 na mapie (w układzie mapy) i opcjonalnie przybliża."""
    band.reset(GEOM_POLYGON)
    if geom_2180 is None or geom_2180.isEmpty():
        return
    g = QgsGeometry(geom_2180)
    dest = canvas.mapSettings().destinationCrs()
    src = QgsCoordinateReferenceSystem("EPSG:2180")
    if dest.isValid() and dest != src:
        g.transform(QgsCoordinateTransform(src, dest, QgsProject.instance()))
    band.setToGeometry(g, None)
    if zoom:
        rect = g.boundingBox()
        rect.scale(1.1)
        canvas.setExtent(rect)
        canvas.refresh()


class RectangleMapTool(QgsMapToolEmitPoint):
    """Prostokąt rysowany myszą; `callback(QgsRectangle)` w układzie mapy."""

    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.band = make_band(canvas)
        self.start = None

    def _rect(self, end) -> QgsRectangle:
        return QgsRectangle(min(self.start.x(), end.x()), min(self.start.y(), end.y()),
                            max(self.start.x(), end.x()), max(self.start.y(), end.y()))

    def canvasPressEvent(self, e):
        self.start = self.toMapCoordinates(e.pos())
        self.band.reset(GEOM_POLYGON)

    def canvasMoveEvent(self, e):
        if self.start is None:
            return
        self.band.setToGeometry(QgsGeometry.fromRect(self._rect(self.toMapCoordinates(e.pos()))), None)

    def canvasReleaseEvent(self, e):
        if self.start is None:
            return
        rect = self._rect(self.toMapCoordinates(e.pos()))
        self.start = None
        self.band.reset(GEOM_POLYGON)
        if rect.width() > 0 and rect.height() > 0:
            self.callback(rect)

    def deactivate(self):
        self.band.reset(GEOM_POLYGON)
        super().deactivate()
