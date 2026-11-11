# -*- coding: utf-8 -*-
"""
Pobieranie z powiatowych usług WFS zgłoszonych do EZiUDP.

Dla każdego powiatu × wybranej warstwy:
  1. adres usługi z EZiUDP i nazwa warstwy z GetCapabilities,
  2. pobranie własnym klientem: GetFeature WFS 2.0 z parametrem BBOX, duże
     zakresy kaflami (core/tiles.py) – bez gubienia obiektów przy stronicowaniu,
  3. gdy usługa tego nie przyjmie (inna wersja WFS, inny układ, błąd) albo
     zakres wyjdzie pusty – dostawca WFS QGIS, jak we wcześniejszych wersjach.

Obiekty są czytane w wątku zadania, a warstwy (tymczasowe albo w GPKG, przez
core.layers) powstają w finished(), w głównym wątku. Pola zostają takie, jak
podaje powiat. Zakres, miejsce zapisu i prefiks przychodzą z kroków 2 i 3 panelu.
"""
import contextlib
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import (
    QgsTask, QgsMessageLog, Qgis,
    QgsVectorLayer, QgsField, QgsFields,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsGeometry, QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import pyqtSignal

from .eziudp_client import EziudpClient
from .bdsog_harmonizer import get_harmonizer
from .core import gml, layers, net
from .core.i18n import mark, server_error, tr
from .core.tiles import TileFetcher
from .qt_compat import FIELD_STRING

LOG_TAG = "PD_GUGiK"

# Wersje WFS próbowane po kolei
_WFS_VERSIONS = ["2.0.0", "1.1.0"]

# Domyślny CRS warstw powiatowych
_TARGET_CRS = "EPSG:2180"

# Pola meta dodawane do każdej warstwy memory
_META_FIELDS = ["teryt_powiatu", "zrodlo_wfs"]


# ---------------------------------------------------------------------------
# Warstwy dostępne w oknie EZiUDP
# ---------------------------------------------------------------------------

AVAILABLE_LAYERS = [
    {
        "key":           "bdsog_poziom",
        "label":         mark("Osnowa pozioma (BDSOG)"),
        "zbior":         "bdsog",
        "layer_pattern": "poziom",
    },
    {
        "key":           "bdsog_wysok",
        "label":         mark("Osnowa wysokościowa (BDSOG)"),
        "zbior":         "bdsog",
        "layer_pattern": "wysok",
    },
    {
        "key":           "egib_dzi",
        "label":         mark("Działki ewidencyjne (EGIB)"),
        "zbior":         "egib",
        "layer_pattern": "dzialki",
    },
    {
        "key":           "egib_bud",
        "label":         mark("Budynki ewidencyjne (EGIB)"),
        "zbior":         "egib",
        "layer_pattern": "budynki",
    },
    {
        "key":           "egib_pkt",
        "label":         mark("Punkty graniczne (EGIB)"),
        "zbior":         "egib",
        "layer_pattern": "punkty_graniczne",
    },
    # Użytki i kontury udostępnia tylko część usług powiatowych (np. ePODGiK)
    {
        "key":           "egib_uzy",
        "label":         mark("Użytki gruntowe (EGIB)"),
        "zbior":         "egib",
        "layer_pattern": "uzytki",
        "layer_name":    "Uzytki",
        "style":         "uzytki",
        "optional":      True,
    },
    {
        "key":           "egib_kon",
        "label":         mark("Kontury klasyfikacyjne (EGIB)"),
        "zbior":         "egib",
        "layer_pattern": "kontury",
        "layer_name":    "Kontury_klasyfikacyjne",
        "style":         "kontury_klasyfikacyjne",
        "optional":      True,
    },
    # RCN w usługach powiatowych ma schemat jak RCN zbiorczy (działki, budynki, lokale);
    # nazwy warstw i style jak dla RCN zbiorczego, z sufiksem TERYT
    {
        "key":           "rcn_dzi",
        "label":         mark("Działki (RCN)"),
        "zbior":         "rcn",
        "layer_pattern": "dzialki",
        "layer_name":    "Dzialki_RCN",
        "style":         "dzialki_rcn",
    },
    {
        "key":           "rcn_bud",
        "label":         mark("Budynki (RCN)"),
        "zbior":         "rcn",
        "layer_pattern": "budynki",
        "layer_name":    "Budynki_RCN",
        "style":         "budynki_rcn",
    },
    {
        "key":           "rcn_lok",
        "label":         mark("Lokale (RCN)"),
        "zbior":         "rcn",
        "layer_pattern": "lokale",
        "layer_name":    "Lokale_RCN",
        "style":         "lokale_rcn",
    },
    {
        "key":           "bdot500",
        "label":         mark("Budynki i obiekty tow. (BDOT500)"),
        "zbior":         "bdot500",
        "layer_pattern": "budynki",
    },
]

AVAILABLE_LAYERS_BY_KEY = {e["key"]: e for e in AVAILABLE_LAYERS}


# ---------------------------------------------------------------------------
# Raporty
# ---------------------------------------------------------------------------

@dataclass
class WfsLayerReport:
    teryt: str
    key: str
    success: bool = False
    feature_count: int = 0
    wfs_url: str = ""
    wfs_layer: str = ""
    wfs_version: str = ""
    error: str = ""


@dataclass
class EziudpWfsReport:
    total_teryts: int = 0
    successful: int = 0
    failed: int = 0
    total_features: int = 0
    layers: list = field(default_factory=list)   # lista WfsLayerReport
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # warstwy opcjonalne, których usługa nie ma
    failed_pairs: list = field(default_factory=list)   # [(teryt, klucz warstwy)] do ponowienia
    written: list = field(default_factory=list)        # warstwy QGIS utworzone / uzupełnione
    canceled: bool = False

    def format_summary(self) -> str:
        lines = [
            f"Powiaty: {self.successful}/{self.total_teryts} OK, {self.failed} błędów",
            f"Pobrane obiekty: {self.total_features}",
        ]
        if self.errors:
            lines.append("Błędy:")
            for e in self.errors[:10]:
                lines.append(f"  • {e}")
            if len(self.errors) > 10:
                lines.append(f"  … i {len(self.errors) - 10} więcej")
        if self.skipped:
            lines.append("Pominięte (usługa nie ma takiej warstwy):")
            lines.extend(f"  • {s}" for s in self.skipped[:10])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pomocnicze funkcje URI WFS
# ---------------------------------------------------------------------------

def _wfs_uri(
    base_url: str,
    layer_name: str,
    version: str,
    bbox_rect: Optional[QgsRectangle] = None,
    crs: str = _TARGET_CRS,
) -> str:
    """
    Buduje URI dla QgsVectorLayer z providerem WFS.

    Parametry QGIS WFS provider:
      url        – adres bazowy (bez ?service=WFS itp.)
      typename   – nazwa warstwy
      version    – "2.0.0" lub "1.1.0"
      srsname    – układ współrzędnych
      bbox       – "xmin,ymin,xmax,ymax,EPSG:XXXX" (opcjonalny)
      restrictToRequestBBOX – "1" gdy BBOX podany → provider sam filtruje

    Dokumentacja parametrów:
    https://docs.qgis.org/latest/en/docs/user_manual/managing_data_source/opening_data.html#wfs-and-wcs-client
    """
    # Wyczyść URL z parametrów OGC – provider sam je buduje
    base = base_url.split("?")[0]

    parts = [
        f"url={base}",
        f"typename={layer_name}",
        f"version={version}",
        f"srsname={crs}",
        "pagingEnabled=true",
        "preferCoordinatesForWfsT11=false",
    ]

    if bbox_rect is not None:
        xmin = bbox_rect.xMinimum()
        ymin = bbox_rect.yMinimum()
        xmax = bbox_rect.xMaximum()
        ymax = bbox_rect.yMaximum()
        parts.append(f"bbox={xmin},{ymin},{xmax},{ymax},{crs}")
        parts.append("restrictToRequestBBOX=1")

    return " ".join(parts)


def _try_wfs_layer(
    base_url: str,
    layer_name: str,
    bbox_rect: Optional[QgsRectangle],
    crs: str = _TARGET_CRS,
) -> tuple:
    """
    Próbuje zbudować QgsVectorLayer kolejno dla WFS 2.0.0 i 1.1.0.

    Zwraca (QgsVectorLayer, version_str) dla pierwszej działającej wersji
    lub (None, "") gdy obie zawiodą.
    """
    for version in _WFS_VERSIONS:
        uri = _wfs_uri(base_url, layer_name, version, bbox_rect, crs)
        vl = QgsVectorLayer(uri, f"_tmp_{layer_name}", "WFS")
        if vl.isValid():
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] WFS {version} OK: {layer_name} @ {base_url}",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            return vl, version
        else:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] WFS {version} nie działa dla {layer_name}: "
                f"{vl.error().message() if hasattr(vl, 'error') else 'nieznany błąd'}",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
    return None, ""


