# -*- coding: utf-8 -*-
"""
Harmonizator schematów dla danych pobieranych z powiatowych usług WFS.

Stan obecny: okno EZiUDP używa stąd tylko nazwy warstwy (LAYER_NAME) i stylu
(STYLE); pola zostają takie jak w usłudze źródłowej. Mapowanie na wspólny
schemat (harmonize) jest gotowe, ale nie jest włączone. Zapis warstw: core.layers.

Zmiany względem poprzedniej wersji:
  - BdsogHarmonizer zastąpiony dwoma wyspecjalizowanymi klasami:
      BdsogPoziomHarmonizer  → warstwa "Osnowa_pozioma"
      BdsogWysokHarmonizer   → warstwa "Osnowa_wysokosciowa"
  - Schemat oparty na rzeczywistych polach z geoportal2/BDSOG.
  - SWAP_XY=False – parser GML (core/gml.py) już ustawia kolejność osi
    N-first (Y,X) na E,N (X,Y) przy tworzeniu QgsPointXY.
"""
from typing import Optional

from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsGeometry,
    QgsProject,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsMessageLog, Qgis,
)
from .qt_compat import FIELD_STRING, FIELD_DOUBLE, FIELD_INTEGER_TYPES

LOG_TAG = "PD_GUGiK"
TARGET_CRS = "EPSG:2180"


def _layer_name_to_harmonizer_key(layer_name: str, zbior: str = "bdsog") -> str:
    """
    Mapuje nazwę warstwy WFS (np. 'ewns:Osnowa_wysokosciowa')
    na klucz harmonizatora ('bdsog_wysok' lub 'bdsog_poziom').
    """
    norm = layer_name.lower().replace(":", "").replace("_", "").replace("-", "")
    if zbior == "bdsog":
        if "wysok" in norm:
            return "bdsog_wysok"
        return "bdsog_poziom"
    if zbior == "egib":
        if "budynki" in norm:
            return "egib_bud"
        if "dzialki" in norm:
                return "egib_dzi"
        if "punkty" in norm:
                return "egib_pkt"


