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
    hits()          -> liczba obiektów całego zakresu (resultType=hits) – pytana tylko, gdy
                      zwykła odpowiedź jej nie podaje (MapServer: numberMatched="unknown")

    Serwer bez numberMatched (usługi zbiorcze EGiB i RCN): pełna strona znaczy
    „może być więcej” – kafel dzielimy tak samo, jak przy znanej liczbie.
    """

    def __init__(self, request: Callable, canceled: Callable, on_total: Optional[Callable] = None,
                 on_progress: Optional[Callable] = None, label: str = "", page_size: int = PAGE_SIZE,
                 hits: Optional[Callable] = None):
        self.request = request
        self.canceled = canceled
        self.on_total = on_total
        self.on_progress = on_progress
        self.label = label
        self.page_size = page_size
        self.stopped = False
        self.hits = hits
        self.got = 0
        self.requests = 0
        self._seen = set()
        self._counts = None     # None – nie wiadomo jeszcze, czy serwer podaje numberMatched

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
        if self.on_progress:
            self.on_progress(self.got)
        return out

    def _total(self, matched: int, ask: bool = True) -> None:
        if matched < 0 and ask and self.hits is not None:
            try:
                matched = self.hits()
            except Exception as exc:     # liczba tylko do postępu i ostrzeżenia – nie przerywa
                log.info(f"[Pobieranie] {self.label}: liczba obiektów niedostępna ({exc})")
                matched = -1
        if self.on_total:
            self.on_total(matched)

    def _fetch(self, rect, depth: int) -> list:
        if self.canceled():
            self.stopped = True
            return []
        can_split = rect is not None and depth < MAX_TILE_DEPTH \
            and rect.width() > MIN_TILE and rect.height() > MIN_TILE
        totaled = False
        if can_split and self._counts is not False:
            # liczba obiektów z małego zapytania (1 obiekt) – bez pobierania pełnej strony
            matched = gml.number_matched(self._page(rect, depth, 0, 1))
            self._counts = matched >= 0
            if depth == 0:
                self._total(matched)
                totaled = True
            if matched > self.page_size:
                return self._split(rect, depth)
        text = self._page(rect, depth, 0, self.page_size)
        page = gml.parse_features(text)
        matched = gml.number_matched(text)
        if depth == 0 and not totaled:
            # bez prostokąta (np. lista ID) to i tak jedna strona – bez dodatkowego zapytania o liczbę
            self._total(matched, ask=can_split)
        if can_split and (matched > len(page) or (matched < 0 and len(page) >= self.page_size)):
            # więcej obiektów niż na stronie (albo pełna strona bez liczby) – dzielimy
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
            kept = self._keep(page)
            features.extend(kept)
            got = start + len(page)
            if not page or not kept:
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