# ---------------------------------------------------------------------------
# Snapshot: kopiuj featuresy z WFS provider do warstwy memory
# ---------------------------------------------------------------------------

def _read_snapshot(
    wfs_layer: QgsVectorLayer,
    clip_geom: Optional[QgsGeometry],
    teryt: str,
    wfs_url: str,
    task_check_cancelled,
) -> tuple:
    """
    Czyta obiekty z warstwy WFS (w wątku zadania) bez dotykania projektu.
    Zwraca (pola QgsFields z polami meta, typ geometrii, [{'geom', 'attrs'}]).
    Warstwy powstają później, w głównym wątku (EziudpWfsTask.finished).
    """
    fields = QgsFields(wfs_layer.fields())
    for meta in _META_FIELDS:
        if fields.indexOf(meta) < 0:
            fields.append(QgsField(meta, FIELD_STRING))
    names = [f.name() for f in wfs_layer.fields()]

    src_crs = wfs_layer.crs()
    tgt_crs = QgsCoordinateReferenceSystem(_TARGET_CRS)
    transform = None
    if src_crs.isValid() and src_crs != tgt_crs:
        transform = QgsCoordinateTransform(src_crs, tgt_crs, QgsCoordinateTransformContext())

    features = []
    for src_feat in wfs_layer.getFeatures():
        if task_check_cancelled():
            break
        geom = src_feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if transform:
            geom.transform(transform)
        if clip_geom is not None and not geom.intersects(clip_geom):
            continue
        attrs = {n: src_feat.attribute(n) for n in names}
        attrs["teryt_powiatu"] = teryt
        attrs["zrodlo_wfs"] = wfs_url
        features.append({"geom": QgsGeometry(geom), "attrs": attrs})

    return fields, QgsWkbTypes.displayString(wfs_layer.wkbType()), features


