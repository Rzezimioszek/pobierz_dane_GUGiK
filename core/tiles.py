# -*- coding: utf-8 -*-
"""
Pobieranie zakresu kaflami zamiast stronicowania.

Stronicowanie (startIndex) bez stałego porządku po stronie serwera potrafi
zgubić część obiektów: kolejne strony nie są rozłączne. Dlatego zakres, który
nie mieści się na jednej stronie, dzielimy na ćwiartki – aż każda się zmieści.
Wspólne dla usług zbiorczych (filtr FES) i powiatowych (parametr BBOX).
"""
from typing import Callable, Optional

from qgis.core import QgsGeometry, QgsRectangle

from . import gml, log

PAGE_SIZE = 1000
MAX_TILE_DEPTH = 10      # 4^10 kafli – zakres podzielony do ok. 1/1000 boku
MIN_TILE = 20.0          # m – mniejszych kafli nie dzielimy (gęsto zabudowany kwartał)
MAX_TILES = 4000         # bezpiecznik: więcej kafli = coś nie tak z usługą – dalej zwykłe stronicowanie


def quadrants(r: QgsRectangle) -> list:
    cx, cy = r.center().x(), r.center().y()
    return [QgsRectangle(r.xMinimum(), r.yMinimum(), cx, cy), QgsRectangle(cx, r.yMinimum(), r.xMaximum(), cy),
            QgsRectangle(r.xMinimum(), cy, cx, r.yMaximum()), QgsRectangle(cx, cy, r.xMaximum(), r.yMaximum())]


