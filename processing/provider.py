# -*- coding: utf-8 -*-
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms import ALGORITHMS, ICON


class GugikProvider(QgsProcessingProvider):
    """Przybornik Processing → „Pobierz dane GUGiK”."""

    def id(self):
        return "pobierz_dane_gugik"

    def name(self):
        return "Pobierz dane GUGiK"

    def icon(self):
        return QIcon(ICON)

    def loadAlgorithms(self):
        for alg in ALGORITHMS:
            self.addAlgorithm(alg())
