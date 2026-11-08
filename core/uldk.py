# -*- coding: utf-8 -*-
"""
ULDK (uldk.gugik.gov.pl) – aktualna lista obrębów jednostki ewidencyjnej.

    ?request=GetRegionById&id=022602_1&result=id,commune,region
    →  0
       022602_1.0001|Złotoryja (miasto)|Obręb 1
       …
Pierwsza linia to status (0 = OK, -1 = brak wyników / błąd).
"""
from . import net

ULDK_URL = "https://uldk.gugik.gov.pl/"
ENABLED = True      # testy wyłączają zapytania do prawdziwej usługi


def parse_regions(text: str) -> list:
    """[(kod obrębu, nazwa obrębu, nazwa jednostki)]; pusta lista dla „-1 …”."""
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n") if l.strip()]
    if not lines:
        raise RuntimeError("Pusta odpowiedź ULDK")
    status = lines[0].split()[0]
    if status == "-1":
        return []
    if status != "0":
        raise RuntimeError(f"Nieoczekiwana odpowiedź ULDK: {lines[0][:120]}")
    out = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) >= 3 and "." in parts[0]:
            out.append((parts[0].strip(), parts[2].strip(), parts[1].strip()))
    return out


def regions(session, jednostka: str, url: str = ULDK_URL, timeout: int = 30) -> list:
    resp = net.get(session, url, {"request": "GetRegionById", "id": jednostka,
                                  "result": "id,commune,region"}, timeout=timeout)
    return parse_regions(resp.text)
