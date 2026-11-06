# -*- coding: utf-8 -*-
"""
Zapis pobranych obiektów do warstw projektu.

Obiekty mają postać {'geom': WKT, 'attrs': {nazwa: wartość}} (patrz core.gml).
Wszystko tutaj działa w głównym wątku: tworzy i dodaje warstwy do projektu.
"""
import os
import zipfile
from typing import Optional

from qgis.core import (
    QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes,
)

from . import log
from .catalog import Source
from ..qt_compat import FIELD_STRING, GEOM_LINE, GEOM_POLYGON

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def style_path(name: Optional[str]) -> Optional[str]:
    """Pełna ścieżka do stylu QML z data/ (bez względu na wielkość liter) albo None."""
    if not name:
        return None
    if not name.lower().endswith(".qml"):
        name += ".qml"
    wanted = name.lower()
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


def write(features: list, layer_name: str, geom_type: str,
          unique_field: Optional[str] = None, style: Optional[str] = None,
          load_style: bool = True, project: QgsProject = None) -> Optional[QgsVectorLayer]:
    """
    Dopisuje obiekty do warstwy tymczasowej `layer_name` (tworzy ją, gdy jej nie ma).
    Obiekty o wartości `unique_field` już obecnej w warstwie są pomijane.
    Zwraca warstwę albo None, gdy nie było czego zapisać.
    """
    if not features:
        return None
    project = project or QgsProject.instance()

    existing = project.mapLayersByName(layer_name)
    seen = set()
    if existing:
        vl = existing[0]
        if unique_field and vl.fields().indexOf(unique_field) != -1:
            seen = {f.attribute(unique_field) for f in vl.getFeatures()}
    else:
        vl = QgsVectorLayer(f"{geom_type}?crs=epsg:2180", layer_name, "memory")
        vl.dataProvider().addAttributes(
            [QgsField(k, FIELD_STRING) for k in features[0]["attrs"].keys()])
        vl.updateFields()
        if load_style:
            apply_style(vl, style)
        project.addMapLayer(vl)

    is_multi = QgsWkbTypes.isMultiType(vl.wkbType())
    names = [f.name() for f in vl.fields()]
    out = []
    for fd in features:
        key = fd["attrs"].get(unique_field) if unique_field else None
        if key not in (None, "") and key in seen:
            continue
        g = QgsGeometry.fromWkt(fd["geom"])
        if g.isNull() or g.isEmpty():
            continue
        if is_multi and not g.isMultipart():
            g.convertToMultiType()
        elif not is_multi and g.isMultipart() and g.constGet().partCount() == 1:
            g.convertToSingleType()  # warstwa jednoczęściowa, np. z wersji 1.x
        feat = QgsFeature(vl.fields())
        feat.setGeometry(g)
        feat.setAttributes([fd["attrs"].get(n) for n in names])
        out.append(feat)
        if key not in (None, ""):
            seen.add(key)

    if out:
        ok, _ = vl.dataProvider().addFeatures(out)
        if not ok:
            log.warning(f"[Warstwa] {layer_name}: część obiektów odrzucona przez warstwę")
        vl.updateExtents()
        vl.triggerRepaint()
    return vl


def write_source(features: list, source: Source, load_style: bool = True) -> list:
    """Zapis według opisu źródła z katalogu. Zwraca listę użytych warstw."""
    if source.split_lines_polygons:
        parts = split_lines_polygons(features)
        layers = [
            write(parts["line"], f"{source.layer_name}_linie", "MultiLineString",
                  source.unique_field, source.style, load_style),
            write(parts["polygon"], f"{source.layer_name}_poligony", "MultiPolygon",
                  source.unique_field, "a08_ulice_poligony", load_style),
        ]
        return [l for l in layers if l is not None]
    layer = write(features, source.layer_name, source.geom_type,
                  source.unique_field, source.style, load_style)
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