URN_2180 = "urn:ogc:def:crs:EPSG::2180"
# Zgrubne granice Polski w EPSG:2180 – kontrola, czy współrzędne z usługi mają sens
_POLAND = QgsRectangle(100000, 80000, 900000, 800000)


def _plausible(features: list, rect: Optional[QgsRectangle]) -> bool:
    """Czy choć jeden obiekt leży tam, gdzie pytaliśmy (zły układ / zamienione osie → False)."""
    area = QgsRectangle(rect) if rect is not None else QgsRectangle(_POLAND)
    if rect is not None:
        area.grow(max(1000.0, rect.width(), rect.height()))
    return any(f["geom"].boundingBox().intersects(area) for f in features)


def _read_direct(
    base_url: str,
    layer_name: str,
    rect: Optional[QgsRectangle],
    clip_geom: Optional[QgsGeometry],
    teryt: str,
    wfs_url: str,
    task_check_cancelled,
    on_progress=None,
) -> tuple:
    """
    Własny klient: GetFeature WFS 2.0 z BBOX, duże zakresy kaflami, własny parser GML.
    Zwraca to samo co _read_snapshot (pola None = tekstowe z atrybutów).
    Błąd usługi albo nieprawdopodobne współrzędne → wyjątek (wtedy dostawca WFS QGIS).
    """
    session = net.make_session()
    # Parametry „własne” usługi z adresu EZiUDP (np. map=...) zostają w zapytaniu
    from urllib.parse import parse_qsl, urlsplit
    own = {k: v for k, v in parse_qsl(urlsplit(wfs_url).query)
           if k.lower() not in ("service", "request", "version", "acceptversions",
                                "typename", "typenames")}

    def params_for(r) -> dict:
        params = {
            **own,
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typenames": layer_name, "srsName": URN_2180,
        }
        if r is not None:
            # URN EPSG:2180 – kolejność osi N, E
            params["bbox"] = f"{r.yMinimum()},{r.xMinimum()},{r.yMaximum()},{r.xMaximum()},{URN_2180}"
        return params

    def request(r, depth, start, count):
        text = net.get(session, base_url, {**params_for(r), "count": count, "startIndex": start},
                       timeout=180).text
        error = gml.service_exception(text)
        if error:
            raise RuntimeError(server_error(error))
        return text

    def hits(r, depth):
        text = net.get(session, base_url, {**params_for(r), "resultType": "hits"}, timeout=120).text
        error = gml.service_exception(text)
        if error:
            raise RuntimeError(server_error(error))
        return gml.number_matched(text)

    features, geom_type = [], ""
    try:
        fetcher = TileFetcher(request, task_check_cancelled, on_progress=on_progress,
                              label=f"{layer_name} ({teryt})", hits=hits)
        raw = fetcher.fetch(rect, clip_geom)
    finally:
        with contextlib.suppress(Exception):
            session.close()
    for f in raw:
        geom = f["geom"] if isinstance(f["geom"], QgsGeometry) else QgsGeometry.fromWkt(f["geom"])
        if geom is None or geom.isEmpty():
            continue
        attrs = dict(f["attrs"])
        if f.get("id"):
            attrs.setdefault("gml_id", f["id"])   # jak w dostawcy WFS QGIS – do usuwania duplikatów
        attrs["teryt_powiatu"] = teryt
        attrs["zrodlo_wfs"] = wfs_url
        features.append({"geom": geom, "attrs": attrs})
    if features and not _plausible(features, rect):
        raise RuntimeError(tr("współrzędne z usługi leżą poza pytanym zakresem "
                              "(inny układ albo kolejność osi)"))
    if clip_geom is not None:
        features = layers.clip(features, clip_geom)
    if features:
        geom_type = QgsWkbTypes.displayString(QgsWkbTypes.multiType(features[0]["geom"].wkbType()))
    return None, geom_type, features


