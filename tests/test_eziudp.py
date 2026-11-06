# -*- coding: utf-8 -*-
"""Odczyt adresów usług z odpowiedzi EZiUDP (dawny i zmieniony układ strony)."""
import importlib

from conftest import PLUGIN_NAME


def client():
    return importlib.import_module(f"{PLUGIN_NAME}.eziudp_client").EziudpClient()


OLD = """<table><tr class="row"><td>PL.1</td><td>Starosta</td><td>BDSOG</td><td>1261</td><td>WMS, WFS</td>
<td><a href="https://ows.example.pl/wms?service=WMS&amp;request=GetCapabilities">WMS</a></td>
<td><a href="https://ows.example.pl/bdsog/wfs?service=WFS&amp;request=GetCapabilities">WFS</a></td></tr></table>"""

NEW = """<div><table class="table"><thead><tr><th>TERYT</th><th>Usługi</th></tr></thead>
<tbody><tr><td>1261</td><td><a href="https://x.pl/geoserver/wms?SERVICE=WMS">podgląd</a>
<a href="https://x.pl/geoserver/ows?service=WFS&request=GetCapabilities&version=2.0.0">pobieranie (WFS)</a>
</td></tr></tbody></table></div>"""


def test_old_layout():
    recs = client()._parse_html(OLD)
    assert [r.url_wfs for r in recs] == ["https://ows.example.pl/bdsog/wfs"]
    assert recs[0].teryt == "1261"


def test_changed_layout_found_by_links():
    recs = [r for r in client()._parse_html(NEW) if r.url_wfs]
    assert len(recs) == 1
    assert recs[0].url_wfs == "https://x.pl/geoserver/ows"
    assert recs[0].url_wms == "https://x.pl/geoserver/wms"
    assert recs[0].teryt == "1261"


def test_no_services():
    assert [r for r in client()._parse_html("<html><p>Brak danych</p></html>") if r.url_wfs] == []


def test_rcn_layers_like_collective_rcn():
    task = importlib.import_module(f"{PLUGIN_NAME}.eziudp_wfs_task")
    keys = {e["key"]: e for e in task.AVAILABLE_LAYERS}
    assert "rcn_tran" not in keys
    assert {keys[k]["layer_name"] for k in ("rcn_dzi", "rcn_bud", "rcn_lok")} == \
        {"Dzialki_RCN", "Budynki_RCN", "Lokale_RCN"}
