# -*- coding: utf-8 -*-
"""
Granice jednostek administracyjnych z PRG (do filtrowania zapytań po TERYT).
"""
from typing import Optional
from xml.sax.saxutils import escape  # nosec B406 – tylko escape tekstu do XML, bez parsowania

from qgis.core import QgsGeometry

from .core import gml, log, net
from .core.catalog import PRG_ADMIN_URL

_FILTER = ('<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
           'xmlns:ms="http://mapserver.gis.umn.edu/mapserver">'
           '<fes:PropertyIsEqualTo><fes:ValueReference>ms:JPT_KOD_JE</fes:ValueReference>'
           '<fes:Literal>{value}</fes:Literal></fes:PropertyIsEqualTo></fes:Filter>')


def layer_for_teryt(teryt: str) -> str:
    """Warstwa PRG właściwa dla postaci kodu TERYT."""
    if len(teryt) == 2 and teryt.isdigit():
        return "ms:A01_Granice_wojewodztw"
    if len(teryt) == 4 and teryt.isdigit():
        return "ms:A02_Granice_powiatow"
    if len(teryt) == 8 and "_" in teryt:
        return "ms:A05_Granice_jednostek_ewidencyjnych"
    if "." in teryt:
        return "ms:A06_Granice_obrebow_ewidencyjnych"
    if len(teryt) == 6 and teryt.isdigit():
        return "ms:A05_Granice_jednostek_ewidencyjnych"
    return "ms:A06_Granice_obrebow_ewidencyjnych"


class PRGClient:
    def __init__(self, url: str = PRG_ADMIN_URL):
        self.url = url
        self.session = net.make_session()

    def get_boundary_geometry(self, teryt: str) -> Optional[QgsGeometry]:
        """Geometria jednostki w EPSG:2180 (np. "14", "1412", "141201_1", "141201_1.0001")."""
        if not teryt:
            return None
        layer = layer_for_teryt(teryt)
        log.info(f"[PRG] Granica {teryt} z warstwy {layer}")
        geom = self._fetch(layer, teryt)
        if geom is None and layer.startswith("ms:A05"):
            log.info("[PRG] Próba z warstwą gmin")
            geom = self._fetch("ms:A03_Granice_gmin", teryt.replace("_", ""))
        return geom

    def _fetch(self, typename: str, value: str) -> Optional[QgsGeometry]:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typenames": typename,
            "outputFormat": "application/gml+xml; version=3.2",
            "language": "pol",
            "filter": _FILTER.format(value=escape(value)),
        }
        try:
            text = net.get(self.session, self.url, params, timeout=60).text
        except Exception as exc:
            log.warning(f"[PRG] Błąd pobierania geometrii: {exc}")
            return None
        features = gml.parse_features(text)
        if not features:
            log.warning(f"[PRG] Brak geometrii dla {value} w {typename}")
            return None
        return QgsGeometry.fromWkt(features[0]["geom"])
