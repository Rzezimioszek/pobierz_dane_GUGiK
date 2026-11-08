# -*- coding: utf-8 -*-
"""Lokalny, udawany serwer WFS 2.0 do testów pobierania (stronicowanie, błędy)."""
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HEADER = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<wfs:FeatureCollection xmlns:ms="http://mapserver.gis.umn.edu/mapserver" '
          'xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:wfs="http://www.opengis.net/wfs/2.0" '
          'numberMatched="{matched}" numberReturned="{returned}">')


def parcel(i: int) -> str:
    n, e = 366000 + (i % 100) * 20, 284000 + (i // 100) * 20
    ring = f"{n} {e} {n} {e + 10} {n + 10} {e + 10} {n + 10} {e} {n} {e}"
    return (f'<wfs:member><ms:dzialki gml:id="d.{i}"><ms:geom>'
            f'<gml:Polygon gml:id="d.{i}.g" srsName="urn:ogc:def:crs:EPSG::2180"><gml:exterior>'
            f'<gml:LinearRing><gml:posList srsDimension="2">{ring}</gml:posList></gml:LinearRing>'
            f'</gml:exterior></gml:Polygon></ms:geom>'
            f'<ms:ID_DZIALKI>022602_1.0003.{i}</ms:ID_DZIALKI><ms:NUMER_OBREBU>0003</ms:NUMER_OBREBU>'
            f'</ms:dzialki></wfs:member>')


class FakeWfs:
    """
    `total` obiektów, stronicowanie po startIndex/count.
    `fail_filters`: fragmenty filtra, dla których serwer odpowiada 500.
    `exception_filters`: fragmenty filtra → ExceptionReport z kodem 200 (jak MapServer).
    `max_count`: serwer oddaje najwyżej tyle obiektów na stronę (limit po stronie usługi).
    """

    def __init__(self, total: int = 1005, fail_filters=(), exception_filters=(), max_count=None):
        self.total = total
        self.fail_filters = list(fail_filters)
        self.exception_filters = list(exception_filters)
        self.max_count = max_count
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                outer.requests.append(q)
                if any(f in q.get("filter", "") for f in outer.fail_filters):
                    self.send_response(500)
                    self.end_headers()
                    return
                if any(f in q.get("filter", "") for f in outer.exception_filters):
                    data = ('<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
                            '<ows:Exception><ows:ExceptionText>msWFSGetFeature(): błędny filtr'
                            '</ows:ExceptionText></ows:Exception></ows:ExceptionReport>').encode("utf-8")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(data)
                    return
                start, count = int(q.get("startIndex", 0)), int(q.get("count", 1000))
                if outer.max_count:
                    count = min(count, outer.max_count)
                ids = range(start, min(start + count, outer.total))
                body = HEADER.format(matched=outer.total, returned=len(ids)) + \
                    "".join(parcel(i) for i in ids) + "</wfs:FeatureCollection>"
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/gml+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/wfs"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
