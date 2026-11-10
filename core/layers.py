# -*- coding: utf-8 -*-
"""
Zapis pobranych obiektów do warstw projektu.

Obiekty mają postać {'geom': WKT albo QgsGeometry, 'attrs': {nazwa: wartość}} (patrz core.gml).
Wyniki trafiają do warstw tymczasowych albo do jednego pliku GeoPackage (Output).
Wszystko tutaj działa w głównym wątku: tworzy i dodaje warstwy do projektu.
"""
import os
import zipfile
from dataclasses import dataclass
from typing import Optional

import re

from qgis.core import (
    QgsAbstractMetadataBase, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsProject,
    QgsSettings, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDateTime

from . import log
from .catalog import Source
from ..qt_compat import (
    FIELD_DOUBLE, FIELD_INT, FIELD_INTEGER_TYPES, FIELD_STRING, GEOM_LINE, GEOM_POLYGON,
)

GROUP_NAME = "GUGiK"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def style_path(name: Optional[str]) -> Optional[str]:
    """Pełna ścieżka do stylu QML z data/ (bez względu na wielkość liter) albo None."""
    if not name:
        return None
    if not name.lower().endswith(".qml"):
        name += ".qml"
    wanted = name.lower().replace(" ", "_")
    try:
        for f in os.listdir(DATA_DIR):
            if f.lower() == wanted:
                return os.path.join(DATA_DIR, f)
    except OSError:
        pass
    return None


def apply_style(layer, name: Optional[str]) -> None:
    path = style_path(name)
    if path:
        layer.loadNamedStyle(path)


# ---------------------------------------------------------------- obiekty → warstwa

def clip(features: list, geom: Optional[QgsGeometry]) -> list:
    """Zostawia obiekty przecinające `geom` (filtr lokalny po zapytaniu BBOX)."""
    if geom is None or geom.isEmpty():
        return features
    out = []
    for f in features:
        g = QgsGeometry.fromWkt(f["geom"])
        if not g.isNull() and g.intersects(geom):
            out.append(f)
    return out


def split_lines_polygons(features: list) -> dict:
    """{'line': [...], 'polygon': [...]}; inne typy są pomijane."""
    result = {"line": [], "polygon": []}
    for f in features:
        g = QgsGeometry.fromWkt(f["geom"])
        if g.isNull() or g.isEmpty():
            continue
        gtype = QgsWkbTypes.geometryType(g.wkbType())
        if gtype == GEOM_POLYGON:
            result["polygon"].append(f)
        elif gtype == GEOM_LINE:
            result["line"].append(f)
        else:
            log.warning(f"[Warstwa] Pominięto nieobsługiwany typ geometrii {g.wkbType()}")
    return result


class Output:
    """Dokąd zapisywać wyniki: warstwy tymczasowe albo jeden plik GeoPackage."""

    def __init__(self, gpkg_path: Optional[str] = None, load_style: bool = True,
                 separate: bool = False, prefix: str = ""):
        self.gpkg_path = gpkg_path or None
        self.load_style = load_style
        # True: każde pobranie do nowej warstwy (Dzialki_1465_1, _2…) zamiast dopisywania
        self.separate = separate
        # Prefiks nazw warstw, np. „ProjektA” → ProjektA_Dzialki (podgrupa GUGiK/ProjektA)
        self.prefix = clean_prefix(prefix)

    def layer_name(self, base: str) -> str:
        return f"{self.prefix}_{base}" if self.prefix else base

    @classmethod
    def from_settings(cls, load_style: bool = True) -> "Output":
        s = QgsSettings()
        separate = s.value(f"{SETTINGS_PREFIX}/separate_layers", False, type=bool)
        prefix = s.value(f"{SETTINGS_PREFIX}/layer_prefix", "", type=str)
        gpkg = None
        if s.value(f"{SETTINGS_PREFIX}/output_mode", "memory", type=str) == "gpkg":
            gpkg = s.value(f"{SETTINGS_PREFIX}/gpkg_path", "", type=str)
        return cls(gpkg, load_style, separate, prefix)


SETTINGS_PREFIX = "pobierz_dane_GUGiK"


# ---------------------------------------------------------------- typy pól

def _to_number(value, kind: str):
    """Liczba z tekstu ('12,5' też) albo None. Wiodące zera = identyfikator, nie liczba."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if kind == "int" else float(value)
    s = str(value).strip().replace(",", ".")
    if s == "" or s.lower() in ("null", "none"):
        return None
    if len(s) > 1 and s[0] == "0" and s[1].isdigit():
        raise ValueError(s)
    num = float(s)
    if kind == "int":
        if not num.is_integer():
            raise ValueError(s)
        return int(num)
    return num


def _all_numeric(features: list, name: str, kind: str) -> bool:
    seen = False
    for f in features:
        v = f["attrs"].get(name)
        try:
            if _to_number(v, kind) is not None:
                seen = True
        except (ValueError, TypeError):
            return False
    return seen


def _schema(features: list, numeric: Optional[dict]) -> QgsFields:
    """Pola nowej warstwy: tekst, chyba że pole jest w `numeric` i każda wartość jest liczbą."""
    fields = QgsFields()
    for name in features[0]["attrs"].keys():
        kind = (numeric or {}).get(name)
        if kind and _all_numeric(features, name, kind):
            fields.append(QgsField(name, FIELD_INT if kind == "int" else FIELD_DOUBLE))
        else:
            fields.append(QgsField(name, FIELD_STRING))
    return fields


def _field_plan(layer_fields: QgsFields, pk: set) -> list:
    """
    [(nazwa pola, rodzaj)] raz na zapis: rodzaj to 'pk' | 'int' | 'double' | 'str' | 'raw'.
    Typ pola czytany jest raz, a nie dla każdej wartości (przy 100 tys. obiektów
    to różnica kilku sekund zamrożenia QGIS).
    """
    plan = []
    for i in range(layer_fields.count()):
        field = layer_fields.at(i)
        t = field.type()
        kind = ("pk" if i in pk else "double" if t == FIELD_DOUBLE
                else "int" if t in FIELD_INTEGER_TYPES else "str" if t == FIELD_STRING else "raw")
        plan.append((field.name(), kind))
    return plan


def _convert(value, kind: str, name: str, bad: dict):
    if value is None or kind == "raw":
        return value
    if kind == "str":
        return value if isinstance(value, str) else str(value)
    try:
        return _to_number(value, kind)
    except (ValueError, TypeError):
        bad[name] = bad.get(name, 0) + 1
        return None


# ---------------------------------------------------------------- warstwa docelowa

def _find_layer(project: QgsProject, name: str, gpkg_path: Optional[str]):
    """Warstwa o tej nazwie w projekcie, pasująca do miejsca zapisu (pamięć albo ten GPKG)."""
    for vl in project.mapLayersByName(name):
        if not isinstance(vl, QgsVectorLayer):
            continue
        is_memory = vl.providerType() == "memory"
        if gpkg_path is None and is_memory:
            return vl
        if gpkg_path is not None and not is_memory:
            src = vl.source().split("|")[0]
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(gpkg_path)):
                return vl
    return None


def _gpkg_layer(path: str, name: str, geom_type: str, fields: QgsFields) -> QgsVectorLayer:
    """Otwiera warstwę `name` w pliku GPKG; tworzy ją (i plik), gdy jej nie ma."""
    uri = f"{path}|layername={name}"
    if os.path.exists(path):
        existing = QgsVectorLayer(uri, name, "ogr")
        if existing.isValid():
            return existing
    template = QgsVectorLayer(f"{geom_type}?crs=epsg:2180", name, "memory")
    template.dataProvider().addAttributes(fields.toList())
    template.updateFields()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = name
    if any(f.name().lower() == "fid" for f in fields):
        opts.layerOptions = ["FID=gpkg_fid"]  # pole „fid” z usługi zostaje zwykłym polem
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer if os.path.exists(path)
        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        template, path, QgsProject.instance().transformContext(), opts)
    if res[0] != QgsVectorFileWriter.WriterError.NoError:
        raise IOError(f"Nie można utworzyć warstwy {name} w {path}: {res[1]}")
    layer = QgsVectorLayer(uri, name, "ogr")
    if not layer.isValid():
        raise IOError(f"Nie można otworzyć warstwy {name} w {path}")
    return layer


def add_to_project(layer, project: QgsProject = None, subgroup: str = "") -> None:
    """
    Dodaje warstwę do projektu, na górę grupy „GUGiK” (tworzy ją na górze drzewa);
    z `subgroup` – do podgrupy GUGiK/<subgroup> (np. prefiks projektu).
    """
    project = project or QgsProject.instance()
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    group = root.findGroup(GROUP_NAME)
    if group is None:
        group = root.insertGroup(0, GROUP_NAME)
    if subgroup:
        sub = group.findGroup(subgroup)
        if sub is None:
            sub = group.insertGroup(0, subgroup)
        group = sub
    group.insertLayer(0, layer)


_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def clean_prefix(text: str) -> str:
    """Prefiks nazw warstw: litery (także polskie), cyfry, _ i -; bez skrajnych podkreśleń."""
    text = re.sub(r"[^\w-]+", "_", (text or "").strip(), flags=re.UNICODE)
    return text.strip("_")[:40]


def _safe(text: str) -> str:
    """Fragment nazwy warstwy: litery, cyfry, podkreślenia."""
    text = (text or "").translate(_PL)
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")[:30]


def unique_name(base: str, tag: str = "", project: QgsProject = None,
                gpkg_path: Optional[str] = None) -> str:
    """Pierwsza wolna nazwa `base_tag_N` (N od 1) w projekcie i w pliku GPKG."""
    project = project or QgsProject.instance()
    stem = f"{base}_{_safe(tag)}" if _safe(tag) else base
    taken = {lyr.name() for lyr in project.mapLayers().values()}
    if gpkg_path and os.path.exists(gpkg_path):
        probe = QgsVectorLayer(gpkg_path, "probe", "ogr")
        if probe.isValid():
            taken |= {sub.split("!!::!!")[1] for sub in probe.dataProvider().subLayers()
                      if len(sub.split("!!::!!")) >= 2}
    n = 1
    while f"{stem}_{n}" in taken:
        n += 1
    return f"{stem}_{n}"


def stamp(layer, info: Optional[dict], added: int) -> None:
    """
    Metadane warstwy: adres usługi, zakres i data każdego pobrania
    (Właściwości warstwy → Metadane; historia rośnie przy dopisywaniu).
    info: {'url', 'typename', 'scope'} – brakujące klucze są pomijane.
    """
    if not info or layer is None:
        return
    md = layer.metadata()
    now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm")
    url, typename, scope = info.get("url", ""), info.get("typename", ""), info.get("scope", "")
    if not md.title():
        md.setTitle(layer.name())
    md.setAbstract("\n".join(x for x in (
        "Dane pobrane wtyczką „Pobierz dane GUGiK”.",
        f"Usługa: {url}" if url else "",
        f"Warstwa usługi: {typename}" if typename else "",
        f"Ostatnie pobranie: {now}" + (f", zakres: {scope}" if scope else ""),
    ) if x))
    md.addHistoryItem(f"{now} · {added} obiektów" + (f" · {scope}" if scope else "")
                      + (f" · {url}" if url else ""))
    if url and not any(link.url == url for link in md.links()):
        link = QgsAbstractMetadataBase.Link("Usługa WFS", "OGC:WFS", url)
        md.addLink(link)
    layer.setMetadata(md)


def make_features(features: list, layer_fields: QgsFields, is_multi: bool,
                  unique_field: Optional[str] = None, seen: Optional[set] = None,
                  pk: Optional[set] = None, label: str = "") -> list:
    """
    Obiekty QgsFeature w schemacie `layer_fields` (bez warstwy i projektu –
    używane też przez algorytmy Processing). Duplikaty `unique_field` (także
    już obecne w `seen`) i puste geometrie są pomijane.
    """
    seen = set() if seen is None else seen
    pk = pk or set()      # np. fid w GPKG – nadaje dostawca
    bad: dict = {}
    out = []
    plan = _field_plan(layer_fields, pk)
    for fd in features:
        key = fd["attrs"].get(unique_field) if unique_field else None
        if key not in (None, "") and key in seen:
            continue
        g = fd["geom"]
        g = QgsGeometry(g) if isinstance(g, QgsGeometry) else QgsGeometry.fromWkt(g)
        if g.isNull() or g.isEmpty():
            continue
        if is_multi and not g.isMultipart():
            g.convertToMultiType()
        elif not is_multi and g.isMultipart() and g.constGet().partCount() == 1:
            g.convertToSingleType()  # warstwa jednoczęściowa, np. z wersji 1.x
        feat = QgsFeature(layer_fields)
        feat.setGeometry(g)
        attrs = fd["attrs"]
        feat.setAttributes([None if kind == "pk" else _convert(attrs.get(name), kind, name, bad)
                            for name, kind in plan])
        out.append(feat)
        if key not in (None, ""):
            seen.add(key)
    for name, n in bad.items():
        log.warning(f"[Warstwa] {label}: {n} wartości pola {name} nie jest liczbą – zapisano puste")
    return out


@dataclass
class Prepared:
    """
    Obiekty gotowe do dodania do warstwy, przygotowane w wątku roboczym
    (zadanie pobierania), żeby w głównym wątku zostało samo addFeatures.
    """
    fields: QgsFields
    features: list          # QgsFeature w schemacie `fields`
    keys: list              # wartość pola unikalnego dla każdego obiektu (albo None)


def prepare(features: list, geom_type: str, numeric: Optional[dict] = None,
            unique_field: Optional[str] = None, label: str = "") -> Prepared:
    """Schemat + QgsFeature z pobranych obiektów; bez warstwy i projektu (bezpieczne w tle)."""
    fields = _schema(features, numeric)
    is_multi = QgsWkbTypes.isMultiType(QgsWkbTypes.parseType(geom_type))
    out = make_features(features, fields, is_multi, unique_field, label=label)
    idx = fields.indexOf(unique_field) if unique_field else -1
    keys = [f.attribute(idx) for f in out] if idx >= 0 else [None] * len(out)
    return Prepared(fields, out, keys)


def _same_schema(a: QgsFields, b: QgsFields) -> bool:
    return a.count() == b.count() and all(
        a.at(i).name() == b.at(i).name() and a.at(i).type() == b.at(i).type() for i in range(a.count()))


def schema_for(features: list, numeric: Optional[dict] = None) -> QgsFields:
    """Schemat pól z atrybutów pobranych obiektów (publiczny odpowiednik _schema)."""
    return _schema(features, numeric)


def write(features: list, layer_name: str, geom_type: str,
          unique_field: Optional[str] = None, style: Optional[str] = None,
          load_style: bool = True, project: QgsProject = None,
          numeric: Optional[dict] = None, fields: Optional[QgsFields] = None,
          gpkg_path: Optional[str] = None, info: Optional[dict] = None,
          group: str = "", prepared: Optional[Prepared] = None) -> Optional[QgsVectorLayer]:
    """
    Dopisuje obiekty do warstwy `layer_name` (tworzy ją, gdy jej nie ma):
    tymczasowej albo w pliku `gpkg_path`.

    features  – [{'geom': WKT albo QgsGeometry, 'attrs': {nazwa: wartość}}]
    fields    – gotowy schemat (np. z dostawcy WFS); domyślnie z atrybutów, jako tekst
    numeric   – {pole: 'double'|'int'}: te pola dostają typ liczbowy, o ile każda
                wartość w pierwszej porcji jest liczbą (inaczej zostają tekstem)
    info      – opis pobrania do metadanych warstwy (patrz stamp)
    group     – podgrupa w grupie „GUGiK” dla nowej warstwy (prefiks projektu)
    prepared  – obiekty przygotowane w tle (prepare); używane, gdy schemat warstwy
                się zgadza – inaczej obiekty są budowane tutaj z `features`
    Obiekty o wartości `unique_field` już obecnej w warstwie są pomijane.
    Zwraca warstwę albo None, gdy nie było czego zapisać.
    """
    if not features:
        return None
    project = project or QgsProject.instance()

    vl = _find_layer(project, layer_name, gpkg_path)
    seen = set()
    if vl is not None:
        if unique_field and vl.fields().indexOf(unique_field) != -1:
            seen = {f.attribute(unique_field) for f in vl.getFeatures()}
    else:
        schema = (fields if fields is not None else prepared.fields if prepared is not None
                  else _schema(features, numeric))
        if gpkg_path:
            vl = _gpkg_layer(gpkg_path, layer_name, geom_type, schema)
            if unique_field and vl.fields().indexOf(unique_field) != -1:
                seen = {f.attribute(unique_field) for f in vl.getFeatures()}
        else:
            vl = QgsVectorLayer(f"{geom_type}?crs=epsg:2180", layer_name, "memory")
            vl.dataProvider().addAttributes(schema.toList())
            vl.updateFields()
        if load_style:
            apply_style(vl, style)
        add_to_project(vl, project, group)

    pk = set(vl.dataProvider().pkAttributeIndexes())
    if prepared is not None and not pk and _same_schema(vl.fields(), prepared.fields) \
            and QgsWkbTypes.isMultiType(vl.wkbType()):
        # szybka ścieżka: obiekty gotowe z wątku roboczego, tu tylko duplikaty i dodanie
        out = [f for f, k in zip(prepared.features, prepared.keys)
               if k in (None, "") or k not in seen] if seen else list(prepared.features)
    else:
        out = make_features(features, vl.fields(), QgsWkbTypes.isMultiType(vl.wkbType()),
                            unique_field, seen, pk, layer_name)
    if out:
        ok, _ = vl.dataProvider().addFeatures(out)
        if not ok:
            log.warning(f"[Warstwa] {layer_name}: część obiektów odrzucona przez warstwę")
        vl.updateExtents()
        vl.triggerRepaint()
    stamp(vl, info, len(out))
    return vl


def write_source(features: list, source: Source, output: Optional[Output] = None,
                 scope: str = "", tag: str = "", prepared: Optional[Prepared] = None) -> list:
    """
    Zapis według opisu źródła z katalogu. Zwraca listę użytych warstw.
    scope – opis zakresu do metadanych; tag – fragment nazwy przy osobnych warstwach.
    """
    output = output or Output()
    info = {"url": source.url, "typename": source.typename, "scope": scope}
    common = dict(load_style=output.load_style, numeric=source.numeric_fields,
                  gpkg_path=output.gpkg_path, info=info, group=output.prefix)

    def name(base):
        base = output.layer_name(base)
        return unique_name(base, tag, gpkg_path=output.gpkg_path) if output.separate else base

    if source.split_lines_polygons:
        parts = split_lines_polygons(features)
        layers = [
            write(parts["line"], name(f"{source.layer_name}_linie"), "MultiLineString",
                  source.unique_field, source.style, **common),
            write(parts["polygon"], name(f"{source.layer_name}_poligony"), "MultiPolygon",
                  source.unique_field, "a08_ulice_poligony", **common),
        ]
        return [lyr for lyr in layers if lyr is not None]
    layer = write(features, name(source.layer_name), source.geom_type,
                  source.unique_field, source.style, prepared=prepared, **common)
    return [layer] if layer is not None else []


def boundary_layer(geom: QgsGeometry, teryt: str, load_style: bool = True) -> QgsVectorLayer:
    """Warstwa z jedną granicą jednostki z PRG."""
    vl = QgsVectorLayer("MultiPolygon?crs=epsg:2180", f"Granica PRG - {teryt}", "memory")
    vl.dataProvider().addAttributes([QgsField("TERYT", FIELD_STRING)])
    vl.updateFields()
    g = QgsGeometry(geom)
    if not g.isMultipart():
        g.convertToMultiType()
    feat = QgsFeature(vl.fields())
    feat.setGeometry(g)
    feat.setAttributes([teryt])
    vl.dataProvider().addFeatures([feat])
    vl.updateExtents()
    if load_style:
        apply_style(vl, "prg.qml")
    add_to_project(vl)
    return vl


# ---------------------------------------------------------------- paczki opendata

def _style_for_file(name: str) -> str:
    """Nazwa stylu z nazwy pliku/warstwy: '1465_dzialki.parquet' → 'dzialki'."""
    base = name.split("_")[-1] if "_" in name else name
    return base.split(".")[0].lower()


def add_package(path: str, load_style: bool = True, output: Optional["Output"] = None) -> list:
    """
    Dodaje do projektu pobraną paczkę: .parquet albo .gpkg.zip (rozpakowuje,
    wczytuje wszystkie warstwy). Zwraca listę dodanych warstw.
    """
    added = []
    if path.lower().endswith(".zip"):
        folder = os.path.dirname(path)
        with zipfile.ZipFile(path) as z:
            gpkgs = [n for n in z.namelist() if n.lower().endswith(".gpkg")]
            if not gpkgs:
                log.error("[Paczka] W archiwum ZIP nie ma pliku GPKG")
                return added
            for name in gpkgs:
                target = os.path.realpath(os.path.join(folder, name))
                if not target.startswith(os.path.realpath(folder) + os.sep):
                    log.error(f"[Paczka] Pominięto podejrzaną ścieżkę w ZIP: {name}")
                    continue
                z.extract(name, folder)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning(f"[Paczka] Nie usunięto archiwum {path}: {exc}")
        path = os.path.join(folder, gpkgs[0])

    probe = QgsVectorLayer(path, "probe", "ogr")
    if not probe.isValid():
        log.error(f"[Paczka] Nie można otworzyć pliku: {path}")
        return added
    sublayers = probe.dataProvider().subLayers() if path.lower().endswith(".gpkg") else []

    if len(sublayers) > 1:
        names = [s.split("!!::!!")[1] for s in sublayers if len(s.split("!!::!!")) >= 2]
        sources = [(f"{path}|layername={n}", n) for n in names if n]
    else:
        sources = [(path, os.path.basename(path))]

    prefix = output.prefix if output is not None else ""
    for uri, name in sources:
        shown = output.layer_name(name) if output is not None else name
        vl = QgsVectorLayer(uri, shown, "ogr")
        if not vl.isValid():
            log.error(f"[Paczka] Nie można wczytać warstwy {name}")
            continue
        if load_style:
            apply_style(vl, _style_for_file(name))
        add_to_project(vl, subgroup=prefix)
        added.append(vl)
        log.success(f"[Paczka] Dodano warstwę: {name}")
    return added
