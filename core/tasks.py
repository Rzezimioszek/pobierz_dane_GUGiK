# -*- coding: utf-8 -*-
"""
Zadania w tle. Cała praca sieciowa wtyczki odbywa się tutaj, nigdy w wątku interfejsu.

    WfsDownloadTask  – jedno źródło, jeden lub wiele filtrów, stronicowanie,
                       opcjonalne przycięcie wyników do geometrii
    HitsTask         – liczba obiektów pasujących do filtra
    FileDownloadTask – plik (paczki opendata, rastry ze skorowidzów) na dysk
"""
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import QgsGeometry, QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from . import gml, layers, log, net
from .catalog import Source
from .wfs import PAGE_SIZE, WfsClient


@dataclass
class Query:
    """Jedno zapytanie: filtr FES i opcjonalna geometria do przycięcia wyników."""
    filter_xml: str
    clip_geom: Optional[QgsGeometry] = None
    label: str = ""


@dataclass
class DownloadResult:
    features: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # [(etykieta zapytania, komunikat)]
    canceled: bool = False


class WfsDownloadTask(QgsTask):
    """
    Pobiera obiekty jednego źródła dla listy zapytań, strona po stronie.
    Błąd jednego zapytania nie przerywa pozostałych; trafia do `result.errors`.
    Sygnał `done(DownloadResult)` przychodzi w głównym wątku.
    """
    done = pyqtSignal(object)

    def __init__(self, source: Source, queries: list, attributes: Optional[list] = None,
                 expected: int = 0, description: str = ""):
        super().__init__(description or f"Pobieranie: {source.label}", QgsTask.Flag.CanCancel)
        self.source = source
        self.queries = [q if isinstance(q, Query) else Query(q) for q in queries]
        self.attributes = attributes if source.attribute_selection else None
        self.expected = expected
        self.result = DownloadResult()
        self.client = WfsClient(source.url, source.typename, source.geom_field)

    def run(self) -> bool:
        n = len(self.queries)
        for qi, query in enumerate(self.queries):
            if self.isCanceled():
                self.result.canceled = True
                return False
            try:
                self.result.features.extend(self._run_query(query, qi, n))
            except Exception as exc:  # sieć, serwer, XML – zapisz i jedź dalej
                label = query.label or f"zapytanie {qi + 1}/{n}"
                log.warning(f"[Pobieranie] {self.source.label}, {label}: {exc}")
                self.result.errors.append((label, str(exc)))
        self.setProgress(100)
        return not self.result.canceled

    def _run_query(self, query: Query, qi: int, n: int) -> list:
        features, start = [], 0
        while True:
            if self.isCanceled():
                self.result.canceled = True
                break
            page = gml.parse_features(
                self.client.page(query.filter_xml, start, PAGE_SIZE, self.attributes))
            features.extend(page)
            self._report(qi, n, start + len(page))
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
        return layers.clip(features, query.clip_geom)

    def _report(self, qi: int, n: int, done_in_query: int) -> None:
        if n > 1:
            self.setProgress(100.0 * (qi + 1) / n)
        elif self.expected > 0:
            self.setProgress(min(99.0, 100.0 * done_in_query / self.expected))

    def finished(self, ok: bool) -> None:
        if self.isCanceled():
            self.result.canceled = True
        self.done.emit(self.result)


class HitsTask(QgsTask):
    """Liczba obiektów dla filtra; `done(int)`: -1 = nieznana, -2 = błąd (`error`)."""
    done = pyqtSignal(int)

    def __init__(self, source: Source, filter_xml: str):
        super().__init__("Sprawdzanie liczby obiektów…", QgsTask.Flag.CanCancel)
        self.client = WfsClient(source.url, source.typename, source.geom_field)
        self.filter_xml = filter_xml
        self.hits = -1
        self.error = None

    def run(self) -> bool:
        try:
            self.hits = self.client.hits(self.filter_xml)
            return True
        except Exception as exc:
            self.error = exc
            return False

    def finished(self, ok: bool) -> None:
        self.done.emit(self.hits if ok else -2)


class FileDownloadTask(QgsTask):
    """Pobiera plik na dysk; `done(path)`, pusty tekst przy błędzie lub anulowaniu."""
    done = pyqtSignal(str)

    CHUNK = 5 * 1024 * 1024

    def __init__(self, url: str, path: str, description: str = ""):
        super().__init__(description or f"Pobieranie pliku {path}", QgsTask.Flag.CanCancel)
        self.url = url
        self.path = path
        self.error = None
        self.session = net.make_session()

    def run(self) -> bool:
        try:
            with self.session.get(self.url, stream=True, timeout=900) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                got = 0
                with open(self.path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=self.CHUNK):
                        if self.isCanceled():
                            return False
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                            if total:
                                self.setProgress(100.0 * got / total)
            return True
        except Exception as exc:
            self.error = exc
            return False

    def finished(self, ok: bool) -> None:
        if ok:
            log.success(f"[Plik] Zapisano: {self.path}")
        elif self.isCanceled():
            log.warning(f"[Plik] Anulowano: {self.url}")
        else:
            log.error(f"[Plik] Błąd pobierania {self.url}: {self.error}")
        self.done.emit(self.path if ok else "")


class BoundaryTask(QgsTask):
    """Granica jednostki z PRG w tle; `done(QgsGeometry or None)`."""
    done = pyqtSignal(object)

    def __init__(self, teryt: str):
        super().__init__(f"Granica PRG: {teryt}", QgsTask.Flag.CanCancel)
        from ..prg_client import PRGClient
        self.teryt = teryt
        self.client = PRGClient()
        self.geometry = None

    def run(self) -> bool:
        try:
            self.geometry = self.client.get_boundary_geometry(self.teryt)
        except Exception as exc:
            log.warning(f"[PRG] {self.teryt}: {exc}")
        return self.geometry is not None and not self.geometry.isEmpty()

    def finished(self, ok: bool) -> None:
        self.done.emit(self.geometry if ok else None)


class SkorowidzSearchTask(QgsTask):
    """
    Szuka arkuszy w skorowidzu dla listy warstw (np. roczników) i geometrii.
    `done(features, errors)`: features jak gml.read_typed, errors [(warstwa, komunikat)].
    Rocznik, którego nie ma w usłudze, trafia do errors i nie przerywa reszty.
    """
    done = pyqtSignal(list, list)

    def __init__(self, url: str, layer_names: list, geom: QgsGeometry):
        super().__init__("Wyszukiwanie w skorowidzu…", QgsTask.Flag.CanCancel)
        from ..wfs_index_client import WFSIndexClient
        self.client = WFSIndexClient(url)
        self.layer_names = list(layer_names)
        self.filter_xml = self.client.build_spatial_filter(geom.asWkt())
        self.features = []
        self.errors = []

    def run(self) -> bool:
        n = len(self.layer_names)
        for i, name in enumerate(self.layer_names):
            if self.isCanceled():
                return False
            try:
                self.features.extend(gml.read_typed(self.client.download(self.filter_xml, name)))
            except Exception as exc:
                log.warning(f"[Skorowidz] {name}: {exc}")
                self.errors.append((name, str(exc)))
            self.setProgress(100.0 * (i + 1) / n)
        return True

    def finished(self, ok: bool) -> None:
        self.done.emit(self.features if ok else [], self.errors)