# ---------------------------------------------------------------------------
# Główne zadanie QgsTask
# ---------------------------------------------------------------------------

class EziudpWfsTask(QgsTask):
    """
    Pobiera wybrane warstwy z usług powiatowych; osobna warstwa na powiat × typ
    danych, np. „Dzialki_0401”, „Dzialki_0402” (z prefiksem z kroku 3).

    rect / clip_geom (EPSG:2180) – zakres z kroku 2; bez nich cały powiat.
    pairs – ponowienie: tylko wskazane (teryt, klucz warstwy).
    names – nazwy warstw z poprzedniego pobrania (ponowienie dopisuje do tych samych
            warstw także przy „osobnej warstwie dla każdego pobrania”).
    Każda gotowa warstwa (powiat × typ danych) trafia na mapę od razu, nie na końcu.
    Sygnały (główny wątek): done(EziudpWfsReport), count_changed(int).
    """

    done = pyqtSignal(object)
    count_changed = pyqtSignal(int)
    item_ready = pyqtSignal(object)     # wewnętrzny: warstwa gotowa w wątku roboczym → zapis

    def __init__(
        self,
        teryt_list: list,
        layer_keys: list,
        rect: Optional[QgsRectangle] = None,
        clip_geom: Optional[QgsGeometry] = None,
        output: Optional[layers.Output] = None,
        pairs: Optional[list] = None,
        scope_text: str = "",
        powiat_rects: Optional[dict] = None,
        names: Optional[dict] = None,
    ):
        labels = [tr(AVAILABLE_LAYERS_BY_KEY[k]["label"]) for k in layer_keys
                  if k in AVAILABLE_LAYERS_BY_KEY]
        super().__init__(tr("Usługi powiatowe: ") + f"{', '.join(labels)} · " + tr("powiatów: ")
                         + str(len(teryt_list)),
                         QgsTask.Flag.CanCancel)
        self.teryt_list = [str(t).strip() for t in teryt_list if str(t).strip()]
        self.layer_keys = [k for k in layer_keys if k in AVAILABLE_LAYERS_BY_KEY]
        self.pairs = set(pairs) if pairs else {(t, k) for t in self.teryt_list for k in self.layer_keys}
        self.scope_text = scope_text
        # Miejsce zapisu ustalone w głównym wątku (krok 3 panelu albo ustawienia)
        self.output = output or layers.Output.from_settings()
        self._pending: list = []   # warstwy do utworzenia w finished() (gdy nie zapisano w trakcie)
        self.names = dict(names or {})     # nazwa bazowa → nazwa warstwy w projekcie
        # obiekt zadania żyje w głównym wątku – sygnał z run() trafia tu kolejką
        self.item_ready.connect(self._write_item)
        self._eziudp = EziudpClient()
        self._report = EziudpWfsReport(total_teryts=len(self.teryt_list))
        self._bbox_rect = QgsRectangle(rect) if rect is not None and not rect.isEmpty() else None
        self._clip_geom = clip_geom if clip_geom is not None and not clip_geom.isEmpty() else None
        # Cały powiat: prostokąt powiatu pozwala pobierać kaflami zamiast stronicować
        self._powiat_rects = powiat_rects or {}
        self._count = 0
        self._steps_done = 0

    # ------------------------------------------------------------------
    # QgsTask
    # ------------------------------------------------------------------

    def run(self) -> bool:
        for i, teryt in enumerate(self.teryt_list):
            if self.isCanceled():
                return False
            QgsMessageLog.logMessage(f"[EziudpWfsTask] [{i + 1}/{len(self.teryt_list)}] TERYT {teryt}",
                                     LOG_TAG, Qgis.MessageLevel.Info)
            if self._process_one_teryt(teryt):
                self._report.successful += 1
            else:
                self._report.failed += 1
        self.setProgress(100)
        return True

    def _write_item(self, item: dict) -> None:
        """Główny wątek: zapis jednej warstwy (powiat × typ danych) do projektu."""
        try:
            # Kolejne pobranie tego samego powiatu (np. inna gmina) dopisuje się
            # do warstwy; gml_id pomija obiekty, które już w niej są.
            first = item["features"][0]["attrs"] if item["features"] else {}
            unique = "gml_id" if first.get("gml_id") not in (None, "") else None
            base = item["name"]
            if base not in self.names:
                self.names[base] = (layers.unique_name(base, gpkg_path=self.output.gpkg_path)
                                    if self.output.separate else base)
            vl = layers.write(item["features"], self.names[base], item["geom_type"], unique,
                              style=item["style"], load_style=self.output.load_style,
                              fields=item["fields"], gpkg_path=self.output.gpkg_path,
                              info=item.get("info"), group=self.output.prefix)
            if vl is not None and vl not in self._report.written:
                self._report.written.append(vl)
        except Exception as exc:
            msg = tr("Zapis warstwy {0} nie powiódł się: {1}").format(item['name'], exc)
            QgsMessageLog.logMessage(f"[EziudpWfsTask] {msg}", LOG_TAG, Qgis.MessageLevel.Critical)
            self._report.errors.append(msg)

    def finished(self, result: bool):
        # Główny wątek; warstwy zwykle zapisane już w trakcie (item_ready)
        for item in self._pending:
            self._write_item(item)
        self._pending = []
        self._report.canceled = self.isCanceled()
        with contextlib.suppress(Exception):
            self._eziudp.session.close()
        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] {'Zakończono' if result else 'Przerwano'}: {self._report.format_summary()}",
            LOG_TAG, Qgis.MessageLevel.Success if result else Qgis.MessageLevel.Warning,
        )
        self.done.emit(self._report)

    # ------------------------------------------------------------------
    # Logika dla jednego powiatu
    # ------------------------------------------------------------------

    def _fail(self, teryt: str, keys: list, msg: str, optional: bool = False) -> None:
        (self._report.skipped if optional else self._report.errors).append(msg)
        if not optional:
            self._report.failed_pairs.extend((teryt, k) for k in keys)

    def _step(self) -> None:
        self._steps_done += 1
        self.setProgress(min(99.0, 100.0 * self._steps_done / max(1, len(self.pairs))))

    def _process_one_teryt(self, teryt: str) -> bool:
        """Przetwarza wszystkie wybrane warstwy dla jednego powiatu."""
        any_ok = False

        # Grupuj klucze po zbiorze – jedno GetCapabilities na zbiór
        zbior_to_keys: dict = {}
        for key in self.layer_keys:
            if (teryt, key) in self.pairs:
                zbior_to_keys.setdefault(AVAILABLE_LAYERS_BY_KEY[key]["zbior"], []).append(key)

        for zbior, keys in zbior_to_keys.items():
            if self.isCanceled():
                return any_ok

            eziudp_result = self._eziudp.get_wfs_urls(teryt, zbior)
            if not eziudp_result.success or not eziudp_result.records:
                err = eziudp_result.error_message or tr("powiat nie zgłosił usługi WFS do EZiUDP")
                QgsMessageLog.logMessage(f"[EziudpWfsTask] TERYT {teryt} zbior={zbior}: {err}",
                                         LOG_TAG, Qgis.MessageLevel.Warning)
                self._fail(teryt, keys, f"TERYT {teryt} [{zbior.upper()}]: {err}")
                for _ in keys:
                    self._step()
                continue

            wfs_url = eziudp_result.records[0].url_wfs
            base_url = wfs_url.split("?")[0]
            all_wfs_layers = self._eziudp.get_layer_names(wfs_url, zbior)

            for key in keys:
                if self.isCanceled():
                    return any_ok
                layer_cfg = AVAILABLE_LAYERS_BY_KEY[key]
                pattern = layer_cfg.get("layer_pattern")
                wfs_layer_name = self._pick_wfs_layer(all_wfs_layers, pattern)

                if not wfs_layer_name:
                    msg = tr("TERYT {0}: usługa powiatu nie udostępnia warstwy „{1}”").format(
                        teryt, tr(layer_cfg['label']))
                    QgsMessageLog.logMessage(
                        f"[EziudpWfsTask] {msg} (wzorzec={pattern!r}, warstwy: {all_wfs_layers})",
                        LOG_TAG, Qgis.MessageLevel.Info)
                    # brak warstwy w usłudze nie zniknie po ponowieniu – bez „Ponów”
                    (self._report.skipped if layer_cfg.get("optional") else self._report.errors).append(msg)
                    self._step()
                    continue

                report = self._fetch_one_layer(teryt, key, base_url, wfs_url, wfs_layer_name)
                self._report.layers.append(report)
                self._report.total_features += report.feature_count
                self._step()
                if report.success:
                    any_ok = True
                else:
                    self._fail(teryt, [key], f"TERYT {teryt} [{tr(layer_cfg['label'])}]: {report.error}")

        return any_ok

    def _progress(self, base: int):
        def on_progress(got: int) -> None:
            self.count_changed.emit(base + got)
        return on_progress

    def _fetch_one_layer(self, teryt: str, key: str, base_url: str, wfs_url: str,
                         wfs_layer_name: str) -> WfsLayerReport:
        """Jedna warstwa jednego powiatu: własny klient z kaflami, w razie czego dostawca WFS QGIS."""
        report = WfsLayerReport(teryt=teryt, key=key, wfs_url=wfs_url, wfs_layer=wfs_layer_name)

        # Nazwa warstwy i styl: z konfiguracji warstwy, a jeśli brak – z harmonizatora
        cfg = AVAILABLE_LAYERS_BY_KEY[key]
        harmonizer = None
        if not cfg.get("layer_name"):
            harmonizer = get_harmonizer(cfg["zbior"], wfs_layer_name)
        base_layer_name = cfg.get("layer_name") or (harmonizer.LAYER_NAME if harmonizer else key)
        style_name = (cfg.get("style") or getattr(harmonizer, "STYLE", None) or base_layer_name)
        layer_name = self.output.layer_name(f"{base_layer_name}_{teryt}")

        fields, geom_type, features, version = None, "", None, ""
        rect = self._bbox_rect or self._powiat_rects.get(teryt)
        try:
            fields, geom_type, features = _read_direct(
                base_url, wfs_layer_name, rect, self._clip_geom, teryt, wfs_url,
                self.isCanceled, self._progress(self._count))
            version = "2.0.0 (kafle)"
            if not features and rect is not None and not self.isCanceled():
                # pusty zakres bywa skutkiem innej kolejności osi BBOX – sprawdza dostawca QGIS
                # (także dla całego powiatu: tam BBOX to prostokąt powiatu)
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': 0 obiektów w zakresie – "
                    f"sprawdzam dostawcą WFS QGIS.", LOG_TAG, Qgis.MessageLevel.Info)
                features = None
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': własny klient nie pobrał danych "
                f"({net.friendly(exc)}) – próbuję dostawcą WFS QGIS.", LOG_TAG, Qgis.MessageLevel.Info)
            direct_error = net.friendly(exc)
        else:
            direct_error = ""

        if features is None:
            try:
                fields, geom_type, features, version = self._read_with_provider(
                    teryt, base_url, wfs_url, wfs_layer_name)
            except Exception as exc:
                report.error = direct_error or net.friendly(exc)
                QgsMessageLog.logMessage(f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': {exc}",
                                         LOG_TAG, Qgis.MessageLevel.Warning)
                return report

        report.wfs_version = version
        count = len(features)
        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}' WFS {version}: {count} obiektów → '{layer_name}'",
            LOG_TAG, Qgis.MessageLevel.Info)
        if features:
            self.item_ready.emit({"name": layer_name, "fields": fields,
                                  "geom_type": geom_type, "style": style_name,
                                  "features": features,
                                  "info": {"url": wfs_url, "typename": wfs_layer_name,
                                           "scope": self.scope_text or f"TERYT {teryt} (EZiUDP)"}})
        self._count += count
        self.count_changed.emit(self._count)
        report.feature_count = count
        report.success = True
        return report

    def _read_with_provider(self, teryt, base_url, wfs_url, wfs_layer_name) -> tuple:
        """Dostawca WFS QGIS (WFS 2.0, potem 1.1; z BBOX, potem bez i z przycięciem u nas)."""
        wfs_vl, version = _try_wfs_layer(base_url, wfs_layer_name, self._bbox_rect)
        if wfs_vl is None and self._bbox_rect is not None:
            # niektóre serwery odrzucają BBOX w adresie – wtedy cała warstwa i przycięcie lokalne
            wfs_vl, version = _try_wfs_layer(base_url, wfs_layer_name, None)
        if wfs_vl is None:
            raise RuntimeError(tr("usługa nie odpowiada jak WFS 2.0 ani 1.1"))
        fields, geom_type, features = _read_snapshot(wfs_vl, self._clip_geom, teryt, wfs_url,
                                                     self.isCanceled)
        del wfs_vl   # zwalnia połączenie i pamięć
        if not features and self._bbox_rect is not None and self._clip_geom is not None \
                and not self.isCanceled():
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': BBOX dał 0 wyników – "
                f"ponawiam bez BBOX, przytnę lokalnie.", LOG_TAG, Qgis.MessageLevel.Info)
            wfs_vl2, version2 = _try_wfs_layer(base_url, wfs_layer_name, None)
            if wfs_vl2 is not None:
                fields, geom_type, features = _read_snapshot(wfs_vl2, self._clip_geom, teryt, wfs_url,
                                                             self.isCanceled)
                del wfs_vl2
                version = version2
        return fields, geom_type, features, version

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_wfs_layer(available: list, pattern: Optional[str]) -> Optional[str]:
        if not available:
            return None
        if not pattern:
            return available[0]
        norm = pattern.lower().replace("_", "").replace("-", "")
        for name in available:
            name_norm = name.lower().replace(":", "").replace("_", "").replace("-", "")
            if norm in name_norm:
                return name
        return None
