# -*- coding: utf-8 -*-
"""Zapytania WFS 2.0: liczba obiektów i kolejne strony wyników."""
from typing import Optional

try:
    import defusedxml.ElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET  # nosec

from . import gml, log, net
from .i18n import server_error

PAGE_SIZE = 1000


class WfsClient:
    """
    Klient jednej warstwy WFS.

    `typename` może być zmieniane między zapytaniami (skorowidze mają
    osobną warstwę na rocznik).
    """

    def __init__(self, url: str, typename: str = "", geom_field: str = "geom",
                 skip_empty_filter: bool = False):
        self.url = url
        self.typename = typename
        self.geom_field = geom_field
        # MapServer zwraca pusty wynik dla pustego parametru filter – wtedy go pomijamy
        self.skip_empty_filter = skip_empty_filter
        self.session = net.make_session()

    # ------------------------------------------------------------------ zapytania

    def hits(self, filter_xml: str) -> int:
        """Liczba pasujących obiektów (numberMatched); -1 gdy serwer jej nie podaje."""
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": self.typename, "startIndex": "0", "count": "100",
            "filter": filter_xml.strip(),
        }
        resp = net.get(self.session, self.url, params, timeout=60)
        root = ET.fromstring(resp.content)  # nosec
        matched = root.get("numberMatched", "unknown")
        if matched == "unknown":
            log.warning("[WFS] numberMatched=unknown")
            return -1
        return int(matched)

    def page(self, filter_xml: str, start_index: int = 0, count: int = PAGE_SIZE,
             attributes: Optional[list] = None) -> str:
        """Jedna strona wyników jako tekst GML."""
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typenames": self.typename, "startIndex": start_index, "count": count,
        }
        if filter_xml and filter_xml.strip():
            params["filter"] = filter_xml.strip()
        elif not self.skip_empty_filter:
            params["filter"] = ""
        if attributes:
            props = list(attributes)
            if self.geom_field not in props:  # geometria zawsze potrzebna
                props.append(self.geom_field)
            params["propertyName"] = ",".join(props)
        log.info(f"[WFS] {self.typename} start={start_index} count={count}")
        text = net.get(self.session, self.url, params, timeout=180).text
        # MapServer zgłasza błąd zapytania jako ExceptionReport z kodem HTTP 200 –
        # bez tej kontroli wyglądałoby to jak „brak obiektów” i ucięłoby wynik.
        error = gml.service_exception(text)
        if error:
            raise RuntimeError(server_error(error))
        return text
