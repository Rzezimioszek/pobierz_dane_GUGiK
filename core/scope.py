# -*- coding: utf-8 -*-
"""
Zakres → zapytania WFS. Wspólne dla panelu i algorytmów Processing.

Wszystkie geometrie w EPSG:2180.
"""
import re
from typing import Optional

from qgis.core import QgsGeometry

from . import filters, log
from .catalog import Source
from .tasks import Query

ID_BATCH = 10            # identyfikatorów w jednym zapytaniu
MAX_EXACT_COORDS = 100   # do tylu wierzchołków filtr ma dokładny kształt


def split_ids(text: str) -> list:
    """Identyfikatory z tekstu: rozdzielone nową linią, przecinkiem, średnikiem lub spacją."""
    return [t for t in re.split(r"[\s,;]+", text or "") if t]


def id_queries(source: Source, ids: list) -> list:
    return [Query(filters.ids(source.id_field, ids[i:i + ID_BATCH]),
                  label=f"ID {i + 1}–{min(i + ID_BATCH, len(ids))}")
            for i in range(0, len(ids), ID_BATCH)]


def geometry_queries(source: Source, geoms: list, label: str) -> list:
    """
    Zapytanie na każdą geometrię. Do 100 wierzchołków: dokładny kształt
    (każda część osobno), więcej: otoczka wypukła albo prostokąt.
    Zawsze z przycięciem po pobraniu, więc wynik odpowiada dokładnie geometrii.
    """
    queries = []
    for i, g in enumerate(geoms):
        name = f"{label} #{i + 1}" if len(geoms) > 1 else label
        if g.constGet().nCoordinates() <= MAX_EXACT_COORDS:
            flt = filters.spatial(source.geom_field, g.asWkt(), use_bbox=False)
        else:
            hull = g.convexHull()
            flt = (filters.spatial(source.geom_field, hull.asWkt(), use_bbox=False)
                   if hull.constGet().nCoordinates() <= MAX_EXACT_COORDS
                   else filters.spatial(source.geom_field, g.asWkt(), use_bbox=True))
        queries.append(Query(flt, clip_geom=g, label=name, rect=g.boundingBox()))
    return queries


def teryt_query(source: Source, teryt: str, boundary: QgsGeometry, precise: bool = True) -> Query:
    """Jednostka administracyjna: granica PRG + (gdy źródło ma pole TERYT) filtr po kodzie."""
    rect = boundary.boundingBox()
    if source.clip_locally:
        return Query(filters.spatial(source.geom_field, boundary.asWkt(), use_bbox=True),
                     clip_geom=boundary, label=teryt, rect=rect)
    code = filters.attribute(source.teryt_field, teryt, like=True)
    return Query(filters.combine([
        filters.spatial(source.geom_field, boundary.asWkt(), use_bbox=not precise), code,
    ]), label=teryt, rect=rect, extra_filter=code)


def rect_query(source: Source, geom: QgsGeometry, label: str) -> Query:
    """Prostokąt (widok mapy, narysowany prostokąt) – z zasięgiem do dzielenia na kafle."""
    return Query(filters.spatial(source.geom_field, geom.asWkt()), label=label, rect=geom.boundingBox())


def boundary(teryt: str) -> Optional[QgsGeometry]:
    """Granica jednostki z PRG, synchronicznie (do użycia w wątku roboczym)."""
    from .tasks import _close
    from ..prg_client import PRGClient
    client = PRGClient()
    try:
        geom = client.get_boundary_geometry(teryt)
    except Exception as exc:
        log.warning(f"[PRG] {teryt}: {exc}")
        geom = None
    finally:
        _close(client.session)
    return geom if geom is not None and not geom.isEmpty() else None
