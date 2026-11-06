# -*- coding: utf-8 -*-
"""
Katalog źródeł danych WFS – jedno miejsce z adresami, warstwami i polami.

Dodanie warstwy = dopisanie wpisu tutaj; interfejs i potok pobierania
biorą wszystko z katalogu. Usługi skorowidzów (ortofotomapy, NMT, LIDAR)
są opisane w `skorowidz_services.py`.
"""
from dataclasses import dataclass, field
from typing import Optional

EGIB_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
RCN_URL = "https://mapy.geoportal.gov.pl/wss/service/rcn"
PRG_ADRESY_URL = ("https://mapy.geoportal.gov.pl/wss/ext/wfs/"
                  "KrajowaIntegracjaNumeracjiAdresowej/adresyUlice/wfs")
PRG_ADMIN_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
PRNG_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGiK/PRNG/WFS/GeographicalNames"
OPENDATA_URL = "https://opendata.geoportal.gov.pl/InneDane/latest_exports"

# Pola EGiB do wyboru w ustawieniach (dotyczą tylko działek EGiB)
EGIB_DZIALKI_ATTRIBUTES = [
    "id_dzialki", "numer_dzialki", "numer_obrebu", "numer_jednostki", "nazwa_obrebu",
    "nazwa_gminy", "pole_powierzchni", "grupa_rejestrowa", "data", "klasouzytki_egib",
]


@dataclass(frozen=True)
class Source:
    """Jedna warstwa WFS i sposób zapisu jej wyników."""
    label: str                      # tekst na liście w panelu
    group: str                      # EGIB, RCN, PRG, PRNG, PRG_A, PRG_K, …
    url: str
    typename: str                   # parametr typenames
    id_field: str                   # pole identyfikatora (filtr po ID)
    teryt_field: str                # pole filtrowane LIKE 'TERYT*'
    layer_name: str                 # nazwa warstwy wynikowej w projekcie
    geom_type: str = "MultiPolygon"  # Point | MultiLineString | MultiPolygon
    geom_field: str = "geom"
    unique_field: Optional[str] = None  # deduplikacja przy dopisywaniu
    style: Optional[str] = None     # plik QML w data/
    clip_locally: bool = False      # tylko BBOX w zapytaniu + przycięcie po pobraniu
    split_lines_polygons: bool = False  # A08: osobne warstwy linii i poligonów
    opendata: Optional[str] = None  # "EGIB" / "RCN": paczki GeoParquet/GPKG
    attribute_selection: bool = False   # czy działa wybór pól z ustawień
    # Pola liczbowe {nazwa: 'double'|'int'}; typ dostają tylko, gdy wszystkie wartości są liczbami
    numeric_fields: Optional[dict] = field(default=None, hash=False, compare=False)


def _egib(kind: str, id_field: str, layer: str, unique: str, style: str) -> Source:
    return Source(label=f"{kind} (EGIB)", group="EGIB", url=EGIB_URL, typename=f"ms:{kind}",
                  id_field=id_field, teryt_field=id_field, layer_name=layer,
                  unique_field=unique, style=style, opendata="EGIB",
                  attribute_selection=(kind == "dzialki"))


# Ceny i powierzchnie w RCN (pola ze stylów data/*_rcn.qml); nie są używane w wyrażeniach stylów
_RCN_NUMERIC = {
    "tran_cena_brutto": "double", "nier_cena_brutto": "double", "nier_pow_gruntu": "double",
    "dzi_cena_brutto": "double", "dzi_pow_ewid": "double",
    "bud_cena_brutto": "double", "bud_pow_uzyt": "double",
    "lok_cena_brutto": "double", "lok_pow_uzyt": "double", "lok_pow_przyn": "double",
    "lok_liczba_izb": "int",
}


