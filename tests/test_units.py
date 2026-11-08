# -*- coding: utf-8 -*-
"""Słownik jednostek (data/jednostki.json.gz) i skrypt odświeżania z PRG."""
import gzip
import importlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from conftest import PLUGIN_DIR, PLUGIN_NAME

os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def units_mod():
    return importlib.import_module(f"{PLUGIN_NAME}.ui.admin_units")


def test_dictionary_loads_all_levels():
    u = units_mod().AdminUnits.instance()
    assert len(u.wojewodztwa) == 16 and len(u.powiaty) >= 370
    assert len(u.gminy) >= 2400 and len(u.obreby_teryt_to_info) >= 50000
    assert os.path.getsize(units_mod().DATA_FILE) < 1_000_000


def test_check_known_new_and_bad_codes():
    m = units_mod()
    u = m.AdminUnits.instance()
    known = next(iter(u.obreby_teryt_to_info))
    assert u.check(known) == ("obręb", "")
    level, note = u.check("146501_1.9998")
    assert level == "obręb" and "nie ma w słowniku" in note
    level, note = u.check("14650")
    assert level == "" and "Nieprawidłowy" in note
    assert m.normalize_teryt("1465011") == "146501_1"
    assert u.check("1465011")[0] in ("gmina", "")      # gmina wpisana bez „_”
    assert m.teryt_format("146501_1") == "gmina" and m.teryt_format("12") == "województwo"


def test_obreb_code_accepted_outside_dictionary():
    u = units_mod().AdminUnits.instance()
    assert u.find_obreby("146501_1.9998") == ["146501_1.9998"]
    assert u.find_obreby("Nieistniejący obręb xyz") == []


# ---------------------------------------------------------------- skrypt odświeżania

def _member(code, name, typename="A06_Granice_obrebow_ewidencyjnych"):
    return (f"<wfs:member><ms:{typename}><ms:JPT_KOD_JE>{code}</ms:JPT_KOD_JE>"
            f"<ms:JPT_NAZWA_>{name}</ms:JPT_NAZWA_></ms:{typename}></wfs:member>")


class _FakePrg:
    """Udawane PRG: po 3 jednostki na poziom, stronicowanie po 2 (PAGE podmieniany)."""
    DATA = {
        "ms:A01_Granice_wojewodztw": [("02", "dolnośląskie"), ("14", "mazowieckie"), ("12", "małopolskie")],
        "ms:A02_Granice_powiatow": [("1465", "powiat Warszawa"), ("1261", "powiat Kraków"), ("0264", "powiat Wrocław")],
        "ms:A03_Granice_gmin": [("1465011", "Warszawa"), ("1261011", "Kraków"), ("0264011", "Wrocław")],
        "ms:A05_Granice_jednostek_ewidencyjnych": [("146501_1", "Warszawa"), ("126101_1", "Kraków"),
                                                   ("026401_1", "Wrocław")],
        "ms:A06_Granice_obrebow_ewidencyjnych": [("146501_1.0001", "Obręb 1"), ("146501_1.0002", "Obręb 2"),
                                                 ("126101_1.0001", "Podgórze &amp; okolice"),
                                                 ("026401_1.0001", "Wrocław-1")],
    }

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                outer.requests.append(q)
                rows = outer.DATA[q["typenames"]]
                start, count = int(q["startIndex"]), int(q["count"])
                page = rows[start:start + count]
                body = ('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
                        'xmlns:ms="http://mapserver.gis.umn.edu/mapserver">'
                        + "".join(_member(c, n) for c, n in page) + "</wfs:FeatureCollection>")
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/prg"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _script(monkeypatch):
    sys.path.insert(0, os.path.join(PLUGIN_DIR, "tools"))
    try:
        mod = importlib.import_module("odswiez_slowniki")
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(mod, "PAGE", 2)
    return mod


def test_refresh_script_pages_normalizes_and_reports(tmp_path, monkeypatch):
    mod = _script(monkeypatch)
    srv = _FakePrg()
    out = str(tmp_path / "jednostki.json.gz")
    logs = []
    try:
        assert mod.main([], url=srv.url, out=out, log=logs.append) == 0
    finally:
        srv.close()
    with gzip.open(out, "rt", encoding="utf-8") as f:
        data = json.load(f)
    assert data["gminy"] == {"146501_1": "Warszawa", "126101_1": "Kraków", "026401_1": "Wrocław"}
    assert data["obreby"]["126101_1.0001"] == "Podgórze & okolice" and data["data"]
    assert {r["startIndex"] for r in srv.requests} == {"0", "2", "4"}   # 4 obręby, strony po 2
    assert all(r["propertyName"] == "ms:JPT_KOD_JE,ms:JPT_NAZWA_" for r in srv.requests)
    assert any(line.startswith("obreby: 4 (nowe 4") for line in logs)
    assert data["jednostki"]["026401_1"] == "Wrocław"


