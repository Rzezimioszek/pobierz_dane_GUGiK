# -*- coding: utf-8 -*-
"""
Algorytmy Processing: pobieranie z usług WFS GUGiK dla jednostki TERYT,
zasięgu warstwy i listy identyfikatorów. Ten sam rdzeń (core/) co panel,
więc wynik jest identyczny; działają w modelach, trybie wsadowym i qgis_process.
"""
import os

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureSink, QgsProcessing,
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
    QgsProcessingParameterString, QgsWkbTypes,
)
from qgis.PyQt.QtGui import QIcon

from ..core import catalog, layers
from ..core import scope as scope_mod
from ..core.i18n import tr
from ..core.tasks import WfsDownloadTask

CRS_2180 = QgsCoordinateReferenceSystem("EPSG:2180")
ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")


def source_label(s) -> str:
    return f"{s.group} – {s.label}"


class _DownloadAlgorithm(QgsProcessingAlgorithm):
    """Wspólne: wybór źródła, pobieranie przez WfsDownloadTask, zapis do wyjścia."""
    SOURCE = "SOURCE"
    OUTPUT = "OUTPUT"

    def __init__(self):
        super().__init__()
        self.sources = self._sources()

    # -- do nadpisania
    def _sources(self) -> list:
        return list(catalog.SOURCES)

    def _queries(self, source, parameters, context, feedback) -> list:
        raise NotImplementedError

    def _scope_text(self, parameters, context) -> str:
        return ""

    # -- wspólne
    def createInstance(self):
        return type(self)()

    def group(self):
        return tr("Pobieranie z WFS")

    def groupId(self):
        return "wfs"

    def icon(self):
        return QIcon(ICON)

    def _add_source_param(self, default: int = 0) -> None:
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, tr("Dane do pobrania"),
            options=[source_label(s) for s in self.sources], defaultValue=default))

    def _add_output_param(self) -> None:
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Pobrane obiekty")))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.sources[self.parameterAsEnum(parameters, self.SOURCE, context)]
        queries = self._queries(source, parameters, context, feedback)
        if not queries:
            raise QgsProcessingException(tr("Brak zakresu do pobrania."))
        feedback.pushInfo(tr("Źródło: ") + f"{source.label} ({source.url}), "
                          + tr("zapytań: ") + str(len(queries)))

        task = WfsDownloadTask(source, queries)
        feedback.canceled.connect(task.cancel)
        task.progressChanged.connect(feedback.setProgress)
        task.count_changed.connect(lambda n: feedback.setProgressText(tr("Pobrano obiektów: ") + str(n)))
        try:
            task.run()
        finally:
            try:
                feedback.canceled.disconnect(task.cancel)
            except (TypeError, RuntimeError):
                pass
            task.client.session.close()
        result = task.result
        for label, msg in result.errors:
            feedback.reportError(f"{label}: {msg}", fatalError=False)
        if feedback.isCanceled():
            raise QgsProcessingException(tr("Przerwano."))
        if not result.features and result.errors:
            raise QgsProcessingException(tr("Nie pobrano danych: ") + result.errors[0][1])

        features = result.features
        if source.split_lines_polygons:
            parts = layers.split_lines_polygons(features)
            if parts["polygon"]:
                feedback.pushWarning(tr("Pominięto obiektów poligonowych: ") + str(len(parts["polygon"]))
                                     + tr(" (wynik algorytmu ma tylko linie)."))
            features = parts["line"]

        wkb = QgsWkbTypes.multiType(QgsWkbTypes.parseType(source.geom_type))
        fields = layers.schema_for(features, source.numeric_fields) if features \
            else layers.schema_for([{"attrs": {}}])
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, wkb, CRS_2180)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        out = layers.make_features(features, fields, True, source.unique_field, label=source.label)
        sink.addFeatures(out, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("Zapisano obiektów: ") + str(len(out)))

        if context.willLoadLayerOnCompletion(dest_id):
            details = context.layerToLoadOnCompletionDetails(dest_id)
            details.name = source.layer_name
            if source.style:
                details.setPostProcessor(_StylePostProcessor.create(source.style))
        return {self.OUTPUT: dest_id}


class _StylePostProcessor:
    """Styl QML z data/ dla warstwy wczytanej po zakończeniu algorytmu."""
    _keep = []   # QGIS nie przejmuje własności – trzymamy referencję

    @classmethod
    def create(cls, style):
        from qgis.core import QgsProcessingLayerPostProcessorInterface

        class _Post(QgsProcessingLayerPostProcessorInterface):
            def postProcessLayer(self, layer, context, feedback):
                layers.apply_style(layer, style)

        post = _Post()
        cls._keep.append(post)
        return post


# ====================================================================== TERYT