def _rcn(kind: str, id_field: str, layer: str, geom_type: str, style: str) -> Source:
    return Source(label=f"{kind} (RCN)", group="RCN", url=RCN_URL, typename=f"ms:{kind}",
                  id_field=id_field, teryt_field=id_field, layer_name=layer,
                  geom_type=geom_type, unique_field="tran_lokalny_id_iip", style=style,
                  opendata="RCN", numeric_fields=_RCN_NUMERIC)


def _prng(name: str) -> Source:
    return Source(label=f"{name} (PRNG)", group="PRNG", url=PRNG_URL, typename=f"ms:{name}",
                  id_field="IDIIP", teryt_field="IDIIP", layer_name=f"{name}_PRNG",
                  geom_type="Point", geom_field="msGeometry", unique_field="IDIIP",
                  style=f"{name.lower()}_prng.qml", clip_locally=True)


# Warstwy morskie (W) będące poligonami; pozostałe W to linie
_PRG_W_POLYGONS = {"W02", "W03", "W04", "W05", "W10", "W11", "W12"}


def _prg_unique_field(name: str, kind: str) -> str:
    if kind == "A":
        return "JPT_KOD_JE"
    if kind == "P":
        return "JPT_JOR_ID"
    if kind == "R":
        return "REJ" if name.startswith("R01") else "OBWOD"
    if kind == "S":
        return "JPT_ID"
    return "boundedBy"  # K, U, W: brak stałego identyfikatora w usłudze


def _prg(name: str) -> Source:
    kind = name[0]
    geom_type = "MultiPolygon"
    if kind == "W" and name[:3] not in _PRG_W_POLYGONS:
        geom_type = "MultiLineString"
    return Source(label=f"{name} (PRG_{kind})", group=f"PRG_{kind}", url=PRG_ADMIN_URL,
                  typename=f"ms:{name}", id_field="IIP_IDENTY", teryt_field="IIP_IDENTY",
                  layer_name=f"{name}-{kind}_prg", geom_type=geom_type,
                  unique_field=_prg_unique_field(name, kind),
                  style=f"{kind.lower()}_prg.qml", clip_locally=True)


_PRG_LAYERS = [
    "A00_Granice_panstwa",
    "A01_Granice_wojewodztw",
    "A02_Granice_powiatow",
    "A03_Granice_gmin",
    "A04_Granice_miast",
    "A05_Granice_jednostek_ewidencyjnych",
    "A06_Granice_obrebow_ewidencyjnych",
    "R01_Granice_rejonow_statystycznych",
    "R02_Granice_obwodow_spisowych",
    "S01_Sad_apelacyjny",
    "S02_Sad_okregowy",
    "S03_Sad_rejonowy",
    "S04_Wojewodzki_sad_administracyjny",
    "P01_Prokuratura_regionalna",
    "P02_Prokuratura_okregowa",
    "P03_Prokuratura_rejonowa",
    "K01_Komenda_wojewodzka_policji",
    "K02_Komenda_powiatowa_policji",
    "K03_Komenda_stoleczna_policji",
    "K04_Komenda_rejonowa_policji",
    "K05_Komisariat_policji",
    "K06_Komenda_wojewodzka_strazy_pozarnej",
    "K07_Komenda_powiatowa_strazy_pozarnej",
    "K08_Oddzial_strazy_granicznej",
    "K09_Placowka_strazy_granicznej",
    "K10_Dywizjon_strazy_granicznej",
    "K11_Obszar_dzialania_szefa_obrony_cywilnej_wojewodztwa",
    "K12_Obszar_dzialania_szefa_obrony_cywilnej_powiatu",
    "K13_Obszar_dzialania_szefa_obrony_cywilnej_gminy",
    "U01_Archiwum_panstwowe",
    "U02_Urzad_skarbowy",
    "U03_Wyspecjalizowany_urzad_skarbowy",
    "U04_Urzad_celno-skarbowy",
    "U05_Izba_administracji_skarbowej",
    "U06_Nadlesnictwo",
    "U07_Regionalna_dyrekcja_lasow_panstwowych",
    "U08_Zarzad_zlewni_PGWWP",
    "U09_Regionalny_zarzad_gospodarki_wodnej_PGWWP",
    "U10_Urzad_morski",
    "U11_Urzad_zeglugi_srodladowej",
    "W01_Linia_podstawowa_morza_terytorialnego",
    "W02_Morze_terytorialne_RP",
    "W03_Morskie_wody_wewnetrzne",
    "W04_Wylaczna_strefa_ekonomiczna",
    "W05_Strefa_przylegla",
    "W06_Morskie_linie_brzegowe",
    "W07_Pas_nadbrzezny",
    "W08_Pas_ochronny",
    "W09_Pas_techniczny",
    "W10_Reda",
    "W11_Port_morski",
    "W12_Przystan_morska",
]

