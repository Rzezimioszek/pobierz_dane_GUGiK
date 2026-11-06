# -*- coding: utf-8 -*-
"""
Klient serwisu EZiUDP (integracja.gugik.gov.pl).

Odpowiada za:
  - pobieranie listy usług WFS dla danego TERYT i zbioru danych
  - parsowanie HTML tabeli z adresami WFS
  - wykrywanie nazw warstw WFS przez GetCapabilities
"""
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

try:
    import defusedxml.ElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter

from qgis.core import QgsMessageLog, Qgis

try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

LOG_TAG = "PD_GUGiK"
EZIUDP_BASE_URL = "https://integracja.gugik.gov.pl/eziudp/index.php"
REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Definicje zbiorów danych
# ---------------------------------------------------------------------------

ZBIOR_CONFIG: dict = {
    "bdsog": {
        "label": "Punkty osnowy (BDSOG)",
        "eziudp_zbior": "bdsog",
        # Wzorce porównywane po normalizacji: lowercase, bez ':', '_', '-'
        # Kolejność ważna – bardziej specyficzne wzorce wcześniej
        "layer_patterns": [
            "Osnowa_pozioma",       # geoportal2 (ewns:Osnowa_pozioma)
            "Osnowa_wysokosciowa",  # geoportal2 (ewns:Osnowa_wysokosciowa)
            "osnowa",               # ogólne
            "bdsog",
            "osnow",
            "punktosnow",
            "geodezyjn",
        ],
        "geom_field": "msGeometry",
        "id_field": "idPunktu",
    },
    "rcn": {
        "label": "Rejestr cen nieruchomości (RCN)",
        "eziudp_zbior": "rcn",
        # Schemat jak RCN zbiorczy: osobne warstwy działek, budynków i lokali
        "layer_patterns": [
            "dzialki", "budynki", "lokale", "transakcje",
        ],
        "geom_field": "geom",
        "id_field": "idDzialki",
    },
    "egib": {
        "label": "ewidencja Gruntów i Budynków (EGIB)",
        "eziudp_zbior": "egib",
        "layer_patterns": [
            "dzialki", "dzialka", "budynki", "punkty_graniczne"],
        "geom_field": "geom",
        "id_field": "id_dzialki",
    },
    "bdot500": {
        "label": "Budynki i obiekty towarzyszące (BDOT500)",
        "eziudp_zbior": "bdot500",
        "layer_patterns": [
            "budynki", "budynki_ot", "budynki_bt"],
        "geom_field": "geom",
        "id_field": "ID_IIP",
    },
}


def _make_session() -> requests.Session:
    session = requests.Session()
    try:
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
    except TypeError:
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["GET"],
        )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "QGIS-PobieranieGUGiK/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return session


@dataclass
class EziudpRecord:
    teryt: str = ""
    nazwa_zbioru: str = ""
    organ: str = ""
    url_wfs: str = ""
    url_wms: str = ""


@dataclass
class EziudpResult:
    success: bool = True
    teryt: str = ""
    zbior: str = ""
    records: list = field(default_factory=list)
    error_message: str = ""


