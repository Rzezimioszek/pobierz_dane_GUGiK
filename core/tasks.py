# -*- coding: utf-8 -*-
"""
Zadania w tle. Cała praca sieciowa wtyczki odbywa się tutaj, nigdy w wątku interfejsu.

    WfsDownloadTask  – jedno źródło, jeden lub wiele filtrów, stronicowanie,
                       opcjonalne przycięcie wyników do geometrii
    HitsTask         – liczba obiektów pasujących do filtra
    FileDownloadTask – plik (paczki opendata, rastry ze skorowidzów) na dysk
"""
import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import QgsGeometry, QgsRectangle, QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from . import filters, gml, layers, log, net
from .catalog import Source
from .i18n import tr
from .tiles import TileFetcher
from .wfs import WfsClient


def _close(session) -> None:
    """Zamyka sesję HTTP zadania (połączenia nie czekają na sprzątanie przez Pythona)."""
    with contextlib.suppress(Exception):
        session.close()


@dataclass
class Query:
    """
    Jedno zapytanie: filtr FES i opcjonalna geometria do przycięcia wyników.

    `rect` (EPSG:2180) – zasięg zapytania; pozwala podzielić je na kafle, gdy
    serwer zgłosi więcej obiektów niż jedna strona. `extra_filter` – część
    filtra niezależna od położenia (np. kod TERYT), dokładana do każdego kafla.
    """
    filter_xml: str
    clip_geom: Optional[QgsGeometry] = None
    label: str = ""
    rect: Optional[QgsRectangle] = None
    extra_filter: str = ""


@dataclass
class DownloadResult:
    features: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # [(etykieta zapytania, komunikat)]
    failed: list = field(default_factory=list)   # zapytania (Query) do ponowienia
    canceled: bool = False
    too_large: int = 0      # > 0: wstrzymano – tyle obiektów zgłosił serwer (ponad size_limit)
    too_large_at: int = 0   # numer zapytania, przy którym wstrzymano (wcześniejsze już zapisane)
    prepared: object = None  # layers.Prepared – obiekty przygotowane w tle do zapisu
    count: int = 0          # wszystkie pobrane obiekty (także te oddane porcjami w trakcie)


@dataclass
class Batch:
    """Porcja obiektów do dopisania w trakcie pobierania (sygnał batch_ready)."""
    features: list
    prepared: object = None


class TooLarge(Exception):
    """Serwer zgłosił więcej obiektów niż limit zadania."""

    def __init__(self, matched: int):
        super().__init__(f"{matched}" + tr(" obiektów"))
        self.matched = matched