class TileFetcher:
    """
    request(rect, depth, start, count) -> tekst odpowiedzi WFS (GML).
        rect  – kafel (EPSG:2180) albo None, gdy zakres nie ma prostokąta
        depth – 0 dla całego zakresu (wolno użyć dokładnego filtra), > 0 dla kafli
    canceled()      -> True przerywa pobieranie (`self.stopped`)
    on_total(n)     – liczba obiektów całego zakresu z pierwszej odpowiedzi
                      (może zgłosić wyjątek, np. „za dużo”)
    on_progress(n)  – łączna liczba pobranych obiektów
    sink(features)  – nowe obiekty od razu po każdej stronie (zapis w trakcie); wtedy fetch()
                      ich nie zbiera i zwraca pustą listę
    hits(rect, depth) -> liczba obiektów kafla (resultType=hits) albo -1; pytana tylko, gdy
                      zwykła odpowiedź liczby nie podaje (MapServer: numberMatched="unknown")

    Skąd wiadomo, że kafel trzeba podzielić:
      1. numberMatched w odpowiedzi (GeoServer, PRG),
      2. resultType=hits – gdy usługa go obsługuje (sprawdzane na pierwszym kaflu;
         liczba mniejsza niż to, co przyszło na stronie = usługa niewiarygodna, dalej bez niej),
      3. bez liczby: pełna strona znaczy „może być więcej”.
    Strona z mniejszą liczbą obiektów niż podana liczba (limit serwera) też dzieli kafel.
    """

    def __init__(self, request: Callable, canceled: Callable, on_total: Optional[Callable] = None,
                 on_progress: Optional[Callable] = None, label: str = "", page_size: int = PAGE_SIZE,
                 hits: Optional[Callable] = None, sink: Optional[Callable] = None):
        self.request = request
        self.canceled = canceled
        self.on_total = on_total
        self.on_progress = on_progress
        self.label = label
        self.page_size = page_size
        self.stopped = False
        self.hits = hits
        self.sink = sink
        self.got = 0
        self.requests = 0
        self._seen = set()
        self._counts = None     # None – nie wiadomo jeszcze, czy serwer podaje numberMatched
        self._hits_ok = None    # None – nie wiadomo jeszcze, czy działa resultType=hits
        self.hits_used = 0      # ile kafli policzono przez resultType=hits (do dziennika i testów)
        self.tiles = 0          # kafle po podziale
        self.no_split = False   # usługa ignoruje BBOX albo kafli za dużo – dalej bez dzielenia

    def fetch(self, rect: Optional[QgsRectangle], area: Optional[QgsGeometry] = None) -> list:
        """Wszystkie obiekty zakresu (bez powtórzeń); `area` pozwala pominąć kafle poza zakresem."""
        self._area = area
        return self._fetch(rect, 0)

    # ------------------------------------------------------------------

    def _page(self, rect, depth, start, count) -> str:
        self.requests += 1
        return self.request(rect, depth, start, count)

    def _keep(self, page: list) -> list:
        """Obiekty, których jeszcze nie było (ten sam obiekt bywa w dwóch kaflach)."""
        out = []
        for f in page:
            key = f.get("id") or hash((str(f["geom"]), tuple(sorted((k, str(v)) for k, v in f["attrs"].items()))))
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(f)
        self.got += len(out)
        if self.sink is not None and out:
            self.sink(out)
        if self.on_progress:
            self.on_progress(self.got)
        return [] if self.sink is not None else out

    def _total(self, matched: int) -> None:
        if self.on_total:
            self.on_total(matched)

    def _count_hits(self, rect, depth: int) -> int:
        """Liczba obiektów kafla z resultType=hits; -1, gdy usługa jej nie podaje (wtedy już nie pytamy)."""
        if self.hits is None or self._hits_ok is False:
            return -1
        try:
            n = self.hits(rect, depth)
        except Exception as exc:     # brak obsługi hits nie przerywa pobierania
            log.info(f"[Pobieranie] {self.label}: resultType=hits nie działa ({exc}) – "
                     f"dzielę kafle po pełnych stronach")
            n = -1
        if n is None or n < 0:
            if self._hits_ok is None:
                log.info(f"[Pobieranie] {self.label}: usługa nie podaje liczby obiektów – "
                         f"dzielę kafle po pełnych stronach")
            self._hits_ok = False
            return -1
        if self._hits_ok is None:
            log.info(f"[Pobieranie] {self.label}: liczba obiektów z resultType=hits ({n}) – "
                     f"kafle dzielone według niej")
        self._hits_ok = True
        self.hits_used += 1
        return n

    def _hits_wrong(self, hits: int, page: list) -> bool:
        """resultType=hits podał mniej, niż przyszło na stronie – liczbie nie wolno ufać."""
        if hits >= 0 and len(page) > hits:
            log.warning(f"[Pobieranie] {self.label}: resultType=hits podał {hits}, a strona ma "
                        f"{len(page)} obiektów – dalej bez hits")
            self._hits_ok = False
            return True
        return False

    def _stop_splitting(self, why: str) -> None:
        if not self.no_split:
            log.warning(f"[Pobieranie] {self.label}: {why} – dalej bez dzielenia na kafle "
                        f"(stronicowanie; przy niestabilnej usłudze mogą być braki)")
        self.no_split = True

    def _ignores_bbox(self, rect, page: list) -> bool:
        """Większość obiektów strony leży poza kaflem – usługa nie filtruje po BBOX."""
        sample = page[:20]
        if not sample:
            return False
        area = QgsRectangle(rect)
        area.grow(1.0)
        outside = 0
        for f in sample:
            g = f["geom"] if isinstance(f["geom"], QgsGeometry) else QgsGeometry.fromWkt(f["geom"])
            if g.isNull() or not g.boundingBox().intersects(area):
                outside += 1
        return outside * 2 > len(sample)

    def _fetch(self, rect, depth: int) -> list:
        if self.canceled():
            self.stopped = True
            return []
        can_split = rect is not None and depth < MAX_TILE_DEPTH and not self.no_split \
            and rect.width() > MIN_TILE and rect.height() > MIN_TILE
        known = -1                     # liczba obiektów kafla, jeśli usługa ją zna
        probe = []
        if can_split and self._counts is not False:
            # liczba obiektów z małego zapytania (1 obiekt) – bez pobierania pełnej strony
            text = self._page(rect, depth, 0, 1)
            known = gml.number_matched(text)
            probe = gml.parse_features(text) if depth == 0 else []
            self._counts = known >= 0
        hits = -1
        if can_split and known < 0:
            hits = known = self._count_hits(rect, depth)
        if depth == 0 and can_split:
            self._total(known)
        if can_split and known > self.page_size and depth == 0:
            # raz, przed pierwszym podziałem: czy usługa w ogóle filtruje po prostokącie
            sample = probe or gml.parse_features(self._page(rect, depth, 0, 20))
            if self._ignores_bbox(rect, sample):
                self._stop_splitting("usługa oddaje obiekty spoza zakresu (ignoruje BBOX)")
                can_split = False
        if can_split and known > self.page_size:
            return self._split(rect, depth)
        text = self._page(rect, depth, 0, self.page_size)
        page = gml.parse_features(text)
        matched = gml.number_matched(text)
        if depth == 0 and not can_split:
            self._total(matched)     # bez prostokąta (np. lista ID) – liczba z samej strony
        if matched < 0 and not self._hits_wrong(hits, page):
            matched = known
        if can_split and (matched > len(page) or len(page) >= self.page_size):
            if self._ignores_bbox(rect, page):
                self._stop_splitting("usługa oddaje obiekty spoza kafla (ignoruje BBOX)")
            else:
                # więcej obiektów niż na stronie (limit serwera) albo pełna strona – dzielimy
                return self._split(rect, depth)
        features = self._keep(page)
        if (matched >= 0 and len(page) < matched) or (matched < 0 and len(page) >= self.page_size):
            features += self._page_rest(rect, depth, len(page), matched)
        return features

    def _split(self, rect, depth: int) -> list:
        log.info(f"[Pobieranie] {self.label}: kafel {rect.width():.0f}×{rect.height():.0f} m "
                 f"ma więcej obiektów niż strona – dzielę na 4")
        out = []
        for quad in quadrants(rect):
            if self._area is not None and not self._area.intersects(QgsGeometry.fromRect(quad)):
                continue        # ćwiartka poza zakresem (np. wielokąt w kształcie litery L)
            self.tiles += 1
            if self.tiles > MAX_TILES:
                self._stop_splitting(f"ponad {MAX_TILES} kafli")
            out += self._fetch(quad, depth + 1)
            if self.stopped:
                break
        return out

    def _page_rest(self, rect, depth: int, start: int, matched: int) -> list:
        """Zwykłe stronicowanie – gdy kafla nie da się już dzielić albo zakres nie jest znany."""
        features = []
        while True:
            if self.canceled():
                self.stopped = True
                break
            text = self._page(rect, depth, start, self.page_size)
            page = gml.parse_features(text)
            matched = gml.number_matched(text) if matched < 0 else matched
            before = self.got
            features.extend(self._keep(page))
            got = start + len(page)
            if not page or self.got == before:
                # pusta strona albo same powtórki (serwer ignoruje startIndex) – koniec
                break
            if matched >= 0:
                # serwer może oddawać mniej niż page_size na stronę – liczy się numberMatched
                if got >= matched:
                    break
                start = got
                continue
            if len(page) < self.page_size:
                break
            start += self.page_size
        return features
