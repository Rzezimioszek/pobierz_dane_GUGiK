# -*- coding: utf-8 -*-
"""
HTTP dla całej wtyczki.

Jedna sesja `requests` z ponawianiem. Jeśli w QGIS (Ustawienia → Opcje → Sieć)
włączono serwer proxy, sesja go używa.
"""
import time
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

from . import log

MAX_RETRIES = 3
USER_AGENT = "QGIS-PobierzDaneGUGiK"

try:
    from urllib3.util.retry import Retry
    from urllib3.exceptions import RemoteDisconnected
except ImportError:  # bardzo stare środowiska
    from requests.packages.urllib3.util.retry import Retry  # type: ignore
    RemoteDisconnected = ConnectionError


def _qgis_proxies() -> dict:
    """Proxy z ustawień QGIS w formacie `requests`; {} gdy wyłączone."""
    try:
        from qgis.core import QgsSettings
        s = QgsSettings()
        if not s.value("proxy/proxyEnabled", False, type=bool):
            return {}
        host = s.value("proxy/proxyHost", "", type=str)
        port = s.value("proxy/proxyPort", "", type=str)
        if not host:
            return {}
        user = s.value("proxy/proxyUser", "", type=str)
        password = s.value("proxy/proxyPassword", "", type=str)
        auth = ""
        if user:
            auth = urllib.parse.quote(user, safe="")
            if password:
                auth += ":" + urllib.parse.quote(password, safe="")
            auth += "@"
        url = f"http://{auth}{host}" + (f":{port}" if port else "")
        return {"http": url, "https": url}
    except Exception as exc:  # ustawienia nie mogą zablokować pobierania
        log.warning(f"[NET] Nie udało się odczytać proxy z ustawień QGIS: {exc}")
        return {}


def make_session() -> requests.Session:
    """Sesja z ponawianiem dla 429/5xx i proxy z ustawień QGIS."""
    session = requests.Session()
    try:
        retry = Retry(total=MAX_RETRIES, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
    except TypeError:  # urllib3 < 1.26
        retry = Retry(total=MAX_RETRIES, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504],
                      method_whitelist=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Nagłówki jak w 1.3 (Content-Type zostaje dla zgodności z dotychczasowymi serwerami)
    session.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/xml",
                            "Connection": "close"})
    proxies = _qgis_proxies()
    if proxies:
        session.proxies.update(proxies)
    return session


def get(session: requests.Session, url: str, params: dict = None,
        timeout: int = 180) -> requests.Response:
    """GET z ponawianiem przy zerwanym połączeniu lub przekroczeniu czasu."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    log.info(f"[NET] GET {full_url}")
    for attempt in range(MAX_RETRIES):
        try:
            t0 = time.time()
            resp = session.get(full_url, timeout=timeout)
            resp.raise_for_status()
            log.info(f"[NET] {resp.status_code} {time.time() - t0:.1f}s "
                     f"{len(resp.content)}B (próba {attempt + 1}/{MAX_RETRIES})")
            return resp
        except (requests.ConnectionError, requests.Timeout, RemoteDisconnected) as exc:
            if attempt == MAX_RETRIES - 1:
                log.error(f"[NET] Błąd po {MAX_RETRIES} próbach: {exc}")
                raise
            wait = (attempt + 1) * 2
            log.warning(f"[NET] {type(exc).__name__} – ponawianie za {wait}s "
                        f"(próba {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    raise RuntimeError("Nieoczekiwany koniec pętli ponawiania")