class WfsDownloadTask(QgsTask):
    """
    Pobiera obiekty jednego źródła dla listy zapytań, strona po stronie.
    Błąd jednego zapytania nie przerywa pozostałych; trafia do `result.errors`.
    Sygnał `done(DownloadResult)` przychodzi w głównym wątku;
    `count_changed(int)` – liczba pobranych obiektów w trakcie.

    `size_limit` – gdy serwer zgłosi w zapytaniu więcej obiektów (numberMatched),
    zadanie staje od razu po pierwszej stronie z `result.too_large`; pytanie
    „pobrać mimo to?” zadaje interfejs i uruchamia zadanie ponownie bez limitu.

    `stream=True` – obiekty idą porcjami w trakcie (`batch_ready(Batch)`): pierwsza od
    razu, kolejne co FLUSH_SECONDS; `result.features` zostaje wtedy puste. Bez tego
    (algorytmy Processing) – wszystko w `result` na końcu.
    """
    done = pyqtSignal(object)
    count_changed = pyqtSignal(int)
    batch_ready = pyqtSignal(object)

    FLUSH_SECONDS = 2.0

    def __init__(self, source: Source, queries: list, attributes: Optional[list] = None,
                 expected: int = 0, description: str = "", size_limit: int = 0, stream: bool = False):
        super().__init__(description or tr("Pobieranie: ") + source.label, QgsTask.Flag.CanCancel)
        self.source = source
        self.queries = [q if isinstance(q, Query) else Query(q) for q in queries]
        self.attributes = attributes if source.attribute_selection else None
        self.expected = expected
        self.size_limit = size_limit
        self.result = DownloadResult()
        self.client = WfsClient(source.url, source.typename, source.geom_field)
        self.stream = stream
        self._buffer = []
        self._flushed_at = None     # None – jeszcze żadnej porcji (pierwsza idzie od razu)
        self._schema = None         # schemat z pierwszej porcji – ten sam dla kolejnych
        self._prev = 0              # obiekty z zakończonych zapytań (do licznika)

    # ------------------------------------------------------------------ porcje w trakcie

    def _prepare(self, features: list):
        if self.source.split_lines_polygons:
            return None
        try:
            prepared = layers.prepare(features, self.source.geom_type, self.source.numeric_fields,
                                      self.source.unique_field, self.source.label, fields=self._schema)
        except Exception as exc:   # zapis i tak zbuduje obiekty sam
            log.warning(f"[Pobieranie] {self.source.label}: przygotowanie w tle nieudane: {exc}")
            return None
        if self._schema is None:
            self._schema = prepared.fields
        return prepared

    def _flush(self, force: bool = False) -> None:
        if not self._buffer:
            return
        now = time.monotonic()
        if not force and self._flushed_at is not None and now - self._flushed_at < self.FLUSH_SECONDS:
            return
        batch, self._buffer = self._buffer, []
        self._flushed_at = now
        self.batch_ready.emit(Batch(batch, self._prepare(batch)))

    def run(self) -> bool:
        n = len(self.queries)
        for qi, query in enumerate(self.queries):
            if self.isCanceled():
                self.result.canceled = True
                if self.stream:
                    self._flush(force=True)   # pobrane do tej chwili obiekty zostają na mapie
                return False
            try:
                got = self._run_query(query, qi, n)
                self.result.features.extend(got)
                self.result.count += len(got)
            except TooLarge as big:
                self._flush(force=True)       # to, co już pobrano z wcześniejszych zapytań
                self.result.too_large = big.matched
                self.result.too_large_at = qi
                self.result.features = []
                return True
            except Exception as exc:  # sieć, serwer, XML – zapisz i jedź dalej
                label = query.label or f"zapytanie {qi + 1}/{n}"
                log.warning(f"[Pobieranie] {self.source.label}, {label}: {exc}")
                self.result.errors.append((label, net.friendly(exc)))
                self.result.failed.append(query)
        if self.stream:
            self._flush(force=True)       # także po anulowaniu – pobrane już obiekty zostają
        elif self.result.features and not self.source.split_lines_polygons and not self.isCanceled():
            # budowa QgsFeature tutaj, w tle – w głównym wątku zostaje samo dodanie do warstwy
            try:
                self.result.prepared = layers.prepare(
                    self.result.features, self.source.geom_type, self.source.numeric_fields,
                    self.source.unique_field, self.source.label)
            except Exception as exc:   # zapis i tak zbuduje obiekty sam
                log.warning(f"[Pobieranie] {self.source.label}: przygotowanie w tle nieudane: {exc}")
        self.setProgress(100)
        return not self.result.canceled

    # Duże zakresy idą kaflami (core/tiles.py), nie stronicowaniem.

    def _tile_filter(self, query: Query, rect: QgsRectangle) -> str:
        bbox = filters.bbox(self.source.geom_field, rect.xMinimum(), rect.yMinimum(),
                            rect.xMaximum(), rect.yMaximum())
        return filters.combine([bbox, query.extra_filter]) if query.extra_filter else bbox

    def _run_query(self, query: Query, qi: int, n: int) -> list:
        total = {"n": -1}

        def on_total(matched):
            if self.size_limit and matched > self.size_limit:
                raise TooLarge(matched)
            total["n"] = matched

        def fetch(exact: bool) -> list:
            def flt(rect, depth):
                return query.filter_xml if (depth == 0 and exact) else self._tile_filter(query, rect)

            def request(rect, depth, start, count):
                return self.client.page(flt(rect, depth), start, count, self.attributes)

            def hits(rect, depth):
                return self.client.hits(flt(rect, depth))

            def sink(features):
                # przycięcie do zakresu od razu, żeby na mapę trafiało tylko to, co zostanie
                kept = layers.clip(features, query.clip_geom)
                self._buffer.extend(kept)
                self.result.count += len(kept)
                self._flush()
            fetcher = TileFetcher(request, self.isCanceled, on_total,
                                  lambda got: self._report(qi, n, got, total["n"]), self.source.label,
                                  hits=hits, sink=sink if self.stream else None)
            out = fetcher.fetch(query.rect, query.clip_geom)
            if fetcher.stopped:
                self.result.canceled = True
            self._prev += fetcher.got
            return out

        try:
            features = fetch(exact=True)
        except RuntimeError as exc:
            # Usługa bez obsługi fes:Intersects z wielokątem → prostokąt + przycięcie u nas
            if "Intersects" not in query.filter_xml or query.rect is None or query.clip_geom is None:
                raise
            log.warning(f"[Pobieranie] {self.source.label}: filtr wielokąta odrzucony ({exc}) – "
                        f"pobieram prostokąt otaczający i przycinam lokalnie")
            features = fetch(exact=False)
        return layers.clip(features, query.clip_geom)

    def _report(self, qi: int, n: int, done_in_query: int, matched: int = -1) -> None:
        self.count_changed.emit(self._prev + done_in_query)
        total = matched if matched > 0 else self.expected
        # ułamek bieżącego zapytania (z liczby zgłoszonej przez serwer) + zapytania już zakończone
        part = min(1.0, done_in_query / total) if total > 0 else (1.0 if n > 1 else 0.0)
        self.setProgress(min(99.0, 100.0 * (qi + part) / n))

    def finished(self, ok: bool) -> None:
        _close(self.client.session)
        if self.isCanceled():
            self.result.canceled = True
        self.done.emit(self.result)


