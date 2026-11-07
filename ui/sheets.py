# -*- coding: utf-8 -*-
"""
Wybór arkuszy skorowidza: zasięgi jako warstwa na mapie, wybór kliknięciem
na mapie albo w tabeli, filtr rocznika, typu i godła, łączny rozmiar plików.

Zaznaczenie arkuszy = zaznaczenie obiektów warstwy (żółte na mapie), więc
tabela, mapa i zwykłe narzędzia zaznaczania QGIS widzą to samo.
"""
import os
import re
from dataclasses import dataclass

from qgis.core import (
    NULL, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsFillSymbol, QgsGeometry, QgsProject, QgsRectangle, QgsSettings, QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QDate, QDateTime, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from . import tr
from .widgets import hint_label
from ..core import layers
from ..core.layers import SETTINGS_PREFIX
from ..qt_compat import FIELD_DOUBLE, FIELD_INT, FIELD_STRING

# Atrybuty, z których bierzemy „typ” arkusza (pierwszy niepusty)
TYPE_FIELDS = ("kolor", "char_przestrz", "format", "typ_danych", "rodzaj", "typ")
YEAR_FIELDS = ("akt_rok", "rok", "rok_pomiaru")
SIZE_FIELDS = ("wlk_pliku_mb", "wielkosc_mb", "rozmiar_mb")
URL_FIELDS = ("url_do_pobrania", "URL_GML", "url")
COL_CHECK, COL_NAME, COL_YEAR, COL_TYPE, COL_SIZE = range(5)


def _text(value) -> str:
    if value is None or value == NULL:
        return ""
    if isinstance(value, QDateTime):
        return value.date().toString("yyyy-MM-dd")
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd")
    return str(value).strip()


def _first(attrs: dict, keys) -> str:
    lower = {k.lower(): v for k, v in attrs.items()}
    for k in keys:
        v = _text(lower.get(k.lower()))
        if v:
            return v
    return ""


@dataclass
class Sheet:
    """Arkusz skorowidza w postaci potrzebnej do wyboru i pobrania."""
    name: str
    url: str
    year: str
    kind: str
    size_mb: float      # 0 = nieznany
    geom: QgsGeometry
    attrs: dict

    @classmethod
    def from_feature(cls, feat: dict) -> "Sheet":
        attrs = feat["attrs"]
        url = _first(attrs, URL_FIELDS)
        year = _first(attrs, YEAR_FIELDS)
        if not year:
            m = re.search(r"(19|20)\d{2}", feat.get("layer", ""))
            year = m.group(0) if m else ""
        year = year[:4] if re.match(r"^(19|20)\d{2}", year) else year
        try:
            size = float(_first(attrs, SIZE_FIELDS).replace(",", ".") or 0)
        except ValueError:
            size = 0.0
        name = _first(attrs, ("godlo",)) or (url.rsplit("/", 1)[-1] if url else "")
        return cls(name, url, year, _first(attrs, TYPE_FIELDS), size, feat["geom"], attrs)

    @property
    def file_name(self) -> str:
        return self.url.rsplit("/", 1)[-1].split("?")[0] if self.url else ""


def format_size(mb: float) -> str:
    if mb <= 0:
        return ""
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB".replace(".", ",")
    return f"{mb:.0f} MB"


class _PickTool(QgsMapToolEmitPoint):
    """Kliknięcie na mapie przełącza zaznaczenie arkusza pod kursorem."""

    def __init__(self, canvas, on_click):
        super().__init__(canvas)
        self.on_click = on_click

    def canvasReleaseEvent(self, e):
        self.on_click(self.toMapCoordinates(e.pos()))


class SheetsDialog(QDialog):
    """
    Okno niemodalne: można w tym czasie klikać na mapie.
    `download_requested(list[Sheet], skip_existing: bool)` po „Pobierz wybrane”.
    """
    download_requested = pyqtSignal(list, bool)

    def __init__(self, features: list, canvas, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Arkusze skorowidza") + (f" – {title}" if title else ""))
        self.setObjectName("sheetsDialog")
        self.resize(720, 520)
        self.canvas = canvas
        self.sheets = [Sheet.from_feature(f) for f in features]
        self.sheets = [s for s in self.sheets if s.url]
        self._syncing = False
        self._prev_tool = None

        self.layer = self._build_layer(title)
        self._build_ui()
        self._fill_table()
        self.layer.selectAll()
        self.layer.selectionChanged.connect(self._layer_selection_changed)
        self._layer_id = self.layer.id()
        QgsProject.instance().layerWillBeRemoved.connect(self._layer_removed)
        self._layer_selection_changed()
        self._zoom_to_all()

    # ------------------------------------------------------------------ warstwa

    def _build_layer(self, title: str) -> QgsVectorLayer:
        vl = QgsVectorLayer("Polygon?crs=epsg:2180", tr("Arkusze: ") + (title or tr("skorowidz")),
                            "memory")
        pr = vl.dataProvider()
        pr.addAttributes([QgsField("godlo", FIELD_STRING), QgsField("rok", FIELD_STRING),
                          QgsField("typ", FIELD_STRING), QgsField("rozmiar_mb", FIELD_DOUBLE),
                          QgsField("url", FIELD_STRING), QgsField("nr", FIELD_INT)])
        vl.updateFields()
        feats = []
        for i, s in enumerate(self.sheets):
            f = QgsFeature(vl.fields())
            f.setGeometry(s.geom)
            f.setAttributes([s.name, s.year, s.kind, s.size_mb or None, s.url, i])
            feats.append(f)
        pr.addFeatures(feats)
        vl.updateExtents()
        sym = QgsFillSymbol.createSimple({"color": "0,120,255,25", "outline_color": "0,70,200,200",
                                          "outline_width": "0.4"})
        vl.setRenderer(QgsSingleSymbolRenderer(sym))
        layers.add_to_project(vl)
        # nr arkusza (pole „nr”) → id obiektu warstwy
        self._fid = {f["nr"]: f.id() for f in vl.getFeatures()}
        self._nr = {fid: nr for nr, fid in self._fid.items()}
        return vl

    def _zoom_to_all(self) -> None:
        if self.canvas is None or not self.sheets:
            return
        rect = self._to_canvas(QgsGeometry.fromRect(self.layer.extent())).boundingBox()
        rect.scale(1.1)
        self.canvas.setExtent(rect)
        self.canvas.refresh()

    def _transform(self, to_canvas: bool) -> QgsCoordinateTransform:
        src = QgsCoordinateReferenceSystem("EPSG:2180")
        dst = self.canvas.mapSettings().destinationCrs()
        if not to_canvas:
            src, dst = dst, src
        return QgsCoordinateTransform(src, dst, QgsProject.instance())

    def _to_canvas(self, geom: QgsGeometry) -> QgsGeometry:
        g = QgsGeometry(geom)
        if self.canvas.mapSettings().destinationCrs().isValid():
            g.transform(self._transform(True))
        return g

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)

        flt = QHBoxLayout()
        self.cmb_year = QComboBox()
        self.cmb_type = QComboBox()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText(tr("godło, np. M-34-64"))
        self.ed_name.setClearButtonEnabled(True)
        years = sorted({s.year for s in self.sheets if s.year}, reverse=True)
        kinds = sorted({s.kind for s in self.sheets if s.kind})
        self.cmb_year.addItem(tr("Wszystkie roczniki"), "")
        for y in years:
            self.cmb_year.addItem(y, y)
        self.cmb_type.addItem(tr("Wszystkie typy"), "")
        for k in kinds:
            self.cmb_type.addItem(k, k)
        flt.addWidget(QLabel(tr("Pokaż:")))
        flt.addWidget(self.cmb_year)
        flt.addWidget(self.cmb_type)
        flt.addWidget(self.ed_name, 1)
        self.cmb_year.setVisible(len(years) > 1)
        self.cmb_type.setVisible(len(kinds) > 1)
        lay.addLayout(flt)

        sel = QHBoxLayout()
        self.btn_pick = QPushButton(tr("Wybieraj na mapie"))
        self.btn_pick.setCheckable(True)
        self.btn_pick.setToolTip(tr("Kliknięcie arkusza na mapie dodaje go do wyboru albo z niego usuwa"))
        self.btn_all = QPushButton(tr("Zaznacz widoczne"))
        self.btn_none = QPushButton(tr("Odznacz wszystkie"))
        self.btn_newest = QPushButton(tr("Najnowsze"))
        self.btn_newest.setToolTip(tr("Dla każdego godła zostawia tylko arkusz z najnowszego rocznika"))
        for b in (self.btn_pick, self.btn_all, self.btn_none, self.btn_newest):
            sel.addWidget(b)
        sel.addStretch(1)
        lay.addLayout(sel)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", tr("Godło"), tr("Rok"), tr("Typ"), tr("Rozmiar")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        self.status = hint_label("sheetsStatus")
        lay.addWidget(self.status)

        bottom = QHBoxLayout()
        self.chk_skip = QCheckBox(tr("Pomiń pliki, które już są w folderze"))
        self.chk_skip.setChecked(QgsSettings().value(f"{SETTINGS_PREFIX}/skip_existing", True, type=bool))
        bottom.addWidget(self.chk_skip, 1)
        self.btn_close = QPushButton(tr("Zamknij"))
        self.btn_download = QPushButton(tr("Pobierz wybrane"))
        self.btn_download.setObjectName("primary")
        self.btn_download.setDefault(True)
        bottom.addWidget(self.btn_close)
        bottom.addWidget(self.btn_download)
        lay.addLayout(bottom)

        self.cmb_year.currentIndexChanged.connect(self._apply_filter)
        self.cmb_type.currentIndexChanged.connect(self._apply_filter)
        self.ed_name.textChanged.connect(self._apply_filter)
        self.btn_pick.toggled.connect(self._toggle_pick)
        self.btn_all.clicked.connect(self.select_visible)
        self.btn_none.clicked.connect(lambda: self.layer is not None and self.layer.removeSelection())
        self.btn_newest.clicked.connect(self.keep_newest)
        self.btn_close.clicked.connect(self.close)
        self.btn_download.clicked.connect(self._download)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemDoubleClicked.connect(self._zoom_to_row)

    def _fill_table(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.sheets))
        for row, s in enumerate(self.sheets):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setData(Qt.ItemDataRole.UserRole, row)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, COL_CHECK, chk)
            name = QTableWidgetItem(s.name)
            name.setToolTip("\n".join(f"{k}: {_text(v)}" for k, v in s.attrs.items() if _text(v)))
            self.table.setItem(row, COL_NAME, name)
            self.table.setItem(row, COL_YEAR, QTableWidgetItem(s.year))
            self.table.setItem(row, COL_TYPE, QTableWidgetItem(s.kind))
            size = QTableWidgetItem(format_size(s.size_mb))
            size.setData(Qt.ItemDataRole.UserRole, s.size_mb)
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, COL_SIZE, size)
        self.table.setColumnHidden(COL_TYPE, not any(s.kind for s in self.sheets))
        self.table.setColumnHidden(COL_SIZE, not any(s.size_mb for s in self.sheets))
        self.table.setSortingEnabled(True)
        self.table.sortItems(COL_NAME)

    def _rows(self):
        """(wiersz tabeli, nr arkusza) – kolejność tabeli zmienia się przy sortowaniu."""
        for row in range(self.table.rowCount()):
            yield row, self.table.item(row, COL_CHECK).data(Qt.ItemDataRole.UserRole)

    # ------------------------------------------------------------------ filtr

    def _visible(self, s: Sheet) -> bool:
        year, kind = self.cmb_year.currentData(), self.cmb_type.currentData()
        text = self.ed_name.text().strip().lower()
        return (not year or s.year == year) and (not kind or s.kind == kind) \
            and (not text or text in s.name.lower())

    def _apply_filter(self, *_) -> None:
        shown = []
        for row, nr in self._rows():
            ok = self._visible(self.sheets[nr])
            self.table.setRowHidden(row, not ok)
            if ok:
                shown.append(nr)
        # na mapie tylko arkusze pasujące do filtra; ukryte wypadają z wyboru
        shown_set = set(shown)
        keep = [fid for fid in self.layer.selectedFeatureIds() if self._nr.get(fid) in shown_set]
        self._syncing_layer = True
        self.layer.setSubsetString(f'"nr" IN ({",".join(map(str, shown)) or "-1"})'
                                   if len(shown) < len(self.sheets) else "")
        self._syncing_layer = False
        self.layer.selectByIds(keep)      # filtr warstwy czyści zaznaczenie – przywracamy
        self.layer.triggerRepaint()
        self._update_status()

    # ------------------------------------------------------------------ wybór

    def selected(self) -> list:
        """Wybrane arkusze w kolejności godeł."""
        if self.layer is None:
            return []
        nrs = sorted(self._nr[fid] for fid in self.layer.selectedFeatureIds() if fid in self._nr)
        return sorted((self.sheets[n] for n in nrs), key=lambda s: (s.name, s.year))

    def select_visible(self) -> None:
        self.layer.select([self._fid[nr] for nr in range(len(self.sheets))
                           if self._visible(self.sheets[nr])])

    def keep_newest(self) -> None:
        """Dla każdego godła tylko najnowszy rocznik spośród aktualnie wybranych."""
        best = {}
        for s_nr in (self._nr[f] for f in self.layer.selectedFeatureIds() if f in self._nr):
            s = self.sheets[s_nr]
            if s.name not in best or s.year > self.sheets[best[s.name]].year:
                best[s.name] = s_nr
        self.layer.selectByIds([self._fid[n] for n in best.values()])

    def toggle_at(self, point_2180) -> None:
        """Przełącza arkusze zawierające punkt (EPSG:2180), tylko widoczne w filtrze."""
        geom = QgsGeometry.fromPointXY(point_2180)
        hits = [f.id() for f in self.layer.getFeatures(QgsRectangle(geom.boundingBox()))
                if f.geometry().intersects(geom)]
        if not hits:
            return
        chosen = set(self.layer.selectedFeatureIds())
        if all(h in chosen for h in hits):
            self.layer.deselect(hits)
        else:
            self.layer.select(hits)

    def _map_click(self, point) -> None:
        p = QgsGeometry.fromPointXY(point)
        if self.canvas.mapSettings().destinationCrs().isValid():
            p.transform(self._transform(False))
        self.toggle_at(p.asPoint())

    def _toggle_pick(self, on: bool) -> None:
        if self.canvas is None:
            return
        if on:
            self._prev_tool = self.canvas.mapTool()
            self._tool = _PickTool(self.canvas, self._map_click)
            self._tool.deactivated.connect(lambda: self.btn_pick.setChecked(False))
            self.canvas.setMapTool(self._tool)
        elif getattr(self, "_tool", None) is not None and self.canvas.mapTool() is self._tool:
            self.canvas.unsetMapTool(self._tool)
            if self._prev_tool is not None:
                self.canvas.setMapTool(self._prev_tool)

    def _item_changed(self, item) -> None:
        if self._syncing or item.column() != COL_CHECK:
            return
        fid = self._fid[item.data(Qt.ItemDataRole.UserRole)]
        if item.checkState() == Qt.CheckState.Checked:
            self.layer.select(fid)
        else:
            self.layer.deselect(fid)

    def _layer_selection_changed(self, *_) -> None:
        if getattr(self, "_syncing_layer", False):
            return
        chosen = set(self.layer.selectedFeatureIds())
        self._syncing = True
        self.table.setSortingEnabled(False)
        for row, nr in self._rows():
            state = Qt.CheckState.Checked if self._fid[nr] in chosen else Qt.CheckState.Unchecked
            item = self.table.item(row, COL_CHECK)
            if item.checkState() != state:
                item.setCheckState(state)
        self.table.setSortingEnabled(True)
        self._syncing = False
        self._update_status()

    def _zoom_to_row(self, item) -> None:
        nr = self.table.item(item.row(), COL_CHECK).data(Qt.ItemDataRole.UserRole)
        if self.canvas is not None:
            rect = self._to_canvas(self.sheets[nr].geom).boundingBox()
            rect.scale(1.5)
            self.canvas.setExtent(rect)
            self.canvas.refresh()

    def _update_status(self) -> None:
        chosen = self.selected()
        shown = sum(1 for s in self.sheets if self._visible(s))
        size = sum(s.size_mb for s in chosen)
        unknown = sum(1 for s in chosen if not s.size_mb)
        text = tr("Wybrano ") + f"{len(chosen)}" + tr(" z ") + f"{shown}" + tr(" arkuszy")
        if size:
            text += " · " + tr("ok. ") + format_size(size)
            if unknown:
                text += tr(" (bez ") + str(unknown) + tr(" o nieznanym rozmiarze)")
        self.status.setText(text)
        self.btn_download.setEnabled(bool(chosen))
        self.btn_download.setText(tr("Pobierz wybrane") + (f" ({len(chosen)})" if chosen else ""))

    # ------------------------------------------------------------------ koniec

    def _download(self) -> None:
        chosen = self.selected()
        if not chosen:
            return
        QgsSettings().setValue(f"{SETTINGS_PREFIX}/skip_existing", self.chk_skip.isChecked())
        self.download_requested.emit(chosen, self.chk_skip.isChecked())
        self.close()

    def _layer_removed(self, layer_id) -> None:
        """Użytkownik usunął warstwę arkuszy z projektu – okno nie ma już czego pokazywać."""
        if layer_id == self._layer_id and self.layer is not None:
            self.layer = None
            self.close()

    def done(self, result):
        """Zamknięcie przyciskiem, Esc albo krzyżykiem: narzędzie i warstwa znikają."""
        self._cleanup()
        super().done(result)

    def closeEvent(self, event):
        self._cleanup()       # także close() okna, które nie było jeszcze pokazane
        super().closeEvent(event)

    def _cleanup(self) -> None:
        self.btn_pick.setChecked(False)
        if self.layer is not None:
            try:
                if QgsProject.instance().mapLayer(self.layer.id()):
                    QgsProject.instance().removeMapLayer(self.layer.id())
            except RuntimeError:   # warstwa usunięta już przez użytkownika
                pass
            self.layer = None
        try:
            QgsProject.instance().layerWillBeRemoved.disconnect(self._layer_removed)
        except (TypeError, RuntimeError):
            pass


def already_downloaded(path: str, size_mb: float) -> bool:
    """Plik jest w folderze i (gdy rozmiar znany) ma mniej więcej właściwą wielkość."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    if size_mb <= 0:
        return True
    have = os.path.getsize(path) / (1024 * 1024)
    return abs(have - size_mb) <= max(1.0, 0.03 * size_mb)
