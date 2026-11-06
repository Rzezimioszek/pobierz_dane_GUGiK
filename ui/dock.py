# -*- coding: utf-8 -*-
"""
Panel wtyczki: trzy kroki (co pobrać → skąd → jak zapisać), przycisk Pobierz
aktywny dopiero przy pełnym wyborze, kolejka zadań na dole.

Panel tylko zbiera wybór i uruchamia zadania z core.tasks; cała praca
sieciowa dzieje się w tle.
"""
import os

from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsPointCloudLayer, QgsProject, QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QLabel, QListWidget,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import tr
from .admin_units import AdminUnits
from .map_tools import RectangleMapTool, make_band, show_geometry
from .widgets import OutputPicker, ScopePicker, SourcePicker, TasksPanel, hint_label, step_box
from ..core import catalog, filters, layers, log, tasks
from ..prg_client import PRGClient
from ..qt_compat import GEOM_POLYGON
from ..wfs_index_dialog import WfsIndexResultsDialog

EPSG2180 = "EPSG:2180"
ID_BATCH = 10
POINT_CLOUD_EXT = (".copc.laz", ".laz", ".las", ".copc")


class PD_GUGiKDock(QDockWidget):
    closingPlugin = pyqtSignal()

    def __init__(self, iface=None, parent=None):
        super().__init__(tr("Pobierz dane GUGiK"), parent)
        if iface is None:
            from qgis.utils import iface as qgis_iface
            iface = qgis_iface
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setObjectName("PD_GUGiKDock")

        self.units = AdminUnits.instance()
        # Atrybuty, z których korzysta okno EZiUDP (szuka ich w rodzicach)
        self.wojewodztwa = self.units.wojewodztwa
        self.powiaty = self.units.powiaty
        self.gminy = self.units.gminy
        self.name_to_teryt = self.units.name_to_teryt
        self.prg_client = PRGClient()

        self.running_tasks = []
        self.map_tool = None
        self._prev_tool = None
        self.band = make_band(self.canvas)
        self._raster_queue = []

        self._build()
        self._validate()

    # ================================================================== budowa

    def _build(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(6, 6, 6, 6)

        box, bl = step_box(1, tr("Co pobrać"))
        self.sources = SourcePicker()
        bl.addWidget(self.sources)
        lay.addWidget(box)

        self.scope_box, bl = step_box(2, tr("Skąd"))
        self.scope = ScopePicker(self.units)
        bl.addWidget(self.scope)
        lay.addWidget(self.scope_box)

        self.output_box, bl = step_box(3, tr("Jak zapisać"))
        self.output = OutputPicker()
        self.chk_load_style = self.output.chk_style   # używane przez okno EZiUDP
        bl.addWidget(self.output)
        lay.addWidget(self.output_box)

        from ..eziudp_download_dialog import EziudpWidget
        self.eziudp = EziudpWidget(parent=content)
        self.eziudp.inject_scope_data(self.wojewodztwa, self.powiaty, self.gminy, self.name_to_teryt)
        self.eziudp.setVisible(False)
        lay.addWidget(self.eziudp)

        self.hint = hint_label()
        lay.addWidget(self.hint)
        self.btn_download = QPushButton(tr("Pobierz"))
        self.btn_download.setMinimumHeight(32)
        f = self.btn_download.font()
        f.setBold(True)
        self.btn_download.setFont(f)
        self.btn_download.clicked.connect(self.start)
        lay.addWidget(self.btn_download)

        self.tasks = TasksPanel()
        self.tasks.open_log.connect(lambda: self.iface.openMessageLog())
        lay.addWidget(self.tasks)
        lay.addStretch(1)

        # Listy rozwijane mogą się zwężać razem z panelem (długie nazwy jednostek)
        for combo in content.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            pol = combo.sizePolicy()
            pol.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            combo.setSizePolicy(pol)
            combo.setMinimumWidth(80)
        scroll.setWidget(content)
        self.setWidget(scroll)
        self.setMinimumWidth(300)

        self.sources.changed.connect(self._validate)
        self.scope.changed.connect(self._validate)
        self.output.changed.connect(self._validate)
        self.scope.draw_requested.connect(self._start_draw)
        self.scope.show_unit_requested.connect(self._show_unit)
        if self.canvas is not None:
            self.canvas.selectionChanged.connect(lambda *_: self._validate())

    def closeEvent(self, event):
        self._stop_draw()
        self.band.reset(GEOM_POLYGON)
        self.closingPlugin.emit()
        event.accept()

    # ================================================================== komunikaty

    def notify(self, text: str, level=Qgis.MessageLevel.Info) -> None:
        """Komunikat na pasku QGIS – bez okienek blokujących pracę."""
        self.iface.messageBar().pushMessage(tr("Pobierz dane GUGiK"), text, level=level, duration=8)

    def ask(self, text: str) -> bool:
        reply = QMessageBox.question(self, tr("Pobierz dane GUGiK"), text,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    # ================================================================== walidacja

    def _validate(self) -> bool:
        sel = self.sources.selection(with_years=False)
        self.eziudp.setVisible(sel.eziudp)
        for w in (self.scope_box, self.output_box, self.btn_download, self.hint):
            w.setVisible(not sel.eziudp)
        if sel.eziudp:
            return False

        problem = self._problem(sel, self.scope.scope())
        self._package_context(sel, self.scope.scope())
        self.btn_download.setEnabled(not problem)
        self.hint.setText(problem or f"{sel.names()} · {self.scope.scope().describe()}")
        self.hint.setStyleSheet("color: #b35900;" if problem else "color: palette(mid);")
        return not problem

    def _problem(self, sel, scope) -> str:
        if sel.empty:
            return tr("Zaznacz w kroku 1, co chcesz pobrać.")
        if scope.kind == "teryt" and not scope.teryt:
            return tr("Wybierz jednostkę albo wpisz kod TERYT.")
        if scope.kind == "rect" and scope.rect is None:
            return tr("Narysuj prostokąt na mapie (przycisk „Rysuj…”).")
        if scope.kind == "layer":
            if scope.layer is None:
                return tr("Wybierz warstwę poligonową.")
            if scope.selected_only and scope.layer.selectedFeatureCount() == 0:
                return tr("Zaznacz obiekty na wybranej warstwie albo odznacz „Tylko zaznaczone”.")
        if scope.kind in ("ids", "obreb") and sel.skorowidz is not None:
            return tr("Dla skorowidzów wybierz zakres: jednostkę, mapę albo warstwę.")
        if scope.kind == "ids" and not scope.ids:
            return tr("Wpisz identyfikatory albo wczytaj je z pliku.")
        if scope.kind == "obreb":
            if not scope.obreb or not scope.nr:
                return tr("Wpisz nazwę obrębu i numer działki.")
            if not self.units.find_obreby(scope.obreb):
                return tr("Nie znaleziono obrębu o tej nazwie.")
            if not self._parcel_sources(sel):
                return tr("Wyszukiwanie po obrębie i numerze dotyczy działek – zaznacz działki EGiB lub RCN.")
        return ""

    @staticmethod
    def _parcel_sources(sel) -> list:
        return [s for s in sel.sources if s.label.startswith("dzialki")]

    def _package_context(self, sel, scope) -> None:
        packaged = [s for s in sel.sources if s.opendata]
        if scope.kind != "teryt" or not packaged or not scope.teryt:
            self.output.set_package_context(False)
            return
        if len(scope.teryt) < 4:
            self.output.set_package_context(False, tr(
                "Dla województwa dane EGiB/RCN pobierane są jako paczka GeoParquet z opendata "
                "(zapis pliku na dysk)."))
        else:
            self.output.set_package_context(len(scope.teryt) == 4)

    # ================================================================== mapa

    def _start_draw(self) -> None:
        if self.map_tool is None:
            self.map_tool = RectangleMapTool(self.canvas, self._on_rect)
        self._prev_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self.map_tool)

    def _stop_draw(self) -> None:
        if self.map_tool is not None and self.canvas.mapTool() is self.map_tool:
            self.canvas.unsetMapTool(self.map_tool)
            if self._prev_tool is not None:
                self.canvas.setMapTool(self._prev_tool)

    def _on_rect(self, rect) -> None:
        self._stop_draw()
        self.scope.set_drawn_rect(rect)
        show_geometry(self.band, self._to_2180(QgsGeometry.fromRect(rect)), self.canvas, zoom=False)

    def _to_2180(self, geom, crs=None) -> QgsGeometry:
        g = QgsGeometry(geom)
        crs = crs or self.canvas.mapSettings().destinationCrs()
        target = QgsCoordinateReferenceSystem(EPSG2180)
        if crs.isValid() and crs != target:
            g.transform(QgsCoordinateTransform(crs, target, QgsProject.instance()))
        return g

    def _show_unit(self, teryt: str) -> None:
        if not teryt:
            return
        row = self.tasks.add(tr("Granica PRG: ") + teryt)
        task = tasks.BoundaryTask(teryt)
        row.attach(task)

        def done(geom):
            if geom is None:
                row.finish(tr("Nie znaleziono granicy dla ") + teryt, ok=False)
                return
            show_geometry(self.band, geom, self.canvas)
            row.finish(tr("Granica podświetlona na mapie."))
        self._run(task, done)

    # ================================================================== uruchamianie

    def _run(self, task, on_done) -> None:
        """Dodaje zadanie do menedżera QGIS i trzyma do niego referencję do końca."""
        self.running_tasks.append(task)

        def forget(*_):
            if task in self.running_tasks:
                self.running_tasks.remove(task)
        task.done.connect(on_done)
        task.taskCompleted.connect(forget)
        task.taskTerminated.connect(forget)
        QgsApplication.taskManager().addTask(task)

    def start(self) -> None:
        if not self._validate():
            return
        sel = self.sources.selection()
        scope = self.scope.scope()
        out = self.output.output()

        if scope.kind == "teryt":
            self._start_teryt(sel, scope, out)
        elif scope.kind in ("extent", "rect"):
            rect = self.canvas.extent() if scope.kind == "extent" else scope.rect
            geom = self._to_2180(QgsGeometry.fromRect(rect))
            label = scope.describe()
            if sel.skorowidz is not None:
                self._search_skorowidz(sel, geom)
            for s in sel.sources:
                self._download(s, [tasks.Query(filters.spatial(s.geom_field, geom.asWkt()), label=label)],
                               out, label)
        elif scope.kind == "layer":
            self._start_layer(sel, scope, out)
        elif scope.kind == "ids":
            for s in sel.sources:
                self._download(s, self._id_queries(s, scope.ids), out, scope.describe())
        elif scope.kind == "obreb":
            self._start_obreb(sel, scope, out)

    # ------------------------------------------------------------------ po TERYT

    def _start_teryt(self, sel, scope, out) -> None:
        teryt = scope.teryt
        package = [s for s in sel.sources if s.opendata and
                   (len(teryt) < 4 or (len(teryt) == 4 and self.output.powiat_method() == "package"))]
        for s in package:
            self._download_package(s, teryt)
        wfs = [s for s in sel.sources if s not in package]
        if not wfs and sel.skorowidz is None:
            return

        rows = {s.label: self.tasks.add(f"{SourcePicker.display_name(s)} · TERYT {teryt}") for s in wfs}
        task = tasks.BoundaryTask(teryt)

        def got_boundary(geom):
            if geom is None:
                for r in rows.values():
                    r.finish(tr("Nie udało się pobrać granicy jednostki z PRG (sprawdź kod TERYT)."), ok=False)
                if sel.skorowidz is not None:
                    self.notify(tr("Nie udało się pobrać granicy jednostki z PRG."), Qgis.MessageLevel.Warning)
                return
            if sel.skorowidz is not None:
                self._search_skorowidz(sel, geom)
            for s in wfs:
                if not rows[s.label].running:   # anulowane przed pobraniem granicy
                    continue
                if s.clip_locally:
                    q = tasks.Query(filters.spatial(s.geom_field, geom.asWkt(), use_bbox=True),
                                    clip_geom=geom, label=teryt)
                else:
                    q = tasks.Query(filters.combine([
                        filters.spatial(s.geom_field, geom.asWkt(), use_bbox=not scope.precise),
                        filters.attribute(s.teryt_field, teryt, like=True),
                    ]), label=teryt)
                self._download(s, [q], out, f"TERYT {teryt}", row=rows[s.label])
        self._run(task, got_boundary)

    def _download_package(self, source, teryt) -> None:
        url, name = catalog.opendata_package(source, teryt)
        ext = name.split(".", 1)[1]
        path, _ = QFileDialog.getSaveFileName(self, tr("Zapisz paczkę danych"), name, f"*.{ext}")
        if not path:
            return
        task = tasks.FileDownloadTask(url, path, f"{name}")
        row = self.tasks.add(tr("Paczka ") + name, task)

        def done(saved):
            if not saved:
                row.finish(tr("Nie udało się pobrać paczki (szczegóły w dzienniku)."), ok=False)
                return
            try:
                added = layers.add_package(saved, self.output.chk_style.isChecked())
            except Exception as exc:
                row.finish(tr("Pobrano, ale nie wczytano: ") + str(exc), ok=False)
                return
            row.finish(tr("Zapisano i wczytano warstw: ") + str(len(added)))
        self._run(task, done)

    # ------------------------------------------------------------------ po warstwie

    def _start_layer(self, sel, scope, out) -> None:
        layer = scope.layer
        feats = list(layer.getSelectedFeatures() if scope.selected_only else layer.getFeatures())
        if len(feats) > 100 and not self.ask(
                tr("Warstwa ma ") + str(len(feats)) + tr(" obiektów – każdy to osobne zapytanie. Kontynuować?")):
            return
        geoms = [self._to_2180(f.geometry(), layer.crs()) for f in feats
                 if f.hasGeometry() and not f.geometry().isEmpty()]
        if sel.skorowidz is not None:
            self._search_skorowidz(sel, QgsGeometry.unaryUnion(geoms))
        for s in sel.sources:
            queries = []
            for i, g in enumerate(geoms):
                label = f"{layer.name()} #{i + 1}"
                if g.constGet().nCoordinates() <= 100:
                    queries.append(tasks.Query(filters.spatial(s.geom_field, g.asWkt(), use_bbox=False),
                                               label=label))
                    continue
                hull = g.convexHull()
                flt = (filters.spatial(s.geom_field, hull.asWkt(), use_bbox=False)
                       if hull.constGet().nCoordinates() <= 100
                       else filters.spatial(s.geom_field, g.asWkt(), use_bbox=True))
                queries.append(tasks.Query(flt, clip_geom=g, label=label))
            self._download(s, queries, out, scope.describe())

    # ------------------------------------------------------------------ po ID / obrębie

    @staticmethod
    def _id_queries(source, ids) -> list:
        return [tasks.Query(filters.ids(source.id_field, ids[i:i + ID_BATCH]),
                            label=f"ID {i + 1}–{min(i + ID_BATCH, len(ids))}")
                for i in range(0, len(ids), ID_BATCH)]

    def _start_obreb(self, sel, scope, out) -> None:
        candidates = self.units.find_obreby(scope.obreb)
        teryt = candidates[0] if len(candidates) == 1 else self._choose_obreb(candidates)
        if not teryt:
            return
        parcel_id = f"{teryt}.{scope.nr}"
        log.info(f"[UI] Obręb {scope.obreb}, działka {scope.nr} → {parcel_id}")
        for s in self._parcel_sources(sel):
            self._download(s, [tasks.Query(filters.ids(s.id_field, [parcel_id]), label=parcel_id)],
                           out, parcel_id)

    def _choose_obreb(self, candidates) -> str:
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Wybierz obręb"))
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(tr("Jest kilka obrębów o tej nazwie. Wybierz właściwy:")))
        lst = QListWidget()
        for t in candidates:
            info = self.units.obreby_teryt_to_info.get(t, {})
            lst.addItem(f"{info.get('nazwa', t)} ({t}) – gmina {self.units.gmina_name(t) or '?'}, "
                        f"{self.units.powiat_name(t) or '?'}")
        lst.setCurrentRow(0)
        lst.itemDoubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(lst)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted and lst.currentRow() >= 0:
            return candidates[lst.currentRow()]
        return ""

    # ------------------------------------------------------------------ pobieranie WFS

    def _download(self, source, queries, out, scope_text, row=None) -> None:
        if not queries:
            return
        task = tasks.WfsDownloadTask(source, queries, self.output.selected_attributes())
        title = f"{SourcePicker.display_name(source)} · {scope_text}"
        if row is None:
            row = self.tasks.add(title, task)
        else:
            row.attach(task)

        def done(result):
            n = len(result.features)
            written = []
            if n:
                try:
                    written = layers.write_source(result.features, source, out)
                except Exception as exc:
                    row.finish(tr("Nie udało się zapisać wyników: ") + str(exc), ok=False)
                    return
            names = ", ".join(l.name() for l in written)
            if result.canceled:
                row.finish(tr("Przerwano. Zapisano obiektów: ") + str(n), ok=False)
            elif not n and result.errors:
                row.finish(tr("Błąd: ") + result.errors[0][1][:200], ok=False)
            elif not n:
                row.finish(tr("Brak obiektów w tym zakresie."), ok=True)
            elif result.errors:
                row.finish(f"{n} " + tr("obiektów → ") + names + "; " + str(len(result.errors)) +
                           tr(" zapytań nie powiodło się (dziennik)."), ok=False)
            else:
                row.finish(f"{n} " + tr("obiektów → ") + names)
        self._run(task, done)

    # ================================================================== skorowidze

    def _search_skorowidz(self, sel, geom) -> None:
        svc = sel.skorowidz
        names = ([f"{svc.layer_name}{y}{svc.layer_suffix}" for y in sel.years]
                 if svc.year_range is not None else [svc.layer_name])
        task = tasks.SkorowidzSearchTask(svc.url, names, geom)
        row = self.tasks.add(tr("Skorowidz: ") + svc.label, task)

        def done(features, errors):
            if not features:
                msg = tr("Brak arkuszy w tym zakresie.")
                if errors and len(errors) == len(names):
                    msg = tr("Błąd: ") + errors[0][1][:200]
                row.finish(msg, ok=not errors or len(errors) < len(names))
                return
            row.finish(tr("Znaleziono arkuszy: ") + str(len(features)))
            dialog = WfsIndexResultsDialog(features, self)
            if dialog.exec() and dialog.selected_urls:
                self._load_rasters(dialog.selected_urls, dialog.load_mode)
        self._run(task, done)

    def _load_file_as_layer(self, path_or_url: str, name: str) -> bool:
        lower = path_or_url.lower()
        if lower.endswith(POINT_CLOUD_EXT):
            if path_or_url.startswith("/vsicurl/") and ".copc" not in lower:
                log.warning(f"[LAZ] {name} nie jest w formacie COPC – strumieniowanie może nie działać.")
            layer = QgsPointCloudLayer(path_or_url, name, "pdal")
        else:
            layer = QgsRasterLayer(path_or_url, name)
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return True
        log.warning(f"Nie udało się wczytać: {path_or_url}")
        return False

    def _load_rasters(self, urls, mode) -> None:
        if mode == "vsicurl":
            added = sum(1 for _, url in urls if self._load_file_as_layer(f"/vsicurl/{url}", url.split("/")[-1]))
            self.notify(tr("Dodano warstw strumieniowych: ") + f"{added}/{len(urls)}")
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Folder na pobrane pliki"))
        if not folder:
            return
        self._raster_queue = [(url, os.path.join(folder, url.split("/")[-1])) for _, url in urls]
        self._next_raster()

    def _next_raster(self) -> None:
        """Pliki skorowidza po jednym, w tle; każdy po pobraniu trafia na mapę."""
        if not self._raster_queue:
            return
        url, path = self._raster_queue.pop(0)
        task = tasks.FileDownloadTask(url, path, os.path.basename(path))
        row = self.tasks.add(tr("Plik ") + os.path.basename(path), task)

        def done(saved):
            if saved:
                self._load_file_as_layer(saved, os.path.basename(saved))
                row.finish(tr("Zapisano i wczytano."))
            else:
                row.finish(tr("Nie pobrano (szczegóły w dzienniku)."), ok=False)
                if task.isCanceled():
                    self._raster_queue = []
            self._next_raster()
        self._run(task, done)