class HitsTask(QgsTask):
    """Liczba obiektów dla filtra; `done(int)`: -1 = nieznana, -2 = błąd (`error`)."""
    done = pyqtSignal(int)

    def __init__(self, source: Source, filter_xml: str):
        super().__init__(tr("Sprawdzanie liczby obiektów…"), QgsTask.Flag.CanCancel)
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
        _close(self.client.session)
        self.done.emit(self.hits if ok else -2)


class FileDownloadTask(QgsTask):
    """Pobiera plik na dysk; `done(path)`, pusty tekst przy błędzie lub anulowaniu."""
    done = pyqtSignal(str)

    CHUNK = 5 * 1024 * 1024

    def __init__(self, url: str, path: str, description: str = ""):
        super().__init__(description or tr("Pobieranie pliku ") + path, QgsTask.Flag.CanCancel)
        self.url = url
        self.path = path
        self.error = None
        self.session = net.make_session()

    def run(self) -> bool:
        # Zapis do „.part” i zmiana nazwy na końcu: przerwany plik nie udaje gotowego
        part = self.path + ".part"
        try:
            with self.session.get(self.url, stream=True, timeout=900) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                got = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=self.CHUNK):
                        if self.isCanceled():
                            break
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                            if total:
                                self.setProgress(100.0 * got / total)
            if self.isCanceled():
                return False
            os.replace(part, self.path)
            return True
        except Exception as exc:
            self.error = exc
            return False
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass

    def finished(self, ok: bool) -> None:
        _close(self.session)
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
        super().__init__(tr("Granica PRG: ") + teryt, QgsTask.Flag.CanCancel)
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
        _close(self.client.session)
        self.done.emit(self.geometry if ok else None)


class SkorowidzSearchTask(QgsTask):
    """
    Szuka arkuszy w skorowidzu dla listy warstw (np. roczników) i geometrii.
    `done(features, errors)`: features jak gml.read_typed, errors [(warstwa, komunikat)].
    Rocznik, którego nie ma w usłudze, trafia do errors i nie przerywa reszty.
    """
    done = pyqtSignal(list, list)

    def __init__(self, url: str, layer_names: list, geom: QgsGeometry):
        super().__init__(tr("Wyszukiwanie w skorowidzu…"), QgsTask.Flag.CanCancel)
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
                found = gml.read_typed(self.client.download(self.filter_xml, name))
                for f in found:
                    f["layer"] = name      # rocznik z nazwy warstwy, gdy brak go w atrybutach
                self.features.extend(found)
            except Exception as exc:
                log.warning(f"[Skorowidz] {name}: {exc}")
                self.errors.append((name, net.friendly(exc)))
            self.setProgress(100.0 * (i + 1) / n)
        return True

    def finished(self, ok: bool) -> None:
        _close(self.client.session)
        self.done.emit(self.features if ok else [], self.errors)


class RegionsTask(QgsTask):
    """
    Aktualna lista obrębów z ULDK dla jednostek ewidencyjnych gminy.
    `done(key, obreby: list[(kod, nazwa)], errors: list[str])` w głównym wątku.
    """
    done = pyqtSignal(str, list, list)

    def __init__(self, key: str, jednostki: list):
        super().__init__(tr("Obręby: ") + key, QgsTask.Flag.CanCancel)
        self.key = key
        self.jednostki = jednostki
        self.session = net.make_session()
        self.obreby, self.errors = [], []

    def run(self) -> bool:
        from . import uldk
        for j in self.jednostki:
            if self.isCanceled():
                return False
            try:
                self.obreby += [(code, name) for code, name, _ in uldk.regions(self.session, j)]
            except Exception as exc:
                log.warning(f"[ULDK] {j}: {exc}")
                self.errors.append(f"{j}: {net.friendly(exc)}")
        return True

    def finished(self, result: bool) -> None:
        _close(self.session)
        if result:
            self.done.emit(self.key, sorted(set(self.obreby)), self.errors)
