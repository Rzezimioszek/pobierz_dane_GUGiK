# -*- coding: utf-8 -*-
"""
Zapis pobranych obiektów do warstw projektu.

Obiekty mają postać {'geom': WKT albo QgsGeometry, 'attrs': {nazwa: wartość}} (patrz core.gml).
Wyniki trafiają do warstw tymczasowych albo do jednego pliku GeoPackage (Output).
Wszystko tutaj działa w głównym wątku: tworzy i dodaje warstwy do projektu.
"""
import os
import zipfile
from typing import Optional

from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsProject, QgsSettings,
    QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)

from . import log
from .catalog import Source
from ..qt_compat import (
    FIELD_DOUBLE, FIELD_INT, FIELD_INTEGER_TYPES, FIELD_STRING, GEOM_LINE, GEOM_POLYGON,
)

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

    def __init__(self, gpkg_path: Optional[str] = None, load_style: bool = True):
        self.gpkg_path = gpkg_path or None
        self.load_style = load_style

    @classmethod
    def from_settings(cls, load_style: bool = True) -> "Output":
        s = QgsSettings()
        if s.value(f"{SETTINGS_PREFIX}/output_mode", "memory", type=str) == "gpkg":
            return cls(s.value(f"{SETTINGS_PREFIX}/gpkg_path", "", type=str), load_style)
        return cls(None, load_style)


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


def _value_for(field: QgsField, value, bad: dict):
    if value is None:
        return None
    t = field.type()
    if t in FIELD_INTEGER_TYPES or t == FIELD_DOUBLE:
        try:
            return _to_number(value, "double" if t == FIELD_DOUBLE else "int")
        except (ValueError, TypeError):
            bad[field.name()] = bad.get(field.name(), 0) + 1
            return None
    if t == FIELD_STRING and not isinstance(value, str):
        return str(value)
    return value


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


def write(features: list, layer_name: str, geom_type: str,
          unique_field: Optional[str] = None, style: Optional[str] = None,
          load_style: bool = True, project: QgsProject = None,
          numeric: Optional[dict] = None, fields: Optional[QgsFields] = None,
          gpkg_path: Optional[str] = None) -> Optional[QgsVectorLayer]:
    """
    Dopisuje obiekty do warstwy `layer_name` (tworzy ją, gdy jej nie ma):
    tymczasowej albo w pliku `gpkg_path`.

    features  – [{'geom': WKT albo QgsGeometry, 'attrs': {nazwa: wartość}}]
    fields    – gotowy schemat (np. z dostawcy WFS); domyślnie z atrybutów, jako tekst
    numeric   – {pole: 'double'|'int'}: te pola dostają typ liczbowy, o ile każda
                wartość w pierwszej porcji jest liczbą (inaczej zostają tekstem)
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
        schema = fields if fields is not None else _schema(features, numeric)
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
        project.addMapLayer(vl)

    is_multi = QgsWkbTypes.isMultiType(vl.wkbType())
    layer_fields = vl.fields()
    pk = set(vl.dataProvider().pkAttributeIndexes())  # np. fid w GPKG – nadaje dostawca
    bad: dict = {}
    out = []
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
        feat.setAttributes([
            None if i in pk else _value_for(layer_fields.at(i), fd["attrs"].get(layer_fields.at(i).name()), bad)
            for i in range(layer_fields.count())])
        out.append(feat)
        if key not in (None, ""):
            seen.add(key)

    for name, n in bad.items():
        log.warning(f"[Warstwa] {layer_name}: {n} wartości pola {name} nie jest liczbą – zapisano puste")
    if out:
        ok, _ = vl.dataProvider().addFeatures(out)
        if not ok:
            log.warning(f"[Warstwa] {layer_name}: część obiektów odrzucona przez warstwę")
        vl.updateExtents()
        vl.triggerRepaint()
    return vl


def write_source(features: list, source: Source, output: Optional[Output] = None) -> list:
    """Zapis według opisu źródła z katalogu. Zwraca listę użytych warstw."""
    output = output or Output()
    common = dict(load_style=output.load_style, numeric=source.numeric_fields,
                  gpkg_path=output.gpkg_path)
    if source.split_lines_polygons:
        parts = split_lines_polygons(features)
        layers = [
            write(parts["line"], f"{source.layer_name}_linie", "MultiLineString",
                  source.unique_field, source.style, **common),
            write(parts["polygon"], f"{source.layer_name}_poligony", "MultiPolygon",
                  source.unique_field, "a08_ulice_poligony", **common),
        ]
        return [l for l in layers if l is not None]
    layer = write(features, source.layer_name, source.geom_type,
                  source.unique_field, source.style, **common)
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
    QgsProject.instance().addMapLayer(vl)
    return vl


# ---------------------------------------------------------------- paczki opendata

def _style_for_file(name: str) -> str:
    """Nazwa stylu z nazwy pliku/warstwy: '1465_dzialki.parquet' → 'dzialki'."""
    base = name.split("_")[-1] if "_" in name else name
    return base.split(".")[0].lower()


def add_package(path: str, load_style: bool = True) -> list:
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

    for uri, name in sources:
        vl = QgsVectorLayer(uri, name, "ogr")
        if not vl.isValid():
            log.error(f"[Paczka] Nie można wczytać warstwy {name}")
            continue
        if load_style:
            apply_style(vl, _style_for_file(name))
        QgsProject.instance().addMapLayer(vl)
        added.append(vl)
        log.success(f"[Paczka] Dodano warstwę: {name}")
    return added
