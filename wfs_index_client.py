# -*- coding: utf-8 -*-
"""Klient WFS skorowidzów GUGiK (ortofotomapy, NMT, LIDAR): osobna warstwa na rocznik."""
from .core import filters
from .core.wfs import WfsClient

DEFAULT_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/Skorowidze"


class WFSIndexClient(WfsClient):
    def __init__(self, url: str = DEFAULT_URL):
        super().__init__(url, typename="", geom_field="gugik:msGeometry")

    def build_spatial_filter(self, wkt: str, use_bbox: bool = True) -> str:
        return filters.spatial(self.geom_field, wkt, use_bbox)

    def download(self, filter_xml: str, layer_name: str, start_index: int = 0,
                 count: int = 1000, attributes=None) -> str:
        """Strona wyników z warstwy `layer_name`, np. 'gugik:SkorowidzOrtofomapy2025'."""
        self.typename = layer_name
        return self.page(filter_xml, start_index, count, attributes)