def test_refresh_script_refuses_suspicious_drop(tmp_path, monkeypatch):
    mod = _script(monkeypatch)
    out = str(tmp_path / "jednostki.json.gz")
    old = {lvl: {f"{i:02d}": "x" for i in range(10)} for lvl, _ in mod.LEVELS}
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(old, f)
    srv = _FakePrg()
    logs = []
    try:
        assert mod.main([], url=srv.url, out=out, log=logs.append) == 2
        assert "NIE zapisano" in logs[-1]
        with gzip.open(out, "rt", encoding="utf-8") as f:
            assert json.load(f) == old                       # stary słownik nietknięty
        assert mod.main(["--force"], url=srv.url, out=out, log=logs.append) == 0
    finally:
        srv.close()


def test_refresh_script_reports_service_exception():
    mod = importlib.import_module("odswiez_slowniki") if "odswiez_slowniki" in sys.modules else None
    if mod is None:
        sys.path.insert(0, os.path.join(PLUGIN_DIR, "tools"))
        mod = importlib.import_module("odswiez_slowniki")
        sys.path.pop(0)
    xml = (b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1"><ows:Exception>'
           b'<ows:ExceptionText>msWFSGetFeature(): bad</ows:ExceptionText></ows:Exception></ows:ExceptionReport>')
    try:
        mod.parse_page(xml)
    except RuntimeError as exc:
        assert "bad" in str(exc)
    else:
        raise AssertionError("brak wyjątku")


def test_refresh_script_refuses_gmina_without_obreby(tmp_path, monkeypatch):
    mod = _script(monkeypatch)
    monkeypatch.setitem(_FakePrg.DATA, "ms:A06_Granice_obrebow_ewidencyjnych",
                        [("146501_1.0001", "Obręb 1")])
    srv = _FakePrg()
    logs = []
    try:
        assert mod.main([], url=srv.url, out=str(tmp_path / "j.json.gz"), log=logs.append) == 2
    finally:
        srv.close()
    assert "gminy bez obrębów: 2" in logs[-1]
    assert mod.coverage({"gminy": {"020104_3": "x", "126101_1": "Kraków"},
                         "obreby": {"020104_5.0001": "a", "126102_9.0001": "b"}}) == []


# ---------------------------------------------------------------- gmina ↔ obręby

def test_obreby_of_urban_rural_gmina_and_big_cities():
    u = units_mod().AdminUnits.instance()
    assert all(u.obreby_for_gmina(g) for g in u.gminy)            # żadna gmina bez obrębów
    mw = next(g for g in u.gminy if g.endswith("_3"))
    assert set(u.jednostki_for_gmina(mw)) == {mw[:6] + "_4", mw[:6] + "_5"}
    assert {c.split(".")[0][-1] for c, _ in u.obreby_for_gmina(mw)} <= {"4", "5"}
    krakow = u.jednostki_for_gmina("126101_1")
    assert krakow and all(j.endswith("_9") for j in krakow)
    assert u.gmina_for("126102_9") == "126101_1" and u.gmina_for(mw[:6] + "_5") == mw
    obr = u.obreby_for_gmina(mw)[0][0]
    assert u.gmina_name(obr) == u.gminy[mw]["nazwa"]


def test_uldk_parse_and_task():
    uldk = importlib.import_module(f"{PLUGIN_NAME}.core.uldk")
    text = "0\n022602_1.0001|Złotoryja (miasto)|Obręb 1\n022602_1.0002|Złotoryja (miasto)|Obręb 2\n"
    assert uldk.parse_regions(text) == [("022602_1.0001", "Obręb 1", "Złotoryja (miasto)"),
                                        ("022602_1.0002", "Obręb 2", "Złotoryja (miasto)")]
    assert uldk.parse_regions("-1 brak wyników") == []
    try:
        uldk.parse_regions("<html>błąd</html>")
    except RuntimeError:
        pass
    else:
        raise AssertionError

    class H(BaseHTTPRequestHandler):
        seen = []

        def log_message(self, *a):
            pass

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            H.seen.append(q)
            body = ("0\n" + q["id"] + ".0001|Gmina (miasto)|A\n") if q["id"].endswith("_4") else "-1 brak"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tasks = importlib.import_module(f"{PLUGIN_NAME}.core.tasks")
        monkey = uldk.ULDK_URL
        uldk.ULDK_URL = f"http://127.0.0.1:{srv.server_port}/"
        try:
            t = tasks.RegionsTask("020104_3", ["020104_4", "020104_5"])
            # regions() bierze adres domyślny z chwili wywołania
            orig = uldk.regions
            uldk.regions = lambda s, j, url=uldk.ULDK_URL, timeout=30: orig(s, j, url, timeout)
            try:
                assert t.run()
            finally:
                uldk.regions = orig
        finally:
            uldk.ULDK_URL = monkey
    finally:
        srv.shutdown()
    assert t.obreby == [("020104_4.0001", "A")] and t.errors == []
    assert H.seen[0]["request"] == "GetRegionById" and H.seen[0]["result"] == "id,commune,region"