class EziudpClient:

    def __init__(self):
        self._session = _make_session()

    # ------------------------------------------------------------------
    # Publiczne API
    # ------------------------------------------------------------------

    def get_wfs_urls(self, teryt: str, zbior: str) -> EziudpResult:
        cfg = ZBIOR_CONFIG.get(zbior)
        if cfg is None:
            return EziudpResult(
                success=False, teryt=teryt, zbior=zbior,
                error_message=(
                    f"Nieznany klucz zbioru: '{zbior}'. "
                    f"Dostępne: {list(ZBIOR_CONFIG.keys())}"
                ),
            )

        params = {
            "teryt": teryt,
            "zbior": cfg["eziudp_zbior"],
            "usluga": "pobierania",
        }
        url = f"{EZIUDP_BASE_URL}?{urllib.parse.urlencode(params)}"
        result = EziudpResult(teryt=teryt, zbior=zbior)

        try:
            html = self._fetch(url)
            if html is None:
                result.success = False
                result.error_message = f"Brak odpowiedzi z EZiUDP dla TERYT={teryt}"
                return result

            records = [r for r in self._parse_html(html) if r.url_wfs]
            result.records = records

            if not records:
                dump = self._dump_for_diagnosis(html, teryt, cfg["eziudp_zbior"])
                result.success = False
                result.error_message = (
                    f"EZiUDP nie zwrócił usługi pobierania dla "
                    f"TERYT={teryt}, zbior={cfg['eziudp_zbior']}"
                    + (f" (odpowiedź zapisana w {dump})" if dump else "")
                )
                QgsMessageLog.logMessage(
                    f"[EziudpClient] {url} → brak adresu WFS. Początek odpowiedzi: "
                    f"{self._strip_tags(html)[:300]!r}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )

        except Exception as exc:
            result.success = False
            result.error_message = str(exc)
            QgsMessageLog.logMessage(
                f"[EziudpClient] Błąd dla TERYT={teryt}: {exc}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )

        return result

    def get_layer_names(self, wfs_url: str, zbior: str) -> list:
        """
        Zwraca WSZYSTKIE nazwy warstw z GetCapabilities pasujące do
        wzorców ZBIOR_CONFIG[zbior]['layer_patterns'].

        Dla BDSOG z geoportal2 zwróci np.:
            ['ewns:Osnowa_pozioma', 'ewns:Osnowa_wysokosciowa']

        Normalizacja: lowercase + usunięcie ':', '_', '-' zarówno
        z wzorca jak i z nazwy warstwy, dzięki czemu:
            wzorzec "Osnowa_pozioma"  dopasowuje  "ewns:Osnowa_pozioma"
            wzorzec "osnowa"          dopasowuje  "ewns:Osnowa_pozioma"
        """
        cfg = ZBIOR_CONFIG.get(zbior)
        if cfg is None:
            return []

        patterns = cfg["layer_patterns"]
        available = self._get_capabilities_layers(wfs_url)
        if not available:
            return []

        def _norm(s: str) -> str:
            return s.lower().replace(":", "").replace("_", "").replace("-", "")

        # Sufiksy wykluczające – warstwy pomocnicze bez geometrii obiektów
        _BLACKLIST_SUFFIXES = (
            "_etykiety", "_etykieta", "_labels", "_label",
            "_opisy", "_opis", "_text",
        )

        matched: list = []
        for name in available:
            # Odrzuć warstwy etykiet (np. ms:Osnowa_pozioma_etykiety)
            name_lower = name.lower()
            if any(name_lower.endswith(sfx) for sfx in _BLACKLIST_SUFFIXES):
                QgsMessageLog.logMessage(
                    f"[EziudpClient] Pominięto warstwę etykiet: {name!r}",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
                continue

            norm_name = _norm(name)
            for pattern in patterns:
                norm_pattern = _norm(pattern)
                if norm_pattern in norm_name or norm_name in norm_pattern:
                    if name not in matched:
                        matched.append(name)
                        QgsMessageLog.logMessage(
                            f"[EziudpClient] Dopasowano: {name!r} "
                            f"← wzorzec {pattern!r}",
                            LOG_TAG, Qgis.MessageLevel.Info,
                        )
                    break  # wystarczy jeden wzorzec na warstwę

        if matched:
            return matched

        # Fallback: jedyna dostępna warstwa
        if len(available) == 1:
            QgsMessageLog.logMessage(
                f"[EziudpClient] Brak dopasowania wzorca – "
                f"używam jedynej dostępnej warstwy: {available[0]}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
            return available[:]

        QgsMessageLog.logMessage(
            f"[EziudpClient] Nie znaleziono pasujących warstw dla "
            f"wzorców {patterns} w {available}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return []

    def get_layer_name(self, wfs_url: str, zbior: str) -> Optional[str]:
        """Kompatybilność wsteczna – zwraca pierwszą pasującą warstwę lub None."""
        names = self.get_layer_names(wfs_url, zbior)
        return names[0] if names else None

    # ------------------------------------------------------------------
    # Prywatne metody pomocnicze
    # ------------------------------------------------------------------

    def _get_capabilities_layers(self, wfs_url: str) -> list:
        """Pobiera GetCapabilities i zwraca listę dostępnych nazw warstw."""
        base_url = wfs_url.split("?")[0]
        caps_url = f"{base_url}?service=WFS&request=GetCapabilities"

        QgsMessageLog.logMessage(
            f"[EziudpClient] GetCapabilities: {caps_url}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        try:
            resp = self._session.get(caps_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[EziudpClient] GetCapabilities błąd: {exc}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
            return []

        available = self._extract_layer_names(resp.content)
        QgsMessageLog.logMessage(
            f"[EziudpClient] Dostępne warstwy: {available}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )
        return available

    def _fetch(self, url: str) -> Optional[str]:
        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[EziudpClient] HTTP błąd: {exc}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
            return None

    def _parse_html(self, html: str) -> list:
        """
        Wyciąga z odpowiedzi EZiUDP wiersze tabeli z adresami usług.

        Dawny układ (kolumny 0–6: id, organ, nazwa, TERYT, typy, WMS, WFS,
        wiersze class="row") jest obsługiwany wprost. Gdy strona wygląda
        inaczej, każdy wiersz <tr> jest przeszukiwany po linkach: WFS =
        link z 'wfs' w adresie, WMS = link z 'wms'.
        """
        html = html.replace("&amp;", "&").replace("&nbsp;", " ")
        records = self._parse_html_columns(html)
        if not any(r.url_wfs for r in records):
            records = self._parse_html_links(html)
        QgsMessageLog.logMessage(
            f"[EziudpClient] Znaleziono {len(records)} rekordów w HTML "
            f"({sum(1 for r in records if r.url_wfs)} z adresem WFS)",
            LOG_TAG, Qgis.MessageLevel.Info,
        )
        return records

    _ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    _CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
    _HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
    _TERYT_RE = re.compile(r"\b(\d{2}(?:\d{2}(?:\d{2}_?\d)?)?)\b")

    def _parse_html_columns(self, html: str) -> list:
        """Dawny układ: wiersze class="row", 7+ kolumn w ustalonej kolejności."""
        records = []
        row_re = re.compile(r'<tr[^>]+class=["\']row["\'][^>]*>(.*?)</tr>',
                            re.DOTALL | re.IGNORECASE)
        for row_match in row_re.finditer(html):
            cells = self._CELL_RE.findall(row_match.group(1))
            if len(cells) < 7:
                continue
            rec = EziudpRecord(
                teryt=self._strip_tags(cells[3]).strip(),
                nazwa_zbioru=self._strip_tags(cells[2]).strip(),
                organ=self._strip_tags(cells[1]).strip(),
            )
            wms_links = self._HREF_RE.findall(cells[5])
            if wms_links:
                rec.url_wms = self._clean_service_url(wms_links[0])
            wfs_links = self._HREF_RE.findall(cells[6])
            if wfs_links:
                rec.url_wfs = self._clean_service_url(wfs_links[0])
            records.append(rec)
        return records

    def _parse_html_links(self, html: str) -> list:
        """Dowolny układ: w każdym wierszu szukamy linków do usług WFS i WMS."""
        records = []
        rows = self._ROW_RE.findall(html) or [html]
        for row in rows:
            links = [l.strip() for l in self._HREF_RE.findall(row)]
            wfs = [l for l in links if "wfs" in l.lower()]
            if not wfs:
                continue
            wms = [l for l in links if "wms" in l.lower() and "wfs" not in l.lower()]
            text = re.sub(r"<[^>]+>", " ", row)
            teryt = self._TERYT_RE.search(text)
            records.append(EziudpRecord(
                teryt=teryt.group(1) if teryt else "",
                url_wfs=self._clean_service_url(wfs[0]),
                url_wms=self._clean_service_url(wms[0]) if wms else "",
            ))
        return records

    @staticmethod
    def _dump_for_diagnosis(html: str, teryt: str, zbior: str) -> str:
        """Zapisuje odpowiedź EZiUDP do pliku tymczasowego; zwraca ścieżkę albo ''."""
        try:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), f"pd_gugik_eziudp_{teryt}_{zbior}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            return path
        except OSError:
            return ""

    @staticmethod
    def _strip_tags(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()

    @staticmethod
    def _clean_service_url(url: str) -> str:
        url = url.strip()
        base, _, query = url.partition("?")
        if not query:
            return url
        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        skip_keys = {"service", "request", "version", "outputformat", "language"}
        filtered = {k: v for k, v in params.items() if k.lower() not in skip_keys}
        if not filtered:
            return base
        return base + "?" + urllib.parse.urlencode(filtered, doseq=True)

    @staticmethod
    def _extract_layer_names(xml_bytes: bytes) -> list:
        """
        Parsuje XML GetCapabilities (WFS 1.x lub 2.0) i zwraca
        listę nazw warstw (<Name> wewnątrz <FeatureType>).
        """
        names = []
        try:
            root = ET.fromstring(xml_bytes) # nosec
            for ns in (
                "http://www.opengis.net/wfs/2.0",
                "http://www.opengis.net/wfs",
                "",
            ):
                prefix = f"{{{ns}}}" if ns else ""
                for ft in root.iter(f"{prefix}FeatureType"):
                    name_el = ft.find(f"{prefix}Name")
                    if name_el is not None and name_el.text:
                        n = name_el.text.strip()
                        if n and n not in names:
                            names.append(n)
                if names:
                    break
        except ET.ParseError as exc:
            QgsMessageLog.logMessage(
                f"[EziudpClient] Błąd parsowania GetCapabilities XML: {exc}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
        return names