_PRNG_LAYERS = [
    "O1_UrzedoweNazwyObiektowFizjograficznych", "O2_ZestandaryzowaneNazwyObiektowFizjograficznych",
    "O3_PozostaleNazwyObiektowFizjograficznych", "M1_UrzedoweNazwyMiejscowosci",
    "M2_PozostaleNazwyMiejscowosci",
]

SOURCES = [
    _egib("dzialki", "id_dzialki", "Dzialki", "ID_DZIALKI", "dzialki.qml"),
    _egib("budynki", "id_budynku", "Budynki", "ID_BUDYNKU", "budynki.qml"),
    _rcn("dzialki", "dzi_id_dzialki", "Dzialki_RCN", "MultiPolygon", "dzialki_rcn.qml"),
    _rcn("budynki", "bud_id_budynku", "Budynki_RCN", "MultiPolygon", "budynki_rcn.qml"),
    _rcn("lokale", "lok_id_lokalu", "Lokale_RCN", "Point", "lokale_rcn.qml"),
    Source(label="A07_Punkty_adresowe (PRG)", group="PRG", url=PRG_ADRESY_URL,
           typename="adresyUlice:A07_Punkty_adresowe", id_field="ID_IIP", teryt_field="teryt",
           layer_name="A07_Punkty_adresowe", geom_type="Point", geom_field="GEOMETRIA",
           unique_field="ID_IIP", style="A07_Punkty_adresowe.qml", clip_locally=True),
    Source(label="A08_Ulice (PRG)", group="PRG", url=PRG_ADRESY_URL,
           typename="adresyUlice:A08_Ulice", id_field="ID_IIP", teryt_field="teryt",
           layer_name="A08_Ulice", geom_type="MultiLineString", geom_field="GEOMETRIA",
           unique_field="ID_IIP", style="A08_Ulice.qml", clip_locally=True,
           split_lines_polygons=True),
    *[_prng(n) for n in _PRNG_LAYERS],
    *[_prg(n) for n in _PRG_LAYERS],
]

SOURCES_BY_LABEL = {s.label: s for s in SOURCES}


def source_for_label(label: str) -> Optional[Source]:
    return SOURCES_BY_LABEL.get(label)


def egib_dzialki() -> Source:
    return SOURCES_BY_LABEL["dzialki (EGIB)"]


def opendata_package(source: Source, teryt: str) -> Optional[tuple]:
    """
    Paczka z opendata.geoportal.gov.pl dla województwa/kraju (GeoParquet)
    albo powiatu (GPKG w ZIP): (url, nazwa_pliku). None, gdy źródło jej nie ma.
    """
    if not source.opendata:
        return None
    kind = source.label.split(" ")[0]
    if len(teryt) < 4:
        ext, suffix = "parquet", f"_{kind}"
        file_type = "PARQUET"
    else:
        ext, suffix = "gpkg.zip", ""
        file_type = "GPKG"
    if source.opendata == "EGIB":
        name = f"{teryt}{suffix}.{ext}"
        url = f"{OPENDATA_URL}/eziudp_wfs/{file_type}/{name}"
    else:
        name = f"{teryt}_transakcje_ceny{suffix}.{ext}"
        url = f"{OPENDATA_URL}/rcn_transakcje_ceny/{file_type}/{name}"
    return url, name
