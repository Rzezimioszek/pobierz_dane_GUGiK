# -*- coding: utf-8 -*-
"""
Zadanie QgsTask dodające warstwy z powiatowych usług WFS przez eziudp
w trybie snapshot (WFS provider → kopia do warstwy memory).

Architektura:
  - Dla każdego powiatu × wybranej warstwy budowany jest URI WFS
    z parametrami BBOX lub filtrem geometrii (subsetString OGC BBOX).
  - QgsVectorLayer jest tworzona z providerem "WFS" – QGIS sam pobiera
    dane przez paginację WFS 2.0 (fallback: 1.1).
  - Obiekty są czytane w wątku zadania do pamięci (snapshot), a warstwy
    (tymczasowe albo w GPKG, przez core.layers) powstają w finished(),
    w głównym wątku – projekt QGIS nie jest dotykany z wątku w tle.
  - Warstwa dostaje styl QML z data/.
  - Brak harmonizacji schematu – pola zachowane tak jak ze źródła WFS.

Filtrowanie zakresu:
  - TERYT powiatu → brak filtra (cały powiat)
  - TERYT gminy / extend / warstwa → BBOX w URI WFS
    ("restrictToRequestBBOX=1" + "bbox=…") oraz subsetString
    z OGC BBOX do lokalnego przycinania po pobraniu jeśli serwer
    nie zastosował filtra
"""
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import (
    QgsTask, QgsMessageLog, Qgis,
    QgsVectorLayer, QgsProject, QgsField, QgsFields,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsGeometry, QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import pyqtSignal

from .eziudp_client import EziudpClient
from .bdsog_harmonizer import get_harmonizer
from .core import gml, layers, net
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
        "label":         "Osnowa pozioma (BDSOG)",
        "zbior":         "bdsog",
        "layer_pattern": "poziom",
    },
    {
        "key":           "bdsog_wysok",
        "label":         "Osnowa wysokościowa (BDSOG)",
        "zbior":         "bdsog",
        "layer_pattern": "wysok",
    },
    {
        "key":           "egib_dzi",
        "label":         "Działki ewidencyjne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "dzialki",
    },
    {
        "key":           "egib_bud",
        "label":         "Budynki ewidencyjne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "budynki",
    },
    {
        "key":           "egib_pkt",
        "label":         "Punkty graniczne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "punkty_graniczne",
    },
    # Użytki i kontury udostępnia tylko część usług powiatowych (np. ePODGiK)
    {
        "key":           "egib_uzy",
        "label":         "Użytki gruntowe (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "uzytki",
        "layer_name":    "Uzytki",
        "style":         "uzytki",
        "optional":      True,
    },
    {
        "key":           "egib_kon",
        "label":         "Kontury klasyfikacyjne (EGIB)",
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
        "label":         "Działki (RCN)",
        "zbior":         "rcn",
        "layer_pattern": "dzialki",
        "layer_name":    "Dzialki_RCN",
        "style":         "dzialki_rcn",
    },
    {
        "key":           "rcn_bud",
        "label":         "Budynki (RCN)",
        "zbior":         "rcn",
        "layer_pattern": "budynki",
        "layer_name":    "Budynki_RCN",
        "style":         "budynki_rcn",
    },
    {
        "key":           "rcn_lok",
        "label":         "Lokale (RCN)",
        "zbior":         "rcn",
        "layer_pattern": "lokale",
        "layer_name":    "Lokale_RCN",
        "style":         "lokale_rcn",
    },
    {
        "key":           "bdot500",
        "label":         "Budynki i obiekty tow. (BDOT500)",
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


_DIRECT_PAGE = 1000
_DIRECT_MAX_PAGES = 300


def _read_direct(
    base_url: str,
    layer_name: str,
    bbox_rect: Optional[QgsRectangle],
    clip_geom: Optional[QgsGeometry],
    teryt: str,
    wfs_url: str,
    task_check_cancelled,
) -> tuple:
    """
    Zapasowa ścieżka, gdy dostawca WFS QGIS odrzuci usługę (np. niepoprawny XML
    w GetCapabilities): GetFeature WFS 2.0 stronami i własny parser GML.
    Zwraca to samo co _read_snapshot (pola None = tekstowe z atrybutów).
    """
    session = net.make_session()
    # Parametry „własne” usługi z adresu EZiUDP (np. map=...) zostają w zapytaniu
    from urllib.parse import parse_qsl, urlsplit
    own = {k: v for k, v in parse_qsl(urlsplit(wfs_url).query)
           if k.lower() not in ("service", "request", "version", "acceptversions",
                                "typename", "typenames")}
    params = {
        **own,
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typenames": layer_name, "srsName": "urn:ogc:def:crs:EPSG::2180",
        "count": _DIRECT_PAGE,
    }
    if bbox_rect is not None:
        # URN EPSG:2180 – kolejność osi N, E
        params["bbox"] = (f"{bbox_rect.yMinimum()},{bbox_rect.xMinimum()},"
                          f"{bbox_rect.yMaximum()},{bbox_rect.xMaximum()},"
                          f"urn:ogc:def:crs:EPSG::2180")
    features, geom_type = [], ""
    try:
        for page in range(_DIRECT_MAX_PAGES):
            if task_check_cancelled():
                break
            params["startIndex"] = page * _DIRECT_PAGE
            text = net.get(session, base_url, params, timeout=180).text
            error = gml.service_exception(text)
            if error:
                raise RuntimeError(f"Serwer zwrócił błąd: {error}")
            batch = gml.parse_features(text)
            for f in batch:
                geom = f["geom"] if isinstance(f["geom"], QgsGeometry) else QgsGeometry.fromWkt(f["geom"])
                if geom is None or geom.isEmpty():
                    continue
                if clip_geom is not None and not geom.intersects(clip_geom):
                    continue
                if not geom_type:
                    geom_type = QgsWkbTypes.displayString(QgsWkbTypes.multiType(geom.wkbType()))
                attrs = dict(f["attrs"])
                attrs["teryt_powiatu"] = teryt
                attrs["zrodlo_wfs"] = wfs_url
                features.append({"geom": geom, "attrs": attrs})
            if len(batch) < _DIRECT_PAGE:
                break
    finally:
        try:
            session.close()
        except Exception:
            pass
    return None, geom_type, features


# ---------------------------------------------------------------------------
# Główne zadanie QgsTask
# ---------------------------------------------------------------------------

class EziudpWfsTask(QgsTask):
    """
    Dodaje warstwy WFS jako snapshot (memory) dla wybranych powiatów.

    Osobna warstwa memory na każdy powiat × typ danych,
    np. "Dzialki_0401", "Dzialki_0402".

    Filtrowanie zakresu:
      - bbox_wkt  → BBOX przekazany do URI WFS + lokalne przycinanie
      - clip_geom_wkt → precyzyjne lokalne przycinanie po pobraniu

    Parametry zgodne z EziudpDownloadTask – możliwa wymiana w dialog.
    """

    downloadFinished = pyqtSignal(list, object)
    progressMessage = pyqtSignal(str)
    progressChanged = pyqtSignal(float)

    def __init__(
        self,
        teryt_list: list,
        layer_keys: list,
        bbox_wkt: Optional[str] = None,
        clip_geom_wkt: Optional[str] = None,
        plugin_dir: str = "",
        load_style: bool = False,
    ):
        labels = [AVAILABLE_LAYERS_BY_KEY[k]["label"] for k in layer_keys
                  if k in AVAILABLE_LAYERS_BY_KEY]
        task_label = f"eziudp WFS: {', '.join(labels)} ({len(teryt_list)} powiatów)"

        super().__init__(task_label, QgsTask.Flag.CanCancel)
        self.teryt_list = [str(t).strip() for t in teryt_list if str(t).strip()]
        self.layer_keys = layer_keys
        self.bbox_wkt = bbox_wkt
        self.clip_geom_wkt = clip_geom_wkt
        self.plugin_dir = plugin_dir
        self.load_style = load_style

        # Miejsce zapisu i nazwy warstw w projekcie – odczytane tu, w głównym wątku
        self.output = layers.Output.from_settings(load_style)
        self._existing_names = {l.name() for l in QgsProject.instance().mapLayers().values()}
        self._pending: list = []   # warstwy do utworzenia w finished()

        self._eziudp = EziudpClient()
        self._report = EziudpWfsReport(total_teryts=len(self.teryt_list))
        self.exception: Optional[Exception] = None

        # Przelicz BBOX raz – używany wielokrotnie
        self._bbox_rect: Optional[QgsRectangle] = None
        if bbox_wkt:
            g = QgsGeometry.fromWkt(bbox_wkt)
            if g and not g.isEmpty():
                self._bbox_rect = g.boundingBox()

        self._clip_geom: Optional[QgsGeometry] = None
        if clip_geom_wkt:
            g = QgsGeometry.fromWkt(clip_geom_wkt)
            if g and not g.isEmpty():
                self._clip_geom = g

    # ------------------------------------------------------------------
    # QgsTask
    # ------------------------------------------------------------------

    def run(self) -> bool:
        total = len(self.teryt_list)

        for i, teryt in enumerate(self.teryt_list):
            if self.isCanceled():
                return False

            progress = int((i / total) * 100)
            self.setProgress(progress)
            self.progressChanged.emit(float(progress))

            msg = f"[{i + 1}/{total}] TERYT {teryt}"
            self.progressMessage.emit(msg)
            QgsMessageLog.logMessage(f"[EziudpWfsTask] {msg}", LOG_TAG, Qgis.MessageLevel.Info)

            powiat_ok = self._process_one_teryt(teryt)
            if powiat_ok:
                self._report.successful += 1
            else:
                self._report.failed += 1

        self.setProgress(100)
        self.progressChanged.emit(100.0)
        return True

    def finished(self, result: bool):
        # Główny wątek: dopiero tu powstają warstwy w projekcie
        for item in self._pending:
            try:
                layers.write(item["features"], item["name"], item["geom_type"],
                             style=item["style"], load_style=self.output.load_style,
                             fields=item["fields"], gpkg_path=self.output.gpkg_path,
                             info=item.get("info"), group=self.output.prefix)
            except Exception as exc:
                msg = f"Zapis warstwy {item['name']} nie powiódł się: {exc}"
                QgsMessageLog.logMessage(f"[EziudpWfsTask] {msg}", LOG_TAG, Qgis.MessageLevel.Critical)
                self._report.errors.append(msg)
        self._pending = []

        status = "Zakończono" if result else (
            "Anulowano" if self.isCanceled() else "Błąd"
        )
        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] {status}: {self._report.format_summary()}",
            LOG_TAG,
            Qgis.MessageLevel.Success if result else Qgis.MessageLevel.Warning,
        )
        # Emituje pustą listę – dane są już w warstwach QGIS
        self.downloadFinished.emit([], self._report)

    def cancel(self):
        super().cancel()

    # ------------------------------------------------------------------
    # Logika dla jednego powiatu
    # ------------------------------------------------------------------

    def _process_one_teryt(self, teryt: str) -> bool:
        """Przetwarza wszystkie wybrane warstwy dla jednego powiatu."""
        any_ok = False

        # Grupuj klucze po zbiorze – jedno GetCapabilities na zbior
        zbior_to_keys: dict = {}
        for key in self.layer_keys:
            cfg = AVAILABLE_LAYERS_BY_KEY.get(key)
            if cfg:
                zbior_to_keys.setdefault(cfg["zbior"], []).append(key)

        for zbior, keys in zbior_to_keys.items():
            if self.isCanceled():
                return any_ok

            # Pobierz URL WFS z eziudp
            eziudp_result = self._eziudp.get_wfs_urls(teryt, zbior)
            if not eziudp_result.success or not eziudp_result.records:
                err = eziudp_result.error_message or "Brak usługi WFS w EZiUDP"
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] TERYT {teryt} zbior={zbior}: {err}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                self._report.errors.append(f"TERYT {teryt} [{zbior}]: {err}")
                continue

            wfs_url = eziudp_result.records[0].url_wfs
            base_url = wfs_url.split("?")[0]

            # Pobierz listę warstw z GetCapabilities
            all_wfs_layers = self._eziudp.get_layer_names(wfs_url, zbior)

            for key in keys:
                if self.isCanceled():
                    return any_ok

                layer_cfg = AVAILABLE_LAYERS_BY_KEY[key]
                pattern = layer_cfg.get("layer_pattern")
                wfs_layer_name = self._pick_wfs_layer(all_wfs_layers, pattern)

                if not wfs_layer_name:
                    msg = (
                        f"TERYT {teryt}: usługa powiatu nie udostępnia warstwy "
                        f"„{layer_cfg['label']}”"
                    )
                    QgsMessageLog.logMessage(
                        f"[EziudpWfsTask] {msg} (wzorzec={pattern!r}, warstwy: {all_wfs_layers})",
                        LOG_TAG, Qgis.MessageLevel.Info,
                    )
                    if layer_cfg.get("optional"):
                        self._report.skipped.append(msg)
                    else:
                        self._report.errors.append(msg)
                    continue

                report = self._fetch_one_layer(
                    teryt=teryt,
                    key=key,
                    base_url=base_url,
                    wfs_url=wfs_url,
                    wfs_layer_name=wfs_layer_name,
                )
                self._report.layers.append(report)
                self._report.total_features += report.feature_count

                if report.success:
                    any_ok = True
                    self.progressMessage.emit(
                        f"  TERYT {teryt} '{wfs_layer_name}': "
                        f"{report.feature_count} obiektów → '{key}_{teryt}'"
                    )
                else:
                    self._report.errors.append(
                        f"TERYT {teryt} [{key}]: {report.error}"
                    )

        return any_ok

    def _fetch_one_layer(
        self,
        teryt: str,
        key: str,
        base_url: str,
        wfs_url: str,
        wfs_layer_name: str,
    ) -> WfsLayerReport:
        """
        Pobiera jedną warstwę WFS dla jednego powiatu jako snapshot.

        Zwraca WfsLayerReport z wynikami.
        """
        report = WfsLayerReport(teryt=teryt, key=key, wfs_url=wfs_url,
                                wfs_layer=wfs_layer_name)

        # Nazwa warstwy i styl: z konfiguracji warstwy, a jeśli brak – z harmonizatora
        cfg = AVAILABLE_LAYERS_BY_KEY[key]
        harmonizer = None
        if not cfg.get("layer_name"):
            harmonizer = get_harmonizer(cfg["zbior"], wfs_layer_name)
        base_layer_name = cfg.get("layer_name") or (harmonizer.LAYER_NAME if harmonizer else key)
        style_name = (cfg.get("style") or getattr(harmonizer, "STYLE", None)
                      or base_layer_name)
        mem_layer_name = self.output.layer_name(f"{base_layer_name}_{teryt}")

        # Warstwa już jest w projekcie (np. ponowne uruchomienie) – pomijamy
        if mem_layer_name in self._existing_names:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] Warstwa '{mem_layer_name}' już istnieje – pomijam.",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            report.success = True
            report.wfs_version = "cached"
            return report

        # Próbuj WFS 2.0, fallback na 1.1
        wfs_vl, version = _try_wfs_layer(
            base_url, wfs_layer_name, self._bbox_rect
        )

        if wfs_vl is None:
            # Ostatnia próba: bez BBOX (niektóre serwery odrzucają BBOX w URI)
            if self._bbox_rect is not None:
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': "
                    f"próba bez BBOX w URI (przytnę lokalnie).",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
                wfs_vl, version = _try_wfs_layer(base_url, wfs_layer_name, None)

            if wfs_vl is None:
                return self._fetch_direct(report, teryt, base_url, wfs_url, wfs_layer_name,
                                          mem_layer_name, style_name)

        report.wfs_version = version

        # Snapshot: obiekty z WFS do pamięci (clip_geom, gdy wybrano gminę / zasięg / warstwę
        # i serwer mógł nie zastosować BBOX z URI)
        fields, geom_type, features = _read_snapshot(
            wfs_layer=wfs_vl,
            clip_geom=self._clip_geom,
            teryt=teryt,
            wfs_url=wfs_url,
            task_check_cancelled=self.isCanceled,
        )
        count = len(features)

        # Jawne usunięcie WFS layer – zwalniamy połączenie i pamięć
        del wfs_vl

        # Jeśli snapshot jest pusty a był BBOX → ponów bez BBOX i przytnij lokalnie
        if count == 0 and self._bbox_rect is not None and self._clip_geom is not None:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': "
                f"BBOX w URI dał 0 wyników – ponawiam bez BBOX, przytnę lokalnie.",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            wfs_vl2, version2 = _try_wfs_layer(base_url, wfs_layer_name, None)
            if wfs_vl2 is not None:
                fields, geom_type, features = _read_snapshot(
                    wfs_layer=wfs_vl2,
                    clip_geom=self._clip_geom,
                    teryt=teryt,
                    wfs_url=wfs_url,
                    task_check_cancelled=self.isCanceled,
                )
                count = len(features)
                del wfs_vl2
                report.wfs_version = version2

        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}' "
            f"WFS {report.wfs_version}: {count} obiektów → '{mem_layer_name}'",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        if features:
            self._pending.append({"name": mem_layer_name, "fields": fields,
                                  "geom_type": geom_type, "style": style_name,
                                  "features": features,
                                  "info": {"url": wfs_url, "typename": wfs_layer_name,
                                           "scope": f"TERYT {teryt} (EZiUDP)"}})
            self._existing_names.add(mem_layer_name)

        report.feature_count = count
        report.success = True
        return report

    def _fetch_direct(self, report, teryt, base_url, wfs_url, wfs_layer_name,
                      mem_layer_name, style_name) -> WfsLayerReport:
        """Dostawca WFS QGIS nie przyjął usługi – pobieranie przez własny klient GML."""
        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': dostawca WFS QGIS odrzucił "
            f"usługę – pobieram bezpośrednio (GetFeature + własny parser GML).",
            LOG_TAG, Qgis.MessageLevel.Info,
        )
        try:
            _, geom_type, features = _read_direct(
                base_url, wfs_layer_name, self._bbox_rect, self._clip_geom,
                teryt, wfs_url, self.isCanceled)
        except Exception as exc:
            report.error = f"{wfs_layer_name} @ {base_url}: {exc}"
            QgsMessageLog.logMessage(f"[EziudpWfsTask] {report.error}", LOG_TAG,
                                     Qgis.MessageLevel.Warning)
            return report
        report.wfs_version = "2.0.0 (GML)"
        if features:
            self._pending.append({"name": mem_layer_name, "fields": None,
                                  "geom_type": geom_type, "style": style_name,
                                  "features": features,
                                  "info": {"url": wfs_url, "typename": wfs_layer_name,
                                           "scope": f"TERYT {teryt} (EZiUDP)"}})
            self._existing_names.add(mem_layer_name)
        report.feature_count = len(features)
        report.success = True
        return report

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