class DownloadTeryt(_DownloadAlgorithm):
    TERYT = "TERYT"
    PRECISE = "PRECISE"

    def name(self):
        return "pobierz_teryt"

    def displayName(self):
        return tr("Pobierz dla jednostki (TERYT)")

    def shortHelpString(self):
        return tr("Pobiera obiekty z usługi WFS GUGiK dla województwa (2 cyfry), powiatu (4), "
                  "gminy (7, np. 1465011) lub obrębu (np. 146501_1.0001). Granica jednostki "
                  "pochodzi z PRG. Dla całych województw EGiB i RCN lepiej użyć paczek opendata "
                  "z panelu wtyczki.")

    def initAlgorithm(self, config=None):
        self._add_source_param()
        self.addParameter(QgsProcessingParameterString(self.TERYT, tr("Kod TERYT")))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PRECISE, tr("Dokładna granica (wolniej; bez tego – prostokąt otaczający + kod TERYT)"),
            defaultValue=True))
        self._add_output_param()

    def _queries(self, source, parameters, context, feedback):
        teryt = self.parameterAsString(parameters, self.TERYT, context).strip()
        if not teryt:
            raise QgsProcessingException(tr("Podaj kod TERYT."))
        if len(teryt) == 2 and source.opendata:
            feedback.pushWarning(tr("Całe województwo z WFS może trwać bardzo długo – "
                                    "rozważ paczkę opendata z panelu wtyczki."))
        feedback.pushInfo(tr("Granica jednostki z PRG: ") + teryt)
        boundary = scope_mod.boundary(teryt)
        if boundary is None:
            raise QgsProcessingException(tr("Nie udało się pobrać granicy z PRG – sprawdź kod TERYT: ")
                                         + teryt)
        precise = self.parameterAsBoolean(parameters, self.PRECISE, context)
        return [scope_mod.teryt_query(source, teryt, boundary, precise=precise)]


# ====================================================================== warstwa

class DownloadLayer(_DownloadAlgorithm):
    INPUT = "INPUT"

    def name(self):
        return "pobierz_warstwa"

    def displayName(self):
        return tr("Pobierz dla zasięgu warstwy")

    def shortHelpString(self):
        return tr("Pobiera obiekty przecinające poligony warstwy wejściowej (każdy poligon to osobne "
                  "zapytanie). Wynik jest przycięty dokładnie do poligonów. „Tylko zaznaczone” "
                  "w parametrze warstwy ogranicza zakres do zaznaczonych obiektów.")

    def initAlgorithm(self, config=None):
        self._add_source_param()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Zasięg (warstwa poligonowa)"),
            [QgsProcessing.SourceType.TypeVectorPolygon]))
        self._add_output_param()

    def _queries(self, source, parameters, context, feedback):
        src = self.parameterAsSource(parameters, self.INPUT, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        xform = QgsCoordinateTransform(src.sourceCrs(), CRS_2180, context.transformContext())
        geoms = []
        for f in src.getFeatures():
            if not f.hasGeometry() or f.geometry().isEmpty():
                continue
            g = f.geometry()
            if src.sourceCrs() != CRS_2180:
                g.transform(xform)
            geoms.append(g)
        if len(geoms) > 100:
            feedback.pushWarning(tr("Obiektów zasięgu: ") + str(len(geoms))
                                 + tr(" – każdy to osobne zapytanie, może to potrwać."))
        return scope_mod.geometry_queries(source, geoms, tr("obiekt"))


# ====================================================================== lista ID

class DownloadIds(_DownloadAlgorithm):
    IDS = "IDS"
    TABLE = "TABLE"
    FIELD = "FIELD"

    def _sources(self):
        return [s for s in catalog.SOURCES if s.id_field]

    def name(self):
        return "pobierz_id"

    def displayName(self):
        return tr("Pobierz według listy identyfikatorów")

    def shortHelpString(self):
        return tr("Pobiera obiekty o podanych identyfikatorach (np. działek 146501_1.0001.23). "
                  "Identyfikatory można wpisać (oddzielone nową linią, przecinkiem lub średnikiem) "
                  "albo wskazać tabelę i pole; „*” i „?” działają jak symbole wieloznaczne.")

    def initAlgorithm(self, config=None):
        self._add_source_param()
        self.addParameter(QgsProcessingParameterString(
            self.IDS, tr("Identyfikatory"), multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.TABLE, tr("…albo tabela z identyfikatorami"),
            [QgsProcessing.SourceType.TypeVector], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Pole z identyfikatorem"), parentLayerParameterName=self.TABLE,
            optional=True))
        self._add_output_param()

    def _queries(self, source, parameters, context, feedback):
        ids = scope_mod.split_ids(self.parameterAsString(parameters, self.IDS, context))
        table = self.parameterAsSource(parameters, self.TABLE, context)
        field = self.parameterAsString(parameters, self.FIELD, context)
        if table is not None:
            if not field:
                raise QgsProcessingException(tr("Wskaż pole z identyfikatorem w tabeli."))
            ids += [str(f[field]).strip() for f in table.getFeatures()
                    if f[field] not in (None, "") and str(f[field]).strip() not in ("", "NULL")]
        ids = list(dict.fromkeys(ids))          # bez powtórzeń, kolejność zachowana
        if not ids:
            raise QgsProcessingException(tr("Podaj identyfikatory albo tabelę z polem identyfikatora."))
        feedback.pushInfo(tr("Identyfikatorów: ") + str(len(ids)))
        return scope_mod.id_queries(source, ids)


ALGORITHMS = (DownloadTeryt, DownloadLayer, DownloadIds)