def _coerce(value, qvariant_type: int):
    """
    Konwertuje wartość z GML do typu pola QgsField.
      - None / pusty string / "null" → None
      - Double → float (błąd → None)
      - Int/LongLong → int (błąd → None)
      - pozostałe → str
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() == "null":
        return None
    if qvariant_type == FIELD_DOUBLE:
        try:
            return float(s.replace(",", "."))
        except (ValueError, TypeError):
            return None
    if qvariant_type in FIELD_INTEGER_TYPES:
        try:
            return int(float(s.replace(",", ".")))
        except (ValueError, TypeError):
            return None
    return s


class BaseHarmonizer:
    """
    Bazowa klasa harmonizatora.

    Podklasy ustawiają:
        LAYER_NAME    – nazwa warstwy QGIS
        STYLE         – nazwa stylu QML z data/ (domyślnie LAYER_NAME)
        GEOM_TYPE     – typ geometrii ("Point", "MultiLineString" itp.)
        TARGET_SCHEMA – lista (nazwa_pola, typ pola z qt_compat)
        FIELD_VARIANTS – {pole_docelowe: [możliwe_nazwy_źródłowe]}
        UNIQUE_FIELD  – pole do deduplikacji lub None
    """

    LAYER_NAME: str = "Dane_WFS"
    STYLE: Optional[str] = None
    GEOM_TYPE: str = "Point"
    TARGET_SCHEMA: list = []
    FIELD_VARIANTS: dict = {}
    UNIQUE_FIELD: Optional[str] = None

    def build_target_fields(self) -> QgsFields:
        fields = QgsFields()
        for name, qtype in self.TARGET_SCHEMA:
            fields.append(QgsField(name, qtype))
        return fields

    def harmonize(
        self,
        features_data: list,
        teryt: str,
        wfs_url: str,
        source_crs: str = TARGET_CRS,
    ) -> list:
        if not features_data:
            return []

        target_fields = self.build_target_fields()

        transform = None
        if source_crs and source_crs != TARGET_CRS:
            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(source_crs),
                QgsCoordinateReferenceSystem(TARGET_CRS),
                QgsProject.instance(),
            )

        sample_keys = list(features_data[0].get("attrs", {}).keys())
        mapping = self._detect_mapping(sample_keys)
        QgsMessageLog.logMessage(
            f"[Harmonizer] {self.__class__.__name__} TERYT={teryt} "
            f"pola źródłowe: {sample_keys} | mapowanie: {mapping}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        harmonized = []
        for fd in features_data:
            feat = QgsFeature(target_fields)

            geom = QgsGeometry.fromWkt(fd.get("geom", ""))
            if geom and not geom.isEmpty() and transform:
                geom.transform(transform)
            feat.setGeometry(geom)

            attrs = fd.get("attrs", {})
            for target_name, src_name in mapping.items():
                idx = target_fields.indexOf(target_name)
                if idx < 0:
                    continue
                feat.setAttribute(
                    idx,
                    _coerce(attrs.get(src_name), target_fields.at(idx).type())
                )

            for meta_field, value in [("teryt_powiatu", teryt), ("zrodlo_wfs", wfs_url)]:
                idx = target_fields.indexOf(meta_field)
                if idx >= 0:
                    feat.setAttribute(idx, value)

            harmonized.append(feat)

        return harmonized

    def _detect_mapping(self, source_keys: list) -> dict:
        def normalize(s: str) -> str:
            return s.split(":")[-1].lower().replace("_", "").replace("-", "")

        norm_src = {normalize(k): k for k in source_keys}
        lower_src = {k.lower(): k for k in source_keys}
        mapping: dict = {}

        for target_name, _ in self.TARGET_SCHEMA:
            if target_name in ("teryt_powiatu", "zrodlo_wfs"):
                continue

            candidates = self.FIELD_VARIANTS.get(target_name, [target_name])
            found = False

            for candidate in candidates:
                # 1. Dokładne (case-insensitive)
                if candidate.lower() in lower_src:
                    mapping[target_name] = lower_src[candidate.lower()]
                    found = True
                    break
                # 2. Znormalizowane
                norm_c = normalize(candidate)
                if norm_c in norm_src:
                    mapping[target_name] = norm_src[norm_c]
                    found = True
                    break

            if not found:
                norm_target = normalize(target_name)
                if norm_target in norm_src:
                    mapping[target_name] = norm_src[norm_target]

        return mapping


# ---------------------------------------------------------------------------
# BDSOG – osnowa pozioma
# ---------------------------------------------------------------------------

class BdsogPoziomHarmonizer(BaseHarmonizer):
    """Osnowa pozioma (ewns:Osnowa_pozioma z geoportal2)."""

    LAYER_NAME = "Osnowa_pozioma"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "nr_punktu"

    TARGET_SCHEMA = [
        ("nr_punktu",          FIELD_STRING),
        ("rodzaj_osnowy",      FIELD_STRING),
        ("x2000",              FIELD_DOUBLE),
        ("y2000",              FIELD_DOUBLE),
        ("strefa2000",         FIELD_STRING),
        ("mp",                 FIELD_STRING),
        ("typ_stabilizacji",   FIELD_STRING),
        ("stan_znaku",         FIELD_STRING),
        ("opis_topograficzny", FIELD_STRING),
        ("teryt_powiatu",      FIELD_STRING),
        ("zrodlo_wfs",         FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "nr_punktu":          ["NR_PUNKTU", "nr_punktu", "nrPunktu", "numerPunktu", "numer"],
        "rodzaj_osnowy":      ["RODZAJ_OSNOWY", "rodzaj_osnowy", "rodzajOsnowy", "rodzaj"],
        "x2000":              ["X2000", "x2000", "wspolrzednaX", "x"],
        "y2000":              ["Y2000", "y2000", "wspolrzednaY", "y"],
        "strefa2000":         ["STREFA2000", "strefa2000", "strefa"],
        "mp":                 ["MP", "mp", "bladPolozenia", "dokladnoscPolozenia"],
        "typ_stabilizacji":   ["TYP_STABILIZACJI", "typ_stabilizacji", "typStabilizacji", "stabilizacja"],
        "stan_znaku":         ["STAN_ZNAKU", "stan_znaku", "stanZnaku", "stan"],
        "opis_topograficzny": ["OPIS_TOPOGRAFICZNY", "opis_topograficzny", "opisTopograficzny", "opis"],
    }


# ---------------------------------------------------------------------------
# BDSOG – osnowa wysokościowa
# ---------------------------------------------------------------------------

class BdsogWysokHarmonizer(BaseHarmonizer):
    """Osnowa wysokościowa (ewns:Osnowa_wysokosciowa z geoportal2)."""

    LAYER_NAME = "Osnowa_wysokosciowa"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "nr_punktu"

    TARGET_SCHEMA = [
        ("nr_punktu",          FIELD_STRING),
        ("rodzaj_osnowy",      FIELD_STRING),
        ("h_plevrf2007nh",     FIELD_DOUBLE),
        ("mh_plevrf2007nh",    FIELD_STRING),
        ("h_plkron86nh",       FIELD_DOUBLE),
        ("mh_plkron86nh",      FIELD_STRING),
        ("typ_stabilizacji",   FIELD_STRING),
        ("stan_znaku",         FIELD_STRING),
        ("opis_topograficzny", FIELD_STRING),
        ("teryt_powiatu",      FIELD_STRING),
        ("zrodlo_wfs",         FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "nr_punktu":          ["NR_PUNKTU", "nr_punktu", "nrPunktu", "numerPunktu", "numer"],
        "rodzaj_osnowy":      ["RODZAJ_OSNOWY", "rodzaj_osnowy", "rodzajOsnowy", "rodzaj"],
        "h_plevrf2007nh":     ["H_PLEVRF2007NH", "h_plevrf2007nh", "wysokoscEVRF", "wysokosc"],
        "mh_plevrf2007nh":    ["MH_PLEVRF2007NH", "mh_plevrf2007nh", "bladWysokosciEVRF"],
        "h_plkron86nh":       ["H_PLKRON86NH", "h_plkron86nh", "wysokoscKron", "h"],
        "mh_plkron86nh":      ["MH_PLKRON86NH", "mh_plkron86nh", "bladWysokosciKron", "mh"],
        "typ_stabilizacji":   ["TYP_STABILIZACJI", "typ_stabilizacji", "typStabilizacji", "stabilizacja"],
        "stan_znaku":         ["STAN_ZNAKU", "stan_znaku", "stanZnaku", "stan"],
        "opis_topograficzny": ["OPIS_TOPOGRAFICZNY", "opis_topograficzny", "opisTopograficzny", "opis"],
    }


# ---------------------------------------------------------------------------
# Pozostałe zbiory
# ---------------------------------------------------------------------------
class EgibPktHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Punkty graniczne"
    STYLE = "punkty_graniczne"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "NUMER_PUNKTU"

    TARGET_SCHEMA = [
        ("NUMER_PUNKTU",    FIELD_STRING),
        ("STABILIZACJA",  FIELD_STRING),
        ("ISD",      FIELD_STRING),
        ("SPD",      FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "NUMER_PUNKTU":   ["NUMER_PUNKTU", "numer_punktu"],
        "STABILIZACJA": ["STABILIZACJA", "stabilizacja"],
        "ISD":     ["ISD", "isd"],
        "SPD":     ["SPD", "spd"],
    }

class EgibBudHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Budynki"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_BUDYNKU"

    TARGET_SCHEMA = [
        ("ID_BUDYNKU",    FIELD_STRING),
        ("RODZAJ",  FIELD_STRING),
        ("KONDYGNACJE_NADZIEMNE",      FIELD_STRING),
        ("KONDYGNACJE_PODZIEMNE",      FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "ID_BUDYNKU":   ["ID_BUDYNKU", "id_budynku"],
        "RODZAJ": ["RODZAJ", "rodzaj", "funkcja", "FUNKCJA"],
        "KONDYGNACJE_NADZIEMNE":     ["KONDYGNACJE_NADZIEMNE", "kondygnacje_nadziemne"],
        "KONDYGNACJE_PODZIEMNE":     ["KONDYGNACJE_PODZIEMNE", "kondygnaje_podziemne"],
    }

class EgibDziHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Dzialki"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_DZIALKI"

    TARGET_SCHEMA = [
        ("ID_DZIALKI",    FIELD_STRING),
        ("NUMER_DZIALKI",  FIELD_STRING),
        ("NUMER_OBREBU",      FIELD_STRING),
        ("NUMER_JEDNOSTKI",      FIELD_STRING),
        ("NAZWA_GMINY",      FIELD_STRING),
        ("POLE_EWIDENCYJNE",      FIELD_STRING),
        ("KLASOUZYTKI_EGIB",      FIELD_STRING),
        ("GRUPA_REJESTROWA",      FIELD_STRING),
        ("DATA",      FIELD_STRING)
    ]

    FIELD_VARIANTS = {
        "ID_DZIALKI":   ["ID_DZIALKI", "id_dzialki"],
        "NUMER_DZIALKI": ["NUMER_DZIALKI", "numer_dzialki"],
        "NUMER_OBREBU":     ["NUMER_OBREBU", "numer_obrebu"],
        "NUMER_JEDNOSTKI":     ["NUMER_JEDNOSTKI", "numer_jednostki"],
        "NAZWA_OBREBU":     ["NAZWA_OBREBU", "nazwa_obrebu"],
        "NAZWA_GMINY":     ["NAZWA_GMINY", "nazwa_gminy"],
        "POLE_EWIDENCYJNE":     ["POLE_EWIDENCYJNE", "pole_ewidencyjne"],
        "KLASOUZYTKI_EGIB":     ["KLASOUZYTKI_EGIB", "klasouzytki_egib"],
        "GRUPA_REJESTROWA":     ["GRUPA_REJESTROWA", "grupa_rejestrowa"],
        "DATA":     ["DATA", "data"],
    }

class RCNHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Transakcje"
    STYLE = "transakcje"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "lokalny_id"

    TARGET_SCHEMA = [
        ("serwis_rcn",    FIELD_STRING),
        ("teryt",  FIELD_STRING),
        ("lokalny_id",      FIELD_STRING),
        ("data_transakcji",      FIELD_STRING),
        ("rodzaj_transakcji",      FIELD_STRING),
        ("rodzaj_nieruchomosci",      FIELD_STRING),
        ("link",      FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "serwis_rcn":   ["serwis_rcn", "SERWIS_RCN", "serwisRCN"],
        "teryt": ["teryt", "TERYT"],
        "lokalny_id":     ["lokalny_id", "LOKALNY_ID", "lokalnyId"],
        "data_transakcji":     ["data_transakcji", "DATA_TRANSAKCJI", "dataTransakcji"],
        "rodzaj_transakcji":     ["rodzaj_transakcji", "RODZAJ_TRANSAKCJI", "rodzajTransakcji"],
        "rodzaj_nieruchomosci":     ["rodzaj_nieruchomosci", "RODZAJ_NIERUCHOMOSCI", "rodzajNieruchomosci"],
        "link":     ["link", "LINK"],
    }

class RCN2026Harmonizer(BaseHarmonizer):
    LAYER_NAME = "Dzialki (RCN)"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "tran_lokalny_id_iip"

    TARGET_SCHEMA = [
        ("serwis_rcn",    FIELD_STRING),
        ("teryt",  FIELD_STRING),
        ("tran_lokalny_id_iip",      FIELD_STRING),
    ]

    FIELD_VARIANTS = {
        "serwis_rcn":   ["serwis_rcn", "SERWIS_RCN", "serwisRCN"],
        "teryt": ["teryt", "TERYT"],
        "tran_lokalny_id_iip":     ["tran_lokalny_id_iip", "TRAN_LOKALNY_ID_IIP"],
    }

class BDOT500BudHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Budynki i obiekty towarzyszące"
    STYLE = "budynki_bdot500"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_IIP"

    TARGET_SCHEMA = [
        ("NAZWA_OBIEKTU",    FIELD_STRING),
        ("KOD_OBKIETU",  FIELD_STRING),
        ("ID_IIP",      FIELD_STRING),
        ("ETYKIETA",      FIELD_STRING),
        ("DATA",      FIELD_STRING)
    ]

    FIELD_VARIANTS = {
        "NAZWA_OBIEKTU":   ["NAZWA_OBIEKTU", "nazwa_obiektu"],
        "KOD_OBKIETU": ["KOD_OBKIETU", "kod_obiektu"],
        "ID_IIP":     ["ID_IIP", "id_iip"],
        "ETYKIETA":     ["ETYKIETA", "etykieta"],
        "DATA":     ["DATA", "data"],
    }





# ---------------------------------------------------------------------------
# Fabryka
# ---------------------------------------------------------------------------

_HARMONIZER_MAP: dict = {
    "bdsog_poziom": BdsogPoziomHarmonizer,
    "bdsog_wysok":  BdsogWysokHarmonizer,
    "bdsog":        BdsogPoziomHarmonizer,   # alias – gdy warstwa nieznana
    "egib_bud": EgibBudHarmonizer,
    "egib_dzi": EgibDziHarmonizer,
    "egib_pkt": EgibPktHarmonizer,
    "rcn": RCNHarmonizer,
    "bdot500": BDOT500BudHarmonizer
}


def get_harmonizer(zbior: str, layer_name: str = "") -> Optional[BaseHarmonizer]:
    """
    Zwraca instancję harmonizatora.

    :param zbior:      klucz zbioru, np. "bdsog"
    :param layer_name: nazwa warstwy WFS (np. "ewns:Osnowa_wysokosciowa")
                       – dla bdsog decyduje o wyborze poziom/wysok
    """
    if zbior == "bdsog" and layer_name:
        key = _layer_name_to_harmonizer_key(layer_name)
    elif zbior == "egib" and layer_name:
        key = _layer_name_to_harmonizer_key(layer_name, zbior)
    else:
        key = zbior

    cls = _HARMONIZER_MAP.get(key)
    if cls is None:
        QgsMessageLog.logMessage(
            f"[Harmonizer] Brak harmonizatora dla zbioru='{zbior}' "
            f"layer_name='{layer_name}' key='{key}'",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return None
    return cls()